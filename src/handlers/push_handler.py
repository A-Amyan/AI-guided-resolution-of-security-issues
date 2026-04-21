import logging
from flask import jsonify

from src.services.github_client import (create_pull_request_for_push)
from src.services.openai_client import generate_pr_description_for_multiple_files

def handle_push(payload):
    """
    Handle GitHub push events by creating an auto-PR for any branch, including the default branch.
    Auto-generated PR branches (named "auto-pr/..." ) are ignored to prevent recursive triggers.
    """
    # 1) Determine ref and branch
    ref = payload.get('ref', '')
    if not ref.startswith('refs/heads/'):
        logging.debug(f"Ignored ref (not a branch): {ref}")
        return jsonify({'status': 'ignored - not a branch push'}), 200
    branch = ref.split('/', 2)[-1]

    # Ignore pushes for auto-generated PR branches
    if branch.startswith("auto-pr/"):
        logging.debug(f"Ignored auto-pr branch push: {branch}")
        return jsonify({'status': 'ignored - auto-pr branch'}), 200

    # 2) Validate payload
    repo_info = payload.get('repository')
    if not repo_info:
        logging.error("Missing 'repository' in push payload.")
        return jsonify({'status': 'missing repository'}), 400
    installation = payload.get('installation')
    if not installation or not installation.get('id'):
        logging.error("Missing 'installation' in push payload.")
        return jsonify({'status': 'missing installation'}), 400
    installation_id = installation['id']

    # 3) Authenticate & get repo client
    full_name = repo_info['full_name']
    github = get_github_client_for_repo(full_name, installation_id=installation_id)
    repo = github.get_repo(full_name)
    default_branch = getattr(repo, 'default_branch', 'main')
    logging.debug(f"Pushed branch: {branch}, default: {default_branch}")

    # 4) Collect Java files in this push
    java_files = {
        f for commit in payload.get('commits', [])
        for f in commit.get('added', []) + commit.get('modified', [])
        if f.endswith('.java')
    }
    if not java_files:
        java_files = {"No Java file changed."}
    logging.debug(f"Java files detected: {java_files}")

    # 5) Prepare PR source branch (create temp branch if pushing to default)
    commit_sha = payload.get('after')
    pr_source = branch
    if branch == default_branch:
        temp_branch = f"auto-pr/{branch}/{commit_sha[:7]}"
        try:
            repo.create_git_ref(ref=f"refs/heads/{temp_branch}", sha=commit_sha)
            pr_source = temp_branch
            logging.debug(f"Created temporary branch for PR: {temp_branch}")
        except Exception as e:
            logging.error(f"Failed to create temporary branch '{temp_branch}': {e}", exc_info=True)
            return jsonify({'status': 'failed to create temp branch'}), 500

    # 6) Build PR description
    pusher = payload.get('pusher', {}).get('name', 'user')
    token_str = git_integration.get_access_token(installation_id=installation_id).token
    try:
        pr_body = generate_pr_description_for_multiple_files(
            branch_name=branch,
            pusher_name=pusher,
            java_file_names=list(java_files),
            repo_full_name=full_name,
            token_str=token_str,
            commit_ref=commit_sha
        )
    except Exception as e:
        logging.error(f"PR description failed: {e}", exc_info=True)
        pr_body = (
            f"Automated PR from '{branch}' by {pusher}.\n\n"
            f"Files changed:\n" + "\n".join(f"- {f}" for f in java_files)
        )

    # 7) Check for existing PR
    try:
        owner_login = repo.owner.login
        existing = repo.get_pulls(state='open', head=f"{owner_login}:{pr_source}", base=default_branch)
        if existing.totalCount > 0:
            pr = existing[0]
            logging.info(f"Existing PR found: {pr.html_url}")
            return jsonify({'status': 'exists', 'pr_url': pr.html_url}), 200
    except Exception:
        logging.debug("Failed to check for existing PRs", exc_info=True)

    # 8) Create the pull request
    try:
        pr = create_pull_request_for_push(repo, pr_source, default_branch, pr_body)
        if not pr:
            raise RuntimeError("create_pull_request_for_push returned None")
        logging.info(f"Created PR: {pr.html_url}")
        return jsonify({'status': 'success', 'pr_url': pr.html_url}), 200
    except Exception as e:
        logging.error(f"Error creating PR: {e}", exc_info=True)
        return jsonify({'status': 'failed to create PR'}), 500
    

