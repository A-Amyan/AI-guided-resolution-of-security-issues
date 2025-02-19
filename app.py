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
# Logging: set to DEBUG for troubleshooting
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
    logging.error("GITHUB_PRIVATE_KEY not set.")
    sys.exit(1)
try:
    APP_ID = int(APP_ID)
except ValueError:
    logging.error("GITHUB_APP_ID must be an integer.")
    sys.exit(1)

###############################################################################
# GitHub App Auth & OpenAI initialization
###############################################################################
auth = Auth.AppAuth(app_id=APP_ID, private_key=PRIVATE_KEY)
git_integration = GithubIntegration(auth=auth)
openai.api_key = OPENAI_API_KEY
if not openai.api_key:
    logging.error("No OPENAI_API_KEY found. AI calls may fail.")

###############################################################################
# In-memory conversation store (keyed by (repo_full_name, issue_number))
###############################################################################
conversation_store = {}

def get_or_create_conversation(repo_full_name, issue_number, issue_body=None):
    """
    Retrieve or create a conversation for ChatCompletion.
    Optionally inject the issue body if first seen.
    """
    key = (repo_full_name, issue_number)
    if key not in conversation_store:
        system_message = (
            "You are an assistant specialized in Java security analysis, "
            "best practices, and general code discussions. Keep context from "
            "previous messages in this issue to maintain a coherent conversation."
            "Do not engage in discussions outside of security."
        )
        conversation_store[key] = [{"role": "system", "content": system_message}]
        if issue_body:
            conversation_store[key].append({
                "role": "assistant",
                "content": f"Issue Body:\n\n{issue_body}"
            })
    return conversation_store[key]

###############################################################################
# Helper: extract a file name from a text string.
###############################################################################
def extract_file_name(text):
    # First, try to find a file name inside triple backticks with "java"
    pattern = r"```java\s+([\w\d_/\\.-]+\.java)\s*```"
    match = re.search(pattern, text)
    if match:
        return match.group(1)
    # Otherwise, try to extract any substring ending in ".java"
    pattern2 = r"([\w\d_/\\.-]+\.java)"
    match = re.search(pattern2, text)
    if match:
        return match.group(1)
    return None

###############################################################################
# Helper: Post a comment on a PR (pull request comment)
###############################################################################
def post_pr_comment(github_instance, repo_full_name, pr_number, comment_body):
    try:
        repo = github_instance.get_repo(repo_full_name)
        pr = repo.get_pull(pr_number)
        pr.create_issue_comment(comment_body)
        logging.debug(f"Posted comment to PR #{pr_number} in {repo_full_name}.")
    except Exception as e:
        logging.error(f"Failed to post PR comment: {e}")

###############################################################################
# Helper: Post a comment on an Issue (non-PR)
###############################################################################
def post_issue_comment(github_instance, repo_full_name, issue_number, comment_body):
    try:
        repo = github_instance.get_repo(repo_full_name)
        issue = repo.get_issue(issue_number)
        issue.create_comment(comment_body)
        logging.debug(f"Posted comment to Issue #{issue_number} in {repo_full_name}.")
    except Exception as e:
        logging.error(f"Failed to post Issue comment: {e}")

###############################################################################
# Flask App
###############################################################################
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    event = request.headers.get('X-GitHub-Event')
    payload = request.json
    logging.debug(f"Received event: {event}")
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
# PUSH Logic: Create PR on branch pushes
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
    if not ref.startswith("refs/heads/"):
        return jsonify({'status': 'ignored - not a branch push'}), 200
    branch_name = ref.replace("refs/heads/", "", 1)
    repo = github.get_repo(full_name)
    try:
        default_branch = repo.default_branch
    except Exception:
        logging.error("Could not identify default branch.")
        default_branch = "main"
    if branch_name == default_branch:
        return jsonify({'status': 'ignored - push on default branch'}), 200

    # Collect all changed Java files
    commits = payload.get('commits', [])
    java_files = set()
    for commit in commits:
        for f in commit.get('added', []) + commit.get('modified', []):
            if f.endswith('.java'):
                java_files.add(f)
    
    if not java_files:
        java_files = {"No Java file changed."}

    commit_sha = payload.get('after')
    short_desc = generate_pr_description_for_multiple_files(
        branch_name=branch_name,
        pusher_name=pusher_name,
        java_file_names=list(java_files),
        repo_full_name=full_name,
        token_str=token_str,
        commit_ref=commit_sha
    )
    pr = create_pull_request_for_push(repo, branch_name, default_branch, short_desc)
    if not pr:
        return jsonify({'status': 'failed to create PR'}), 500
    return jsonify({'status': 'success', 'pr_url': pr.html_url}), 200


def generate_pr_description_with_ai(branch_name, pusher_name, java_file_name, repo_full_name, token_str, commit_ref=None):
    if not openai.api_key:
        return (f"Automated PR from '{branch_name}' by {pusher_name}.\n\n"
                f"Impacted file (no AI summary):\n```java\n{java_file_name}\n```")
    github = Github(token_str)
    repo = github.get_repo(repo_full_name)
    ref_to_fetch = commit_ref if commit_ref else branch_name
    try:
        file_obj = repo.get_contents(java_file_name, ref=ref_to_fetch)
        file_content = base64.b64decode(file_obj.content).decode("utf-8")
    except Exception as e:
        return (f"Automated PR from '{branch_name}' by {pusher_name}.\n\n"
                f"Could not fetch the file `{java_file_name}` for AI summary.\nError: {e}")
    MAX_CHARS = 1000
    snippet = file_content[:MAX_CHARS]
    if len(file_content) > MAX_CHARS:
        snippet += "\n... [Truncated for prompt brevity] ..."
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
        return (f"Automated PR from '{branch_name}' by {pusher_name}.\n\n"
                f"Could not get AI-based summary. Error: {e}\n\n"
                f"File:\n```java\n{java_file_name}\n```")
    admin_username = repo.owner.login
    return (f"@{admin_username}\n\n"
            f"{gpt_text}\n\n"
            f"**Branch**: `{branch_name}`\n"
            f"**Pusher**: `{pusher_name}`\n"
            "**Impacted file**:\n"
            f"```java\n{java_file_name}\n```")

def generate_pr_description_for_multiple_files(branch_name, pusher_name, java_file_names, repo_full_name, token_str, commit_ref=None):
    if not openai.api_key:
        return (f"Automated PR from '{branch_name}' by {pusher_name}.\n\n"
                f"Impacted files (no AI summary):\n" + "\n".join(f"```java\n{f}\n```" for f in java_file_names))
    
    github = Github(token_str)
    repo = github.get_repo(repo_full_name)
    ref_to_fetch = commit_ref if commit_ref else branch_name
    file_details = []
    
    for java_file in java_file_names:
        try:
            file_obj = repo.get_contents(java_file, ref=ref_to_fetch)
            file_content = base64.b64decode(file_obj.content).decode("utf-8")
        except Exception as e:
            file_details.append(f"Could not fetch file `{java_file}`: {e}")
            continue
        MAX_CHARS = 1000
        snippet = file_content[:MAX_CHARS]
        if len(file_content) > MAX_CHARS:
            snippet += "\n... [Truncated for prompt brevity] ..."
        file_details.append(f"File: {java_file}\nSnippet:\n```java\n{snippet}\n```")
    
    combined_details = "\n\n".join(file_details)
    system_prompt = (
        "You are an assistant who writes short PR descriptions focused on Java security improvements. "
        "Given the branch name, the pusher's name, and details for multiple Java files, provide a concise summary "
        "that emphasizes any security-related changes or vulnerabilities addressed. "
        "Mention each file name in triple backticks where appropriate."
    )
    user_prompt = (
        f"Branch Name: {branch_name}\n"
        f"Pusher: {pusher_name}\n"
        f"Files Changed:\n{combined_details}\n\n"
        "Please write a short PR description that summarizes these changes with an emphasis on security improvements."
    )
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
        return (f"Automated PR from '{branch_name}' by {pusher_name}.\n\n"
                f"Could not get AI-based summary. Error: {e}\n\n"
                f"Files:\n" + "\n".join(f"```java\n{f}\n```" for f in java_file_names))
    admin_username = repo.owner.login
    return (f"@{admin_username}\n\n"
            f"{gpt_text}\n\n"
            f"**Branch**: `{branch_name}`\n"
            f"**Pusher**: `{pusher_name}`\n"
            f"**Impacted files**: " + ", ".join(f"`{f}`" for f in java_file_names))


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
# Pull Request Logic: Post comment on PR events.
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
    github_instance = Github(token_str)
    repo = github_instance.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)
    admin_username = repo.owner.login

    # Loop over each changed Java file in the PR
    for pr_file in pr.get_files():
        if pr_file.filename.endswith('.java') and pr_file.status in ['added', 'modified']:
            content = fetch_file_content(repo, pr_file.filename, pr.head.sha, token_str)
            if content:
                analysis_result = analyze_code_no_issue(content, token_str, repo_full_name)
                comment = (
                    f"@{admin_username} **Security Analysis for `{pr_file.filename}`**\n\n"
                    f"{analysis_result}"
                )
                post_pr_comment(github_instance, repo_full_name, pr_number, comment)
    return jsonify({'status': 'success'}), 200


def analyze_pr_no_issue(repo_full_name, pr_number, token_str):
    """
    Analyzes Java files from a pull request for potential vulnerabilities.
    Uses structural splitting to divide code into logical sections before analysis.
    Then, it calls GPT‑4 to merge the vulnerability findings from each section.
    """
    github = Github(token_str)
    repo = github.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)
    admin_username = repo.owner.login
    java_files = []

    # Retrieve the contents of changed .java files
    for pr_file in pr.get_files():
        if pr_file.filename.endswith('.java') and pr_file.status in ['added', 'modified']:
            content = fetch_file_content(repo, pr_file.filename, pr.head.sha, token_str)
            if content:
                # Prepend a header with the file name to preserve context
                snippet = f"--- {pr_file.filename} ---\n{content}"
                java_files.append(snippet)
    if not java_files:
        return "No Java code found in this pull request."

    # Combine all Java file snippets into one string.
    combined_code = "\n\n".join(java_files)
    # Use structural splitting to divide the code into logical sections.
    sections = structural_split_java_code(combined_code)

    system_prompt = (
        "As a security expert in cryptography, follow the steps below to avoid any verbosity, and provide your analysis just in this enhanced JSON format: {File Name, JCA API or Class, Misuses across All Code Paths, Misuses in Executed Path, Secure Alternative if not secure, Executed from Main Method based on conditions?} add the location, description, severity (High with a CVSS score above 8.0 or those that allow remote code execution. Medium with a CVSS score between 5.0 and 8.0 or that requires local access for exploitation. Low with a CVSS score below 5.0.), and correction for each value in the JSON file:"
        "1-JCA API Usages: Note all uses of Java Cryptography Architecture (JCA) APIs and classes, with attention to any conditions or variable assignments that affect the API choice."
        "2- Provide a list of misuses for these APIs."
        "3-Comprehensive Code Path Review: Identify potential misuses across all branches and paths, even if not executed in the specific scenario, to provide a full security review of the code structure."
        "4-Execution Path Focus: Highlight issues observed specifically in the path executed given the initial values and conditions, ensuring that actual runtime security risks are prioritized."
        "5-Runtime Accessibility: Confirm if the JCA API usage is accessible and executed from the main method based on the given initial conditions."
        "Return a JSON array with keys: name, location, description, JCA API or Class, Misuses across All Code Paths, Misuses in Executed Path, severity (High with a CVSS score above 8.0 or those that allow remote code execution. Medium with a CVSS score between 5.0 and 8.0 or that requires local access for exploitation. Low with a CVSS score below 5.0.), Secure Alternative if not secure. If none, return an empty array plus a summary."
    )

    all_misuses = []
    # Analyze each section separately.
    for idx, section in enumerate(sections):
        user_prompt = (
            f"Analyze the following Java code section ({idx + 1} of {len(sections)}):\n\n{section}\n\n"
            "Return only a JSON array."
        )
        try:
            resp = openai.ChatCompletion.create(
                model="gpt-4",
                temperature=0,
                max_tokens=1200,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            )
            ai_text = resp.choices[0].message.content.strip()
        except Exception as e:
            logging.error(f"OpenAI call failed on section {idx + 1}: {e}")
            continue
        misuses = parse_ai_output(ai_text)
        all_misuses.extend(misuses)

    # Post-processing: merge the per-section results.
    merged_misuses = merge_vulnerability_findings(all_misuses)
    if not merged_misuses:
        return f"@{admin_username} **Potential Security Misuses**\n\nNo vulnerabilities detected in analyzed code sections."

    combined_json = json.dumps(merged_misuses, indent=2)
    return (
        f"@{admin_username} **Potential Security Misuses**\n\n"
        f"**Aggregated AI Output**:\n```json\n{combined_json}\n```"
    )

def analyze_code_no_issue(java_code, token_str, repo_full_name):
    """
    Analyzes a single Java code snippet for vulnerabilities.
    Uses structural splitting to divide the code into logical sections, then analyzes each,
    and finally merges the results using GPT‑4.
    """
    github = Github(token_str)
    repo = github.get_repo(repo_full_name)
    admin_username = repo.owner.login

    # Split the code structurally into sections.
    sections = structural_split_java_code(java_code)

    system_prompt = (
        "As a security expert in cryptography, follow the steps below to avoid any verbosity, and provide your analysis just in this enhanced JSON format: {File Name, JCA API or Class, Misuses across All Code Paths, Misuses in Executed Path, Secure Alternative if not secure, Executed from Main Method based on conditions?} add the location, description, severity (High with a CVSS score above 8.0 or those that allow remote code execution. Medium with a CVSS score between 5.0 and 8.0 or that requires local access for exploitation. Low with a CVSS score below 5.0.), and correction for each value in the JSON file:"
        "1-JCA API Usages: Note all uses of Java Cryptography Architecture (JCA) APIs and classes, with attention to any conditions or variable assignments that affect the API choice."
        "2- Provide a list of misuses for these APIs."
        "3-Comprehensive Code Path Review: Identify potential misuses across all branches and paths, even if not executed in the specific scenario, to provide a full security review of the code structure."
        "4-Execution Path Focus: Highlight issues observed specifically in the path executed given the initial values and conditions, ensuring that actual runtime security risks are prioritized."
        "5-Runtime Accessibility: Confirm if the JCA API usage is accessible and executed from the main method based on the given initial conditions."
        "Return a JSON array with keys: name, location, description, JCA API or Class, Misuses across All Code Paths, Misuses in Executed Path, severity (High with a CVSS score above 8.0 or those that allow remote code execution. Medium with a CVSS score between 5.0 and 8.0 or that requires local access for exploitation. Low with a CVSS score below 5.0.), Secure Alternative if not secure. If none, return an empty array plus a summary."
    )

    all_misuses = []
    for idx, section in enumerate(sections):
        user_prompt = (
            f"Analyze the following Java code section ({idx + 1} of {len(sections)}):\n\n{section}\n\n"
            "Return only a JSON array."
        )
        try:
            resp = openai.ChatCompletion.create(
                model="gpt-4",
                temperature=0,
                max_tokens=1200,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            )
            ai_text = resp.choices[0].message.content.strip()
        except Exception as e:
            logging.error(f"OpenAI call failed on section {idx + 1}: {e}")
            continue
        misuses = parse_ai_output(ai_text)
        all_misuses.extend(misuses)

    # Post-processing: merge findings from all sections.
    merged_misuses = merge_vulnerability_findings(all_misuses)
    if not merged_misuses:
        return f"@{admin_username} **Potential Security Misuses**\n\nNo vulnerabilities detected in the analyzed code."

    combined_json = json.dumps(merged_misuses, indent=2)
    return (
        f"@{admin_username} **Potential Security Misuses**\n\n"
        f"**Aggregated AI Output**:\n```json\n{combined_json}\n```"
    )

def fetch_file_content(repo, filename, ref, token_str):
    """
    Helper function to fetch and decode the content of a file from GitHub.
    """
    import base64
    import requests
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
    except Exception as e:
        logging.error(f"Failed to fetch file '{filename}': {e}")
        return None
    
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
    
    # Retrieve all open issues once.
    open_issues = list(repo.get_issues(state='open'))
    
    created_count = 0
    for file_path in java_files:
        # Check if an open issue already mentions this file.
        if any(file_path in (issue.title or "") or file_path in (issue.body or "") for issue in open_issues):
            continue  # Skip analysis for this file.
        
        raw_code = fetch_file_content(repo, file_path, default_branch, token_str)
        if not raw_code:
            continue
        analysis = analyze_code_no_issue(raw_code, token_str, repo_full_name)
        title = f"Security Analysis for {file_path}"
        body = f"**File:** `{file_path}`\n\n**Analysis:**\n\n{analysis}\n"
        try:
            repo.create_issue(title=title, body=body)
            created_count += 1
        except Exception:
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
    except Exception:
        return None

def parse_ai_output(ai_text):
    try:
        data = json.loads(ai_text)
        if isinstance(data, dict):
            data = [data]
        elif not isinstance(data, list):
            data = []
        valid = []
        required_keys = ["name", "location", "description", "JCA API or Class", "Misuses across All Code Paths", "Misuses in Executed Path", "severity" , "Secure Alternative if not secure"]
        for item in data:
            if all(k in item for k in required_keys):
                valid.append(item)
        return valid
    except Exception:
        return []

def structural_split_java_code(java_code):
    """
    Splits the Java code into logical sections based on class, interface, or enum definitions.
    If no such structure is found, attempts to split based on method definitions.
    Returns a list of code sections.
    """
    # First, try to split by class/interface/enum declarations.
    pattern = r"(?=^\s*(public\s+)?(class|interface|enum)\s+\w+)"
    sections = re.split(pattern, java_code, flags=re.MULTILINE)
    if len(sections) > 1:
        result = []
        # If there's an initial preamble (e.g. package/import statements), add it as its own section.
        if sections[0].strip():
            result.append(sections[0])
        # Combine each marker (captured group) with its following code.
        for i in range(1, len(sections), 2):
            if i + 1 < len(sections):
                result.append((sections[i] or '') + (sections[i + 1] or ''))
            else:
                result.append(sections[i] or '')
        if result:
            return result

    # Fallback: try splitting by method definitions (public, protected, private, static)
    pattern_method = r"(?=^\s*(public|protected|private|static)\s+[\w\<\>\[\]]+\s+\w+\s*\(.*?\)\s*\{)"
    sections = re.split(pattern_method, java_code, flags=re.MULTILINE | re.DOTALL)
    if len(sections) > 1:
        result = []
        if sections[0].strip():
            result.append(sections[0])
        for i in range(1, len(sections), 2):
            if i + 1 < len(sections):
                result.append((sections[i] or '') + (sections[i + 1] or ''))
            else:
                result.append(sections[i] or '')
        return result

    # If no splitting is possible, return the entire code as one section.
    return [java_code]


def merge_vulnerability_findings(misuses):
    """
    Merges vulnerability analysis results from multiple sections.
    Calls GPT‑4 to deduplicate and summarize findings that may span multiple sections.
    Returns a merged JSON array of vulnerability objects.
    """
    merged_prompt = (
        "You are a security analysis assistant. The following JSON array contains vulnerability analysis results "
        "from multiple sections of Java code. Some vulnerabilities might be duplicated or refer to the same issue. "
        "Please merge these results, removing duplicates and summarizing any issues that span multiple sections. "
        "Return only a JSON array of objects with the keys: name, location, description, severity, correction."
    )
    input_json = json.dumps(misuses, indent=2)
    user_prompt = f"Merge the following vulnerability findings:\n\n{input_json}\n\nReturn only a JSON array."
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            temperature=0,
            max_tokens=2200,
            messages=[
                {"role": "system", "content": merged_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        merged_text = resp.choices[0].message.content.strip()
        merged_misuses = parse_ai_output(merged_text)
        if merged_misuses:
            return merged_misuses
        else:
            return misuses
    except Exception as e:
        logging.error(f"OpenAI merge call failed: {e}")
        return misuses
    
def post_pr_comment(github, repo_full_name, pr_number, comment_body):
    try:
        repo = github.get_repo(repo_full_name)
        pr = repo.get_pull(pr_number)
        pr.create_issue_comment(comment_body)
    except Exception:
        pass

###############################################################################
# New Function: Post a comment on an Issue (non-PR)
###############################################################################
def post_issue_comment(github, repo_full_name, issue_number, comment_body):
    try:
        repo = github.get_repo(repo_full_name)
        issue = repo.get_issue(issue_number)
        issue.create_comment(comment_body)
    except Exception:
        pass

###############################################################################
# New Function: Merge code based on admin correction (for Issue comments)
###############################################################################
def attempt_merge_corrected_code_issue(repo_full_name, issue_number, token_str, comment_body, issue_body):
    """
    For an issue comment command:
      1. Extract the correction instructions from the admin's comment (plain text, not a code snippet).
      2. Get the file name from the issue body (or PR description if needed).
      3. Retrieve the original file content from the repository.
      4. Use AI (via analyze_code_no_issue) to analyze the file.
      5. Then use AI to apply the correction instructions to the original code.
      6. Update the repository with the new corrected code.
      7. Return a short message confirming that the code was updated.
         (Do not post the new code in the comment.)
    """
    # Step 1: Extract correction instructions by removing the command text.
    correction_instructions = comment_body.replace("@AIBot merge code", "").strip()
    if not correction_instructions:
        return ("No correction instructions found in your comment. "
                "Please include the correction details after '@AIBot merge code'.")
    # Step 2: Get file name from the issue body; if not found and it's a PR, try the PR description.
    file_name = extract_file_name(issue_body)
    if not file_name:
        # Optionally, one could check the conversation here as well.
        return ("No file name found in the issue body. "
                "Please include the file name in triple backticks (e.g., ```java\nMyClass.java\n```).")
    github_instance = Github(token_str)
    repo_instance = github_instance.get_repo(repo_full_name)
    default_branch = repo_instance.default_branch
    try:
        file_obj = repo_instance.get_contents(file_name, ref=default_branch)
        original_code = base64.b64decode(file_obj.content).decode("utf-8")
    except Exception as e:
        return f"Failed to fetch file '{file_name}' from branch '{default_branch}'. Error: {e}"
    # (Step 3 was getting the file content above.)
    # Step 4: Analyze the original code.
    analysis_result = analyze_code_no_issue(original_code, token_str, repo_full_name)
    # (You might log or include the analysis result if needed.)
    # Step 5: Create a prompt for AI to apply the correction instructions.
    system_prompt = (
        "You are a code merging assistant. The admin has provided correction instructions. "
        "Apply these corrections to the original Java code while making only minimal changes."
    )
    user_prompt = (
        f"Original Code:\n```java\n{original_code}\n```\n\n"
        f"Correction Instructions:\n{correction_instructions}\n\n"
        "Please output the updated Java code as plain text."
    )
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            temperature=0,
            max_tokens=3000,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        )
        new_code = resp.choices[0].message.content.strip()
    except Exception as e:
        return f"OpenAI call failed: {e}"
    # Step 6: Update the repository file with the new code.
    commit_message = f"[AIBot] Merge correction in {file_name} from Issue #{issue_number}"
    try:
        repo_instance.update_file(
            path=file_name,
            message=commit_message,
            content=new_code,
            sha=file_obj.sha,
            branch=default_branch
        )
    except Exception as e:
        return f"Failed to commit updated code: {e}"
    # Step 7: Return a short confirmation message.
    return f"Code in `{file_name}` updated successfully on branch `{default_branch}`."

###############################################################################
# Issue Comment Handler
###############################################################################
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
    if user_login.endswith('[bot]'):
        return jsonify({'status': 'bot comment ignored'}), 200
    admin_username = repo_info.get('owner', {}).get('login')
    if not admin_username:
        admin_username = "unknown-admin"
    admin_commands = [
        "@AIBot analyze repo",
        "@AIBot update code",
        "@AIBot update",
        "@AIBot merge code",
        "@AIBot analyze code",
    ]
    if any(cmd in comment_body for cmd in admin_commands) and (user_login != admin_username):
        response = f"Sorry, only the repository owner (@{admin_username}) can use that command."
        if 'pull_request' in issue_data:
            post_pr_comment(github, repo_full_name, issue_number, response)
        else:
            post_issue_comment(github, repo_full_name, issue_number, response)
        return jsonify({'status': 'forbidden'}), 403
    conversation = get_or_create_conversation(repo_full_name, issue_number, issue_body=issue_body)
    conversation.append({"role": "user", "content": comment_body})
    # --- Admin-only commands ---
    if "@AIBot analyze repo" in comment_body:
        result_msg = analyze_repo_and_open_issues(github, repo_full_name, token_str)
        conversation.append({"role": "assistant", "content": result_msg})
        if 'pull_request' in issue_data:
            post_pr_comment(github, repo_full_name, issue_number, result_msg)
        else:
            post_issue_comment(github, repo_full_name, issue_number, result_msg)
        return jsonify({'status': 'success'}), 200
    if "@AIBot update code" in comment_body:
        update_msg = attempt_update_pr_code(repo_full_name, issue_number, token_str, conversation)
        conversation.append({"role": "assistant", "content": update_msg})
        if 'pull_request' in issue_data:
            post_pr_comment(github, repo_full_name, issue_number, update_msg)
        else:
            post_issue_comment(github, repo_full_name, issue_number, update_msg)
        return jsonify({'status': 'success'}), 200
    if "@AIBot update" in comment_body:
        fetched_code_msg = attempt_fetch_current_code(repo_full_name, issue_number, token_str)
        conversation.append({"role": "assistant", "content": fetched_code_msg})
        if 'pull_request' in issue_data:
            post_pr_comment(github, repo_full_name, issue_number, fetched_code_msg)
        else:
            post_issue_comment(github, repo_full_name, issue_number, fetched_code_msg)
        return jsonify({'status': 'success'}), 200
    if "@AIBot merge code" in comment_body:
        # Use the new behavior if it's an issue comment; otherwise use the existing behavior.
        if 'pull_request' in issue_data:
            merged_msg = attempt_merge_corrected_code(repo_full_name, issue_number, token_str)
        else:
            merged_msg = attempt_merge_corrected_code_issue(repo_full_name, issue_number, token_str, comment_body, issue_body)
        conversation.append({"role": "assistant", "content": merged_msg})
        if 'pull_request' in issue_data:
            post_pr_comment(github, repo_full_name, issue_number, merged_msg)
        else:
            post_issue_comment(github, repo_full_name, issue_number, merged_msg)
        return jsonify({'status': 'success'}), 200
    if "@AIBot analyze file" in comment_body:
        # New admin order for "@AIBot analyze code":
        # 1. Get the file name from the issue body or from the PR description.
        file_name = extract_file_name(issue_body)
        if not file_name and 'pull_request' in issue_data:
            # if this is a PR comment, try to get it from the PR description (if available)
            pr_description = issue_data.get('pull_request', {}).get('body', '')
            file_name = extract_file_name(pr_description)
        if not file_name:
            # As a fallback, check conversation history.
            combined_conv = "\n".join([msg['content'] for msg in conversation])
            file_name = extract_file_name(combined_conv)
        # 2. If a file name was found, retrieve its content.
        if file_name:
            github_instance = Github(token_str)
            repo_instance = github_instance.get_repo(repo_full_name)
            default_branch = repo_instance.default_branch
            try:
                file_obj = repo_instance.get_contents(file_name, ref=default_branch)
                file_code = base64.b64decode(file_obj.content).decode("utf-8")
            except Exception as e:
                analyze_result = f"Failed to fetch file '{file_name}' from branch '{default_branch}'. Error: {e}"
                logging.error(analyze_result)
            else:
                # 3. Use the existing analyze_code_no_issue function to analyze the file.
                analyze_result = analyze_code_no_issue(file_code, token_str, repo_full_name)
        else:
            analyze_result = ("No file name found in the issue body or PR description. "
                              "Please provide the file name in triple backticks (e.g., ```java\nMyClass.java\n```).")
        # 4. Post the analysis result.
        conversation.append({"role": "assistant", "content": analyze_result})
        if 'pull_request' in issue_data:
            post_pr_comment(github, repo_full_name, issue_number, analyze_result)
        else:
            post_issue_comment(github, repo_full_name, issue_number, analyze_result)
        return jsonify({'status': 'success'}), 200
    # Otherwise, use general chat response.
    ai_reply = chat_with_history(conversation)
    conversation.append({"role": "assistant", "content": ai_reply})
    if 'pull_request' in issue_data:
        post_pr_comment(github, repo_full_name, issue_number, ai_reply)
    else:
        post_issue_comment(github, repo_full_name, issue_number, ai_reply)
    return jsonify({'status': 'success'}), 200

def chat_with_history(messages):
    if not openai.api_key:
        return "No AI is configured."
    ephemeral_instruction = {
        "role": "system",
        "content": (
            "Remember: this conversation is strictly about security analysis and vulnerability remediation."
            "If a topic falls outside of Java security, respond by stating that only security-related discussion is supported."
            "Additionally, if you propose any code changes or corrected lines,"
            "always provide them in triple-backtick format. For example:\n\n"
            "```java\nCode Snippet Block\n```\n\n"
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
    except Exception:
        return "Failed to respond with AI."

def attempt_update_pr_code(repo_full_name, pr_number, token_str, conversation):
    github = Github(token_str)
    repo = github.get_repo(repo_full_name)
    try:
        pr = repo.get_pull(pr_number)
    except Exception as e:
        return f"Could not get PR #{pr_number}: {e}"
    pr_branch = pr.head.ref
    file_to_update = None
    for f in pr.get_files():
        if f.filename.endswith('.java'):
            file_to_update = f.filename
            break
    if not file_to_update:
        return "No .java files found in this PR to update."
    try:
        contents = repo.get_contents(file_to_update, ref=pr_branch)
    except Exception as e:
        return f"Failed to fetch '{file_to_update}' from branch '{pr_branch}'. Error: {e}"
    original_code = base64.b64decode(contents.content).decode("utf-8")
    instructions = ""
    for msg in conversation:
        if msg['role'] == 'user':
            instructions += f"User said:\n{msg['content']}\n\n"
    if pr.body:
        instructions += f"PR Description:\n{pr.body}\n\n"
    system_prompt = (
        "You are a Java code refactoring assistant. The user has provided instructions "
        "for how to fix or update this Java file. You have the original code below.\n\n"
        "Apply only the minimal changes needed to address the user's instructions.\n\n"
        "Return the entire updated file content as plain text."
    )
    user_prompt = (
        f"User instructions / discussion:\n\n{instructions}\n"
        "Original file content:\n"
        "```java\n"
        f"{original_code}\n"
        "```\n\n"
        "Please return the updated file with minimal changes."
    )
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
            ]
        )
        updated_code = resp.choices[0].message.content.strip()
    except Exception as e:
        return f"OpenAI call failed: {e}"
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
    return f"Successfully updated the file `{file_to_update}` on branch `{pr_branch}`.\n\n```java\n{updated_code}\n```"

def attempt_merge_corrected_code(repo_full_name, issue_number, token_str):
    """
    Existing behavior for merging code (used when the command is posted in a PR comment).
    """
    messages = conversation_store.get((repo_full_name, issue_number), [])
    if not messages:
        return "No conversation found; cannot merge code."
    file_name = find_file_name_in_conversation(messages)
    if not file_name:
        return ("I couldn't find a file reference in triple backticks or text. "
                "Please provide the correct file name in triple backticks (e.g., ```java\nActualFile.java\n```), "
                "then the code snippet, and type `@AIBot merge code`.")
    code_snippet = find_last_code_snippet(messages)
    if not code_snippet:
        return ("I couldn't detect a code snippet to merge. "
                "Please provide it in triple-backtick format and then type `@AIBot merge code`.")
    github = Github(token_str)
    repo = github.get_repo(repo_full_name)
    default_branch = repo.default_branch
    try:
        file_contents = repo.get_contents(file_name, ref=default_branch)
    except Exception as e:
        return f"Failed to fetch '{file_name}' on branch '{default_branch}'. Error: {e}"
    commit_msg = f"Update {file_name} from Issue #{issue_number}"
    try:
        repo.update_file(
            path=file_name,
            message=commit_msg,
            content=code_snippet,
            sha=file_contents.sha,
            branch=default_branch
        )
        return f"Code snippet successfully merged into `{file_name}` on branch `{default_branch}`."
    except Exception as e:
        return f"Failed to merge code snippet into `{file_name}`: {e}"

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
        return ("Could not detect the Java file name in the conversation. "
                "Please provide the actual file name in triple backticks (e.g., ```java\nMyClass.java\n```), "
                "then use `@AIBot update` again.")
    github = Github(token_str)
    repo = github.get_repo(repo_full_name)
    default_branch = repo.default_branch
    try:
        file_contents = repo.get_contents(file_name, ref=default_branch)
    except Exception as e:
        return f"Failed to fetch '{file_name}' on branch '{default_branch}'. Error: {e}"
    current_code = base64.b64decode(file_contents.content).decode("utf-8")
    return f"Here is the current code in `{file_name}`:\n\n```java\n{current_code}\n```"

def find_file_name_in_conversation(messages):
    pattern = r"```java\s+([\w\d_/\\.-]+\.java)\s*```"
    for msg in messages:
        if msg['role'] in ['assistant', 'system', 'user']:
            match = re.search(pattern, msg['content'])
            if match:
                return match.group(1)
    return None

def find_last_code_snippet(messages):
    pattern = r"```(?:java)?\s*(.*?)```"
    for msg in reversed(messages):
        if msg['role'] in ['assistant', 'user']:
            blocks = re.findall(pattern, msg['content'], flags=re.DOTALL)
            if blocks:
                return blocks[-1].strip()
    return None

###############################################################################
# Flask App Entry Point
###############################################################################
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
