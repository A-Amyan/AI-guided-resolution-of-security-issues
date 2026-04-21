import base64
import logging
from github import GithubIntegration, Github, Auth

# Internal imports
from config import APP_ID, PRIVATE_KEY

auth = Auth.AppAuth(app_id=APP_ID, private_key=PRIVATE_KEY)
git_integration = GithubIntegration(auth=auth)


###############################################################################
# Post a comment on a PR (pull request comment)
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
# Post a comment on an Issue (non-PR)
###############################################################################
def post_issue_comment(github_instance, repo_full_name, issue_number, comment_body):
    try:
        repo = github_instance.get_repo(repo_full_name)
        issue = repo.get_issue(issue_number)
        issue.create_comment(comment_body)
        logging.debug(f"Posted comment to Issue #{issue_number} in {repo_full_name}.")
    except Exception as e:
        logging.error(f"Failed to post Issue comment: {e}")


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