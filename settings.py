"""Read deployment settings from the local, ignored .env file."""

import os
from datetime import timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

DATABASE = os.getenv("DATABASE_PATH", "data/bot.sqlite3")
TIMEZONE = timezone(timedelta(hours=2))


def admin_ids() -> set[int]:
    return {int(item.strip()) for item in os.getenv("ADMIN_IDS", "").split(",") if item.strip()}
