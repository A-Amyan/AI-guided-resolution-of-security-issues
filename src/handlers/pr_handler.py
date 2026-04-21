import logging
from flask import jsonify

from src.services.github_client import get_github_client, post_pr_comment
from src.services.analyzer import analyze_pr_no_issue

def handle_pull_request(payload):
    action = payload.get('action')
    pr_data = payload.get('pull_request')
    repo_info = payload.get('repository')
    installation = payload.get('installation')
    
    if not (pr_data and repo_info and installation):
        return jsonify({'status': 'missing fields'}), 400
    
    if action not in ['opened', 'synchronize', 'edited', 'ready_for_review']:
        return jsonify({'status': f'ignored {action}'}), 200
    
    repo_full_name = repo_info.get('full_name', '')
    installation_id = installation.get('id')
    if not installation_id:
        return jsonify({'status': 'missing installation'}), 400
    
    # Obtain a Github instance using your helper
    github_instance = get_github_client_for_repo(repo_full_name, installation_id=installation_id)

    # Now do everything with github_instance instead of referencing "access_token" or "token_str":
    repo = github_instance.get_repo(repo_full_name)
    pr_number = pr_data.get('number', 0)
    pr = repo.get_pull(pr_number)
    admin_username = repo.owner.login

    # For each changed file, analyze, etc.
    for pr_file in pr.get_files():
        if pr_file.filename.endswith('.java') and pr_file.status in ['added', 'modified']:
            content = fetch_file_content(repo, pr_file.filename, pr.head.sha)
            # ^ You could pass None or a token here, depending on how fetch_file_content uses the token
            if content:
                analysis_result = analyze_code_no_issue(content, repo)
                comment = f"@{admin_username} **Security Analysis for `{pr_file.filename}`**\n\n{analysis_result}"
                post_pr_comment(github_instance, repo_full_name, pr_number, comment)

    return jsonify({'status': 'success'}), 200


