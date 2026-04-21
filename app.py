import os
from flask import Flask
from config import PORT
from src.routes.webhook import webhook_bp

def create_app():
    app = Flask(__name__)
    
    # Register blueprints
    app.register_blueprint(webhook_bp)
    
    return app

if __name__ == '__main__':
    app = create_app()
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)