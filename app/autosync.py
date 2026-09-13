"""Sequential background synchronization while the Telegram bot is running."""

import asyncio
import logging

from aiogram import Bot

from app import db
from app.notifications import notify_rating_updates
from app.services.sync import sync_player


AUTO_SYNC_INTERVAL_SECONDS = 300
logger = logging.getLogger(__name__)


async def sync_tracked_players(bot: Bot, *, api_key: str | None = None) -> None:
    for account_id in db.get_tracked_account_ids():
        try:
            result = await sync_player(account_id, api_key=api_key)
            await notify_rating_updates(bot, account_id, result.rating_updates)
        except Exception as exc:
            logger.error("Autosync failed account_id=%s error=%s", account_id, type(exc).__name__)


async def run_autosync(bot: Bot, *, api_key: str | None = None) -> None:
    logger.info("Autosync started interval_seconds=%s", AUTO_SYNC_INTERVAL_SECONDS)
    try:
        while True:
            try:
                await sync_tracked_players(bot, api_key=api_key)
            except Exception as exc:
                logger.error("Autosync cycle failed error=%s", type(exc).__name__)
            await asyncio.sleep(AUTO_SYNC_INTERVAL_SECONDS)
    finally:
        logger.info("Autosync stopped")
