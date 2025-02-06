# GitHub AI Code Analysis Bot

This repository contains a GitHub App written in Python using Flask. The bot listens to GitHub webhook events (push, pull request, and issue comments) and uses OpenAI's GPT-4 to analyze Java code for potential security misuses. It also supports admin commands to update or merge code based on AI analysis.

## Features

- **Automated Pull Requests:**  
  When new branches are pushed that include Java file changes, the bot automatically creates a pull request with an AI-generated summary.

- **Pull Request Analysis:**  
  The bot analyzes Java code changes in pull requests for security issues (e.g., hardcoded keys, weak encryption practices) and posts the results as comments.

- **Issue Comment Commands:**  
  Admin-only commands can be issued in issue or pull request comments. Commands include:
  - `@AIBot analyze repo`: Analyze the entire repository for common security misuses.
  - `@AIBot update code`: Update code in a pull request based on admin instructions.
  - `@AIBot update`: Retrieve and post the current code of a file.
  - `@AIBot merge code`:  
    - For PR comments, merge provided code snippets.
    - For Issue comments, the bot extracts the Java file name from the issue body, retrieves its content, runs analysis (using `analyze_code_no_issue`), and then posts the analysis result.
  - `@AIBot analyze code`:  
    Extracts the Java file name from the issue body (or PR description if applicable), retrieves the file content from the repository, analyzes it using GPT-4, and posts the analysis result.

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
   gunicorn==20.1.0
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
   PORT=5000
   ```

   Replace the placeholders with your actual values.

## Running the Bot Locally

Start the Flask application by running:

```bash
python app.py

```

The server will listen on port 5000 (or the port specified in your `.env` file).

## Deployment

For production deployment, consider using platforms such as [Heroku](https://heroku.com), [AWS Elastic Beanstalk](https://aws.amazon.com/elasticbeanstalk/), or [Google Cloud Run](https://cloud.google.com/run). Ensure your webhook endpoint is publicly accessible and configure environment variables on your hosting platform.

## Webhook Endpoints

- **`/webhook`**: Main endpoint for receiving GitHub webhook events (push, pull request, issue comment).
- **`/ping`**: Health-check endpoint.

## Admin Commands

Only the repository owner (admin) can issue commands. Example commands include:

- **`@AIBot analyze repo`**: Analyze the repository for security misuses.
- **`@AIBot update code`**: Update code in a pull request based on instructions.
- **`@AIBot update`**: Retrieve and display the current code.
- **`@AIBot merge code`**:  
  - **For PR comments:** Merges a provided code snippet.  
  - **For Issue comments:**  
    - Extracts the Java file name from the issue body, retrieves the file content from the repository, and analyzes it using GPT-4.
    - (See the README instructions in the code for more details.)
- **`@AIBot analyze code`**:  
  Extracts the file name from the issue body (or PR description), retrieves the file content, analyzes it using GPT-4, and posts the analysis result.

## Contributing

Contributions, bug fixes, and feature enhancements are welcome. Please fork the repository and submit a pull request with your changes.

## License

This project is licensed under the [MIT License](LICENSE).
