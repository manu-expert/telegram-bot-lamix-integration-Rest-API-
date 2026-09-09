import os

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")

DB_PATH = os.getenv("DB_PATH", "bot.db")

ADMIN_IDS = {
    int(uid.strip()) for uid in os.getenv("ADMIN_IDS", "").split(",") if uid.strip()
}


def _parse_channels(raw: str):
    channels = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "|" in entry:
            chat_ref, label = entry.split("|", 1)
        else:
            chat_ref, label = entry, entry
        channels.append({"chat_ref": chat_ref.strip(), "label": label.strip()})
    return channels


# Format: "@main_channel|Main Channel,@backup_channel|Backup Channel"
REQUIRED_CHANNELS = _parse_channels(os.getenv("REQUIRED_CHANNELS", ""))

# Lamix agent API — client roster, ranges, numbers, assign/unassign, messages.
# See lamix_api.py.
LAMIX_API_TOKEN = os.getenv("LAMIX_API_TOKEN")
LAMIX_BASE_URL = os.getenv("LAMIX_BASE_URL", "https://panel.lamix.org")

# Channel where the automated traffic report gets posted every 10 minutes.
TRAFFIC_CHANNEL_ID = os.getenv("TRAFFIC_CHANNEL_ID")
