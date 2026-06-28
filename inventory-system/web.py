"""Railway/Cloud deployment entry point - starts Flask without local file imports"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app, init_db

# Initialize DB only (no Excel import on cloud)
init_db()
print("Database initialized for cloud deployment.")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
else:
    # For gunicorn
    application = app
