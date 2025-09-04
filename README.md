# WhatsApp Business Chatbot Dashboard

A Flask-based web application for managing WhatsApp Business chatbot with FAQ management and mobile simulation.

## Features

- **Admin Authentication**: Secure login (Admin/Admin)
- **WhatsApp Business Integration**: Connect to real WhatsApp Business API (Meta)
- **FAQ Management**: Create, edit, delete FAQs with sub-questions support
- **Mobile Simulation**: Test chatbot responses in real-time
- **Import/Export**: Backup and restore FAQ data
- **Persistent Storage**: SQLite database for data persistence

## Installation

1. Install Python dependencies:
```bash
pip install -r requirements.txt
```

2. Run the application:
```bash
python app.py
```

3. Open your browser and go to: http://localhost:5000

## Live WhatsApp Business (Meta) Integration

This app now supports real WABA integration.

### Required Settings (Dashboard → Settings → WhatsApp Business Connection)
- API Token (Permanent or temporary access token)
- Phone Number ID (from Meta)
- Business Account ID (WABA ID)
- App ID
- App Secret
- Webhook Verify Token
- Webhook Callback URL (optional; for your reference)

Values are saved to the SQLite `settings` table and used by:
- `GET/POST /webhook` for verification and incoming events
- `POST /api/send-test` for sending a test message via Graph API

### Webhook Endpoint
- Verify: `GET /webhook?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...`
- Receive: `POST /webhook` (Meta will POST message events here)

### Send Test API
- Endpoint: `POST /api/send-test`
- JSON body:
```json
{ "to": "15551234567", "message": "Hello from WB Dashboard!" }
```
Requires `whatsapp_api_token` and `whatsapp_phone_number_id` in settings.

## Deploy to Railway

This repo includes a `Procfile` so Railway runs Gunicorn bound to `$PORT`.

### Steps
1. Push this repo to GitHub.
2. In Railway, create a new project → Deploy from GitHub.
3. Ensure build picks up `requirements.txt` and `Procfile`.
4. After deploy, Railway will assign a public URL, e.g., `https://your-app.up.railway.app`.
5. Use `https://your-app.up.railway.app/webhook` as your Meta Webhook Callback URL.

Notes:
- SQLite is stored on the app filesystem and may reset on redeploys/restarts. For production, consider a hosted DB (e.g., Railway Postgres) and update the code to use it.
- The app reads PORT from env (`PORT`) automatically.

### Configure Meta Webhook
1. In Meta App Dashboard → WhatsApp → Configuration:
   - Set Callback URL: `https://<railway-domain>/webhook`
   - Set Verify Token: same value as in the app Settings.
   - Verify and subscribe to messages.
2. Add your phone number(s) and ensure you have the correct Phone Number ID and a valid API token.

### Testing
1. Visit the Railway URL and log in (Admin/Admin).
2. Fill the WhatsApp connection fields under Settings and Save.
3. Use "Send Test WhatsApp" to send a message to your own number (E.164 format).

## Default Login
- Username: Admin
- Password: Admin

## Usage

### Left Panel - Settings
- Configure WhatsApp Business API token and phone number
- Edit greeting messages
- Export/Import FAQ data

### Middle Panel - FAQ Management
- Add new FAQs with questions and answers
- Create sub-FAQs for detailed responses
- Edit and delete existing FAQs
- All data persists across app restarts

### Right Panel - Mobile Simulation
- Test your chatbot as a customer would
- See greeting messages and FAQ suggestions
- Interactive chat interface with suggestion buttons

## File Structure
```
WBCB/
├── app.py              # Main Flask application
├── requirements.txt    # Python dependencies
├── templates/
│   ├── login.html     # Login page
│   └── index.html     # Main dashboard
└── whatsapp_business.db # SQLite database (auto-created)
```
