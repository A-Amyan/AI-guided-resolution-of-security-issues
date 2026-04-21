import logging
from flask import Blueprint, request, jsonify

from src.handlers import push_handler
from src.handlers import pr_handler
from src.handlers import issue_handler

webhook_bp = Blueprint('webhook', __name__)

@webhook_bp.route('/ping')
def ping():
    return 'pong', 200

@webhook_bp.route('/webhook', methods=['POST'])
def webhook():
    event = request.headers.get('X-GitHub-Event')
    payload = request.json
    logging.debug(f"Received event: {event}")
    
    if event == 'push':
        return push_handler.handle_push(payload)
    elif event == 'pull_request':
        return pr_handler.handle_pull_request(payload)
    elif event == 'issue_comment':
        return issue_handler.handle_issue_comment(payload)
        
    return jsonify({'status': 'ignored'}), 200

