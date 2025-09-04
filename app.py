from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
import json
import os
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
from urllib.parse import urlparse, urljoin
from html import unescape
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    except Exception:
        pass
    # Insert default settings
    cursor.execute('''
        INSERT OR IGNORE INTO settings (key, value) VALUES
        ('whatsapp_api_token', ''),
        ('whatsapp_phone_number', ''),
        ('whatsapp_phone_number_id', ''),
        ('webhook_verify_token', ''),
        ('greeting_message', 'Hello! Welcome to our WhatsApp Business. How can I help you today?'),
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

@app.before_first_request
def _initialize_database():
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
    bad_keywords = ['badword1', 'badword2']  # Add your list of prohibited words/phrases
    for word in bad_keywords:
        if word.lower() in content.lower():
            return True
    return False

# Login required decorator
def login_required(f):
    def wrapper(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
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
        return 'Invalid credentials'
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
            return 'Reset link sent to your email.'
        except Exception as e:
            return f'Error sending email: {str(e)}'
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
    faq_tree = {}
    for faq in faqs:
        if faq[3] is None:
            faq_tree[faq[0]] = {'question': faq[1], 'answer': faq[2], 'sub_faqs': []}
        else:
            if faq[3] in faq_tree:
                faq_tree[faq[3]]['sub_faqs'].append({'question': faq[1], 'answer': faq[2]})
    return jsonify(list(faq_tree.values()))

@app.route('/add_faq', methods=['POST'])
@login_required
def add_faq():
    data = request.json
    question = data['question']
    answer = data['answer']
    parent_id = data.get('parent_id')
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
        # Process incoming message
        if 'messages' in data['entry'][0]['changes'][0]['value']:
            message = data['entry'][0]['changes'][0]['value']['messages'][0]
            sender = message['from']
            text = message['text']['body'] if 'text' in message else ''
            # Moderate content
            if is_moderated(text):
                # Handle moderated content (e.g., log or ignore)
                return 'Moderated', 200
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
    # Search knowledge base
    cursor.execute('SELECT content FROM knowledge WHERE content LIKE ? LIMIT 1', (f'%{query}%',))
    row = cursor.fetchone()
    conn.close()
    if row:
        return row[0]
    return 'Sorry, I don\'t have an answer for that. Please contact support.'

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
    urls = request.json['urls']
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(crawl_url, url) for url in urls]
        for future in as_completed(futures):
            data = future.result()
            if data:
                save_to_knowledge(data)
    return jsonify({'success': True})

def crawl_url(url):
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
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
    cursor.execute('INSERT INTO knowledge (url, title, content, images) VALUES (?, ?, ?, ?)',
                   (data['url'], data['title'], data['content'], data['images']))
    conn.commit()
    conn.close()

@app.route('/export_faqs', methods=['GET'])
@login_required
def export_faqs():
    if not PANDAS_AVAILABLE or not OPENPYXL_AVAILABLE:
        return 'Excel export requires pandas and openpyxl installed.', 400
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query('SELECT * FROM faqs', conn)
    conn.close()
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    output.seek(0)
    return send_file(output, as_attachment=True, download_name='faqs.xlsx')

@app.route('/import_faqs', methods=['POST'])
@login_required
def import_faqs():
    if not PANDAS_AVAILABLE or not OPENPYXL_AVAILABLE:
        return 'Excel import requires pandas and openpyxl installed.', 400
    file = request.files['file']
    df = pd.read_excel(file)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    for _, row in df.iterrows():
        cursor.execute('INSERT INTO faqs (question, answer, parent_id) VALUES (?, ?, ?)',
                       (row['question'], row['answer'], row.get('parent_id')))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

 
