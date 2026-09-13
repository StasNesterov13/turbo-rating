"""Compact Telegram notifications for rating updates."""

import logging

from aiogram import Bot

from app import db
from app.services.sync import RatingUpdate
from app.services.heroes import get_hero_name


logger = logging.getLogger(__name__)
MAX_DETAIL_LINES = 50


def format_rating_updates(updates: list[RatingUpdate], *, manual: bool = False) -> str:
    if not updates:
        return ""
    before, after = updates[0].rating_before, updates[-1].rating_after
    if len(updates) == 1 and not manual:
        update = updates[0]
        title = "🟢 Победа в Turbo" if update.win else "🔴 Поражение в Turbo"
        return (
            f"{title}\n\n{get_hero_name(update.hero_id)}\n\n"
            f"{update.rating_delta:+.0f} TR\n{before:.0f} → {after:.0f}"
        )

    lines = [] if manual else [f"🎮 Новые Turbo-матчи: {len(updates)}", ""]
    shown = 0
    detail_units = 0
    for update in updates[:MAX_DETAIL_LINES]:
        icon, result = ("🟢", "WIN") if update.win else ("🔴", "LOSE")
        label = f"Turbo {result}: " if manual else ""
        line = f"{icon} {label}{get_hero_name(update.hero_id)}  {update.rating_delta:+.0f} TR"
        units = len((line + "\n").encode("utf-16-le")) // 2
        # Leave room for the summary and the manual /sync response prefix.
        if detail_units + units > 3400:
            break
        lines.append(line)
        detail_units += units
        shown += 1
    if len(updates) > shown:
        lines.append(f"… ещё матчей: {len(updates) - shown}")
    lines.extend(["", f"Rating: {before:.0f} → {after:.0f}"])
    return "\n".join(lines)


async def notify_rating_updates(bot: Bot, account_id: int, updates: list[RatingUpdate]) -> None:
    if not updates:
        return
    text = format_rating_updates(updates)
    for telegram_id in db.get_telegram_ids_by_account(account_id):
        try:
            await bot.send_message(telegram_id, text)
        except Exception as exc:
            logger.error(
                "Notification failed account_id=%s telegram_id=%s error=%s",
                account_id, telegram_id, type(exc).__name__,
            )
