# Contributing to GitHub AI Code Analysis Bot

First off, thank you for considering contributing to this project! Whether you're fixing a bug, adding a new AI feature, or expanding the GitHub integrations, your help is appreciated.

This document outlines the architecture of the bot and provides step-by-step guides for the most common contribution scenarios.

## 🏗️ Architecture Overview

This project is built using **Flask** and follows a strictly modular, service-oriented architecture. Data flows in one direction:
`Routes` ➔ `Handlers` ➔ `Services` ➔ `Prompts & Utils`

Here is where everything lives:

* **`src/routes/`**: Receives the raw HTTP webhooks from GitHub.
* **`src/handlers/`**: The "traffic cops". They parse the GitHub payload and decide what action to take (e.g., `push_handler.py`, `pr_handler.py`).
* **`src/services/`**: The heavy lifting.
    * `github_client.py`: All PyGithub API calls.
    * `openai_client.py`: AI generation (PR summaries, code merging).
    * `analyzer.py`: The core Java JCA security analysis engine.
* **`src/prompts/`**: Stores all system and user prompts for OpenAI.
* **`src/utils/`**: Helper functions and in-memory state.

---

## 🛠️ How to Add a New Feature

### 1. Adding a New GitHub Webhook Event
If you want the bot to start listening to a new GitHub event (for example, a `release` event or a `repository` creation event):

1.  **Create a Handler:** Create a new file in `src/handlers/` (e.g., `release_handler.py`).
    ```python
    import logging

    def handle_release(payload):
        logging.info("Processing release event...")
        # Add logic here, calling functions from src.services
        return {"status": "success"}, 200
    ```
2.  **Register the Handler:**
    Open `src/routes/webhook.py` and add the new event to the routing logic:
    ```python
    from src.handlers import release_handler

    @webhook_bp.route('/webhook', methods=['POST'])
    def webhook():
        event = request.headers.get('X-GitHub-Event')
        payload = request.json
        
        if event == 'release':
            return release_handler.handle_release(payload)
        # ... existing events ...
    ```

### 2. Modifying or Adding AI Prompts
To improve the AI's behavior or add a new type of analysis, **do not hardcode prompts in the logic files**.

1.  Navigate to `src/prompts/`.
2.  Open the relevant file (e.g., `jca_prompts.py` for security rules, `pr_prompts.py` for chat/summaries).
3.  Update the string constants. If your prompt requires dynamic variables, use a function that returns an f-string:
    ```python
    def get_custom_analysis_prompt(file_content: str) -> str:
        return f"Analyze this specific file:\\n\\n{file_content}"
    ```

### 3. Adding a New Admin Command
To add a new chat command (e.g., `@AI_Bot explain`):
1.  Open `src/handlers/issue_handler.py`.
2.  Find the section parsing the comment body.
3.  Add an `elif` block for your new command and call the appropriate service.

---

## 💻 Local Development & Testing

Working with GitHub webhooks locally requires a way for GitHub to send POST requests to your local machine.

1.  **Set up your `.env` file** (see `README.md` for required variables).
2.  **Run the Flask app:**
    ```bash
    python app.py
    ```
3.  **Expose your local server:** Use a tool like [ngrok](https://ngrok.com/) to expose port 5000 to the internet.
    ```bash
    ngrok http 5000
    ```
4.  **Update GitHub App settings:** Go to your GitHub App settings and change the **Webhook URL** to the `https://<your-ngrok-id>.ngrok-free.app/webhook` address provided by ngrok.

---

## 🚀 Pull Request Process

1.  **Fork the repo** and create your branch from `main`.
2.  Ensure your code follows the existing modular structure.
3.  Do not leave `print()` statements in the code; use the `logging` module (`logging.debug`, `logging.info`, `logging.error`).
4.  Test your changes locally using `ngrok` before submitting.
5.  Submit a Pull Request with a clear description of the problem and the proposed solution.

Thank you for helping make this bot better and more secure!
