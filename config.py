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
ALERT_CHECK_INTERVAL = 120  # 2 minutes

# Price history recording interval (in seconds)
PRICE_HISTORY_INTERVAL = 3600  # 1 hour

# Cooldown for repeat volume spike alerts (in seconds)
VOLUME_ALERT_COOLDOWN_SECONDS = int(os.getenv("VOLUME_ALERT_COOLDOWN_SECONDS", "21600"))  # 6 hours

# Volume spike detection multiplier (e.g., 2.0 = 2x average)
VOLUME_SPIKE_MULTIPLIER = 2.0

# Etherscan API Key for gas price
ETHERSCAN_API_KEY = os.getenv("ETHERSCAN_API_KEY", "")

# PostgreSQL database URL. If set, the bot uses Postgres instead of SQLite.
DATABASE_URL = os.getenv("DATABASE_URL", "")

# SQLite database file used when DATABASE_URL is not set.
DATABASE_FILE = os.getenv("DATABASE_FILE", "nft_tracker.db")
