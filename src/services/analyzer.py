import openai
import json
import logging

# Internal imports
from src.prompts.jca_prompts import (JCA_ANALYSIS_SYSTEM_PROMPT, get_jca_pr_analysis_prompt)


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
    
    user_prompt = get_jca_pr_analysis_prompt(combined_code)
    try:
        resp = openai.ChatCompletion.create(
            model="o3-mini",
            reasoning_effort="medium",
            messages=[
                {"role": "system", "content": JCA_ANALYSIS_SYSTEM_PROMPT},
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