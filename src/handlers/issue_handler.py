import logging
from flask import jsonify

from src.services.github_client import get_github_client, post_issue_comment
from src.services.openai_client import attempt_merge_corrected_code_issue
from src.utils.memory import get_or_create_conversation, conversation_store

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