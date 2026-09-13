"""Minimal Telegram commands for Turbo Rating."""

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import logging
import math
import sqlite3
from time import monotonic
from uuid import uuid4

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand, CallbackQuery, Message
import httpx

from app import db
from app.autosync import run_autosync
from app.keyboards import (
    MAIN_KEYBOARD, UNLINKED_KEYBOARD, get_main_keyboard, RATING_BUTTON,
    STATS_BUTTON, TOP_BUTTON, MATCHES_BUTTON, SYNC_BUTTON, PROFILE_BUTTON, LINK_BUTTON,
    VIEW_TOP_BUTTON, RATING_HELP_BUTTON,
    CHANGE_BUTTON, CHANGE_CONFIRM_PREFIX, CHANGE_CANCEL_PREFIX, get_change_keyboard,
    SHARE_BUTTON, HISTORY_BUTTON,
)
from app.services.accounts import InvalidDotaAccountError, link_dota_account
from app.services.heroes import load_heroes
from app.services.sync import MatchHistoryUnavailable, sync_player
from app.services.rating import initialize_rating
from app.screens import (
    format_home, format_rating, format_history, format_top, format_matches,
    format_profile, format_nickname as _nickname,
)


router = Router()
logger = logging.getLogger(__name__)
MANUAL_SYNC_COOLDOWN = 30
_sync_cooldowns: dict[int, float] = {}
_sync_in_progress: set[int] = set()
OPENDOTA_ERROR_MESSAGE = "OpenDota временно недоступен. Попробуйте позже."
HISTORY_UNAVAILABLE_MESSAGE = "Не удалось получить историю матчей.\nВключите Expose Public Match Data в Dota 2."
ADD_ACCOUNT_MESSAGE = "Сначала привяжите Dota-профиль."
LINK_PROMPT = "Привязка Dota\n\nОтправьте Friend ID или ссылку на профиль.\n\nНапример:\n165682118"
CHANGE_PROMPT = "Смена Dota-профиля\n\nОтправьте новый Friend ID или ссылку на профиль."
CHANGE_CANCELLED_MESSAGE = "Смена аккаунта отменена."
INVALID_ACCOUNT_MESSAGE = "Не удалось определить Dota аккаунт.\n\nОтправьте Friend ID, например:\n165682118"
ONBOARDING_MESSAGE = (
    "🏆 Turbo Rating\n\n"
    "Рейтинг Turbo-игр среди друзей.\n\n"
    "Чтобы начать, привяжите Dota-профиль."
)
RATING_HELP_MESSAGE = (
    "🏆 Как считается Turbo Rating\n\n"
    "Стартовый рейтинг считается по последним 20 Turbo-матчам.\n\n"
    "Например:\n"
    "10W / 10L → ~1000 TR\n"
    "14W / 6L → ~1095 TR\n"
    "8W / 12L → ~953 TR\n\n"
    "Дальше каждая новая Turbo-игра меняет рейтинг.\n\n"
    "Если у тебя 1000 TR:\n"
    "WIN → примерно +16\n"
    "LOSE → примерно -16\n\n"
    "Если ты уже поднялся до 1200 TR:\n"
    "WIN → примерно +8\n"
    "LOSE → примерно -24\n\n"
    "То есть чем выше твой TR, тем сложнее подниматься дальше и удерживать рейтинг.\n\n"
    "Учитывается только WIN / LOSE."
)


class LinkDota(StatesGroup):
    waiting_for_account = State()


class ChangeDota(StatesGroup):
    confirming = State()
    waiting_for_account = State()


BOT_COMMANDS = [
    BotCommand(command="start", description="Начать работу"),
    BotCommand(command="add", description="Подключить Dota аккаунт"),
    BotCommand(command="rating", description="Мой рейтинг"),
    BotCommand(command="history", description="История TR и движение в топе"),
    BotCommand(command="top", description="Таблица лидеров"),
    BotCommand(command="matches", description="Последние матчи"),
    BotCommand(command="sync", description="Обновить матчи"),
    BotCommand(command="profile", description="Мой профиль"),
]


def _player(message: Message):
    return db.get_telegram_player(message.from_user.id) if message.from_user else None


def _position(account_id: int) -> str:
    place = db.get_leaderboard_position(account_id)
    return f"#{place['position']}" if place else "пока нет"


def _history_cutoffs() -> dict[str, int]:
    now = datetime.now(timezone.utc)
    return {
        "Сегодня": int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()),
        "7 дней": int((now - timedelta(days=7)).timestamp()),
        "30 дней": int((now - timedelta(days=30)).timestamp()),
    }


def _service_error(exc: Exception, fallback: str) -> str:
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    logger.error("Bot service request failed error=%s status=%s", type(exc).__name__, status)
    if isinstance(exc, MatchHistoryUnavailable):
        return HISTORY_UNAVAILABLE_MESSAGE
    if isinstance(exc, httpx.HTTPError):
        return OPENDOTA_ERROR_MESSAGE
    return fallback


def _leaderboard(account_id: int | None) -> str:
    leaderboard = db.get_leaderboard()
    if not leaderboard:
        return format_top([], {}, account_id, None)
    past_positions = {row["account_id"]: row["position"] for row in db.get_leaderboard_at(_history_cutoffs()["7 дней"])}
    place = db.get_leaderboard_position(account_id) if account_id is not None else None
    return format_top(leaderboard, past_positions, account_id, place)


async def _exit_account_input(message: Message, state: FSMContext | None) -> None:
    if state is not None:
        changing = await state.get_state() in (ChangeDota.confirming.state, ChangeDota.waiting_for_account.state)
        await state.clear()
        if changing:
            await message.answer(
                CHANGE_CANCELLED_MESSAGE,
                reply_markup=get_main_keyboard(_player(message) is not None),
            )


async def _linked_player(message: Message, state: FSMContext | None):
    await _exit_account_input(message, state)
    player = _player(message)
    if player is None:
        await message.answer(ADD_ACCOUNT_MESSAGE, reply_markup=UNLINKED_KEYBOARD)
    return player


@router.message(CommandStart())
async def start_command(message: Message, state: FSMContext | None = None) -> None:
    await _exit_account_input(message, state)
    player = _player(message)
    if player is None:
        await message.answer(ONBOARDING_MESSAGE, reply_markup=UNLINKED_KEYBOARD)
        return
    account_id = player["account_id"]
    rating = db.get_rating(account_id)
    await message.answer(
        format_home(player, rating, _position(account_id) if rating else "пока нет"),
        reply_markup=MAIN_KEYBOARD,
    )


@router.message(F.text == SHARE_BUTTON)
async def share_command(message: Message, state: FSMContext | None = None) -> None:
    await _exit_account_input(message, state)
    me = await message.bot.me()
    await message.answer(
        f"🏆 Turbo Rating — рейтинг Turbo среди друзей.\nПрисоединяйся: https://t.me/{me.username}",
        reply_markup=get_main_keyboard(_player(message) is not None),
    )


@router.message(F.text == RATING_HELP_BUTTON)
async def rating_help_command(message: Message, state: FSMContext | None = None) -> None:
    await _exit_account_input(message, state)
    await message.answer(
        RATING_HELP_MESSAGE,
        reply_markup=get_main_keyboard(_player(message) is not None),
    )


@router.message(F.text == LINK_BUTTON)
async def link_command(message: Message, state: FSMContext) -> None:
    if _player(message) is not None:
        await change_command(message, state)
        return
    await state.set_state(LinkDota.waiting_for_account)
    await message.answer(LINK_PROMPT, reply_markup=get_main_keyboard(_player(message) is not None))


@router.message(F.text == CHANGE_BUTTON)
async def change_command(message: Message, state: FSMContext) -> None:
    player = _player(message)
    if player is None:
        await state.clear()
        await message.answer(ADD_ACCOUNT_MESSAGE, reply_markup=UNLINKED_KEYBOARD)
        return
    token = uuid4().hex
    await state.set_data({"change_token": token})
    await state.set_state(ChangeDota.confirming)
    await message.answer(
        f"Сейчас подключён:\n\n{_nickname(player)}\nDota ID: {player['account_id']}\n\n"
        "Хотите привязать другой Dota-профиль?",
        reply_markup=get_change_keyboard(token, confirm=True),
    )


@router.callback_query(F.data.startswith(CHANGE_CONFIRM_PREFIX))
@router.callback_query(F.data.startswith(CHANGE_CANCEL_PREFIX))
async def change_callback(callback: CallbackQuery, state: FSMContext) -> None:
    current = await state.get_state()
    data = await state.get_data()
    token = data.get("change_token")
    confirming = callback.data == CHANGE_CONFIRM_PREFIX + token if token else False
    valid = (
        token and current in (ChangeDota.confirming.state, ChangeDota.waiting_for_account.state)
        and callback.data in (CHANGE_CONFIRM_PREFIX + token, CHANGE_CANCEL_PREFIX + token)
        and (not confirming or current == ChangeDota.confirming.state)
        and isinstance(callback.message, Message)
    )
    if not valid:
        await callback.answer("Этот диалог уже закрыт. Откройте «Сменить Dota» снова.")
        return
    await callback.answer()
    if confirming:
        await state.set_state(ChangeDota.waiting_for_account)
        await callback.message.edit_text(CHANGE_PROMPT, reply_markup=get_change_keyboard(token))
    else:
        await state.clear()
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            CHANGE_CANCELLED_MESSAGE,
            reply_markup=get_main_keyboard(db.get_telegram_player(callback.from_user.id) is not None),
        )


@router.message(ChangeDota.confirming, Command("cancel"))
@router.message(ChangeDota.waiting_for_account, Command("cancel"))
@router.message(ChangeDota.confirming, F.text == "Отмена")
@router.message(ChangeDota.waiting_for_account, F.text == "Отмена")
async def cancel_change_command(message: Message, state: FSMContext) -> None:
    await _exit_account_input(message, state)


@router.message(Command("add"))
async def add_command(
    message: Message, command: CommandObject, api_key: str | None = None,
    state: FSMContext | None = None,
) -> None:
    linked = _player(message) is not None
    if linked and not command.args and state is not None:
        await change_command(message, state)
        return
    if state is not None:
        await state.set_data({"change_token": uuid4().hex} if linked else {})
        await state.set_state(ChangeDota.waiting_for_account if linked else LinkDota.waiting_for_account)
    if not command.args:
        await message.answer(CHANGE_PROMPT if linked else LINK_PROMPT, reply_markup=get_main_keyboard(linked))
        return
    await _connect_account(message, command.args, api_key, state)


async def _connect_account(
    message: Message, text: str, api_key: str | None, state: FSMContext | None,
) -> None:
    if message.from_user is None:
        return
    data = await state.get_data() if state is not None else {}
    token = data.get("change_token")
    error_markup = get_change_keyboard(token) if token else None

    try:
        result = await link_dota_account(message.from_user.id, text, api_key=api_key)
    except InvalidDotaAccountError:
        await message.answer(INVALID_ACCOUNT_MESSAGE, reply_markup=error_markup)
        return
    except (httpx.HTTPError, MatchHistoryUnavailable) as exc:
        error = _service_error(exc, OPENDOTA_ERROR_MESSAGE)
        if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
            error = "Профиль не найден. Проверьте Friend ID и отправьте ещё раз."
        await message.answer(error, reply_markup=error_markup)
        return
    except (ValueError, sqlite3.Error) as exc:
        fallback = "Не удалось подключить профиль. Проверьте Friend ID и попробуйте ещё раз." if isinstance(exc, ValueError) else "Не удалось сохранить аккаунт и рейтинг. Попробуйте позже."
        await message.answer(_service_error(exc, fallback), reply_markup=error_markup)
        return

    if state is not None:
        await state.clear()
    rating = result.rating
    if result.unchanged:
        status = f"Turbo Rating: {rating['current_rating']:.0f}" if rating else "Рейтинг пока не рассчитан."
        await message.answer(
            f"Этот Dota-профиль уже подключён.\n\n{_nickname(result.player)}\n{status}",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    account_id = result.player["account_id"]
    place = _position(account_id)
    if result.previous_account_id is not None:
        await message.answer(
            f"Готово.\n\nТеперь подключён:\n\n{_nickname(result.player)}\n"
            f"Dota ID: {account_id}\n\nTurbo Rating: {rating['current_rating']:.0f}\nМесто: {place}",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    count = rating["calibration_matches"]
    if count == 1:
        explanation = "Стартовый рейтинг рассчитан по последнему Turbo-матчу."
    elif count:
        explanation = f"Стартовый рейтинг рассчитан по последним {count} Turbo-матчам."
    else:
        explanation = "Предыдущих Turbo-матчей пока нет. Стартовый рейтинг: 1000."
    await message.answer(
        f"Готово.\n\n{_nickname(result.player)}\n"
        f"Turbo Rating: {rating['current_rating']:.0f}\nМесто: {place}\n\n"
        f"Старт: {rating['initial_rating']:.0f}\n{explanation}",
        reply_markup=MAIN_KEYBOARD,
    )


@router.message(Command("top"))
@router.message(F.text.in_({TOP_BUTTON, VIEW_TOP_BUTTON, "🥇 Топ игроков"}))
async def top_command(message: Message, state: FSMContext | None = None) -> None:
    await _exit_account_input(message, state)
    player = _player(message)
    await message.answer(
        _leaderboard(player['account_id'] if player else None),
        reply_markup=get_main_keyboard(player is not None),
    )


@router.message(Command("profile"))
@router.message(F.text == PROFILE_BUTTON)
async def profile_command(message: Message, state: FSMContext | None = None) -> None:
    player = await _linked_player(message, state)
    if player is None:
        return

    await message.answer(format_profile(player), reply_markup=MAIN_KEYBOARD)


async def _load_rating(message: Message, account_id: int, api_key: str | None):
    try:
        return await initialize_rating(account_id, api_key=api_key)
    except (httpx.HTTPError, sqlite3.Error, ValueError) as exc:
        await message.answer(_service_error(exc, "Не удалось загрузить рейтинг. Попробуйте позже."))
        return None


@router.message(Command("rating", "stats"))
@router.message(F.text.in_({RATING_BUTTON, "🏆 Рейтинг", STATS_BUTTON}))
async def rating_command(message: Message, api_key: str | None = None, state: FSMContext | None = None) -> None:
    player = await _linked_player(message, state)
    if player is None:
        return
    rating = await _load_rating(message, player["account_id"], api_key)
    if rating is None:
        return
    text = format_rating(
        rating, _position(player['account_id']), db.get_peak_rating(player['account_id']),
    )
    await message.answer(text, reply_markup=MAIN_KEYBOARD)


@router.message(Command("history"))
@router.message(F.text == HISTORY_BUTTON)
async def history_command(message: Message, api_key: str | None = None, state: FSMContext | None = None) -> None:
    player = await _linked_player(message, state)
    if player is None:
        return
    account_id = player["account_id"]
    rating = await _load_rating(message, account_id, api_key)
    if rating is None:
        return
    cutoffs = _history_cutoffs()
    current_place = _position(account_id)
    changes = {label: db.get_rating_change(account_id, since) for label, since in cutoffs.items()}
    past_positions = {label: db.get_rank_at(account_id, cutoffs[label]) for label in ("7 дней", "30 дней")}
    history = db.get_rating_history(account_id, limit=10, by_recorded_time=True)
    await message.answer(
        format_history(changes, past_positions, current_place, history), reply_markup=MAIN_KEYBOARD,
    )


@router.message(Command("matches"))
@router.message(F.text == MATCHES_BUTTON)
async def matches_command(message: Message, state: FSMContext | None = None) -> None:
    player = await _linked_player(message, state)
    if player is None:
        return

    matches = db.get_player_matches(player["account_id"], limit=5)
    await message.answer(format_matches(matches), reply_markup=MAIN_KEYBOARD)


@router.message(Command("sync"))
@router.message(F.text == SYNC_BUTTON)
async def sync_command(message: Message, api_key: str | None = None, state: FSMContext | None = None) -> None:
    player = await _linked_player(message, state)
    if player is None:
        return

    telegram_id = message.from_user.id
    remaining = math.ceil(_sync_cooldowns.get(telegram_id, 0) - monotonic())
    if remaining > 0:
        await message.answer(
            f"Данные недавно обновлялись.\nПопробуйте через {remaining} сек.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    if telegram_id in _sync_in_progress:
        await message.answer("Обновление уже выполняется. Подождите немного.", reply_markup=MAIN_KEYBOARD)
        return
    _sync_in_progress.add(telegram_id)
    _sync_cooldowns[telegram_id] = monotonic() + MANUAL_SYNC_COOLDOWN
    succeeded = False
    try:
        result = await sync_player(player["account_id"], api_key=api_key)
        succeeded = True
        _sync_cooldowns[telegram_id] = monotonic() + MANUAL_SYNC_COOLDOWN
    except (httpx.HTTPError, sqlite3.Error, ValueError) as exc:
        fallback = "Не удалось обновить матчи. Попробуйте позже." if isinstance(exc, sqlite3.Error) else OPENDOTA_ERROR_MESSAGE
        await message.answer(_service_error(exc, fallback), reply_markup=MAIN_KEYBOARD)
        return
    finally:
        _sync_in_progress.discard(telegram_id)
        if not succeeded:
            _sync_cooldowns.pop(telegram_id, None)

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
@router.message(ChangeDota.waiting_for_account, F.text, ~F.text.startswith("/"))
async def account_input(message: Message, state: FSMContext, api_key: str | None = None) -> None:
    await _connect_account(message, message.text, api_key, state)


@router.message(LinkDota.waiting_for_account)
async def invalid_account_input(message: Message) -> None:
    await message.answer(INVALID_ACCOUNT_MESSAGE)


@router.message(ChangeDota.confirming)
@router.message(ChangeDota.waiting_for_account)
async def other_change_input(message: Message, state: FSMContext) -> None:
    if await state.get_state() == ChangeDota.confirming.state or (message.text or "").startswith("/"):
        await _exit_account_input(message, state)
    else:
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
