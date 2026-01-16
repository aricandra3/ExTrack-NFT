import os
from dotenv import load_dotenv

load_dotenv()

# Telegram Bot Token
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

# OpenSea API Key (optional)
OPENSEA_API_KEY = os.getenv("OPENSEA_API_KEY", "")

# OpenSea API Base URL
OPENSEA_API_BASE_URL = "https://api.opensea.io/api/v2"

# Check interval for price alerts (in seconds)
ALERT_CHECK_INTERVAL = 300  # 5 minutes

# Database file
DATABASE_FILE = "nft_tracker.db"
