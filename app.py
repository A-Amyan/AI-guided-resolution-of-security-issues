import os
import sys
import re
import json
import base64
import logging
import requests

from flask import Flask, request, jsonify
from dotenv import load_dotenv

import openai

# GitHub imports
from github import GithubIntegration, Github, Auth

###############################################################################
# Minimal logging: only errors
###############################################################################
logging.basicConfig(level=logging.ERROR)

###############################################################################
# Load environment variables
###############################################################################
load_dotenv()
APP_ID = os.getenv('GITHUB_APP_ID')
PRIVATE_KEY = os.getenv('GITHUB_PRIVATE_KEY')
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not APP_ID:
    logging.error("GITHUB_APP_ID not set.")
    sys.exit(1)

if not PRIVATE_KEY:
    logging.error("GITHUB_ORIVET_KEY not set")

try:
    APP_ID = int(APP_ID)
except ValueError:
    logging.error("GITHUB_APP_ID must be an integer.")
    sys.exit(1)

###############################################################################
# GitHub App Auth + OpenAI init
###############################################################################
auth = Auth.AppAuth(app_id=APP_ID, private_key=PRIVATE_KEY)
git_integration = GithubIntegration(auth=auth)
openai.api_key = OPENAI_API_KEY
if not openai.api_key:
    logging.error("No OPENAI_API_KEY found. AI calls may fail.")

###############################################################################
# In-memory conversation store
###############################################################################
conversation_store = {}

def get_or_create_conversation(repo_full_name, issue_number, issue_body=None):
    """
    Retrieve or create a conversation for ChatCompletion.
    Optionally inject the issue body as a message if it's the first time we see this issue.
    """
    key = (repo_full_name, issue_number)
    if key not in conversation_store:
        system_message = (
            "You are an assistant specialized in Java security analysis, "
            "best practices, and general code discussions. Keep context from "
            "previous messages in this issue to maintain a coherent conversation."
        )
        conversation_store[key] = [{"role": "system", "content": system_message}]
        if issue_body:
            conversation_store[key].append({
                "role": "assistant",
                "content": f"Issue Body:\n\n{issue_body}"
            })
    return conversation_store[key]

###############################################################################
# Flask App
###############################################################################
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    event = request.headers.get('X-GitHub-Event')
    payload = request.json

    if event == 'push':
        return handle_push(payload)
    elif event == 'pull_request':
        return handle_pull_request(payload)
    elif event == 'issue_comment':
        return handle_issue_comment(payload)

    return jsonify({'status': 'ignored'}), 200

@app.route('/ping')
def ping():
    return 'pong', 200

###############################################################################
# PUSH Logic
# (No new Issue creation for misuses; only create PR if needed, no merges)
###############################################################################
def handle_push(payload):
    repository = payload.get('repository')
    installation = payload.get('installation')
    if not (repository and installation):
        logging.error("Missing 'repository' or 'installation' in push payload.")
        return jsonify({'status': 'missing fields'}), 400

    installation_id = installation.get('id')
    if not installation_id:
        return jsonify({'status': 'missing installation'}), 400

    access_token = git_integration.get_access_token(installation_id=installation_id)
    token_str = access_token.token
    github = Github(token_str)

    full_name = repository.get('full_name', '')
    ref = payload.get('ref', '')
    pusher_name = payload.get('pusher', {}).get('name', 'user')

    # Only handle branch pushes
    if not ref.startswith("refs/heads/"):
        return jsonify({'status': 'ignored - not a branch push'}), 200

    branch_name = ref.replace("refs/heads/", "", 1)
    repo = github.get_repo(full_name)

    try:
        default_branch = repo.default_branch
    except:
        logging.error("Could not identify default branch.")
        default_branch = "main"

    # Skip if pushing to the default branch
    if branch_name == default_branch:
        return jsonify({'status': 'ignored - push on default branch'}), 200
    
    commits = payload.get('commits', [])
    java_files = set()
    for commit in commits:
        # Check "added" files
        for f in commit.get('added', []):
            if f.endswith('.java'):
                java_files.add(f)
        # Check "modified" files
        for f in commit.get('modified', []):
            if f.endswith('.java'):
                java_files.add(f)

    # Pick one Java file to highlight, or use a fallback
    if java_files:
        java_file_name = list(java_files)[0]  # or use some logic to pick a specific file
    else:
        java_file_name = "No Java file changed."

    # Possibly we have a commit SHA from the payload, e.g.
    commit_sha = payload.get('after')

    # Suppose we found or guessed a Java file in your commits as `java_file_name`
    # Then call the new function:
    short_desc = generate_pr_description_with_ai(
        branch_name=branch_name,
        pusher_name=pusher_name,
        java_file_name=java_file_name,
        repo_full_name=full_name,
        token_str=token_str,
        commit_ref=commit_sha  # or just branch_name
    )

    pr = create_pull_request_for_push(repo, branch_name, default_branch, short_desc)
    if not pr:
        return jsonify({'status': 'failed to create PR'}), 500

    return jsonify({'status': 'success', 'pr_url': pr.html_url}), 200

def generate_pr_description_with_ai(
    branch_name: str,
    pusher_name: str,
    java_file_name: str,
    repo_full_name: str,
    token_str: str,
    commit_ref: str = None
):
    """
    Generate a short PR description using GPT:
      - Mentions the branch name, pusher name, and the file name in triple backticks.
      - Fetches the actual file content from GitHub and provides a short GPT summary of it.
    :param branch_name: Name of the branch that was pushed
    :param pusher_name: Name of the user who pushed
    :param java_file_name: Path to the .java file that was changed
    :param repo_full_name: e.g. "myorg/myrepo"
    :param token_str: GitHub token for fetching file content
    :param commit_ref: (Optional) the specific commit SHA or branch ref to fetch from
    :return: A string containing the PR description
    """

    # 1) If there's no OpenAI key, fallback
    if not openai.api_key:
        return (
            f"Automated PR from '{branch_name}' by {pusher_name}.\n\n"
            f"Impacted file (no AI summary):\n```java\n{java_file_name}\n```"
        )

    # 2) Try fetching the file content from GitHub
    github = Github(token_str)
    repo = github.get_repo(repo_full_name)
    
    # By default, we’ll fetch from the pushed commit SHA (if provided)
    # or from the branch name itself.
    ref_to_fetch = commit_ref if commit_ref else branch_name

    try:
        file_obj = repo.get_contents(java_file_name, ref=ref_to_fetch)
        file_content = base64.b64decode(file_obj.content).decode("utf-8")
    except Exception as e:
        # If we fail to fetch the file, still return a minimal description
        return (
            f"Automated PR from '{branch_name}' by {pusher_name}.\n\n"
            f"Could not fetch the file `{java_file_name}` for AI summary.\n"
            f"Error: {e}"
        )

    # 3) Build a short snippet of the file to keep GPT prompt concise
    # (If your file is large, you may want to limit to first N lines or N characters.)
    MAX_CHARS = 1000
    snippet = file_content[:MAX_CHARS]
    if len(file_content) > MAX_CHARS:
        snippet += "\n... [Truncated for prompt brevity] ..."

    # 4) Construct the GPT prompt
    system_prompt = (
        "You are an assistant who writes short PR descriptions.\n"
        "You have been given the name of a branch, the pusher's name, and a Java file's content.\n"
        "Write a concise summary of what the file does or changes. Then mention the branch, pusher, and file name.\n"
    )
    user_prompt = (
        f"Branch Name: {branch_name}\n"
        f"Pusher: {pusher_name}\n"
        f"File Path: {java_file_name}\n\n"
        "File Content Snippet:\n"
        "```java\n"
        f"{snippet}\n"
        "```\n\n"
        "Please write a short summary for a PR that merges this branch to the main branch. "
        "Mention the file in triple backticks at the end."
    )

    # 5) Call GPT to generate a short description
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            temperature=0,
            max_tokens=300,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        gpt_text = resp.choices[0].message.content.strip()
    except Exception as e:
        return (
            f"Automated PR from '{branch_name}' by {pusher_name}.\n\n"
            f"Could not get AI-based summary. Error: {e}\n\n"
            f"File:\n```java\n{java_file_name}\n```"
        )

    # 6) Return the final PR description
    #    Include the GPT summary, then a final mention of the file in triple backticks:
    return (
        f"{gpt_text}\n\n"
        f"**Branch**: `{branch_name}`\n"
        f"**Pusher**: `{pusher_name}`\n\n"
        "**Impacted file**:\n"
        f"```java\n{java_file_name}\n```"
    )


def create_pull_request_for_push(repo, source_branch, target_branch, pr_body):
    pr_title = f"Auto PR from branch '{source_branch}'"
    try:
        new_pr = repo.create_pull(
            title=pr_title,
            body=pr_body,
            head=source_branch,
            base=target_branch
        )
        return new_pr
    except Exception:
        return None

###############################################################################
# Pull Request Logic
# (No new Issues on misuses in PR. We just post a summary comment.)
###############################################################################
def handle_pull_request(payload):
    action = payload.get('action')
    pr_data = payload.get('pull_request')
    repo_info = payload.get('repository')
    installation = payload.get('installation')

    if not (pr_data and repo_info and installation):
        return jsonify({'status': 'missing fields'}), 400

    if action not in ['opened', 'synchronize', 'edited', 'ready_for_review']:
        return jsonify({'status': f'ignored {action}'}), 200

    installation_id = installation.get('id')
    if not installation_id:
        return jsonify({'status': 'missing installation'}), 400

    access_token = git_integration.get_access_token(installation_id=installation_id)
    token_str = access_token.token

    repo_full_name = repo_info.get('full_name', '')
    pr_number = pr_data.get('number', 0)
    pr_user_login = pr_data.get('user', {}).get('login', 'user')

    # Analyze PR .java changes, but do NOT open an issue for each misuse
    summary_comment = analyze_pr_no_issue(repo_full_name, pr_number, token_str, pr_user_login)
    post_pr_comment(Github(token_str), repo_full_name, pr_number, summary_comment)
    return jsonify({'status': 'success'}), 200

def analyze_pr_no_issue(repo_full_name, pr_number, token_str, pr_user_login):
    github = Github(token_str)
    repo = github.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)

    java_files = []
    for pr_file in pr.get_files():
        if pr_file.filename.endswith('.java') and pr_file.status in ['added', 'modified']:
            content = fetch_file_content(repo, pr_file.filename, pr.head.sha, token_str)
            if content:
                snippet = f"--- {pr_file.filename} ---\n{content}"
                java_files.append(snippet)

    if not java_files:
        return "No Java code found in this pull request."

    combined_code = "\n\n".join(java_files)

    # *** Changes to the user prompt to add a short summary if no misuses found ***
    system_prompt = (
        "You are a Java security analyst. The user wants to detect any of these 16 misuses:\n"
        "1) Hardcoded cryptographic keys in SecretKeySpec\n"
        "2) Hardcoded password in PBEKeySpec\n"
        "3) Hardcoded KeyStore password\n"
        "4) HostnameVerifier returning true\n"
        "5) X509TrustManager with empty certificate validation\n"
        "6) SSLSocket with omitted hostname verification\n"
        "7) Using HTTP instead of HTTPS\n"
        "8) Using java.util.Random instead of SecureRandom\n"
        "9) Using constant seed in SecureRandom\n"
        "10) Using constant salt in PBEParameterSpec\n"
        "11) Using ECB mode instead of CBC/GCM\n"
        "12) Using static/constant IV\n"
        "13) Using iteration count < 1000 in PBEParameterSpec\n"
        "14) Using broken symmetric ciphers (DES, Blowfish, RC4, etc.) instead of AES\n"
        "15) Using RSA key size < 2048 bits\n"
        "16) Using broken hash function (e.g. SHA1, MD5) instead of stronger ones (e.g. SHA-256)\n"
        "If you find any, return them in a JSON array with:\n"
        " - name\n"
        " - location\n"
        " - description\n"
        " - severity\n"
        " - correction\n\n"
        "If you find none, return an empty array **and** a short textual summary "
        "of why there are no misuses or any best practices."
    )
    user_prompt = (
        f"Analyze the following Java code:\n\n{combined_code}\n\n"
        "Return a JSON array of objects if you find any misuses. If none, return an empty array plus a short explanation."
    )

    if not openai.api_key:
        return "No OpenAI API key configured; skipping AI analysis."

    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            temperature=0,
            max_tokens=2200,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        ai_text = resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"OpenAI call failed: {e}")
        return "Failed to analyze code with AI."


    misuses = parse_ai_output(ai_text)
    if not misuses:
        # The AI text might contain a short summary after an empty array. We'll show it.
        return f"**AI Output**:\n```json\n{ai_text}\n```"

    # Build a summary
    summary_lines = []
    for misuse in misuses:
        summary_lines.append(
            f"- **{misuse.get('name')}**\n"
            f"  Location: {misuse.get('location')}\n"
            f"  Severity: {misuse.get('severity')}\n"
            f"  Description: {misuse.get('description')}\n"
            f"  Correction:\n```java\n{misuse.get('correction')}\n```"
        )

    return (
        "**Potential Security Misuses Found** (No Issues created)\n\n"
        + "\n\n".join(summary_lines)
        + f"\n\n**AI Output**:\n```json\n{ai_text}\n```"
    )

def analyze_code_no_issue(java_code):
    """
    Same system and user prompts as `analyze_pr_no_issue`,
    but for a single snippet of code. 
    Returns a textual analysis with potential misuses or 'No issues' + summary.
    """
    if not openai.api_key:
        return "No OpenAI API key configured; skipping AI analysis."

    system_prompt = (
        "You are a Java security analyst. The user wants to detect any of these 16 misuses:\n"
        "1) Hardcoded cryptographic keys in SecretKeySpec\n"
        "2) Hardcoded password in PBEKeySpec\n"
        "3) Hardcoded KeyStore password\n"
        "4) HostnameVerifier returning true\n"
        "5) X509TrustManager with empty certificate validation\n"
        "6) SSLSocket with omitted hostname verification\n"
        "7) Using HTTP instead of HTTPS\n"
        "8) Using java.util.Random instead of SecureRandom\n"
        "9) Using constant seed in SecureRandom\n"
        "10) Using constant salt in PBEParameterSpec\n"
        "11) Using ECB mode instead of CBC/GCM\n"
        "12) Using static/constant IV\n"
        "13) Using iteration count < 1000 in PBEParameterSpec\n"
        "14) Using broken symmetric ciphers (DES, Blowfish, RC4, etc.) instead of AES\n"
        "15) Using RSA key size < 2048 bits\n"
        "16) Using broken hash function (SHA1, MD5) instead of stronger ones (e.g. SHA-256)\n"
        "If you find any, return them in a JSON array with:\n"
        " - name\n"
        " - location\n"
        " - description\n"
        " - severity\n"
        " - correction\n\n"
        "If you find none, return an empty array **and** a short textual summary."
    )
    user_prompt = (
        f"Analyze this Java code:\n\n{java_code}\n\n"
        "Return a JSON array of objects if you find any misuses. If none, return an empty array plus a short explanation."
    )

    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            temperature=0,
            max_tokens=2200,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        ai_text = resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"OpenAI call failed: {e}")
        return "Failed to analyze code with AI."

    misuses = parse_ai_output(ai_text)
    if not misuses:
        return f"```json\n{ai_text}\n```"

    summary_lines = []
    for misuse in misuses:
        summary_lines.append(
            f"- **{misuse.get('name')}**\n"
            f"  Location: {misuse.get('location')}\n"
            f"  Severity: {misuse.get('severity')}\n"
            f"  Description: {misuse.get('description')}\n"
            f"  Correction:\n```java\n{misuse.get('correction')}\n```"
        )

    return (
        "**Potential Security Misuses Found** (No Issues created)\n\n"
        + "\n\n".join(summary_lines)
        + f"\n\n**AI Output**:\n```json\n{ai_text}\n```"
    )

def analyze_repo_and_open_issues(github, repo_full_name, token_str):
    repo = github.get_repo(repo_full_name)
    default_branch = repo.default_branch

    java_files = []
    contents = repo.get_contents("", ref=default_branch)
    queue = contents[:]
    while queue:
        item = queue.pop()
        if item.type == 'dir':
            queue.extend(repo.get_contents(item.path, ref=default_branch))
        elif item.type == 'file' and item.path.endswith('.java'):
            java_files.append(item.path)

    created_count = 0
    for file_path in java_files:
        raw_code = fetch_file_content(repo, file_path, default_branch, token_str)
        if not raw_code:
            continue

        print(f"=== DEBUG: read file '{file_path}' (length {len(raw_code)} chars) ===\n")
        print(raw_code)
        print("=== END OF FILE ===\n")

        analysis = analyze_code_no_issue(raw_code)
        title = f"Security Analysis for {file_path}"
        body = (
            f"**File:** `{file_path}`\n\n"
            f"**Analysis:**\n\n{analysis}\n"
        )
        try:
            repo.create_issue(title=title, body=body)
            created_count += 1
        except:
            pass

    return f"Analyzed {len(java_files)} .java files. Created {created_count} Issues."

def fetch_file_content(repo, filename, ref, token_str):
    api_url = f"https://api.github.com/repos/{repo.full_name}/contents/{filename}?ref={ref}"
    headers = {"Authorization": f"Bearer {token_str}"}
    try:
        r = requests.get(api_url, headers=headers)
        r.raise_for_status()
        data = r.json()
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8")
        else:
            return None
    except:
        return None

def parse_ai_output(ai_text):
    try:
        data = json.loads(ai_text)
        if isinstance(data, dict):
            data = [data]
        elif not isinstance(data, list):
            data = []
        valid = []
        required_keys = ["name", "location", "description", "severity", "correction"]
        for item in data:
            if all(k in item for k in required_keys):
                valid.append(item)
        return valid
    except:
        return []

def post_pr_comment(github, repo_full_name, pr_number, comment_body):
    try:
        repo = github.get_repo(repo_full_name)
        pr = repo.get_pull(pr_number)
        pr.create_issue_comment(comment_body)
    except:
        pass

def handle_issue_comment(payload):
    action = payload.get('action')
    comment_data = payload.get('comment')
    issue_data = payload.get('issue')
    repo_info = payload.get('repository')
    installation = payload.get('installation')

    if not (comment_data and issue_data and repo_info and installation):
        return jsonify({'status': 'missing fields'}), 400

    if action not in ['created', 'edited']:
        return jsonify({'status': f'ignored {action}'}), 200

    installation_id = installation.get('id')
    if not installation_id:
        return jsonify({'status': 'missing installation'}), 400

    access_token = git_integration.get_access_token(installation_id=installation_id)
    token_str = access_token.token
    github = Github(token_str)

    repo_full_name = repo_info.get('full_name', '')
    issue_number = issue_data.get('number', 0)
    issue_body = issue_data.get('body', '')

    comment_body = comment_data.get('body', '')
    user_login = comment_data.get('user', {}).get('login', 'user')

    # Ignore any bot's own comments
    if user_login.endswith('[bot]'):
        return jsonify({'status': 'bot comment ignored'}), 200

    # Retrieve or create the conversation thread
    conversation = get_or_create_conversation(repo_full_name, issue_number, issue_body=issue_body)
    conversation.append({"role": "user", "content": comment_body})

    # 1) @AIBot analyze repo: analyze all the exicting Java files in the repo
    if "@AIBot analyze repo" in comment_body:
        result_msg = analyze_repo_and_open_issues(github, repo_full_name, token_str)
        conversation.append({"role": "assistant", "content": result_msg})
        post_issue_comment(github, repo_full_name, issue_number, result_msg)
        return jsonify({'status': 'success'}), 200

    # 2) @AIBot update code: Get the correction from the PR (description & comments) to update the PR code
    if "@AIBot update code" in comment_body:
        update_msg = attempt_update_pr_code(repo_full_name, issue_number, token_str, conversation)
        conversation.append({"role": "assistant", "content": update_msg})
        post_issue_comment(github, repo_full_name, issue_number, update_msg)
        return jsonify({'status': 'success'}), 200


    # 3) @AIBot update: Print the last version of the relevant file
    if "@AIBot update" in comment_body:
        fetched_code_msg = attempt_fetch_current_code(repo_full_name, issue_number, token_str)
        conversation.append({"role": "assistant", "content": fetched_code_msg})
        post_issue_comment(github, repo_full_name, issue_number, fetched_code_msg)
        return jsonify({'status': 'success'}), 200

    # 4) @AIBot merge code merger the laste written code between tripletick
    if "@AIBot merge code" in comment_body:
        merged_msg = attempt_merge_corrected_code(repo_full_name, issue_number, token_str)
        conversation.append({"role": "assistant", "content": merged_msg})
        post_issue_comment(github, repo_full_name, issue_number, merged_msg)
        return jsonify({'status': 'success'}), 200

    # 5) @AIBot analyze code: analyze the relevant file
    if "@AIBot analyze code" in comment_body:
        # Look for the most recent code snippet in the conversation
        code_snippet = find_last_code_snippet(conversation)
        if code_snippet:
            analyze_result = analyze_code_no_issue(code_snippet)
        else:
            analyze_result = (
                "No code snippet found in this conversation. "
                "Please provide your Java code in triple-backticks, for example:\n\n"
                "```java\npublic class Example {\n    // ...\n}\n```\n\n"
                "Then mention `@AIBot analyze code` again."
            )

        conversation.append({"role": "assistant", "content": analyze_result})
        post_issue_comment(github, repo_full_name, issue_number, analyze_result)
        return jsonify({'status': 'success'}), 200

    # 6) @AIBot close issue
    if "@AIBot close issue" in comment_body:
        msg = attempt_close_issue(github, repo_full_name, issue_number)
        conversation.append({"role": "assistant", "content": msg})
        post_issue_comment(github, repo_full_name, issue_number, msg)
        return jsonify({'status': 'success'}), 200

    # Otherwise, let the assistant respond in a general chat style
    ai_reply = chat_with_history(conversation)
    conversation.append({"role": "assistant", "content": ai_reply})
    post_issue_comment(github, repo_full_name, issue_number, ai_reply)
    return jsonify({'status': 'success'}), 200


def post_issue_comment(github, repo_full_name, issue_number, reply_text):
    try:
        repo = github.get_repo(repo_full_name)
        issue_obj = repo.get_issue(issue_number)
        issue_obj.create_comment(reply_text)
    except:
        pass

def chat_with_history(messages):
    if not openai.api_key:
        return "No AI is configured."

    ephemeral_instruction = {
        "role": "system",
        "content": (
            "Additionally, if you propose any code changes or corrected lines, "
            "always provide them in triple-backtick format. For example:\n\n"
            "1) File Reference Block:\n```java\nMyClass.java\n```\n"
            "2) Code Snippet Block:\n```java\npublic class MyClass { ... }\n```\n\n"
            "Then type `@AIBot merge code`."
        )
    }

    ephemeral_messages = [ephemeral_instruction] + messages
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            messages=ephemeral_messages,
            temperature=0
        )
        return resp.choices[0].message.content.strip()
    except:
        return "Failed to respond with AI."

def attempt_update_pr_code(repo_full_name, pr_number, token_str, conversation):
    """
    1) Fetch the Pull Request object and get the PR branch name (pr.head.ref).
    2) Find the relevant .java file(s) from the PR or from user instructions.
    3) Collect context from the PR body and conversation.
    4) Send everything to GPT asking for an updated version of the file with minimal changes.
    5) Commit the updated file to the PR branch.
    """

    github = Github(token_str)
    repo = github.get_repo(repo_full_name)

    try:
        pr = repo.get_pull(pr_number)
    except Exception as e:
        return f"Could not get PR #{pr_number}: {e}"

    pr_branch = pr.head.ref  # e.g. "feature/some-branch"

    # -------------------------------------------------------------------------
    # 1) Identify which file(s) to update.
    #
    #   For simplicity, we assume only ONE relevant Java file is being changed
    #   or we simply pick the first .java file. If you want to handle multiple
    #   files, you could gather them all or ask the user to specify exactly
    #   which file to update in triple backticks, similarly to your existing
    #   find_file_name_in_conversation().
    # -------------------------------------------------------------------------
    file_to_update = None
    for f in pr.get_files():
        if f.filename.endswith('.java'):
            file_to_update = f.filename
            break
    if not file_to_update:
        return "No .java files found in this PR to update."

    # We can also see if the user explicitly provided a file in conversation:
    # user_file_name = find_file_name_in_conversation(conversation)
    # if user_file_name:
    #     file_to_update = user_file_name

    # -------------------------------------------------------------------------
    # 2) Fetch the current content of that file from the PR branch
    # -------------------------------------------------------------------------
    try:
        contents = repo.get_contents(file_to_update, ref=pr_branch)
    except Exception as e:
        return (
            f"Failed to fetch '{file_to_update}' from branch '{pr_branch}'. "
            f"Make sure that file exists in the PR. Error: {e}"
        )

    original_code = base64.b64decode(contents.content).decode("utf-8")

    # -------------------------------------------------------------------------
    # 3) Build an AI prompt that includes:
    #    - The user instructions (from the conversation & PR body)
    #    - The original file content
    #    - A request to only do minimal changes
    # -------------------------------------------------------------------------
    # Join up the conversation messages from the user or the PR description
    # that might contain instructions. You can decide how much or how little
    # context to feed GPT. As an example, we’ll do a short approach:
    # -------------------------------------------------------------------------
    instructions = ""
    # Collect some relevant conversation messages
    for msg in conversation:
        if msg['role'] == 'user':
            instructions += f"User said:\n{msg['content']}\n\n"

    # Also append the PR body if needed
    if pr.body:
        instructions += f"PR Description:\n{pr.body}\n\n"

    system_prompt = (
        "You are a Java code refactoring assistant. The user has provided instructions "
        "for how to fix or update this Java file. You have the original code below.\n\n"
        "Apply only the minimal changes needed to address the user's instructions.\n\n"
        "Return the entire updated file content. Do NOT wrap in JSON or mention line numbers; "
        "simply return the new file as plain text."
    )

    user_prompt = (
        f"User instructions / discussion:\n\n{instructions}\n"
        "Original file content:\n"
        "```java\n"
        f"{original_code}\n"
        "```\n\n"
        "Please return the updated file with minimal changes. "
        "Only change lines necessary to implement the user's requests."
    )

    # -------------------------------------------------------------------------
    # 4) Call GPT to get the updated code
    # -------------------------------------------------------------------------
    if not openai.api_key:
        return "No OpenAI API key is configured; skipping AI-based code update."

    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            temperature=0,
            max_tokens=3000,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        updated_code = resp.choices[0].message.content.strip()
    except Exception as e:
        return f"OpenAI call failed: {e}"

    # -------------------------------------------------------------------------
    # 5) Commit changes to the PR branch
    # -------------------------------------------------------------------------
    commit_message = f"[AIBot] Update code in {file_to_update} from PR #{pr_number}"
    try:
        repo.update_file(
            path=file_to_update,
            message=commit_message,
            content=updated_code,
            sha=contents.sha,
            branch=pr_branch,
        )
    except Exception as e:
        return f"Failed to commit updated code: {e}"

    # -------------------------------------------------------------------------
    # 6) Return a success message
    # -------------------------------------------------------------------------
    return (
        f"Successfully updated the following file in the PR branch `{pr_branch}`:\n\n"
        f"```java\n{file_to_update}\n```\n\n"
        "Below is the updated code:\n\n"
        f"```java\n{updated_code}\n```"
    )

def attempt_merge_corrected_code(repo_full_name, issue_number, token_str):
    messages = conversation_store.get((repo_full_name, issue_number), [])
    if not messages:
        return "No conversation found; cannot merge code."

    file_name = find_file_name_in_conversation(messages)
    if not file_name:
        return (
            "I couldn't find a file reference in triple backticks or text. "
            "If `MyClass.java` was just an example, please provide the correct file name, e.g.:\n\n"
            "```java\nActualFile.java\n```\n\n"
            "Then the code snippet in triple-backticks as well. "
            "After that, type `@AIBot merge code`."
        )

    code_snippet = find_last_code_snippet(messages)
    if not code_snippet:
        return (
            "I couldn't detect a code snippet to merge. "
            "Please provide it in triple-backtick format, e.g. ```java\n...\n``` then '@AIBot merge code'."
        )

    github = Github(token_str)
    repo = github.get_repo(repo_full_name)
    default_branch = repo.default_branch

    try:
        file_contents = repo.get_contents(file_name, ref=default_branch)
    except Exception as e:
        error_msg = f"The file '{file_name}' doesn't exist in default branch '{default_branch}'. Error: {e}"
        return error_msg

    new_content = code_snippet
    commit_msg = f"Update {file_name} from Issue #{issue_number}"
    try:
        repo.update_file(
            path=file_name,
            message=commit_msg,
            content=new_content,
            sha=file_contents.sha,
            branch=default_branch
        )
        return f"Code snippet successfully merged into `{file_name}` on branch `{default_branch}`."
    except Exception as e:
        return f"Failed to merge code snippet into `{file_name}`: {e}"

def attempt_close_issue(github, repo_full_name, issue_number):
    try:
        repo = github.get_repo(repo_full_name)
        issue_obj = repo.get_issue(issue_number)
        if issue_obj.state.lower() == 'closed':
            return "Issue is already closed."
        issue_obj.edit(state='closed')
        return f"Issue #{issue_number} has been closed."
    except Exception as e:
        return f"Failed to close the issue: {e}"

def find_file_name_in_conversation(messages):
    pattern_triple_tick = r"```java\s+([\w\d_/\\.-]+\.java)\s*```"
    for msg in messages:
        if msg['role'] in ['assistant', 'system', 'user']:
            match = re.search(pattern_triple_tick, msg['content'])
            if match:
                return match.group(1)
    pattern_file_line = r"([\w\d_/\\.-]+\.java)"
    for msg in messages:
        if msg['role'] in ['assistant', 'system', 'user']:
            match = re.search(pattern_file_line, msg['content'])
            if match:
                return match.group(1)
    return None

def find_last_code_snippet(messages):
    pattern = r"```(?:java)?\s*(.*?)```"
    for msg in reversed(messages):
        if msg['role'] in ('user', 'assistant'):
            blocks = re.findall(pattern, msg['content'], flags=re.DOTALL)
            if blocks:
                return blocks[-1].strip()
    return None

def attempt_fetch_current_code(repo_full_name, issue_number, token_str):
    messages = conversation_store.get((repo_full_name, issue_number), [])
    if not messages:
        return "No conversation found; cannot fetch code."

    file_name = None
    pattern_triple_tick = r"```java\s+([\w\d_/\\.-]+\.java)\s*```"
    for msg in messages:
        if msg['role'] in ['assistant', 'system']:
            match = re.search(pattern_triple_tick, msg['content'])
            if match:
                file_name = match.group(1)
                break

    if not file_name:
        pattern_file_line = r"([\w\d_/\\.-]+\.java)"
        for msg in messages:
            if msg['role'] in ['assistant', 'system']:
                match = re.search(pattern_file_line, msg['content'])
                if match:
                    file_name = match.group(1)
                    break

    if not file_name:
        return (
            "Could not detect the Java file name in the conversation. "
            "If `MyClass.java` was just an example, please provide the actual file name in triple-backticks or text. "
            "Then use `@AIBot update` again."
        )

    github = Github(token_str)
    repo = github.get_repo(repo_full_name)
    default_branch = repo.default_branch

    try:
        file_contents = repo.get_contents(file_name, ref=default_branch)
    except Exception as e:
        return f"Failed to fetch '{file_name}' on branch '{default_branch}'. Error: {e}"

    current_code = base64.b64decode(file_contents.content).decode("utf-8")
    return f"Here is the current code in `{file_name}`:\n\n```java\n{current_code}\n```"

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
