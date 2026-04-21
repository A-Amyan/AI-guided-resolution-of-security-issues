from src.prompts.pr_prompts import CONVERSATION_SYSTEM_MESSAGE

###############################################################################
# memory conversation store (keyed by (repo_full_name, issue_number))
###############################################################################
conversation_store = {}

def get_or_create_conversation(repo_full_name, issue_number, issue_body=None):
    """
    Retrieve or create a conversation for ChatCompletion.
    Optionally inject the issue body if first seen.
    """
    key = (repo_full_name, issue_number)
    if key not in conversation_store:
        
        conversation_store[key] = [{"role": "system", "content": CONVERSATION_SYSTEM_MESSAGE}]
        if issue_body:
            conversation_store[key].append({
                "role": "assistant",
                "content": f"Issue Body:\n\n{issue_body}"
            })
    return conversation_store[key]