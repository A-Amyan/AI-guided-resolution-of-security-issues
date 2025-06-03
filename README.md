# GitHub AI Code Analysis Bot

This repository contains a GitHub App written in Python using Flask. The bot listens to GitHub webhook events (push, pull request, and issue comments) and uses OpenAI's GPT-4 to analyze Java code for potential security misuses. It also supports admin commands to update or merge code based on AI analysis.

## Features

- **GitHub App Integration & Authentication:**
  - Uses environment variables (`GITHUB_APP_ID`, `GITHUB_PRIVATE_KEY`, `OPENAI_API_KEY`) for secure configuration.
  - Authenticates as a GitHub App using PyGithub and a custom `Auth.AppAuth`.
- **Flask-Based Webhook Server:**
  - Listen for GitHub webhook events on `/webhook` and a health check endpoint `/ping`.
  - Support the following GitHub events:
    - **Push Events:** Automatically triggers actions on branch pushes.
    - **Pull Request Events:** Analyzes code changes in PRs.
    - **Issue Comment Events:** Responds to chat commands and admin instructions.
- **Push Event Handling:**
  - **Automatic Pull Request Creation:** When a push occurs on a non-default branch, the bot automatically creates a pull request.
  - **Multi-File AI-Generated PR Description:**
    - Collect all changed Java files from the push.
    - Generate a short, security-focused PR description that summarizes all impacted Java files using OpenAI GPT‑4.
- **Pull Request Event Handling:**
  - **Per-File Security Analysis:**
    - Iterate through each changed Java file in the pull request.
    - Retrieve file contents and splits code into logical sections (using structural splitting by class or method definitions).
    - Analyze each section for potential vulnerabilities, misuse of Java Cryptography Architecture (JCA) APIs, and security weaknesses.
  - **Aggregated Reporting:**
     - Generate a JSON-formatted report for each file.
     - Post individual comments on the pull request with the security analysis for each impacted Java file.
- **Issue Comment Command Handling:**
  - **In-Memory Conversation Context:**
     - Maintains conversation history per repository and issue number to ensure coherent discussions.
  - **Admin Commands (Restricted to Repository Owner):**
    - `@AI_Bot analyze repo`: Automatically scans the entire repository for Java files, analyzes each for security vulnerabilities, and opens issues with the findings.
    - `@AI_Bot analyze file`: Analyzes a specific file (extracted from the issue or PR body) for security vulnerabilities.
    - `@AI_Bot update code` / `@AIBot update`: Uses AI to generate updated code based on user instructions.
    - `@AI_Bot merge code`: Merges AI-proposed corrections into the repository.
    - `@AI_Bot trace <method_name>`: search for the method in the repository and analyze it.
  - **Strict Security-Only Discussion:**
     - The bot’s system instructions enforce that all conversations remain focused on security analysis and vulnerability remediation.
     - If topics fall outside Java security, the bot responds by indicating that only security-related discussion is supported.
- **AI-Powered Analysis & Summarization:**
  - **GPT‑4 Integration:**
    - Generate concise PR descriptions based on Java file snippets.
    - Perform in-depth security analysis on code sections, identifying vulnerabilities, and misuses, and suggesting secure alternatives.
    - Merge findings from different sections to avoid duplication
  - **Custom Prompts:**
    - System and user prompts are tailored to focus on security best practices, cryptography, and vulnerability remediation.
- **Code Merging & Updates:**
  - Support updating code based on admin correction instructions.
  - Fetche the original code from GitHub, apply minimal changes via GPT‑4 and update the repository file with a new commit.
- **Robust Error Handling & Logging:**
  - Log errors (set to DEBUG level for troubleshooting) for operations such as file fetching, GitHub API calls, and AI interactions.
  - Ensure graceful handling of missing fields or authentication issues.

## How to Install the GitHub App

Follow these steps to install the GitHub AI Bot on your repositories:
  - **Step 1:** Go to the Installation URL
    - Navigate to the installation page for the GitHub App:
    [https://github.com/apps/GitHub_AI_Bot](https://github.com/apps/GitHub_AI_Bot)
  - **Step 2:** Install the App
    - On the installation page, click the **Install** (or **Configure**) button. You will be prompted to choose whether to install the app on:
      - **All repositories:**
        The app will have access to every repository in your account or organization.
      - **Selected repositories:**
        You can select the specific repositories you want the app to have access to.
  - **Step 3:** Grant Permissions
    After selecting the repositories, you will see a list of permissions that the app requires (for example, read/write access to issues, pull requests, and repository contents). Review and grant the necessary permissions.
    Once you confirm, the app will be installed and will start receiving webhook events from the repositories it has access to.

## What Happens After Installation?

After installation, the GitHub AI Bot will begin processing webhook events from your repositories. It will:
- Analyze code in pull requests and issues.
- Automatically generate pull request descriptions.
- Execute admin commands (when issued by the repository owner) such as code updates, merges, and analysis.

## Prerequisites

- **Python 3.8+**
- A GitHub App with appropriate permissions (e.g., repository contents, issues, pull requests)
- An [OpenAI API key](https://openai.com) with access to GPT-4

## Installation

1. **Clone the Repository:**

   ```bash
   git clone https://github.com/A-Amyan/AI-guided-resolution-of-security-issues.git
   cd your-repo-name
   ```

2. **Create and Activate a Virtual Environment (optional but recommended):**

  - On Linux/MacOS, run:
    ```bash
    source venv/bin/activate
    ```
  - On Windows, run:
    ```bash
    venv\Scripts\activate
    ```

3. **Install Dependencies:**

   Ensure your `requirements.txt` file includes:

   ```
   Flask==2.3.2
   python-dotenv==1.0.0
   PyGithub==1.59.0
   openai==0.27.8
   requests==2.31.0
   gunicorn==20.1.0 # needed for deploying
   ```

   Then install with:

   ```bash
   pip install -r requirements.txt
   ```

4. **Configure Environment Variables:**

   Create a `.env` file in the root directory with the following content:

   ```dotenv
   GITHUB_APP_ID=your_github_app_id
   GITHUB_PRIVATE_KEY="your_github_private_key_contents"
   OPENAI_API_KEY=your_openai_api_key
   ```

   Replace the placeholders with your actual values.

## Running the Bot Locally

Start the Flask application by running:
```python
if __name__ == '__main__':
    app.run(debug=True, port=5000)
```

```bash
python app.py

```

The server will listen on port 5000.

## Deployment

GitHub AI Bot is already deployed on [Render](https://render.com/). Its publicly accessible URL (for webhook events) is provided during installation. 

## Webhook Endpoints

- **`/webhook`**: Main endpoint for receiving GitHub webhook events (push, pull request, issue comment).
- **`/ping`**: Health-check endpoint.

## Contributing

Contributions, bug fixes, and feature enhancements are welcome. Please fork the repository and submit a pull request with your changes.

## License

This project is licensed under the [MIT License](LICENSE).
