import os
import sys
import re
import json
import time
import base64
import logging

from flask import Flask, request, jsonify
from dotenv import load_dotenv

import openai

# GitHub imports
from github import GithubIntegration, Github, Auth

###############################################################################
# Logging: set to DEBUG for troubleshooting
###############################################################################
logging.basicConfig(level=logging.DEBUG) # ERROR, DEBUG

###############################################################################
# Load environment variables
###############################################################################
load_dotenv()
APP_ID = os.getenv('GITHUB_APP_ID')
PRIVATE_KEY = os.getenv('GITHUB_PRIVATE_KEY')
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
BOT_ADMIN = os.getenv("BOT_ADMIN")
BOT_FALLBACK_PAT = os.getenv("BOT_FALLBACK_PAT")

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
if not BOT_ADMIN:
    logging.error("BOT_ADMIN not set. Please set the GitHub username of the bot admin.")
if not BOT_FALLBACK_PAT:
    logging.error("BOT_GITHUB_TOKEN not set. Please set a GitHub PAT with repo write permissions.")

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

def analyze_pr_no_issue(repo_full_name, pr_number, token_str):
    """
    Analyzes Java files from a pull request for potential vulnerabilities.
    The contents of all changed .java files are combined into one analysis.
    """
    github = Github(token_str)
    repo = github.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)
    admin_username = repo.owner.login
    java_files = []

    # Retrieve the contents of changed .java files
    for pr_file in pr.get_files():
        if pr_file.filename.endswith('.java') and pr_file.status in ['added', 'modified']:
            content = fetch_file_content(repo, pr_file.filename, pr.head.sha)
            if content:
                snippet = f"--- {pr_file.filename} ---\n{content}"
                java_files.append(snippet)
    if not java_files:
        return "No Java code found in this pull request."

    # Combine all Java file snippets into one string.
    combined_code = "\n\n".join(java_files)
    
    system_prompt = (
            "Analyze the provided Java source code and detect all Direct and Indirect (dynamically) occurrences of cryptographic initialization and parameter handling."
            "In particular, please identify and list the following:"   
                "1. Direct Calls:"
                    "1. Cipher.getInstance(...)."
                    "2. New SecretKeySpec(...)."
                    "3. New PBEKeySpec(...) and new PBEParameterSpec(...)."
                    "4. KeyGenerator.getInstance(...) and SecretKeyFactory.getInstance(...)."
                    "5. New IvParameterSpec(...)."
                    "6. KeyStore.getInstance(...)."
                "2. Indirect Cryptographic Instantiations: Detect any method calls or dynamic mechanisms (for example, helper methods)"
                    "that ultimately return an instance of a JCA object corresponding to the above patterns."
                    "This includes cases where the cryptographic object is not instantiated inline,"
                    "but is returned from a function call,"
                    "e.g., final IvParameterSpec iv = genIv(...);, where genIv(...) returns an IvParameterSpec."
            "Output Requirement: For each detected occurrence answer the 8 follwing points:"
                    "1- The type of cryptographic object."
                    "2- CallType: Indirect or Direct."
                    "3- The exact code snippet."
                    "4- Location: if found in one location (method name and line number if available), or in mulitple locations."
                    "5- Vulnerability type:"
                        "1. The type of the Vulnerability:"
                            	"a) Weak Symmetric Encryption Algorithms (e.g., DES, Blowfish, RC4, RC2, IDEA)."
                                "b) Weak Encryption Mode (ECB)."
                                "c) Insecure Asymmetric Ciphers (RSA with keysize<1024)."
                                "d) hardcoded or constant cryptographic keys in SecretKeySpec."
                                "e) Static Salt for key derivation in PBEKeySpec or PBEParameterSpec."
                                "f) Hardcoded or constant passwords in PBEKeySpec."
                                "g) iteration count < 1000 in PBEKeySpec or PBEParameterSpec."
                                "h) weak Random function for generating secret key or Predictableseed instead of SecureRandom."
                                "i) Hardcoded or Constant IV."
                                "j) Hardcoded or Constant Password in KeyStore."
                                "k) brokenhash functions (SHA1, MD2, MD5, ...)."
                                "l) constant seed in SecureRandom."
                        "2. If thers is not any Vulnerability, type is secure."""
                        "3. If the content of the JCA API method is missing, type is More Information is Needed"                       
                    "6- Severity: If Indirect calltype, print Undefined."
                        " If direct call type: High with a CVSS score above 8.0 or those that allow remote code execution. Medium with a CVSS score between 5.0 and 8.0 or that requires local access for exploitation. Low with a CVSS score between 5.0 and 1.0. Secure with CVSS score less than 1.0.\n"
                    "7- Correction: How to correct the Vulnerability if found, why it is secure, or why we need more information"
                    "8- JCA Execution: Answer the following questions"
                        "1. The call chain leading to JCA instantiations."
                        "2. Whether a default algorithm is stored in a class member and used later in the JCA instantiations."
                        "3. Whether conditions directly affect the source value passed to JCA instantiation. The source when the condition is true versus when it is false."
                        "4. The class with cryptographic logic and the driver class."

"""
4. Identify the classes directly involved in cryptographic logic,
including the primary class where cryptographic objects are instantiated
and any helper or utility classes that contribute to these instantiations,
and the driver class (Ignore any frameworks that simply execute the cryptographic routines) that executes the cryptographic routines
"""
    )
    user_prompt = (
        f"Analyze the following combined Java code from the PR:\n\n{combined_code}\n\n"
        "Return only a JSON array."
    )
    try:
        resp = openai.ChatCompletion.create(
            model="o3-mini",
            reasoning_effort="medium",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        ai_text = resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"OpenAI call failed during PR analysis: {e}")
        return f"AI analysis failed: {e}"
    misuses = parse_ai_output(ai_text)
    if not misuses:
        return f"@{admin_username} **Potential Security Misuses**\n\nNo vulnerabilities detected in analyzed code."
    combined_json = json.dumps(misuses, indent=2)
    return (
        f"@{admin_username} **Potential Security Misuses**\n\n"
        f"**Aggregated AI Output**:\n```json\n{combined_json}\n```"
    )

def analyze_code_no_issue(java_code, repo=None):
    # 1. Detect occurrences.
    occurrences = detect_jca_api_occurrences(java_code)
    # 2. Analyze vulnerabilities based only on the occurrences.
    vulnerability_results = analyze_jca_occurrences_vulnerability(occurrences, java_code)
    # 3. Analyze execution details using both the full Java code and occurrences.
    execution_results = analyze_jca_occurrences_execution(java_code, occurrences)
    
    # Merge each corresponding result into one dictionary.
    merged_results = []
    for occ, vul, exe in zip(occurrences, vulnerability_results, execution_results):
        merged_item = occ.copy()
        merged_item.update(vul)
        merged_item.update(exe)
        merged_results.append(merged_item)
    
    combined_json = json.dumps(merged_results, indent=2)
    admin_username = repo.owner.login if repo else "unknown-admin"
    return f"@{admin_username} **Aggregated Security Analysis**:\n```json\n{combined_json}\n```"

def fetch_file_content(repo, filename, ref=None):
    """
    Fetch and decode the content of a file from GitHub using PyGithub,
    without manually handling the auth token.
    """
    try:
        file_obj = repo.get_contents(filename, ref=ref)
        return base64.b64decode(file_obj.content).decode("utf-8")
    except Exception as e:
        logging.error(f"Failed to fetch '{filename}' from '{repo.full_name}' (ref={ref}): {e}")
        return None

def analyze_repo_and_open_issues(github, repo_full_name):
    repo = github.get_repo(repo_full_name)
    default_branch = repo.default_branch
    contents_queue = list(repo.get_contents("", ref=default_branch))
    java_files = []

    # 1. Collect Java files
    while contents_queue:
        item = contents_queue.pop()
        if item.type == 'dir':
            contents_queue.extend(repo.get_contents(item.path, ref=default_branch))
        elif item.type == 'file' and item.path.endswith('.java'):
            java_files.append(item.path)

    open_issues = list(repo.get_issues(state='open'))
    created_count = 0

    # 2. Analyze each Java file with a 60-second delay after processing each file.
    for file_path in java_files:
        # Skip if an open issue already references this file.
        if any(file_path in (issue.title or "") or file_path in (issue.body or "") for issue in open_issues):
            continue

        try:
            file_obj = repo.get_contents(file_path, ref=default_branch)
            raw_code = base64.b64decode(file_obj.content).decode('utf-8')
        except Exception as e:
            logging.error(f"Could not fetch content for {file_path}: {e}")
            continue

        # Analyze the file content.
        analysis = analyze_code_no_issue(raw_code, repo)

        # 3. Create an issue with the analysis.
        title = f"Security Analysis for {file_path}"
        body = f"**File:** `{file_path}`\n\n**Analysis:**\n\n{analysis}\n"
        try:
            repo.create_issue(title=title, body=body)
            created_count += 1
        except Exception as e:
            logging.error(f"Failed to create issue for {file_path}: {e}")

        # Delay 60 seconds after analyzing each file.
        time.sleep(60)

    return f"Analyzed {len(java_files)} .java files. Created {created_count} Issues."

def parse_ai_output(ai_text):
    """
    Parse the AI output from a JSON string.
    
    The function expects the JSON to be an array or a single object.
    It normalizes keys to lowercase to allow for case-insensitive matching against
    a set of required keys. Items missing some required keys are logged with a warning.
    
    Returns:
        A list of dictionaries (each representing an AI output item).
    """
    try:
        data = json.loads(ai_text)
    except Exception as e:
        logging.error(f"Error parsing JSON: {e}")
        return []

    # Normalize: if it's a dict, wrap it in a list; otherwise expect a list.
    if isinstance(data, dict):
        data = [data]
    elif not isinstance(data, list):
        logging.warning("Unexpected JSON structure; expected list or dict.")
        return []

    # Define required keys (in lowercase)
    required_keys = [
        "objectType",
        "codeSnippet",
        "vulnerability",
        "correction",
        "jca execution"
    ]
    
    valid_items = []
    for item in data:
        if not isinstance(item, dict):
            continue
        # Create a mapping with lowercase keys for comparison
        lower_item = {k.lower(): v for k, v in item.items()}
        # Determine missing keys
        missing_keys = [req for req in required_keys if req not in lower_item]
        if missing_keys:
            logging.warning(f"Item missing keys {missing_keys}: {item}")
        # Append the original item regardless (or modify as needed)
        valid_items.append(item)
    return valid_items

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
def attempt_merge_corrected_code_issue(github_instance, repo_full_name, issue_number, comment_body, issue_body):
    """
    1. Extract correction instructions from the admin's comment.
    2. Get the file name from the issue body.
    3. Retrieve the original file content from the repository.
    4. Use AI to apply the correction instructions.
    5. Update the repository with the new corrected code.
    6. Return a short message confirming that the code was updated.
    """
    # 1. Extract correction instructions
    correction_instructions = comment_body.replace("@AI_Bot merge code", "").strip()
    if not correction_instructions:
        return ("No correction instructions found in your comment. "
                "Please include the correction details after '@AI_Bot merge code'.")

    # 2. Get file name from the issue body
    file_name = extract_file_name(issue_body)
    if not file_name:
        return ("No file name found in the issue body. "
                "Please include the file name in triple backticks (e.g., ```java\nMyClass.java\n```).")

    # 3. Retrieve the original file content
    repo_instance = github_instance.get_repo(repo_full_name)
    default_branch = repo_instance.default_branch
    try:
        file_obj = repo_instance.get_contents(file_name, ref=default_branch)
        original_code = base64.b64decode(file_obj.content).decode("utf-8")
    except Exception as e:
        return f"Failed to fetch file '{file_name}' from branch '{default_branch}'. Error: {e}"

    # 4. Apply the correction instructions (and optionally do your code analysis if needed).
    #    Below, we directly apply instructions using OpenAI:
    analysis_result = analyze_code_no_issue(original_code, repo_full_name)  # if you want a prior analysis
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

    # 5. Update the repository with the new corrected code.
    commit_message = f"[AI_Bot] Merge correction in {file_name} from Issue #{issue_number}"
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

    # 6. Return a short message
    return f"Code in `{file_name}` updated successfully on branch `{default_branch}`."

def extract_file_name_from_comments(conversation):
    """
    Scan the prior conversation messages for a `.java` file name.
    Returns the first one found, or None.
    """
    for msg in conversation:
        content = msg.get("content", "")
        fn = extract_file_name(content)
        if fn:
            return fn
    return None
###############################################################################
# Issue Comment Handler
###############################################################################
def handle_issue_comment(payload):
    action = payload.get('action')
    comment_data = payload.get('comment')
    issue_data = payload.get('issue')
    repo_info = payload.get('repository')
    # Allow payloads without an installation field
    installation = payload.get('installation')  # Could be None

    if not (comment_data and issue_data and repo_info):
        return jsonify({'status': 'missing fields'}), 400

    # If installation is missing, set installation_id to None.
    installation_id = installation.get('id') if installation and 'id' in installation else None

    repo_full_name = repo_info.get('full_name', '')
    try:
        # This function will fall back to using BOT_FALLBACK_PAT if installation_id is None.
        github = get_github_client_for_repo(repo_full_name, installation_id=installation_id)
    except Exception as e:
        logging.error(f"Failed to authenticate for {repo_full_name}: {e}")
        return jsonify({'status': 'authentication failed'}), 403

    issue_number = issue_data.get('number', 0)
    issue_body = issue_data.get('body', '')
    comment_body = comment_data.get('body', '')
    user_login = comment_data.get('user', {}).get('login', 'user')
    
    # only proceed if @AI_Bot is mentioned
    if "@AI_Bot" not in comment_body:
        return jsonify({'status': 'ignored'}), 200
    # Ignore bot comments
    if user_login.endswith('[bot]'):
        return jsonify({'status': 'bot comment ignored'}), 200

    admin_username = repo_info.get('owner', {}).get('login', 'unknown-admin')
    # Define the list of admin commands
    admin_commands = [
        "@AI_Bot analyze repo",
        "@AI_Bot update code",
        "@AI_Bot update",
        "@AI_Bot merge code",
        "@AI_Bot trace",
        "@AI_Bot analyze file"
    ]
    # If the comment contains an admin command but the user isn't the repo owner, deny access.
    if any(cmd in comment_body for cmd in admin_commands) and (user_login != admin_username):
        response = f"Sorry, only the repository owner (@{admin_username}) can use that command."
        if 'pull_request' in issue_data:
            post_pr_comment(github, repo_full_name, issue_number, response)
        else:
            post_issue_comment(github, repo_full_name, issue_number, response)
        return jsonify({'status': 'forbidden'}), 403

    # Retrieve or create conversation context
    conversation = get_or_create_conversation(repo_full_name, issue_number, issue_body=issue_body)
    conversation.append({"role": "user", "content": comment_body})

    # --------------------------------------------------------------------------
    #  Admin-Only Commands
    # --------------------------------------------------------------------------
    if "@AI_Bot trace" in comment_body:
        # 1) extract method name safely
        m = re.search(r"@AI_Bot\s+trace\s+([\w\d_]+)", comment_body, re.IGNORECASE)
        if not m:
            return jsonify({'status':'invalid command','error':"Please specify a method name"}), 400
        method_name = m.group(1)

        # 2) gather possible bodies & look for a file name
        issue_body = issue_data.get('body', '')
        pr_body    = payload.get('pull_request', {}).get('body', '')
        file_name = (
            extract_file_name(issue_body)
            or extract_file_name(pr_body)
            or extract_file_name_from_comments(conversation)
        )
        if not file_name:
            analyze_result = {
                "method": "",
                "class": "",
                "method_content": "",
                "occurrences": [],
                "vulnerabilities": [],
                "error": "I couldn’t find a `.java` file name—please wrap it in ```java\nMyClass.java\n```."
            }
        else:
            # 3) call new helpers
            try:
                trace = trace_method_and_class(github, repo_full_name, file_name, method_name)
                if not trace:
                    raise ValueError(f"Couldn’t trace `{method_name}` in `{file_name}`")
                # fetch the full method body
                method_body = get_method_from_trace_result(github, repo_full_name, trace)
                # Detect all JCA API uses in the method
                occurrences = detect_jca_api_occurrences(method_body)
                # Analyze those occurrences for potential vulnerabilities
                vulnerabilities = analyze_jca_occurrences_vulnerability(occurrences, method_body)

                # 4) Build a richer result
                analyze_result = {
                    "method": trace["method"],
                    "class":  trace["class"],
                    "method_content": method_body,
                    "occurrences":     occurrences,
                    "vulnerabilities": vulnerabilities
                }
            except Exception as e:
                analyze_result = {
                    "method": "",
                    "class": "",
                    "method_content": "",
                    "occurrences":    [],
                    "vulnerabilities": [],
                    "error": str(e)
                }

        # 4) post & return
        comment_json  = json.dumps(analyze_result, indent=2)
        rendered = f"```json\n{comment_json}\n```"
        if 'pull_request' in issue_data:
            post_pr_comment(github, repo_full_name, issue_number, rendered)
        else:
            post_issue_comment(github, repo_full_name, issue_number, rendered)
        return jsonify({'status':'success'}), 200

    if "@AI_Bot analyze repo" in comment_body:
        result_msg = analyze_repo_and_open_issues(github, repo_full_name)
        conversation.append({"role": "assistant", "content": result_msg})

        if 'pull_request' in issue_data:
            post_pr_comment(github, repo_full_name, issue_number, result_msg)
        else:
            post_issue_comment(github, repo_full_name, issue_number, result_msg)
        return jsonify({'status': 'success'}), 200

    if "@AI_Bot update code" in comment_body:
        # 1) Figure out which PR we’re updating
        try:
            pr_number = extract_pr_number_from_comment(payload)
        except ValueError as e:
            err = str(e)
            # reply with the error in the same thread
            if 'pull_request' in issue_data:
                post_pr_comment(github, repo_full_name, issue_number, err)
            else:
                post_issue_comment(github, repo_full_name, issue_number, err)
            return jsonify({'status': 'invalid context', 'error': err}), 400

        # 2) Delegate to the helper, now with the correct PR number
        update_msg = attempt_update_pr_code(github, repo_full_name, pr_number, conversation)
        conversation.append({"role":"assistant","content":update_msg})

        # 3) Always post back on the PR itself
        post_pr_comment(github, repo_full_name, pr_number, update_msg)
        return jsonify({'status':'success'}), 200


    if "@AI_Bot update" in comment_body:
        # Also pass 'github' here
        fetched_code_msg = attempt_fetch_current_code(github, repo_full_name, issue_number)
        conversation.append({"role": "assistant", "content": fetched_code_msg})

        if 'pull_request' in issue_data:
            post_pr_comment(github, repo_full_name, issue_number, fetched_code_msg)
        else:
            post_issue_comment(github, repo_full_name, issue_number, fetched_code_msg)
        return jsonify({'status': 'success'}), 200

    if "@AI_Bot merge code" in comment_body:
        # Also pass 'github' here
        if 'pull_request' in issue_data:
            merged_msg = attempt_merge_corrected_code(github, repo_full_name, issue_number)
        else:
            merged_msg = attempt_merge_corrected_code_issue(
                github, repo_full_name, issue_number, comment_body, issue_body
            )
        conversation.append({"role": "assistant", "content": merged_msg})

        if 'pull_request' in issue_data:
            post_pr_comment(github, repo_full_name, issue_number, merged_msg)
        else:
            post_issue_comment(github, repo_full_name, issue_number, merged_msg)
        return jsonify({'status': 'success'}), 200

    if "@AI_Bot analyze file" in comment_body:
        # 1. Try to extract a file name from the issue body.
        file_name = extract_file_name(issue_body)
        if not file_name and 'pull_request' in issue_data:
            pr_description = issue_data.get('pull_request', {}).get('body', '')
            file_name = extract_file_name(pr_description)
        if not file_name:
            # If not found, try to extract it from the conversation history.
            combined_conv = "\n".join([msg['content'] for msg in conversation])
            file_name = extract_file_name(combined_conv)

        # 2. If a file name was found, fetch its content and analyze it.
        if file_name:
            repo_obj = github.get_repo(repo_full_name)
            default_branch = repo_obj.default_branch
            try:
                file_obj = repo_obj.get_contents(file_name, ref=default_branch)
                file_code = base64.b64decode(file_obj.content).decode("utf-8")
            except Exception as e:
                analyze_result = f"Failed to fetch file '{file_name}' from '{default_branch}': {e}"
            else:
                analyze_result = analyze_code_no_issue(file_code, repo=repo_obj)
        else:
            analyze_result = (
                "No file name found in the issue body or PR description. "
                "Please provide the file name in triple backticks (e.g., ```java\nMyClass.java\n```)."
            )
        conversation.append({"role": "assistant", "content": analyze_result})
        if 'pull_request' in issue_data:
            post_pr_comment(github, repo_full_name, issue_number, analyze_result)
        else:
            post_issue_comment(github, repo_full_name, issue_number, analyze_result)
        return jsonify({'status': 'success'}), 200

    # If no specific admin command is matched, fall back to a normal AI chat response.
    ai_reply = chat_with_history(conversation)
    conversation.append({"role": "assistant", "content": ai_reply})
    if 'pull_request' in issue_data:
        post_pr_comment(github, repo_full_name, issue_number, ai_reply)
    else:
        post_issue_comment(github, repo_full_name, issue_number, ai_reply)
    return jsonify({'status': 'success'}), 200

def extract_pr_number_from_comment(payload):
    """
    Given a GitHub webhook payload for an issue_comment or pull_request_review_comment,
    determine and return the associated pull request number.
    Raises ValueError if no PR can be found.
    """
    # 1. If the payload is from a PR comment event, the PR is top-level
    if 'pull_request' in payload:
        return payload['pull_request']['number']

    # 2. If the payload is an issue_comment on a PR (issue.pull_request present)
    issue = payload.get('issue', {})
    pr_info = issue.get('pull_request')
    if pr_info and isinstance(pr_info, dict) and 'url' in pr_info:
        # URL is https://api.github.com/repos/{owner}/{repo}/pulls/{number}
        try:
            return int(pr_info['url'].rstrip('/').split('/')[-1])
        except (ValueError, IndexError):
            raise ValueError(f"Cannot parse PR number from URL: {pr_info['url']}")

    # 3. Fallback: payload contains issue and it's actually a PR issue
    # (in some edge cases, GH might not include pull_request field)
    # Attempt to fetch PR via repo.get_pulls() by matching issue.number
    issue_number = issue.get('number')
    if issue_number:
        # assume issue_number is the PR number
        return issue_number

    raise ValueError('No pull request number found in payload')

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

def attempt_update_pr_code(github_instance, repo_full_name, pr_number, conversation):
    """
    Attempt to update code in a .java file within a PR, based on user instructions in 'conversation'.
    """
    repo = github_instance.get_repo(repo_full_name)
    try:
        pr = repo.get_pull(pr_number)
    except Exception as e:
        return f"Could not get PR #{pr_number} in {repo_full_name}: {e}"

    pr_branch = pr.head.ref

    # Find the .java file
    file_to_update = None
    for f in pr.get_files():
        if f.filename.endswith('.java'):
            file_to_update = f.filename
            break

    if not file_to_update:
        return "No .java files found in this PR to update."

    # Get the original code from that file on the PR branch
    try:
        contents = repo.get_contents(file_to_update, ref=pr_branch)
        original_code = base64.b64decode(contents.content).decode('utf-8')
    except Exception as e:
        return f"Failed to fetch '{file_to_update}' from branch '{pr_branch}': {e}"

    # Gather user instructions from conversation
    instructions = ""
    for msg in conversation:
        if msg['role'] == 'user':
            instructions += f"{msg['content']}\n"
    if pr.body:
        instructions += f"PR Description:\n{pr.body}\n"

    # Call your new helper
    updated_code = run_openai_update_code(original_code, instructions)
    if not updated_code or "Error calling OpenAI" in updated_code:
        return f"OpenAI failed to provide updated code. Output: {updated_code}"

    # Commit the updated code
    commit_message = f"[AI_Bot] Update code in {file_to_update} from PR #{pr_number}"
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

    return (
        f"Successfully updated `{file_to_update}` on branch `{pr_branch}`.\n\n"
        f"```java\n{updated_code}\n```"
    )

def attempt_merge_corrected_code(github_instance, repo_full_name, issue_number):
    messages = conversation_store.get((repo_full_name, issue_number), [])
    if not messages:
        return "No conversation found; cannot merge code."

    file_name = find_file_name_in_conversation(messages)
    if not file_name:
        return ("I couldn't find a file reference...")

    code_snippet = find_last_code_snippet(messages)
    if not code_snippet:
        return ("I couldn't detect a code snippet to merge...")

    repo = github_instance.get_repo(repo_full_name)
    default_branch = repo.default_branch
    
    try:
        file_contents = repo.get_contents(file_name, ref=default_branch)
    except Exception as e:
        return f"Failed to fetch '{file_name}' on branch '{default_branch}': {e}"

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

def attempt_fetch_current_code(github_instance, repo_full_name, issue_number):
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
                "then use `@AI_Bot update` again.")
    repo = github_instance.get_repo(repo_full_name)
    default_branch = repo.default_branch

    try:
        file_contents = repo.get_contents(file_name, ref=default_branch)
        current_code = base64.b64decode(file_contents.content).decode("utf-8")
    except Exception as e:
        return f"Failed to fetch '{file_name}' on branch '{default_branch}': {e}"

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

def get_github_client_for_repo(repo_full_name, installation_id=None):
    """
    Tries to get a GitHub client with app installation_id if available,
    otherwise falls back to a personal access token if BOT_FALLBACK_PAT is set.
    """
    fallback_pat = os.getenv("BOT_FALLBACK_PAT", "")
    if installation_id:
        # Attempt to get an installation token
        try:
            access_token = git_integration.get_access_token(installation_id=installation_id)
            return Github(access_token.token)
        except Exception as e:
            logging.warning(f"Failed to get installation token for {repo_full_name}: {e}")

    # If we reach here, either no installation_id or it failed. Use fallback PAT if available:
    if fallback_pat:
        logging.info(f"Using fallback PAT for {repo_full_name}")
        return Github(fallback_pat)

    # Otherwise, we have no way to authenticate
    raise RuntimeError(
        "No installation token and no fallback personal access token. "
        "Cannot access repository."
    )

def post_comment_any_repo(target_repo_full_name, issue_or_pr_number, comment_body):
    """
    Post a comment on an issue or PR in some other repo,
    even if the App isn't installed there.
    """
    # No known installation ID for that repo? Use None or read from some config if you have it:
    github = get_github_client_for_repo(target_repo_full_name, installation_id=None)
    repo = github.get_repo(target_repo_full_name)
    issue = repo.get_issue(issue_or_pr_number)
    issue.create_comment(comment_body)

def run_openai_update_code(original_code, instructions):
    """
    Sends the user's instructions and the original code to OpenAI,
    returning the updated code as plain text.
    """
    system_prompt = (
        "You are a Java code refactoring assistant. The user has provided instructions "
        "for how to fix or update this Java file. Please apply only minimal changes."
    )
    user_prompt = (
        f"User instructions:\n{instructions}\n\n"
        "Original file content:\n"
        "```java\n"
        f"{original_code}\n"
        "```\n\n"
        "Return the updated Java file content as plain text."
    )

    # If you want to ensure an API key is set, or handle failures, etc.
    if not openai.api_key:
        return "No OpenAI API key is configured."

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
        return updated_code
    except Exception as e:
        return f"[Error calling OpenAI: {e}]"


    """
    Recursively list all .java files in the GitHub repo at the given ref.
    Returns a list of file paths (e.g. 'src/com/example/MyClass.java').
    """
    if ref is None:
        ref = repo.default_branch
    queue = [""]  # start at the repo root
    java_files = []

    while queue:
        path = queue.pop()
        try:
            contents = repo.get_contents(path, ref=ref)
        except Exception:
            # Could be a file or an error
            continue
        
        for item in contents:
            if item.type == "dir":
                # Enqueue the directory path to explore subfolders
                queue.append(item.path)
            elif item.type == "file" and item.path.endswith(".java"):
                # Found a Java file
                java_files.append(item.path)

    return java_files




    if visited is None:
        visited = set()
    if file_path in visited:
        return "Method not found"
    visited.add(file_path)

    repo_obj = github_instance.get_repo(repo_full_name)
    default_branch = repo_obj.default_branch
    try:
        file_obj = repo_obj.get_contents(file_path, ref=default_branch)
        file_code = base64.b64decode(file_obj.content).decode("utf-8")
    except Exception as e:
        logging.error(f"Failed to fetch file '{file_path}': {e}")
        return "Method not found"

    # Build a prompt to extract the complete source of the method
    trace_prompt = (
        f"Given the following Java file content:\n\n"
        f"```java\n{file_code}\n```\n\n"
        f"Please find and return the complete source code (including signature and body) of the method named '{method_name}'.\n"
        "Return only the method's source code. If the method is not found, output exactly: Method not found."
    )

    try:
        resp = openai.ChatCompletion.create(
            model="o3-mini",
            reasoning_effort="medium",
            messages=[
                {"role": "system", "content": "You are a Java code analysis assistant."},
                {"role": "user", "content": trace_prompt}
            ]
        )
        traced_method = resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"OpenAI call failed in trace: {e}")
        return "Method not found"

    if traced_method.lower() != "method not found":
        return traced_method

    # If the method wasn't found in the current file, check imported files
    imports = re.findall(r"import\s+([\w\.]+);", file_code)
    for imp in imports:
        candidate_file = imp.replace('.', '/') + ".java"
        result = trace_method_recursive(github_instance, repo_full_name, candidate_file, method_name, visited)
        if result and result.lower() != "method not found":
            return result

    return "Method not found"

def trace_method_and_class(github_instance, repo_full_name, file_name, method_name):
    # 1) Fetch the file contents using fetch_file_content()
    repo = github_instance.get_repo(repo_full_name)
    code = fetch_file_content(repo, file_name, ref=repo.default_branch)
    if not code:
        return None

    # 2) (Option A) Parse with a simple regex/AST lib:
    #    - Find “class X { …” above the method definition
    #    - Extract the name X
    #
    #  Or (Option B) call OpenAI to do it robustly:
    prompt = f"""
    Given this Java source (in ```java blocks``` below) and the method name "{method_name}",
    1) Extract the method name.
    2) fully-qualified name (FQN) of the class, which includes it, including the `.java` suffix.
    Return exactly two fields in JSON: {{ "method": "...", "class": "..." }}.
    ```java
    {code}
    ```
    """
    resp = openai.ChatCompletion.create(
        model="o3-mini",
        reasoning_effort="high",
        messages=[{"role":"user","content":prompt}]
    )
    try:
        return json.loads(resp.choices[0].message.content)
    except:
        return None

def list_all_java_files_in_repo(repo, ref=None):
    """
    Recursively list all .java files in the GitHub repo at the given ref.
    Returns a list of file paths (e.g. 'src/com/example/MyClass.java').
    """
    if ref is None:
        ref = repo.default_branch
    queue = [""]  # start at the repo root
    java_files = []

    while queue:
        path = queue.pop()
        try:
            contents = repo.get_contents(path, ref=ref)
        except Exception:
            # Could be a file or an error
            continue
        
        for item in contents:
            if item.type == "dir":
                # Enqueue the directory path to explore subfolders
                queue.append(item.path)
            elif item.type == "file" and item.path.endswith(".java"):
                # Found a Java file
                java_files.append(item.path)

    return java_files

def get_method_from_trace_result(github_instance, repo_full_name, trace_result):
    """
    Given trace_result = {
      "method": "...",
      "class":  "com.foo.BarTest.java"
    }
    find the actual file in the repo, fetch it, and extract the full method body.
    """
    import re

    # 1) Normalize the FQN → file path suffix
    class_fqn = trace_result["class"]  # e.g. "com.amazon.corretto.crypto.provider.test.AesCbcIso10126Test.java"

    # strip off the ".java" so we don't mangle it when replacing dots
    if class_fqn.endswith(".java"):
        base = class_fqn[:-5]      # "com.amazon....AesCbcIso10126Test"
    else:
        base = class_fqn

    # now turn into a path and re-append the suffix
    path_suffix = base.replace(".", "/") + ".java"
    # e.g. "com/amazon/corretto/crypto/provider/test/AesCbcIso10126Test.java"

    # 2) List every .java file in the repo
    repo      = github_instance.get_repo(repo_full_name)
    all_files = list_all_java_files_in_repo(repo, ref=repo.default_branch)

    # 3) Look for an exact end-match
    matches = [f for f in all_files if f.endswith(path_suffix)]
    if not matches:
        # as a fallback, match on the simple filename alone
        simple = base.split(".")[-1] + ".java"
        matches = [f for f in all_files if f.endswith(simple)]
        if not matches:
            raise FileNotFoundError(f"No Java file matching “{path_suffix}” or “{simple}”")
    file_path = matches[0]

    # 4) Fetch the content
    code = fetch_file_content(repo, file_path, ref=repo.default_branch)
    if code is None:
        raise IOError(f"Failed to fetch {file_path}")

    # 5) Find the method signature
    name  = trace_result["method"]
    sig_re = re.compile(rf'([^\n]{{0,80}}\b{name}\s*\([^)]*\)\s*\{{)', re.MULTILINE)
    m = sig_re.search(code)
    if not m:
        raise ValueError(f"Method “{name}” not found in {file_path}")
    start = m.start(1)

    # 6) Brace-match to grab the full body
    brace = 0
    in_method = False
    for i, ch in enumerate(code[start:], start):
        if ch == "{":
            brace += 1
            in_method = True
        elif ch == "}":
            brace -= 1
        if in_method and brace == 0:
            return code[start : i+1]

    # fallback on mismatch
    return code[start:]

def detect_jca_api_occurrences(java_code):
    
    system_prompt = (
    "Analyze the provided Java source code and detect all Direct and Indirect (dynamically) occurrences of cryptographic initialization "
    "and parameter handling. In particular, please identify and list all occurrences of the following and store them in a JSON array, "
    "with one object per usage (do not group similar calls together):\n\n"
    "Direct instantiations (each unique occurrence must be listed separately):\n"
    "  - Cipher.getInstance(...)\n"
    # "  - MessageDigest.getInstance \n"
    "  - SecretKeySpec(...)\n"
    "  - PBEKeySpec(...)\n"
    "  - PBEParameterSpec(...)\n"
    "  - KeyGenerator.getInstance(...)\n"
    "  - SecretKeyFactory.getInstance(...)\n"
    "  - IvParameterSpec(...)\n"
    "  - KeyStore.getInstance(...)\n\n"
    "Indirect instantiations: Detect any line of code or dynamic mechanisms (such as helper methods) that ultimately return one of the above objects. "
    "For example, if the code calls helper methods like getNativeCipher(...), getJceCipher(...), or similar functions, you must capture each call "
    "individually with its complete parameters (e.g., getNativeCipher(NO_PADDING), getJceCipher(OAEP_PADDING), etc.).\n\n"
    "For each detected usage, return a JSON object with the following keys:\n"
    "  'type': 'Direct' or 'Indirect'\n"
    "  'apiCall': The exact method or constructor call with its parameters (for example, 'getNativeCipher(NO_PADDING)' or 'Cipher.getInstance(NO_PADDING, NATIVE_PROVIDER)')\n"
    "  'snippet': The exact line of code where the usage appears\n"
    "  'parameters': The actual values of the parameters. For any parameter that is a constant defined in the code (such as NO_PADDING, OAEP_PADDING, PKCS1_PADDING, etc.), include its real value as defined (e.g., 'RSA/ECB/NoPadding', 'RSA/ECB/OAEPPadding', etc.).\n"
    "  'explanation': A brief note explaining why the usage is considered direct or indirect\n\n"
    "Return only the deduplicated JSON array as your final answer."
)

    user_prompt = (
        f"Analyze the following Java code:\n\n{java_code}\n\n"
        "Return only a JSON array of detected JCA API occurrences."
    )
    try:
        resp = openai.ChatCompletion.create(
            model="o3-mini",
            reasoning_effort="high",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        response_text = resp.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"OpenAI call failed in detect_jca_api_occurrences: {e}")
        return []
    
    try:
        occurrences = json.loads(response_text)
        if isinstance(occurrences, dict):
            occurrences = [occurrences]
        return occurrences
    except Exception as e:
        logging.error(f"Error parsing JCA API detection output: {e}")
        return []

def analyze_jca_occurrences_vulnerability(occurrences_list, java_code):
    system_prompt = (
        "For every occurrence provided in the list, first locate the corresponding code snippet within the full Java code to capture its complete context. "
        "You are a Java security assistant. Given a list of JCA API usage occurrences along with their bound parameters, evaluate each occurrence solely based on those parameters."
        "Your analysis should trace any conditions or variable assignments that affect the parameters of the JCA API call, and evaluate the vulnerability based on the following criteria:\n\n"
        "1. Cryptographic Object Type: Identify the type of cryptographic object being instantiated (e.g., Cipher, SecretKeySpec, etc.).\n\n"
        "2. Code Context: Provide the exact code snippet (as found in the full code context) where the API is invoked.\n\n"
        "3. Vulnerability Type: Classify each as 'Insecure', 'Undefined', or 'Secure'."
            "1. Insecure if any case of:"
                "a) Weak Symmetric Encryption Algorithms (e.g., DES, Blowfish, RC4, RC2, IDEA)."
                "b) Weak Encryption Mode (ECB)."
                "c) Insecure Asymmetric Ciphers (RSA with keysize<1024)."
                "d) hardcoded cryptographic keys in SecretKeySpec."
                "e) constant cryptographic keys in SecretKeySpec."             
                "f) Static Salt for key derivation in PBEKeySpec."
                "g) Static Salt for key derivation in PBEParameterSpec."
                "h) Hardcoded passwords in PBEKeySpec."
                "i) constant passwords in PBEKeySpec."
                "j) iteration count < 1000 in PBEKeySpec."
                "k) iteration count < 1000 in PBEParameterSpec."
                "l) weak Random function for generating secret key."
                "m) weak Random function for Predictableseed instead of SecureRandom."
                "n) Hardcoded or Constant IV."
                "o) Hardcoded Password in KeyStore."
                "p) Constant Password in KeyStore."
                "q) constant seed in SecureRandom."
                # "r) broken hash function (e.g., SHA1, MD5, MD4, MD2)."
                
            "2. Undefined_1: if the Cryptographic Object is calling a computed parameter AND the actual implementation of computed parameter is not defined in the script."
            "3. Undefined_2: if the Cryptographic Object is called as indirect AND the actual implementation of cryptographic function is not defined in the script."
            "4. Secure: If not insecure and not undefined."
        "4. Correction: Provide clear guidance on how to fix the vulnerability if one is found, or explain why the usage is secure.\n\n"
    )
    user_prompt = (
        f"Analyze the following Java code in full:\n```java\n{java_code}\n```\n\n"
        f"Given the following list of detected occurrences:\n{json.dumps(occurrences_list, indent=2)}\n\n"
        "Return a JSON array where each element is an object with the following keys:\n"
        "1. 'cryptographicObjectType'\n"
        "2. 'codeSnippet' (exact snippet from full code context)\n"
        "3. 'vulnerabilityType'\n"
        "4. 'correction'"
    )
    try:
        resp = openai.ChatCompletion.create(
            model="o3-mini",
            reasoning_effort="high",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        response_text = resp.choices[0].message.content.strip()
        vulnerability_results = json.loads(response_text)
        if isinstance(vulnerability_results, dict):
            vulnerability_results = [vulnerability_results]
        return vulnerability_results
    except Exception as e:
        logging.error(f"OpenAI call failed in analyze_jca_occurrences_vulnerability: {e}")
        return []

def analyze_jca_occurrences_execution(java_code, occurrences_list):
    system_prompt = (
        "JCA Execution Details: Provide comprehensive details including:\n"
        "   (a) The call chain leading to the JCA instantiation.\n"
        "   (b) Whether a default algorithm is stored in a class member and used later in the JCA instantiations or false.\n"
        "   (c) Whether conditions directly affect the source value passed to JCA instantiation. The source when the condition is true versus when it is false or false.\n"
        "   (d) Identification of the classes directly involved in the cryptographic logic.\n"
            "1. primary class or false."
            "2. driver class or false."
        "   (e) Identification of the classes directly involved in the cryptographic parameters logic.\n\n"
            "1. primary class for the parameter or false."
            "2. driver class for the parameter or false."    
    )   
    user_prompt = (
        f"Given the following full Java code:\n\n{java_code}\n\n"
        f"And the following list of detected occurrences:\n{json.dumps(occurrences_list, indent=2)}\n\n"
        "Please return a JSON array where each element is an object with the key 'jcaExecution', which itself is an object with sub-keys:\n"
        "   'callChain', 'defaultAlgorithmUsage', 'conditionalEvaluation', 'cryptographicLogic', 'cryptographicParameters'"
    )
    try:
        resp = openai.ChatCompletion.create(
            model="o3-mini",
            reasoning_effort="high",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        )
        response_text = resp.choices[0].message.content.strip()
        execution_results = json.loads(response_text)
        if isinstance(execution_results, dict):
            execution_results = [execution_results]
        return execution_results
    except Exception as e:
        logging.error(f"OpenAI call failed in analyze_jca_occurrences_execution: {e}")
        return []

###############################################################################
# Flask App Entry Point
###############################################################################
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
