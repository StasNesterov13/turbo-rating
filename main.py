"""Start the Turbo Rating Telegram bot."""

import asyncio
import logging
import os
from pathlib import Path
import sys

from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.utils.token import TokenValidationError
from dotenv import load_dotenv

from app.bot import run_bot


def main() -> int:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("Укажите TELEGRAM_BOT_TOKEN в .env для запуска бота.", file=sys.stderr)
        return 1

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # OpenDota uses api_key in the URL; don't log HTTP request URLs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        asyncio.run(run_bot(token, api_key=os.getenv("OPENDOTA_API_KEY") or None))
    except (TokenValidationError, TelegramUnauthorizedError):
        print("Некорректный TELEGRAM_BOT_TOKEN. Проверьте значение в .env.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
