"""Minimal Telegram commands for Turbo Rating."""

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import logging
import sqlite3

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BotCommand, Message
import httpx

from app import db
from app.autosync import run_autosync
from app.notifications import format_rating_updates
from app.keyboards import MAIN_KEYBOARD, RATING_BUTTON, STATS_BUTTON, TOP_BUTTON, MATCHES_BUTTON, SYNC_BUTTON
from app.services.heroes import get_hero_name, load_heroes
from app.services.game_modes import get_game_mode_name
from app.services.sync import ensure_player, sync_player
from app.services.rating import initialize_rating


router = Router()
logger = logging.getLogger(__name__)
ADD_ACCOUNT_MESSAGE = "Сначала добавьте аккаунт: /add <Dota ID>"
BOT_COMMANDS = [
    BotCommand(command="start", description="Начать работу"),
    BotCommand(command="add", description="Подключить Dota аккаунт"),
    BotCommand(command="rating", description="Рейтинг и последние игры"),
    BotCommand(command="stats", description="Статистика Turbo"),
    BotCommand(command="top", description="Таблица лидеров"),
    BotCommand(command="matches", description="Последние матчи"),
    BotCommand(command="sync", description="Обновить матчи"),
]


@router.message(CommandStart())
async def start_command(message: Message) -> None:
    await message.answer(
        "Turbo Rating\n\nДобавь свой Dota аккаунт:\n\n/add 123456789",
        reply_markup=MAIN_KEYBOARD,
    )


@router.message(Command("add"))
async def add_command(
    message: Message, command: CommandObject, api_key: str | None = None
) -> None:
    if message.from_user is None:
        return
    try:
        account_id = int(command.args or "")
        if not 0 < account_id < 2**32:
            raise ValueError
    except ValueError:
        await message.answer("Используйте /add <Dota ID>, например /add 123456789. Нужен Steam32.")
        return

    try:
        profile = await ensure_player(account_id, api_key=api_key)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            await message.answer("Игрок не найден в OpenDota. Проверьте Dota ID.")
        elif exc.response.status_code == 429:
            await message.answer("Лимит запросов OpenDota исчерпан. Попробуйте позже.")
        else:
            await message.answer("OpenDota сейчас недоступен. Попробуйте позже.")
        return
    except httpx.RequestError:
        await message.answer("Не удалось получить профиль из OpenDota. Попробуйте позже.")
        return
    except ValueError as exc:
        await message.answer(str(exc))
        return
    except sqlite3.Error:
        await message.answer("Не удалось сохранить аккаунт и рейтинг. Попробуйте позже.")
        return

    nickname = profile.get("personaname") or "имя недоступно"
    db.link_telegram_user(message.from_user.id, account_id)
    rating = db.get_rating(account_id)
    await message.answer(
        f"Аккаунт подключён.\n\nИгрок: {nickname}\nDota ID: {account_id}\n"
        f"Turbo Rating: {rating['current_rating']:.0f}",
        reply_markup=MAIN_KEYBOARD,
    )


@router.message(Command("top"))
@router.message(F.text == TOP_BUTTON)
async def top_command(message: Message) -> None:
    leaderboard = db.get_leaderboard()
    if not leaderboard:
        await message.answer("🏆 Turbo Rating\n\nРейтинг игроков пока пуст.")
        return
    lines = ["🏆 Turbo Rating", ""]
    current_player = db.get_telegram_player(message.from_user.id) if message.from_user else None
    current_account = current_player["account_id"] if current_player else None
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for position, player in enumerate(leaderboard, start=1):
        nickname = " ".join((player["nickname"] or "имя недоступно").splitlines())[:80]
        prefix = medals.get(position, f"{position}.")
        if player["account_id"] == current_account:
            prefix = f"👉 {prefix}"
        lines.append(f"{prefix} {nickname} — {player['current_rating']:.0f}")
    place = db.get_leaderboard_position(current_account) if current_account is not None else None
    if place and place["position"] > len(leaderboard):
        lines.extend(["", "...", "", f"Ваше место: #{place['position']} — {place['current_rating']:.0f}"])
    await message.answer("\n".join(lines))


@router.message(Command("profile"))
async def profile_command(message: Message, api_key: str | None = None) -> None:
    if message.from_user is None:
        return
    player = db.get_telegram_player(message.from_user.id)
    if player is None:
        await message.answer(ADD_ACCOUNT_MESSAGE)
        return

    rating = await _load_rating(message, player["account_id"], api_key)
    if rating is None:
        return

    tracking_date = datetime.fromtimestamp(
        player["tracking_started_at"], tz=timezone.utc
    ).strftime("%d.%m.%Y")
    stats = db.get_turbo_stats(player["account_id"])
    await message.answer(
        f"👤 Профиль\n\n{player['nickname'] or 'имя недоступно'}\n"
        f"Dota ID: {player['account_id']}\n\n"
        f"🏆 Turbo Rating: {rating['current_rating']:.0f}\n"
        f"📊 Turbo игр: {stats['matches']}\n"
        f"📈 Winrate: {stats['winrate']:.1f}%\n\n"
        f"Отслеживание с:\n{tracking_date}"
    )


async def _load_rating(message: Message, account_id: int, api_key: str | None):
    try:
        return await initialize_rating(account_id, api_key=api_key)
    except (httpx.HTTPError, sqlite3.Error, ValueError):
        await message.answer("Не удалось инициализировать рейтинг. Попробуйте позже.")
        return None


@router.message(Command("rating"))
@router.message(F.text == RATING_BUTTON)
async def rating_command(message: Message, api_key: str | None = None) -> None:
    if message.from_user is None:
        return
    player = db.get_telegram_player(message.from_user.id)
    if player is None:
        await message.answer(ADD_ACCOUNT_MESSAGE)
        return
    rating = await _load_rating(message, player["account_id"], api_key)
    if rating is None:
        return
    text = (
        "🏆 Turbo Rating\n\n"
        f"{player['nickname'] or 'имя недоступно'}\n\n"
        f"Rating: {rating['current_rating']:.0f}\n"
        f"Старт: {rating['initial_rating']:.0f}\n"
        f"Изменение: {rating['current_rating'] - rating['initial_rating']:+.0f} TR"
    )
    history = db.get_rating_history(player["account_id"], limit=5)
    if history:
        text += "\n\nПоследние игры:\n\n" + "\n".join(
            f"{'🟢' if game['result'] else '🔴'} {get_hero_name(game['hero_id'])}  "
            f"{game['rating_delta']:+.0f} TR" for game in history
        )
    else:
        text += "\n\nПосле регистрации пока нет учтённых Turbo-матчей."
    await message.answer(text)


@router.message(Command("stats"))
@router.message(F.text == STATS_BUTTON)
async def stats_command(message: Message, api_key: str | None = None) -> None:
    if message.from_user is None:
        return
    player = db.get_telegram_player(message.from_user.id)
    if player is None:
        await message.answer(ADD_ACCOUNT_MESSAGE)
        return
    rating = await _load_rating(message, player["account_id"], api_key)
    if rating is None:
        return
    stats = db.get_turbo_stats(player["account_id"])
    text = (
        f"📊 Статистика\n\n{player['nickname'] or 'имя недоступно'}\n\n"
        f"Turbo Rating: {rating['current_rating']:.0f}\n"
        f"Стартовый рейтинг: {rating['initial_rating']:.0f}\n\n"
        f"Turbo матчей: {stats['matches']}\n"
        f"Победы: {stats['wins']}\nПоражения: {stats['losses']}\n"
        f"Winrate: {stats['winrate']:.1f}%"
    )
    if stats["unknown"]:
        text += f"\nБез результата: {stats['unknown']}"
    text += f"\n\nИзменение рейтинга:\n{rating['current_rating'] - rating['initial_rating']:+.0f} TR"
    await message.answer(text)


@router.message(Command("matches"))
@router.message(F.text == MATCHES_BUTTON)
async def matches_command(message: Message) -> None:
    if message.from_user is None:
        return
    player = db.get_telegram_player(message.from_user.id)
    if player is None:
        await message.answer(ADD_ACCOUNT_MESSAGE)
        return

    matches = db.get_player_matches(player["account_id"], limit=5)
    if not matches:
        await message.answer("Сохранённых матчей пока нет.")
        return

    lines = ["🎮 Последние матчи"]
    for match in matches:
        result = {1: "WIN", 0: "LOSE", None: "UNKNOWN"}[match["win"]]
        icon = {1: "🟢", 0: "🔴", None: "⚪"}[match["win"]]
        mode = get_game_mode_name(match["game_mode"])
        duration = f"{match['duration'] / 60:.0f} мин" if match["duration"] is not None else "? мин"
        lines.append(f"{icon} {get_hero_name(match['hero_id'])}\n{result} · {mode} · {duration}")
    await message.answer("\n\n".join(lines))


@router.message(Command("sync"))
@router.message(F.text == SYNC_BUTTON)
async def sync_command(message: Message, api_key: str | None = None) -> None:
    if message.from_user is None:
        return
    player = db.get_telegram_player(message.from_user.id)
    if player is None:
        await message.answer(ADD_ACCOUNT_MESSAGE)
        return

    try:
        result = await sync_player(player["account_id"], api_key=api_key)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            await message.answer("Лимит запросов OpenDota исчерпан. Попробуйте позже.")
        else:
            await message.answer("OpenDota сейчас недоступен. Попробуйте позже.")
        return
    except httpx.RequestError:
        await message.answer("Не удалось получить матчи из OpenDota. Попробуйте позже.")
        return
    except (sqlite3.Error, ValueError):
        await message.answer("Не удалось синхронизировать матчи. Попробуйте позже.")
        return

    if not result.rating_updates:
        await message.answer("Синхронизация завершена.\nНовых Turbo-матчей нет.")
    else:
        await message.answer(
            "Синхронизация завершена.\n\n"
            f"Получено матчей: {result.received_count}\n"
            f"Новых матчей: {result.new_count}\n\n"
            f"{format_rating_updates(result.rating_updates, manual=True)}"
        )


async def run_bot(token: str, api_key: str | None = None) -> None:
    db.init_db()
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    async with Bot(token=token) as bot:
        logger.info("Bot starting")
        await load_heroes()
        await bot.set_my_commands(BOT_COMMANDS)
        task = asyncio.create_task(run_autosync(bot, api_key=api_key), name="turbo-autosync")
        try:
            await dispatcher.start_polling(bot, api_key=api_key, close_bot_session=False)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            logger.info("Bot stopped")
