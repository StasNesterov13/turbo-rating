"""Compact Telegram notifications for rating updates."""

import logging

from aiogram import Bot

from app import db
from app.services.sync import RatingUpdate
from app.services.heroes import get_hero_name


logger = logging.getLogger(__name__)
MAX_DETAIL_LINES = 50


def format_rating_breakdown(update: RatingUpdate) -> str:
    if update.is_correction:
        return f"Performance: {update.rating_delta:+.0f}\nБаза уже учтена."
    text = f"База: {update.rating_delta - update.performance_bonus:+.0f}"
    if update.performance_bonus > 0:
        text += f"\nИгра: +{update.performance_bonus}"
    return text


def format_rating_updates(
    updates: list[RatingUpdate], *, manual: bool = False,
    position_before: int | None = None, position_after: int | None = None,
) -> str:
    if not updates:
        return ""
    before, after = updates[0].rating_before, updates[-1].rating_after
    position = ""
    if position_after is not None:
        if position_before is not None and position_before != position_after:
            position = f"\n\nМесто:\n#{position_before} → #{position_after}"
        else:
            position = f"\n\nМесто: #{position_after}"
    if len(updates) == 1 and not manual:
        update = updates[0]
        title = "🟢 Победа в Turbo" if update.win else "🔴 Поражение в Turbo"
        if update.is_correction:
            title = "Performance восстановлен"
        breakdown = "\n\n" + format_rating_breakdown(update)
        return (
            f"{title}\n\n"
            f"{update.rating_delta:+.0f} TR\n{before:.0f} → {after:.0f}{breakdown}{position}"
        )

    new_count = sum(not update.is_correction for update in updates)
    lines = [] if manual else [f"🎮 Новые Turbo-матчи: {new_count}"]
    if len(updates) != new_count:
        lines.append(f"Performance восстановлен: {len(updates) - new_count}")
    lines.append("")
    shown = 0
    detail_units = 0
    for update in updates[:MAX_DETAIL_LINES]:
        icon, result = ("🟢", "WIN") if update.win else ("🔴", "LOSE")
        label = "Performance: " if update.is_correction else (f"Turbo {result}: " if manual else "")
        line = f"{icon} {label}{get_hero_name(update.hero_id)}  {update.rating_delta:+.0f} TR"
        if update.performance_bonus > 0 and not update.is_correction:
            line += f" · performance +{update.performance_bonus}"
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
    return "\n".join(lines) + position


async def notify_rating_updates(bot: Bot, account_id: int, updates: list[RatingUpdate]) -> None:
    if not updates:
        return
    before = db.get_leaderboard_position(account_id, rating=updates[0].rating_before)
    after = db.get_leaderboard_position(account_id, rating=updates[-1].rating_after)
    text = format_rating_updates(
        updates, position_before=before["position"] if before else None,
        position_after=after["position"] if after else None,
    )
    for telegram_id in db.get_telegram_ids_by_account(account_id):
        try:
            await bot.send_message(telegram_id, text)
        except Exception as exc:
            logger.error(
                "Notification failed account_id=%s telegram_id=%s error=%s",
                account_id, telegram_id, type(exc).__name__,
            )
