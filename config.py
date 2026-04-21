import os
import sys
import logging
from dotenv import load_dotenv

###############################################################################
# 1. Logging Configuration
# Set to logging.DEBUG for troubleshooting during development
###############################################################################
logging.basicConfig(
    level=logging.ERROR, 
    format='%(asctime)s - %(levelname)s - %(message)s'
)

###############################################################################
# 2. Load Environment Variables
###############################################################################
load_dotenv()

APP_ID = os.getenv('GITHUB_APP_ID')
PRIVATE_KEY = os.getenv('GITHUB_PRIVATE_KEY')
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
BOT_ADMIN = os.getenv("BOT_ADMIN")
BOT_FALLBACK_PAT = os.getenv("BOT_FALLBACK_PAT")
PORT = int(os.getenv('PORT', 5000))

###############################################################################
# 3. Validate Critical Configurations
###############################################################################
if not APP_ID:
    logging.error("GITHUB_APP_ID not set.")
    sys.exit(1)

try:
    # GitHub App IDs must be integers
    APP_ID = int(APP_ID)
except ValueError:
    logging.error("GITHUB_APP_ID must be an integer.")
    sys.exit(1)

if not PRIVATE_KEY:
    logging.error("GITHUB_PRIVATE_KEY not set.")
    sys.exit(1)

if not OPENAI_API_KEY:
    # We don't necessarily exit here in case you want the bot to run 
    # basic non-AI GitHub operations even if OpenAI is temporarily down,
    # but we definitely log a strong warning.
    logging.warning("OPENAI_API_KEY not set. AI features will fail.")

if not BOT_ADMIN:
    logging.error("BOT_ADMIN not set. Please set the GitHub username of the bot admin.")

if not BOT_FALLBACK_PAT:
    logging.error("BOT_FALLBACK_PAT not set. Please set a GitHub PAT with repo write permissions.")

