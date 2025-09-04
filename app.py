from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
import json
import os
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timedelta
import sqlite3
from io import BytesIO
import requests
import secrets
import hashlib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from werkzeug.security import generate_password_hash, check_password_hash
from urllib.parse import urljoin, urlparse
from html import unescape
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from werkzeug.exceptions import RequestEntityTooLarge, HTTPException
import logging

# Optional dependencies for Excel import/export
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

try:
    import openpyxl
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'whatsapp_business_secret_key_2024')
app.config['ENV'] = 'production'
app.config['DEBUG'] = False
app.config['TESTING'] = False
# Limit upload size to avoid upstream/proxy HTML errors on oversized files
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB

# Basic logging
logging.basicConfig(level=logging.INFO)

# Configurable database path (for Railway volume persistence). For production, consider migrating to PostgreSQL.
DB_PATH = os.environ.get('DATABASE_PATH', 'whatsapp_business.db')

# Database initialization
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Create FAQs table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS faqs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            parent_id INTEGER DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (parent_id) REFERENCES faqs (id)
        )
    ''')
    # Create settings table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT UNIQUE NOT NULL,
            value TEXT NOT NULL
        )
    ''')
    # Create password reset tokens table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            used BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Create users table for admin credentials
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            email TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Create knowledge table to store website content
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            title TEXT,
            content TEXT NOT NULL,
            images TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Migration: ensure 'images' column exists for older DBs
    try:
        cursor.execute("PRAGMA table_info(knowledge)")
        cols = [row[1] for row in cursor.fetchall()]
        if 'images' not in cols:
            cursor.execute("ALTER TABLE knowledge ADD COLUMN images TEXT")
        # Migration: add 'domain' column to scope knowledge to a company/site
        cursor.execute("PRAGMA table_info(knowledge)")
        cols = [row[1] for row in cursor.fetchall()]
        if 'domain' not in cols:
            cursor.execute("ALTER TABLE knowledge ADD COLUMN domain TEXT")
    except Exception:
        pass
    # Create contacts table to track WhatsApp user greeting state
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT UNIQUE NOT NULL,
            welcomed BOOLEAN DEFAULT FALSE,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Insert default settings
    cursor.execute('''
        INSERT OR IGNORE INTO settings (key, value) VALUES
        ('whatsapp_api_token', ''),
        ('whatsapp_phone_number', ''),
        ('whatsapp_phone_number_id', ''),
        ('webhook_verify_token', ''),
        ('greeting_message', 'Dear Esteemed Guest, Welcome to Souq Waqif Boutique Hotels by Tivoli. I am your Virtual Butler and remain at your service. Please select from the options below for your convenience.'),
        ('smtp_server', 'smtp.gmail.com'),
        ('smtp_port', '587'),
        ('smtp_username', ''),
        ('smtp_password', ''),
        ('admin_email', 'admin@example.com')
    ''')
    # Insert default admin user with hashed password (change this immediately after deployment!)
    hashed_password = generate_password_hash('Admin')
    cursor.execute('''
        INSERT OR IGNORE INTO users (username, password, email) VALUES (?, ?, ?)
    ''', ('Admin', hashed_password, 'admin@example.com'))
    # Migration for existing users: Hash any plaintext passwords (for backward compatibility)
    cursor.execute('SELECT id, password FROM users')
    for row in cursor.fetchall():
        user_id, stored_password = row
        if not stored_password.startswith('pbkdf2:'):
            hashed = generate_password_hash(stored_password)  # Assume stored_password was plaintext
            cursor.execute('UPDATE users SET password = ? WHERE id = ?', (hashed, user_id))
    conn.commit()
    conn.close()

# Initialize database at import time (Flask 3.x safe; idempotent)
init_db()

# Helper to fetch a setting value by key
def get_setting(key, default=None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

# Helper to update a setting
def update_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit()
    conn.close()

# Basic content moderation function (simple keyword filter; expand with NLP if needed)
def is_moderated(content):
    # Comprehensive moderation list: dangerous, sexual, and cursing
    if not content:
        return False
    text = str(content).lower()
    patterns = [
        # Cursing / profanity
        r"\b(fuck|shit|bitch|asshole|bastard|dick|pussy|motherfucker|mf|cunt|slut|whore|prick)\b",
        # Sexual content
        r"\b(sex|sexual|porn|pornography|nude|nudity|blowjob|handjob|anal|fetish|erotic|xxx)\b",
        # Dangerous / violent / illegal
        r"\b(bomb|kill|murder|suicide|terror(ist|ism)?|attack|shoot(ing)?|gun|weapon|drugs?|heroin|cocaine|meth|hack(ing|er)?|breach)\b",
        # Hate / slurs (basic sample, expand as needed)
        r"\b(racist|hate\s*speech|lynch)\b",
    ]
    try:
        for pat in patterns:
            if re.search(pat, text, flags=re.IGNORECASE):
                return True
        return False
    except Exception:
        # Fail-safe: if regex fails, do not moderate
        return False

# Detect intent to escalate to human staff when user mentions 'additional inquiries'
def is_additional_inquiries(content: str) -> bool:
    try:
        if not content:
            return False
        return re.search(r"\badditional\s+inquir", str(content), re.IGNORECASE) is not None
    except Exception:
        return False

# Login required decorator
def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'logged_in' not in session:
            # If this looks like an API/AJAX request, return JSON 401 instead of HTML redirect
            wants_json = (
                request.is_json or (
                    request.accept_mimetypes and
                    request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html
                ) or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            )
            # For any non-GET (e.g., POST uploads), prefer JSON 401 to avoid HTML responses
            if request.method != 'GET':
                wants_json = True
            if wants_json:
                return jsonify({'error': 'Unauthorized'}), 401
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

@app.route('/')
@login_required
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT password FROM users WHERE username = ?', (username,))
        row = cursor.fetchone()
        conn.close()
        if row and check_password_hash(row[0], password):
            session['logged_in'] = True
            return redirect(url_for('index'))
        # Render inline error on the login page
        return render_template('login.html', error='Invalid username or password.')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form['email']
        # Generate token
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now() + timedelta(hours=1)
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('INSERT INTO password_reset_tokens (email, token, expires_at) VALUES (?, ?, ?)',
                       (email, token, expires_at))
        conn.commit()
        conn.close()
        # Send email
        reset_link = url_for('reset_password', token=token, _external=True)
        msg = MIMEMultipart()
        msg['From'] = get_setting('smtp_username')
        msg['To'] = email
        msg['Subject'] = 'Password Reset Request'
        body = f'Click here to reset your password: {reset_link}'
        msg.attach(MIMEText(body, 'plain'))
        try:
            server = smtplib.SMTP(get_setting('smtp_server'), int(get_setting('smtp_port')))
            server.starttls()
            server.login(get_setting('smtp_username'), get_setting('smtp_password'))
            server.sendmail(get_setting('smtp_username'), email, msg.as_string())
            server.quit()
            return jsonify({'success': True, 'message': 'Reset link sent to your email.'})
        except Exception as e:
            return jsonify({'success': False, 'error': f'Error sending email: {str(e)}'}), 500
    return render_template('forgot_password.html')

@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT email, expires_at, used FROM password_reset_tokens WHERE token = ?', (token,))
    row = cursor.fetchone()
    if not row or row[2] or datetime.now() > datetime.fromisoformat(row[1]):
        conn.close()
        return 'Invalid or expired token.'
    email = row[0]
    if request.method == 'POST':
        new_password = request.form['password']
        hashed = generate_password_hash(new_password)
        cursor.execute('UPDATE users SET password = ? WHERE email = ?', (hashed, email))
        cursor.execute('UPDATE password_reset_tokens SET used = TRUE WHERE token = ?', (token,))
        conn.commit()
        conn.close()
        return 'Password reset successful. <a href="/login">Login</a>'
    conn.close()
    return render_template('reset_password.html', token=token)

@app.route('/faqs', methods=['GET'])
@login_required
def get_faqs():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, question, answer, parent_id FROM faqs ORDER BY created_at DESC')
    faqs = cursor.fetchall()
    conn.close()
    # Build a tree of FAQs with IDs included (needed by frontend for edit/delete)
    faq_tree = {}
    for faq in faqs:
        fid, question, answer, parent_id = faq
        # Normalize parent_id: treat 0 or empty as None for compatibility
        try:
            parent_id_norm = None if (parent_id is None or int(parent_id) == 0) else int(parent_id)
        except Exception:
            parent_id_norm = None
        if parent_id_norm is None:
            faq_tree[fid] = {
                'id': fid,
                'question': question,
                'answer': answer,
                'parent_id': None,
                'sub_faqs': []
            }
        else:
            # If parent already in tree, append as sub-FAQ; otherwise, create a placeholder parent
            if parent_id_norm not in faq_tree:
                faq_tree[parent_id_norm] = {
                    'id': parent_id_norm,
                    'question': '',
                    'answer': '',
                    'parent_id': None,
                    'sub_faqs': []
                }
            faq_tree[parent_id_norm]['sub_faqs'].append({
                'id': fid,
                'question': question,
                'answer': answer,
                'parent_id': parent_id_norm
            })
    return jsonify(list(faq_tree.values()))

@app.route('/add_faq', methods=['POST'])
@login_required
def add_faq():
    data = request.json
    question = data['question']
    answer = data['answer']
    parent_id = data.get('parent_id')
    # Normalize parent_id: convert empty string or invalid to None; otherwise int
    try:
        if parent_id in (None, '', 'null', 'None'):
            parent_id = None
        else:
            parent_id = int(parent_id)
            if parent_id == 0:
                parent_id = None
    except Exception:
        parent_id = None
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('INSERT INTO faqs (question, answer, parent_id) VALUES (?, ?, ?)',
                   (question, answer, parent_id))
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()
    return jsonify({'id': new_id, 'question': question, 'answer': answer, 'parent_id': parent_id})

@app.route('/update_faq/<int:faq_id>', methods=['PUT'])
@login_required
def update_faq(faq_id):
    data = request.json
    question = data['question']
    answer = data['answer']
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('UPDATE faqs SET question = ?, answer = ? WHERE id = ?',
                   (question, answer, faq_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# New: API endpoint matching frontend for training from a URL
@app.route('/api/train-url', methods=['POST'])
@login_required
def api_train_url():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    deep = bool(data.get('deep', False))
    try:
        max_pages = int(data.get('max_pages', 50))
    except Exception:
        max_pages = 50
    if not url:
        return jsonify({'success': False, 'error': 'Missing url'}), 400
    # Normalize URL scheme
    if not url.lower().startswith(('http://', 'https://')):
        url = 'http://' + url
    parsed = urlparse(url)
    if not parsed.netloc:
        return jsonify({'success': False, 'error': 'Invalid URL'}), 400

    crawled = 0
    saved = 0
    last_error = None
    visited = set()
    queue = [url]
    domain = parsed.netloc
    # Persist current domain for scoping answers
    try:
        update_setting('current_domain', domain)
    except Exception:
        pass
    # Purge old trainings not related to current company/domain
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Prefer domain column; also clean legacy entries with NULL domain
        cursor.execute('DELETE FROM knowledge WHERE domain IS NULL OR domain != ?', (domain,))
        conn.commit()
        conn.close()
    except Exception:
        # Non-fatal; continue training
        pass
    try:
        while queue and crawled < max_pages:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            data = None
            try:
                data = crawl_url(current)
            except Exception as e:
                last_error = f'crawl_url failed for {current}: {e}'
                data = None
            crawled += 1
            if data:
                try:
                    save_to_knowledge(data)
                    saved += 1
                except Exception as e:
                    last_error = f'save_to_knowledge failed for {current}: {e}'
            # Enqueue links for deep crawl
            if deep:
                try:
                    resp = requests.get(current, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
                    if resp.status_code == 200:
                        # Only consider HTML content
                        ctype = resp.headers.get('Content-Type', '')
                        if 'html' not in ctype.lower():
                            continue
                        links = re.findall(r'href=["\'](.*?)["\']', resp.text)
                        for link in links:
                            try:
                                absolute = urljoin(current, link)
                                p = urlparse(absolute)
                                # Skip non-http(s), fragments, mailto, javascript
                                if p.scheme not in ('http', 'https'):
                                    continue
                                if absolute.startswith('mailto:') or absolute.startswith('javascript:'):
                                    continue
                                if p.netloc != domain:
                                    continue
                                # Avoid binary/document files
                                if re.search(r'\.(pdf|jpg|jpeg|png|gif|svg|zip|rar|7z|mp3|mp4|avi|mov|wmv|xlsx?|docx?|pptx?)($|\?)', p.path, re.I):
                                    continue
                                if absolute not in visited and absolute not in queue:
                                    queue.append(absolute)
                            except Exception as e:
                                last_error = f'link parse failed for {link} on {current}: {e}'
                                continue
                except Exception as e:
                    last_error = f'link fetch failed for {current}: {e}'
                    pass
        # If nothing saved, report a helpful failure so UI won't show Unknown error
        if saved == 0:
            msg = 'No pages could be saved. The site may block bots, contain no parsable HTML, or be outside allowed domain.'
            if last_error:
                msg += f' Last error: {last_error}'
            return jsonify({'success': False, 'error': msg, 'pages_crawled': crawled, 'pages_saved': saved}), 200
        return jsonify({'success': True, 'message': 'Training completed', 'pages_crawled': crawled, 'pages_saved': saved})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# -------- API aliases (to match frontend /api/* endpoints) --------

@app.route('/api/settings', methods=['GET', 'POST'])
@login_required
def api_settings():
    return settings()

@app.route('/api/faqs', methods=['GET'])
@login_required
def api_get_faqs():
    return get_faqs()

@app.route('/api/faqs', methods=['POST'])
@login_required
def api_add_faq():
    return add_faq()

@app.route('/api/faqs/<int:faq_id>', methods=['PUT'])
@login_required
def api_update_faq(faq_id):
    return update_faq(faq_id)

@app.route('/api/faqs/<int:faq_id>', methods=['DELETE'])
@login_required
def api_delete_faq(faq_id):
    return delete_faq(faq_id)

@app.route('/api/faqs/clear', methods=['POST'])
@login_required
def api_clear_all_faqs():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM faqs')
        (count_before,) = cursor.fetchone()
        cursor.execute('DELETE FROM faqs')
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'deleted': count_before})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/export-faqs', methods=['GET'])
@login_required
def api_export_faqs():
    return export_faqs()

@app.route('/api/import-faqs', methods=['POST'])
@login_required
def api_import_faqs():
    return import_faqs()

# New: Clear training data for the current domain (or specified domain)
@app.route('/api/clear-training', methods=['POST'])
@login_required
def api_clear_training():
    try:
        data = request.get_json(silent=True) or {}
        # If all is true, delete everything regardless of domain
        delete_all = bool(data.get('all', False))
        domain = (data.get('domain') or '').strip()
        if not domain and not delete_all:
            domain = get_setting('current_domain', None)
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        if delete_all:
            cursor.execute('DELETE FROM knowledge')
        elif domain:
            cursor.execute('DELETE FROM knowledge WHERE domain = ?', (domain,))
        else:
            # If no domain is set, clear legacy entries with NULL domain
            cursor.execute('DELETE FROM knowledge WHERE domain IS NULL')
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        # Optionally reset current_domain if we cleared it
        if (delete_all or domain) and (data.get('reset_current', True)):
            update_setting('current_domain', '')
        return jsonify({'success': True, 'deleted': deleted, 'domain': None if delete_all else (domain or None), 'all': delete_all})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/chat', methods=['POST'])
@login_required
def api_chat():
    data = request.get_json(silent=True) or {}
    message = data.get('message', '').strip()
    # Two distinct handoff messages based on how the user indicates inquiries:
    # - PLEASE_LEAVE_MSG: shown when user clicks the "Additional Inquiries" suggestion
    # - THANK_YOU_MSG: shown when user types a message indicating inquiries or while handoff is active
    PLEASE_LEAVE_MSG = "Please leave us a message. We value your interest and will respond to your questions promptly."
    THANK_YOU_MSG = "Thank you for your message. Our team will respond to you shortly."
    # Content moderation for chat UI as well
    if message and is_moderated(message):
        warn = "🚫 Content Guidelines Reminder\nYour message contains language that doesn't align with our professional community guidelines. Please revise your content to maintain a respectful environment."
        # Provide suggestions anyway
        suggestions = get_main_faq_suggestions(limit=9)
        return jsonify({'response': warn, 'suggestions': suggestions})
    # Human handoff: if user TYPES an inquiries message, thank and handoff
    if message and is_additional_inquiries(message):
        session['handoff'] = True
        return jsonify({'response': THANK_YOU_MSG, 'suggestions': [], 'restart_option': True})
    # If a previous selection triggered handoff, do not auto-reply to subsequent messages
    if session.get('handoff', False) and message:
        return jsonify({'response': THANK_YOU_MSG, 'suggestions': [], 'restart_option': True})
    # First-time behavior: greet only on the first NON-empty user message.
    if not session.get('welcomed_user', False):
        if message:
            # First actual user message -> send admin greeting and mark welcomed
            response_text = get_setting('greeting_message', 'Hello!')
            session['welcomed_user'] = True
        else:
            # Empty init call from frontend -> show greeting but DO NOT mark welcomed yet
            response_text = get_setting('greeting_message', 'Hello!')
    else:
        response_text = find_response(message) if message else get_setting('greeting_message', 'Hello!')
        # Avoid dumping entire web pages from knowledge base
        response_text = _truncate_response(response_text, limit=600)
    # Provide suggestions: show main FAQs for first message or always include some
    suggestions = get_main_faq_suggestions(limit=9)
    return jsonify({'response': response_text, 'suggestions': suggestions})

@app.route('/api/faq-answer/<int:faq_id>', methods=['GET'])
@login_required
def api_faq_answer(faq_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id, question, answer FROM faqs WHERE id = ?', (faq_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'FAQ not found'}), 404
    cursor.execute('SELECT id, question, answer FROM faqs WHERE parent_id = ?', (faq_id,))
    subs = cursor.fetchall()
    conn.close()
    # If this FAQ represents Additional Inquiries, enable handoff in session and override answer
    try:
        qtext = (row[1] or '').strip()
        if re.search(r"\badditional\s+inquir", qtext, re.IGNORECASE):
            session['handoff'] = True
            handoff_text = "Please leave us a message. We value your interest and will respond to your questions promptly."
            return jsonify({
                'id': row[0],
                'question': row[1],
                'answer': handoff_text,
                'restart_option': True,
                'sub_faqs': [{'id': s[0], 'question': s[1], 'answer': s[2]} for s in subs]
            })
    except Exception:
        pass
    return jsonify({
        'id': row[0],
        'question': row[1],
        'answer': row[2],
        'sub_faqs': [{'id': s[0], 'question': s[1], 'answer': s[2]} for s in subs]
    })

@app.route('/api/send-test', methods=['POST'])
@login_required
def api_send_test():
    data = request.get_json(silent=True) or {}
    to = data.get('to')
    message = data.get('message', '')
    if not to:
        return jsonify({'success': False, 'error': 'Missing recipient "to"'}), 400
    try:
        # Prefer to use the existing helper; if not configured properly, it may fail silently.
        send_whatsapp_message(to, message)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/restart', methods=['POST'])
@login_required
def api_restart():
    try:
        # Clear handoff and welcome state for a fresh start
        session.pop('handoff', None)
        session.pop('welcomed_user', None)
        greeting = get_setting('greeting_message', 'Hello!')
        suggestions = get_main_faq_suggestions(limit=9)
        return jsonify({'response': greeting, 'suggestions': suggestions})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# JSON error handlers to avoid HTML responses in API calls
@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(e):
    return jsonify({'error': 'Uploaded file is too large.'}), 413

@app.errorhandler(HTTPException)
def handle_http_exception(e):
    # For API endpoints, prefer JSON over HTML default pages
    api_like = (
        request.path.startswith('/api/') or
        request.path.startswith('/import_faqs') or
        request.path.startswith('/export_faqs') or
        request.path.startswith('/webhook') or
        request.path.startswith('/train') or
        request.path.startswith('/settings') or
        request.path.startswith('/add_faq') or
        request.path.startswith('/update_faq') or
        request.path.startswith('/delete_faq') or
        request.path.startswith('/faqs')
    )
    if api_like:
        return jsonify({'error': e.description, 'code': e.code}), e.code
    return e

# Catch-all JSON for unhandled exceptions on API routes
@app.errorhandler(Exception)
def handle_unexpected_exception(e):
    api_like = (
        request.path.startswith('/api/') or
        request.path.startswith('/import_faqs') or
        request.path.startswith('/export_faqs') or
        request.path.startswith('/webhook') or
        request.path.startswith('/train') or
        request.path.startswith('/settings') or
        request.path.startswith('/add_faq') or
        request.path.startswith('/update_faq') or
        request.path.startswith('/delete_faq') or
        request.path.startswith('/faqs')
    )
    if api_like:
        app.logger.exception('Unhandled exception processing API request')
        return jsonify({'error': 'Internal server error'}), 500
    # Fallback generic
    return ('Internal Server Error', 500)

@app.route('/delete_faq/<int:faq_id>', methods=['DELETE'])
@login_required
def delete_faq(faq_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Delete sub-FAQs first
    cursor.execute('DELETE FROM faqs WHERE parent_id = ?', (faq_id,))
    cursor.execute('DELETE FROM faqs WHERE id = ?', (faq_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        data = request.json
        for key, value in data.items():
            update_setting(key, value)
        return jsonify({'success': True})
    settings_keys = [
        'whatsapp_api_token', 'whatsapp_phone_number', 'whatsapp_phone_number_id', 'webhook_verify_token', 'greeting_message',
        'smtp_server', 'smtp_port', 'smtp_username', 'smtp_password', 'admin_email'
    ]
    settings_dict = {key: get_setting(key, '') for key in settings_keys}
    return jsonify(settings_dict)

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    if request.method == 'GET':
        # Verify webhook subscription
        # Prefer DB setting, then environment; do NOT use insecure defaults
        verify_token = get_setting('webhook_verify_token') or os.environ.get('VERIFY_TOKEN')
        token_supplied = request.args.get('hub.verify_token')
        if not verify_token:
            return 'Verification token not configured', 403
        if token_supplied == verify_token:
            return request.args.get('hub.challenge')
        return 'Verification failed', 403
    elif request.method == 'POST':
        data = request.json
        # Defensive checks for structure
        try:
            changes = data.get('entry', [])[0].get('changes', [])[0].get('value', {})
            msgs = changes.get('messages', [])
        except Exception:
            msgs = []
        if msgs:
            message = msgs[0]
            sender = message.get('from')
            text = message.get('text', {}).get('body', '') if isinstance(message.get('text'), dict) else ''
            # Moderate content: reply with warning and do not process further
            if text and is_moderated(text):
                send_whatsapp_message(sender, "🚫 Content Guidelines Reminder\nYour message contains language that doesn't align with our professional community guidelines. Please revise your content to maintain a respectful environment.")
                return 'Moderated', 200
            # Human handoff: if guest TYPES inquiries, thank and leave for staff
            if text and is_additional_inquiries(text):
                send_whatsapp_message(sender, "Thank you for your message. Our team will respond to you shortly.")
                return 'Handoff to human', 200
            # Greeting logic per WhatsApp sender
            try:
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute('SELECT welcomed FROM contacts WHERE phone = ?', (sender,))
                row = cursor.fetchone()
                if not row:
                    cursor.execute('INSERT INTO contacts (phone, welcomed, last_seen) VALUES (?, ?, ?)', (sender, False, datetime.now()))
                    conn.commit()
                    welcomed = False
                else:
                    welcomed = bool(row[0])
                # Update last seen
                cursor.execute('UPDATE contacts SET last_seen = ? WHERE phone = ?', (datetime.now(), sender))
                conn.commit()
            except Exception:
                welcomed = True  # fail open to avoid greeting loop
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            if text and not welcomed:
                greet = get_setting('greeting_message', 'Hello!')
                send_whatsapp_message(sender, greet)
                # Mark welcomed
                try:
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute('UPDATE contacts SET welcomed = TRUE WHERE phone = ?', (sender,))
                    conn.commit()
                    conn.close()
                except Exception:
                    pass
            else:
                # Find matching FAQ or knowledge
                response = find_response(text)
                send_whatsapp_message(sender, response)
        return 'OK', 200

def find_response(query):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Search FAQs
    cursor.execute('SELECT answer FROM faqs WHERE question LIKE ? LIMIT 1', (f'%{query}%',))
    row = cursor.fetchone()
    if row:
        conn.close()
        return row[0]
    # Search knowledge base scoped to current domain and most recent
    current_domain = get_setting('current_domain', None)
    if current_domain:
        # Try title match first, then content; prefer most recent
        cursor.execute('SELECT content FROM knowledge WHERE domain = ? AND title LIKE ? ORDER BY created_at DESC LIMIT 1', (current_domain, f'%{query}%'))
        row = cursor.fetchone()
        if row:
            conn.close()
            return row[0]
        cursor.execute('SELECT content FROM knowledge WHERE domain = ? AND content LIKE ? ORDER BY created_at DESC LIMIT 1', (current_domain, f'%{query}%'))
        row = cursor.fetchone()
        if row:
            conn.close()
            return row[0]
    # Fallback: legacy entries without domain (least preferred)
    cursor.execute('SELECT content FROM knowledge WHERE domain IS NULL AND (title LIKE ? OR content LIKE ?) ORDER BY created_at DESC LIMIT 1', (f'%{query}%', f'%{query}%'))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0]
    # Fallback to configured greeting message as the default assistant response
    return get_setting('greeting_message', 'Dear Esteemed Guest, Welcome to Souq Waqif Boutique Hotels by Tivoli. I am your Virtual Butler and remain at your service. Please select from the options below for your convenience.')

# Helper: keep responses concise for UI
def _truncate_response(text, limit=600):
    try:
        s = re.sub(r'\s+', ' ', str(text)).strip()
        return (s[:limit] + '…') if len(s) > limit else s
    except Exception:
        return text

# Helper: fetch top-level FAQs as suggestions
def get_main_faq_suggestions(limit=9):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT id, question FROM faqs WHERE parent_id IS NULL ORDER BY created_at DESC LIMIT ?', (limit,))
        rows = cursor.fetchall()
        # Fallback: if no main FAQs exist, return most recent FAQs regardless of parent
        if not rows:
            cursor.execute('SELECT id, question FROM faqs ORDER BY created_at DESC LIMIT ?', (limit,))
            rows = cursor.fetchall()
        conn.close()
        return [{'id': r[0], 'question': r[1]} for r in rows]
    except Exception:
        return []

def send_whatsapp_message(to, text):
    token = get_setting('whatsapp_api_token')
    phone_number = get_setting('whatsapp_phone_number')
    url = f'https://graph.facebook.com/v13.0/{phone_number}/messages'
    headers = {'Authorization': f'Bearer {token}'}
    data = {
        'messaging_product': 'whatsapp',
        'to': to,
        'type': 'text',
        'text': {'body': text}
    }
    requests.post(url, headers=headers, json=data)

@app.route('/train', methods=['POST'])
@login_required
def train():
    data = request.get_json(silent=True)
    if not data or 'urls' not in data or not isinstance(data['urls'], list):
        return jsonify({'error': 'Invalid payload. Expecting JSON body with key "urls" as a list.'}), 400
    urls = data['urls']
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(crawl_url, url) for url in urls]
        for future in as_completed(futures):
            data = future.result()
            if data:
                save_to_knowledge(data)
    return jsonify({'success': True})

def crawl_url(url):
    try:
        response = requests.get(url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if response.status_code != 200:
            return None
        # Skip non-HTML content quickly using Content-Type or URL extension
        ctype = response.headers.get('Content-Type', '')
        path = urlparse(url).path.lower()
        if ('html' not in ctype.lower()) or re.search(r'\.(css|js|json|xml|txt|ico|woff2?|ttf|eot|otf|map)($|\?)', path):
            return None
        # Simple HTML parsing (use BeautifulSoup for better if installed, but avoid extra deps)
        content = re.sub('<[^<]+?>', '', response.text)  # Strip tags
        content = unescape(content)
        title = re.search('<title>(.*?)</title>', response.text)
        title = title.group(1) if title else ''
        # Extract images
        images = re.findall(r'<img.*?src="(.*?)"', response.text)
        images = [urljoin(url, img) for img in images]
        return {'url': url, 'title': title, 'content': content, 'images': json.dumps(images)}
    except Exception:
        return None

def save_to_knowledge(data):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    try:
        dom = urlparse(data['url']).netloc
    except Exception:
        dom = None
    # Heuristics: skip saving CSS/JS or obviously non-informative blobs
    try:
        p = urlparse(data.get('url', '')).path.lower()
        content_snippet = (data.get('content') or '')[:2000]
        if re.search(r'\.(css|js|json|xml|txt|ico|woff2?|ttf|eot|otf|map)($|\?)', p):
            conn.close()
            return
        if ('@font-face' in content_snippet) or ('@charset' in content_snippet) or content_snippet.strip().startswith('/*'):
            conn.close()
            return
    except Exception:
        pass
    # Insert with domain if column exists; fallback to legacy insert
    try:
        cursor.execute('INSERT INTO knowledge (url, title, content, images, domain) VALUES (?, ?, ?, ?, ?)',
                       (data['url'], data['title'], data['content'], data['images'], dom))
    except Exception:
        cursor.execute('INSERT INTO knowledge (url, title, content, images) VALUES (?, ?, ?, ?)',
                       (data['url'], data['title'], data['content'], data['images']))
    conn.commit()
    conn.close()

@app.route('/api/knowledge-delete', methods=['POST'])
@login_required
def api_knowledge_delete():
    try:
        payload = request.get_json(silent=True) or {}
        pattern = (payload.get('pattern') or '').strip()
        field = (payload.get('field') or 'content').strip().lower()  # one of: content, title, url
        domain_only = payload.get('domain_only', True)
        if field not in ('content', 'title', 'url'):
            return jsonify({'success': False, 'error': 'Invalid field. Use content, title, or url.'}), 400
        if not pattern:
            return jsonify({'success': False, 'error': 'Missing pattern'}), 400
        # Build query
        column = field
        params = []
        where = f"{column} LIKE ?"
        params.append(f"%{pattern}%")
        if domain_only:
            current = get_setting('current_domain', None)
            if current:
                where += ' AND domain = ?'
                params.append(current)
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(f'DELETE FROM knowledge WHERE {where}', tuple(params))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'deleted': deleted})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/export_faqs', methods=['GET'])
@login_required
def export_faqs():
    if not PANDAS_AVAILABLE or not OPENPYXL_AVAILABLE:
        return jsonify({'error': 'Excel export requires pandas and openpyxl installed.'}), 400
    # Select only needed fields and compute a Type column
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query('SELECT id, question, answer, parent_id FROM faqs ORDER BY id ASC', conn)
    conn.close()
    # Add Type column: Main FAQ when parent_id is NULL, else Sub-FAQ
    def _type_from_parent(pid):
        try:
            return 'Main FAQ' if pd.isna(pid) or pid is None else 'Sub-FAQ'
        except Exception:
            return 'Main FAQ'
    if not df.empty:
        df['Type'] = df['parent_id'].apply(_type_from_parent)
        # Reorder columns for clarity
        df = df[['id', 'question', 'answer', 'Type', 'parent_id']]
        # Rename parent_id to Parent ID for friendlier Excel header
        df = df.rename(columns={'parent_id': 'Parent ID', 'id': 'ID', 'question': 'Question', 'answer': 'Answer'})
    else:
        # Create an empty template with proper headers
        df = pd.DataFrame(columns=['ID', 'Question', 'Answer', 'Type', 'Parent ID'])
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='FAQs')
    output.seek(0)
    return send_file(output, as_attachment=True, download_name='faqs.xlsx')

@app.route('/import_faqs', methods=['POST'])
@login_required
def import_faqs():
    if not PANDAS_AVAILABLE or not OPENPYXL_AVAILABLE:
        return jsonify({'error': 'Excel import requires pandas and openpyxl installed.'}), 400
    if 'file' not in request.files:
        return jsonify({'error': 'No file part in the request.'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file.'}), 400
    try:
        df = pd.read_excel(file)
    except Exception as e:
        return jsonify({'error': f'Failed to read Excel file: {str(e)}'}), 400
    # Normalize column names to ease matching
    rename_map = {}
    for c in df.columns:
        cl = str(c).strip().lower()
        if cl == 'id':
            rename_map[c] = 'ID'
        elif cl == 'question':
            rename_map[c] = 'question'
        elif cl == 'answer':
            rename_map[c] = 'answer'
        elif cl in ('parent id', 'parent_id', 'parentid'):
            rename_map[c] = 'parent_id'
        elif cl == 'type':
            rename_map[c] = 'Type'
    if rename_map:
        df = df.rename(columns=rename_map)
    # Validate required columns
    required = {'question', 'answer'}
    missing = [c for c in required if c not in df.columns]
    if missing:
        return jsonify({'error': f"Missing required columns: {', '.join(missing)}"}), 400

    has_parent = 'parent_id' in df.columns
    has_type = 'Type' in df.columns
    has_id = 'ID' in df.columns

    # Helpers
    def _parse_int(v):
        try:
            if v is None:
                return None
            if isinstance(v, float) and pd.isna(v):
                return None
            if isinstance(v, (int,)):
                return int(v)
            if isinstance(v, float):
                return int(v)
            s = str(v).strip()
            if s == '':
                return None
            return int(float(s))
        except Exception:
            return None

    # Preprocess rows into dicts for easier handling
    rows = []
    for _, r in df.iterrows():
        q = ('' if (pd.isna(r['question']) if 'question' in r else True) else str(r['question']).strip())
        a = ('' if (pd.isna(r['answer']) if 'answer' in r else True) else str(r['answer']).strip())
        tval = None
        if has_type:
            tv = r['Type']
            tval = ('' if pd.isna(tv) else str(tv)).strip().lower()
        pval = _parse_int(r['parent_id']) if has_parent else None
        oid = _parse_int(r['ID']) if has_id else None
        rows.append({'orig_id': oid, 'question': q, 'answer': a, 'type': tval, 'parent_ref': pval})

    # Determine mains vs subs
    mains = []
    subs = []
    for item in rows:
        is_main_by_type = item['type'] in ('main faq', 'main', 'root', 'top', 'top-level') if item['type'] else False
        if is_main_by_type:
            mains.append(item)
        else:
            # If type not specified, treat as main if no parent_ref
            if item['parent_ref'] is None:
                mains.append(item)
            else:
                subs.append(item)

    inserted_mains = 0
    inserted_subs = 0
    skipped_subs = 0
    id_map = {}  # maps original Excel ID -> DB ID

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # First pass: insert mains
        for m in mains:
            cursor.execute('INSERT INTO faqs (question, answer, parent_id) VALUES (?, ?, ?)', (m['question'], m['answer'], None))
            db_id = cursor.lastrowid
            inserted_mains += 1
            if m['orig_id'] is not None:
                id_map[m['orig_id']] = db_id
        # Second pass: insert subs with mapped parent IDs
        for s in subs:
            pref = s['parent_ref']
            db_parent = None
            if pref is not None:
                db_parent = id_map.get(pref)
            if db_parent is None:
                # Parent not found; skip safely
                skipped_subs += 1
                continue
            cursor.execute('INSERT INTO faqs (question, answer, parent_id) VALUES (?, ?, ?)', (s['question'], s['answer'], db_parent))
            inserted_subs += 1
        conn.commit()
    except Exception as e:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
        return jsonify({'error': f'Database error during import: {str(e)}'}), 500
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    return jsonify({
        'success': True,
        'message': 'FAQs imported successfully.',
        'inserted_main': inserted_mains,
        'inserted_sub': inserted_subs,
        'skipped_sub': skipped_subs
    })
