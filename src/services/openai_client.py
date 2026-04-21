from src.prompts.pr_prompts import PR_SINGLE_FILE_SYSTEM_PROMPT, get_pr_single_file_user_prompt
from src.prompts.pr_prompts import PR_MULTI_FILE_SYSTEM_PROMPT, get_pr_multi_file_user_prompt
from src.prompts.pr_prompts import MERGE_CODE_SYSTEM_PROMPT, get_merge_code_user_prompt

import openai
import logging
from config import OPENAI_API_KEY
from src.prompts.pr_prompts import (
    PR_SINGLE_FILE_SYSTEM_PROMPT, 
    get_pr_single_file_user_prompt,
    PR_MULTI_FILE_SYSTEM_PROMPT,
    get_pr_multi_file_user_prompt,
    MERGE_CODE_SYSTEM_PROMPT,
    get_merge_code_user_prompt
)

openai.api_key = OPENAI_API_KEY

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

    user_prompt = get_pr_single_file_user_prompt(branch_name, pusher_name, java_file_name, snippet)
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            temperature=0,
            max_tokens=300,
            messages=[
                {"role": "system", "content": PR_SINGLE_FILE_SYSTEM_PROMPT},
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
    
    user_prompt = get_pr_multi_file_user_prompt(branch_name, pusher_name, combined_details)
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            temperature=0,
            max_tokens=300,
            messages=[
                {"role": "system", "content": PR_MULTI_FILE_SYSTEM_PROMPT},
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
    
    user_prompt = get_merge_code_user_prompt(original_code, correction_instructions)
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            temperature=0,
            max_tokens=3000,
            messages=[
                {"role": "system", "content": MERGE_CODE_SYSTEM_PROMPT},
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


