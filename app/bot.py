"""Minimal Telegram commands for Turbo Rating."""

import asyncio
from contextlib import suppress
import logging
import sqlite3

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand, Message
import httpx

from app import db
from app.autosync import run_autosync
from app.keyboards import (
    MAIN_KEYBOARD, UNLINKED_KEYBOARD, get_main_keyboard, RATING_BUTTON,
    STATS_BUTTON, TOP_BUTTON, MATCHES_BUTTON, SYNC_BUTTON, PROFILE_BUTTON, LINK_BUTTON,
    VIEW_TOP_BUTTON, RATING_HELP_BUTTON,
)
from app.services.accounts import parse_dota_account_id
from app.services.heroes import get_hero_name, load_heroes
from app.services.game_modes import get_game_mode_name
from app.services.sync import ensure_player, sync_player
from app.services.rating import initialize_rating


router = Router()
logger = logging.getLogger(__name__)
ADD_ACCOUNT_MESSAGE = "Сначала привяжите Dota-профиль."
LINK_PROMPT = "Привязка Dota\n\nОтправьте Friend ID или ссылку на профиль.\n\nНапример:\n165682118"
INVALID_ACCOUNT_MESSAGE = "Не удалось определить Dota аккаунт.\n\nОтправьте Friend ID, например:\n165682118"
ONBOARDING_MESSAGE = (
    "🏆 Turbo Rating\n\n"
    "Рейтинг Turbo-игр среди друзей.\n\n"
    "Как это работает:\n"
    "• стартовый рейтинг рассчитывается по последним 20 Turbo-матчам;\n"
    "• после подключения каждая новая Turbo-игра меняет рейтинг;\n"
    "• победа повышает рейтинг, поражение снижает;\n"
    "• чем выше рейтинг, тем сложнее его удерживать;\n"
    "• учитываются только матчи Turbo.\n\n"
    "Здесь можно смотреть своё место, статистику,\n"
    "последние матчи и общий топ.\n\n"
    "Чтобы начать, привяжите Dota-профиль."
)
RATING_HELP_MESSAGE = (
    "Как считается Turbo Rating\n\n"
    "При подключении берём до 20 последних Turbo-игр\n"
    "и по их результатам определяем стартовый рейтинг.\n\n"
    "Средняя точка — 1000 TR.\n\n"
    "После подключения:\n\n"
    "WIN → рейтинг растёт\n"
    "LOSE → рейтинг падает\n\n"
    "Изменение зависит от текущего рейтинга:\n"
    "чем выше игрок находится, тем больше нужно выигрывать,\n"
    "чтобы продолжать расти.\n\n"
    "KDA, убийства, GPM, XPM и другие личные показатели\n"
    "не влияют на рейтинг.\n\n"
    "Учитывается только результат команды."
)


class LinkDota(StatesGroup):
    waiting_for_account = State()


BOT_COMMANDS = [
    BotCommand(command="start", description="Начать работу"),
    BotCommand(command="add", description="Подключить Dota аккаунт"),
    BotCommand(command="rating", description="Рейтинг и последние игры"),
    BotCommand(command="stats", description="Статистика Turbo"),
    BotCommand(command="top", description="Таблица лидеров"),
    BotCommand(command="matches", description="Последние матчи"),
    BotCommand(command="sync", description="Обновить матчи"),
    BotCommand(command="profile", description="Мой профиль"),
]


def _player(message: Message):
    return db.get_telegram_player(message.from_user.id) if message.from_user else None


def _nickname(player: dict) -> str:
    return " ".join((player.get("nickname") or "имя недоступно").split())[:80]


def _count_label(count: int, forms: tuple[str, str, str]) -> str:
    if 11 <= count % 100 <= 14:
        word = forms[2]
    elif count % 10 == 1:
        word = forms[0]
    elif 2 <= count % 10 <= 4:
        word = forms[1]
    else:
        word = forms[2]
    return f"{count} {word}"


def _position(account_id: int) -> str:
    place = db.get_leaderboard_position(account_id)
    return f"#{place['position']}" if place else "пока нет"


def _leaderboard(account_id: int | None) -> str:
    leaderboard = db.get_leaderboard()
    if not leaderboard:
        return "Рейтинг игроков пока пуст."
    lines = []
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for position, player in enumerate(leaderboard, start=1):
        suffix = " ← вы" if player["account_id"] == account_id else ""
        lines.append(
            f"{medals.get(position, f'{position}.')} {_nickname(player)} — "
            f"{player['current_rating']:.0f}{suffix}"
        )
    place = db.get_leaderboard_position(account_id) if account_id is not None else None
    if place and place["position"] > len(leaderboard):
        lines.extend(["", "...", "", f"Ваше место:\n#{place['position']} — {place['current_rating']:.0f}"])
    return "\n".join(lines)


async def _linked_player(message: Message, state: FSMContext | None):
    if state is not None:
        await state.clear()
    player = _player(message)
    if player is None:
        await message.answer(ADD_ACCOUNT_MESSAGE, reply_markup=UNLINKED_KEYBOARD)
    return player


@router.message(CommandStart())
async def start_command(message: Message, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.clear()
    player = _player(message)
    if player is None:
        await message.answer(ONBOARDING_MESSAGE, reply_markup=UNLINKED_KEYBOARD)
        return
    account_id = player["account_id"]
    text = f"🏆 Turbo Rating\n\nРейтинг Turbo среди друзей.\n\n{_leaderboard(account_id)}"
    rating = db.get_rating(account_id)
    status = f"Ваш рейтинг: {rating['current_rating']:.0f}\nМесто: {_position(account_id)}" if rating else "Профиль привязан. Нажмите «Мой рейтинг», чтобы узнать свой рейтинг."
    await message.answer(f"{text}\n\n{status}", reply_markup=MAIN_KEYBOARD)


@router.message(F.text == RATING_HELP_BUTTON)
async def rating_help_command(message: Message, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.clear()
    await message.answer(
        RATING_HELP_MESSAGE,
        reply_markup=get_main_keyboard(_player(message) is not None),
    )


@router.message(F.text == LINK_BUTTON)
async def link_command(message: Message, state: FSMContext) -> None:
    await state.set_state(LinkDota.waiting_for_account)
    await message.answer(LINK_PROMPT, reply_markup=get_main_keyboard(_player(message) is not None))


@router.message(Command("add"))
async def add_command(
    message: Message, command: CommandObject, api_key: str | None = None,
    state: FSMContext | None = None,
) -> None:
    if state is not None:
        await state.set_state(LinkDota.waiting_for_account)
    if not command.args:
        await message.answer(LINK_PROMPT, reply_markup=get_main_keyboard(_player(message) is not None))
        return
    await _connect_account(message, command.args, api_key, state)


async def _connect_account(
    message: Message, text: str, api_key: str | None, state: FSMContext | None,
) -> None:
    if message.from_user is None:
        return
    account_id = parse_dota_account_id(text)
    if account_id is None:
        await message.answer(INVALID_ACCOUNT_MESSAGE)
        return

    try:
        profile = await ensure_player(account_id, api_key=api_key)
        db.link_telegram_user(message.from_user.id, account_id)
        rating = db.get_rating(account_id)
        place = _position(account_id)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            await message.answer("Профиль не найден. Проверьте Friend ID и отправьте ещё раз.")
        elif exc.response.status_code == 429:
            await message.answer("Сейчас слишком много запросов. Попробуйте позже.")
        else:
            await message.answer("Не удалось загрузить профиль. Попробуйте позже.")
        return
    except httpx.RequestError:
        await message.answer("Не удалось загрузить профиль. Попробуйте позже.")
        return
    except ValueError:
        await message.answer("Не удалось подключить профиль. Проверьте Friend ID и попробуйте ещё раз.")
        return
    except sqlite3.Error:
        await message.answer("Не удалось сохранить аккаунт и рейтинг. Попробуйте позже.")
        return

    if state is not None:
        await state.clear()
    count = rating["calibration_matches"]
    if count == 1:
        explanation = "Стартовый рейтинг рассчитан по последнему Turbo-матчу."
    elif count:
        explanation = f"Стартовый рейтинг рассчитан по последним {count} Turbo-матчам."
    else:
        explanation = "Предыдущих Turbo-матчей пока нет. Стартовый рейтинг: 1000."
    await message.answer(
        f"Готово.\n\n{_nickname({'nickname': profile.get('personaname')})}\n"
        f"Turbo Rating: {rating['current_rating']:.0f}\nМесто: {place}\n\n"
        f"Старт: {rating['initial_rating']:.0f}\n{explanation}",
        reply_markup=MAIN_KEYBOARD,
    )


@router.message(Command("top"))
@router.message(F.text.in_({TOP_BUTTON, VIEW_TOP_BUTTON, "🥇 Топ"}))
async def top_command(message: Message, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.clear()
    player = _player(message)
    await message.answer(
        f"🥇 Turbo Rating\n\n{_leaderboard(player['account_id'] if player else None)}",
        reply_markup=get_main_keyboard(player is not None),
    )


@router.message(Command("profile"))
@router.message(F.text == PROFILE_BUTTON)
async def profile_command(message: Message, api_key: str | None = None, state: FSMContext | None = None) -> None:
    player = await _linked_player(message, state)
    if player is None:
        return

    rating = await _load_rating(message, player["account_id"], api_key)
    if rating is None:
        return

    stats = db.get_turbo_stats(player["account_id"])
    await message.answer(
        f"👤 Профиль\n\n{_nickname(player)}\n\n"
        f"Dota ID: {player['account_id']}\n"
        f"Turbo Rating: {rating['current_rating']:.0f}\n"
        f"Место: {_position(player['account_id'])}\n\n"
        f"Turbo игр: {stats['matches']}\nWinrate: {stats['winrate']:.1f}%",
        reply_markup=MAIN_KEYBOARD,
    )


async def _load_rating(message: Message, account_id: int, api_key: str | None):
    try:
        return await initialize_rating(account_id, api_key=api_key)
    except (httpx.HTTPError, sqlite3.Error, ValueError):
        await message.answer("Не удалось загрузить рейтинг. Попробуйте позже.")
        return None


@router.message(Command("rating"))
@router.message(F.text.in_({RATING_BUTTON, "🏆 Рейтинг"}))
async def rating_command(message: Message, api_key: str | None = None, state: FSMContext | None = None) -> None:
    player = await _linked_player(message, state)
    if player is None:
        return
    rating = await _load_rating(message, player["account_id"], api_key)
    if rating is None:
        return
    stats = db.get_turbo_stats(player["account_id"])
    text = (
        f"🏆 Мой рейтинг\n\n{_nickname(player)}\n\n"
        f"{rating['current_rating']:.0f} TR\nМесто: {_position(player['account_id'])}\n\n"
        f"Старт: {rating['initial_rating']:.0f}\n"
        f"Изменение: {rating['current_rating'] - rating['initial_rating']:+.0f}\n\n"
        "Turbo после регистрации:\n"
        f"{_count_label(stats['matches'], ('игра', 'игры', 'игр'))} · "
        f"{_count_label(stats['wins'], ('победа', 'победы', 'побед'))} · "
        f"{_count_label(stats['losses'], ('поражение', 'поражения', 'поражений'))}\n"
        f"Winrate: {stats['winrate']:.1f}%"
    )
    if stats["unknown"]:
        text += f"\nБез результата: {stats['unknown']}"
    history = db.get_rating_history(player["account_id"], limit=5)
    if history:
        text += "\n\nПоследние изменения:\n\n" + "\n".join(
            f"{game['rating_delta']:+.0f}  {get_hero_name(game['hero_id'])}" for game in history
        )
    else:
        text += "\n\nПосле регистрации пока нет учтённых Turbo-матчей."
    await message.answer(text, reply_markup=MAIN_KEYBOARD)


@router.message(Command("stats"))
@router.message(F.text == STATS_BUTTON)
async def stats_command(message: Message, api_key: str | None = None, state: FSMContext | None = None) -> None:
    player = await _linked_player(message, state)
    if player is None:
        return
    rating = await _load_rating(message, player["account_id"], api_key)
    if rating is None:
        return
    stats = db.get_turbo_stats(player["account_id"])
    text = (
        f"📊 Статистика\n\n{_nickname(player)}\n\n"
        f"Turbo Rating: {rating['current_rating']:.0f}\n"
        f"Стартовый рейтинг: {rating['initial_rating']:.0f}\n\n"
        f"Turbo матчей: {stats['matches']}\n"
        f"Победы: {stats['wins']}\nПоражения: {stats['losses']}\n"
        f"Winrate: {stats['winrate']:.1f}%"
    )
    if stats["unknown"]:
        text += f"\nБез результата: {stats['unknown']}"
    text += f"\n\nИзменение рейтинга:\n{rating['current_rating'] - rating['initial_rating']:+.0f} TR"
    await message.answer(text, reply_markup=MAIN_KEYBOARD)


@router.message(Command("matches"))
@router.message(F.text == MATCHES_BUTTON)
async def matches_command(message: Message, state: FSMContext | None = None) -> None:
    player = await _linked_player(message, state)
    if player is None:
        return

    matches = db.get_player_matches(player["account_id"], limit=5)
    if not matches:
        await message.answer("Матчей пока нет. Нажмите «Обновить» после игры.", reply_markup=MAIN_KEYBOARD)
        return

    lines = ["🎮 Последние матчи"]
    for match in matches:
        result = {1: "WIN", 0: "LOSE", None: "Результат пока неизвестен"}[match["win"]]
        mode = get_game_mode_name(match["game_mode"])
        duration = f"{match['duration'] / 60:.0f} мин" if match["duration"] is not None else "? мин"
        lines.append(f"{result}\n{get_hero_name(match['hero_id'])} · {mode} · {duration}")
    await message.answer("\n\n".join(lines), reply_markup=MAIN_KEYBOARD)


@router.message(Command("sync"))
@router.message(F.text == SYNC_BUTTON)
async def sync_command(message: Message, api_key: str | None = None, state: FSMContext | None = None) -> None:
    player = await _linked_player(message, state)
    if player is None:
        return

    try:
        result = await sync_player(player["account_id"], api_key=api_key)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            await message.answer("Сейчас слишком много запросов. Попробуйте позже.")
        else:
            await message.answer("Не удалось обновить матчи. Попробуйте позже.")
        return
    except httpx.RequestError:
        await message.answer("Не удалось обновить матчи. Попробуйте позже.")
        return
    except (sqlite3.Error, ValueError):
        await message.answer("Не удалось обновить матчи. Попробуйте позже.")
        return

    if not result.rating_updates:
        rating = db.get_rating(player["account_id"])
        await message.answer(
            f"Данные актуальны.\n\nTurbo Rating: {rating['current_rating']:.0f}",
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        updates = result.rating_updates
        lines = [f"Обновлено.\n\nНовых Turbo: {len(updates)}", ""]
        lines.extend(f"{'WIN' if u.win else 'LOSE'}  {u.rating_delta:+.0f}" for u in updates[:50])
        if len(updates) > 50:
            lines.append(f"… ещё матчей: {len(updates) - 50}")
        lines.extend(["", f"Rating:\n{updates[0].rating_before:.0f} → {updates[-1].rating_after:.0f}"])
        await message.answer("\n".join(lines), reply_markup=MAIN_KEYBOARD)


# Register after menu handlers so navigation is never consumed as a Friend ID.
@router.message(LinkDota.waiting_for_account, F.text, ~F.text.startswith("/"))
async def account_input(message: Message, state: FSMContext, api_key: str | None = None) -> None:
    await _connect_account(message, message.text, api_key, state)


@router.message(LinkDota.waiting_for_account)
async def invalid_account_input(message: Message) -> None:
    await message.answer(INVALID_ACCOUNT_MESSAGE)


async def run_bot(token: str, api_key: str | None = None) -> None:
    db.init_db()
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
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
