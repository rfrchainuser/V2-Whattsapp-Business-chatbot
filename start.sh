#!/bin/bash
# Wait for any initialization tasks
sleep 5

# Start Gunicorn
exec gunicorn --bind 0.0.0.0:$PORT app:app --workers=4
