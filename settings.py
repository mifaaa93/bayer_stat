"""Read deployment settings from the local, ignored .env file."""

import logging
import os
from datetime import timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

DATABASE = os.getenv("DATABASE_PATH", "data/bot.sqlite3")
LOG_PATH = os.getenv("LOG_PATH") or str(Path(DATABASE).expanduser().resolve().with_name("bot.log"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
REASONING_EFFORT = os.getenv("OPENAI_REASONING_EFFORT", "").strip() or None
TIMEZONE = timezone(timedelta(hours=2))

_LOGGING_CONFIGURED = False


def admin_ids() -> set[int]:
    return {int(item.strip()) for item in os.getenv("ADMIN_IDS", "").split(",") if item.strip()}


def configure_logging() -> Path:
    """Log to stderr and a rotating file under data/ (or LOG_PATH)."""
    global _LOGGING_CONFIGURED
    path = Path(LOG_PATH).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    path.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, LOG_LEVEL, logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )

    root = logging.getLogger()
    if _LOGGING_CONFIGURED:
        root.setLevel(level)
        return path

    root.setLevel(level)
    if not any(isinstance(handler, logging.StreamHandler)
               and not isinstance(handler, logging.FileHandler)
               for handler in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)

    file_handler = RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    _LOGGING_CONFIGURED = True
    logging.getLogger(__name__).info("Logging to file %s level=%s", path, logging.getLevelName(level))
    return path