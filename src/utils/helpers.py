import re
import json
import logging

###############################################################################
# Helpers: extract a file name from a text string.
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