# Deployment Guide

## Prerequisites
- Python 3.9+
- WhatsApp Business Account
- Facebook Developer Account

## Environment Setup
1. Clone repository
2. Create virtual environment: `python -m venv venv`
3. Activate environment: `source venv/bin/activate`
4. Install dependencies: `pip install -r requirements.txt`

## Environment Variables
Copy `.env.example` to `.env` and fill in your values:
- WHATSAPP_TOKEN: From Facebook Developer Portal
- VERIFY_TOKEN: Your custom verify token
- APP_ID: Your app ID
- APP_SECRET: Your app secret
- PHONE_NUMBER_ID: Your WhatsApp business number ID

## Local Testing
Run: `python app.py`

## Production Deployment
### Heroku
1. `heroku create your-app-name`
2. `git push heroku main`
3. Set environment variables in Heroku dashboard

### Docker
1. `docker build -t whatsapp-bot .`
2. `docker run -p 8000:8000 whatsapp-bot`
