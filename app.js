require('dotenv').config();
const express = require('express');
const axios = require('axios');
const bodyParser = require('body-parser');

const app = express();
const port = process.env.PORT || 3000;

// Middleware
app.use(bodyParser.json());

// Webhook verification
app.get('/webhook', (req, res) => {
    const mode = req.query['hub.mode'];
    const token = req.query['hub.verify_token'];
    const challenge = req.query['hub.challenge'];

    console.log('Webhook verification attempt:', { mode, token });

    if (mode && token) {
        if (mode === 'subscribe' && token === process.env.VERIFY_TOKEN) {
            console.log('WEBHOOK_VERIFIED');
            res.status(200).send(challenge);
        } else {
            console.log('Verification failed - token mismatch');
            res.sendStatus(403);
        }
    } else {
        console.log('Verification failed - missing parameters');
        res.sendStatus(400);
    }
});

// Handle incoming messages
app.post('/webhook', (req, res) => {
    console.log('✓ Received webhook POST request');
    
    // Immediately respond to Meta to acknowledge receipt
    res.status(200).send('EVENT_RECEIVED');

    try {
        const entry = req.body.entry?.[0];
        if (!entry) {
            console.log('No entry found in webhook');
            return;
        }

        const changes = entry?.changes?.[0];
        if (!changes) {
            console.log('No changes found in entry');
            return;
        }

        const value = changes?.value;
        if (!value) {
            console.log('No value found in changes');
            return;
        }

        const message = value?.messages?.[0];
        if (!message) {
            console.log('No message found in webhook - might be status update');
            return;
        }

        const from = message.from;
        const messageType = message.type;
        let messageBody = '';

        if (messageType === 'text') {
            messageBody = message.text?.body || '';
        } else if (messageType === 'interactive') {
            messageBody = message.interactive?.button_reply?.title || '';
        } else {
            console.log(`Unhandled message type: ${messageType}`);
            return;
        }

        console.log(`📩 Received message from ${from}: "${messageBody}" (type: ${messageType})`);

        if (messageBody) {
            handleMessage(from, messageBody);
        }

    } catch (error) {
        console.error('❌ Error processing webhook:', error.message);
        console.error(error.stack);
    }
});

// Handle message and generate response
function handleMessage(from, messageBody) {
    let reply = '';

    // Convert to lowercase for easier matching
    const lowerCaseMessage = messageBody.toLowerCase().trim();

    // FAQ Logic - Add your business-specific responses here
    if (lowerCaseMessage.includes('hi') || lowerCaseMessage.includes('hello') || lowerCaseMessage.includes('hey')) {
        reply = `Hello! Welcome to our business. How can we help you today? 😊`;
    } else if (lowerCaseMessage.includes('price') || lowerCaseMessage.includes('how much') || lowerCaseMessage.includes('cost')) {
        reply = `Our pricing starts from $50. For detailed pricing information, please visit our website or contact our sales team.`;
    } else if (lowerCaseMessage.includes('hours') || lowerCaseMessage.includes('open') || lowerCaseMessage.includes('time')) {
        reply = `We're open from 9 AM to 6 PM, Monday through Friday. We're closed on weekends and public holidays.`;
    } else if (lowerCaseMessage.includes('contact') || lowerCaseMessage.includes('number') || lowerCaseMessage.includes('email')) {
        reply = `You can reach us at +1-555-0123 or email us at info@business.com. Our team is available during business hours.`;
    } else if (lowerCaseMessage.includes('service') || lowerCaseMessage.includes('offer') || lowerCaseMessage.includes('provide')) {
        reply = `We offer a wide range of services including consulting, implementation, and support. Would you like to know more about any specific service?`;
    } else if (lowerCaseMessage.includes('thank') || lowerCaseMessage.includes('thanks')) {
        reply = `You're welcome! 😊 Is there anything else I can help you with?`;
    } else if (lowerCaseMessage.includes('bye') || lowerCaseMessage.includes('goodbye')) {
        reply = `Thank you for contacting us! Have a great day! 👋`;
    } else {
        reply = `Thank you for your message! Our team will get back to you shortly. For immediate assistance, please call us during business hours.`;
    }

    console.log(`🤖 Bot reply: "${reply}"`);
    sendMessage(from, reply);
}

// Send message function
async function sendMessage(to, text) {
    const phoneNumberId = process.env.PHONE_NUMBER_ID;
    const accessToken = process.env.WHATSAPP_ACCESS_TOKEN;

    if (!phoneNumberId || !accessToken) {
        console.error('❌ Missing required environment variables: PHONE_NUMBER_ID or WHATSAPP_ACCESS_TOKEN');
        return;
    }

    const url = `https://graph.facebook.com/v19.0/${phoneNumberId}/messages`;

    const data = {
        messaging_product: 'whatsapp',
        to: to,
        type: 'text',
        text: {
            body: text
        }
    };

    const config = {
        headers: {
            'Authorization': `Bearer ${accessToken}`,
            'Content-Type': 'application/json',
            'User-Agent': 'WhatsApp-Business-Bot/1.0'
        },
        timeout: 10000
    };

    try {
        console.log('🚀 Attempting to send message to Meta API...');
        console.log('API URL:', url);
        console.log('Request payload:', JSON.stringify(data, null, 2));
        
        const response = await axios.post(url, data, config);
        console.log('✅ Message sent successfully!');
        console.log('Response from Meta:', JSON.stringify(response.data, null, 2));
        
    } catch (error) {
        console.error('❌ Failed to send message:');
        
        if (error.response) {
            // Server responded with error status
            console.error('Status:', error.response.status);
            console.error('Headers:', error.response.headers);
            console.error('Error Data:', JSON.stringify(error.response.data, null, 2));
        } else if (error.request) {
            // Request was made but no response received
            console.error('No response received from Meta API');
            console.error('Request details:', error.request);
        } else {
            // Other errors
            console.error('Error message:', error.message);
        }
        
        console.error('Full error config:', error.config);
    }
}

// Health check endpoint
app.get('/', (req, res) => {
    res.json({
        status: 'OK',
        message: 'WhatsApp Business Bot is running',
        timestamp: new Date().toISOString()
    });
});

// Start server
app.listen(port, () => {
    console.log('='.repeat(60));
    console.log(`🚀 WhatsApp Business Bot Server started`);
    console.log(`📍 Listening on port ${port}`);
    console.log(`🌐 Webhook URL: https://your-app-name.up.railway.app/webhook`);
    console.log('='.repeat(60));
    
    // Check if required environment variables are set
    const requiredVars = ['WHATSAPP_ACCESS_TOKEN', 'VERIFY_TOKEN', 'PHONE_NUMBER_ID'];
    const missingVars = requiredVars.filter(varName => !process.env[varName]);
    
    if (missingVars.length > 0) {
        console.warn('⚠️  WARNING: Missing environment variables:', missingVars);
    } else {
        console.log('✅ All required environment variables are set');
    }
});