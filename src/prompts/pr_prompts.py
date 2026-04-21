# --- Conversational Persona Prompts ---
CONVERSATION_SYSTEM_MESSAGE = (
    "You are an assistant specialized in Java security analysis, "
    "best practices, and general code discussions. Keep context from "
    "previous messages in this issue to maintain a coherent conversation."
    "Do not engage in discussions outside of security."
)

# --- PR Description Prompts ---
PR_SINGLE_FILE_SYSTEM_PROMPT = (
    "You are an assistant who writes short PR descriptions.\n"
    "You have been given the name of a branch, the pusher's name, and a Java file's content.\n"
    "Write a concise summary of what the file does or changes. Then mention the branch, pusher, and file name.\n"
)

def get_pr_single_file_user_prompt(branch_name: str, pusher_name: str, java_file_name: str, snippet: str) -> str:
    return (
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

PR_MULTI_FILE_SYSTEM_PROMPT = (
    "You are an assistant who writes short PR descriptions focused on Java security improvements. "
    "Given the branch name, the pusher's name, and details for multiple Java files, provide a concise summary "
    "that emphasizes any security-related changes or vulnerabilities addressed. "
    "Mention each file name in triple backticks where appropriate."
)

def get_pr_multi_file_user_prompt(branch_name: str, pusher_name: str, combined_details: str) -> str:
    return (
        f"Branch Name: {branch_name}\n"
        f"Pusher: {pusher_name}\n"
        f"Files Changed:\n{combined_details}\n\n"
        "Please write a short PR description that summarizes these changes with an emphasis on security improvements."
    )

# --- Code Merging / Correction Prompts ---
MERGE_CODE_SYSTEM_PROMPT = (
    "You are a code merging assistant. The admin has provided correction instructions. "
    "Apply these corrections to the original Java code while making only minimal changes."
)

def get_merge_code_user_prompt(original_code: str, correction_instructions: str) -> str:
    return (
        f"Original Code:\n```java\n{original_code}\n```\n\n"
        f"Correction Instructions:\n{correction_instructions}\n\n"
        "Please output the updated Java code as plain text."
    )

