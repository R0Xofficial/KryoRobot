import os

TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN")

DB_NAME = os.getenv("DB_NAME", "data/gbanbotdata.db")
APPEAL_CHAT_USERNAME = os.getenv("APPEAL_CHAT_USERNAME", "@YourAppealChat")

OWNER_ID = int(os.getenv("OWNER_ID", "123456789"))
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID", "-100123456789"))

# --- HEARTBEAT CONFIG ---
HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "False").lower() in ("true", "1", "yes")
HEARTBEAT_CHAT_ID = int(os.getenv("HEARTBEAT_CHAT_ID", "-100123456789"))
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "30"))
