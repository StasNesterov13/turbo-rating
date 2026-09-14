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

from app import db, season
from app.autosync import run_autosync
from app.notifications import format_rating_breakdown
from app.keyboards import (
    MAIN_KEYBOARD, UNLINKED_KEYBOARD, get_main_keyboard, RATING_BUTTON,
    STATS_BUTTON, TOP_BUTTON, MATCHES_BUTTON, SYNC_BUTTON, PROFILE_BUTTON, LINK_BUTTON,
    VIEW_TOP_BUTTON, RATING_HELP_BUTTON, TURBO_INFO_BUTTON, RATING_HELP_CALLBACK, get_info_keyboard,
    CHANGE_BUTTON, CHANGE_CONFIRM_PREFIX, CHANGE_CANCEL_PREFIX, get_change_keyboard,
    get_link_keyboard, FRIEND_CODE_HELP_CALLBACK, MATCH_HISTORY_HELP_CALLBACK, LINK_RETRY_PREFIX,
    SHARE_BUTTON, HISTORY_BUTTON, PRIZES_BUTTON,
)
from app.services.accounts import InvalidDotaAccountError, link_dota_account, parse_dota_account_id
from app.services.heroes import load_heroes
from app.services.sync import MatchHistoryUnavailable, is_sync_in_progress, sync_player
from app.services.rating import initialize_rating
from app.screens import (
    format_home, format_top, format_matches,
    format_profile, format_nickname as _nickname, format_prizes, format_season_results,
)


router = Router()
logger = logging.getLogger(__name__)
MANUAL_SYNC_COOLDOWN = 30
_sync_cooldowns: dict[int, float] = {}
_sync_in_progress: set[int] = set()
OPENDOTA_ERROR_MESSAGE = "OpenDota временно недоступен. Попробуйте позже."
HISTORY_UNAVAILABLE_MESSAGE = "Не удалось получить историю матчей.\nВключите Expose Public Match Data в Dota 2."
ADD_ACCOUNT_MESSAGE = "Сначала привяжите Dota-профиль."
LINK_PROMPT = (
    "<b>Подключение Dota 2</b>\n\n"
    "Чтобы бот мог находить твои матчи:\n\n"
    "1. Отправь свой код друга из Steam.\n"
    "2. В Dota 2 включи <code>Общедоступная история матчей</code>.\n\n"
    "Если включил её только что, нужно немного подождать, пока данные обновятся."
)
FRIEND_CODE_HELP_MESSAGE = (
    "<b>Как найти код друга в Steam</b>\n\n"
    "1. Открой Steam.\n"
    "2. Перейди в раздел <code>Друзья</code>.\n"
    "3. Выбери <code>Добавить друга</code>.\n"
    "4. Найди свой <code>Код друга</code>.\n"
    "5. Скопируй цифры и отправь их боту.\n\n"
    "Нужен именно код друга Steam, а не ник или ссылка на профиль."
)
MATCH_HISTORY_HELP_MESSAGE = (
    "<b>Как открыть историю матчей в Dota 2</b>\n\n"
    "1. Открой Dota 2.\n"
    "2. Нажми <code>Настройки</code> (шестерёнка).\n"
    "3. Перейди в раздел <code>Сообщество</code>.\n"
    "4. Включи <code>Общедоступная история матчей</code>.\n\n"
    "⏳ Если ты только что включил «Общедоступную историю матчей», данные обновятся не сразу. "
    "Подожди некоторое время и попробуй снова."
)
ACCOUNT_UNAVAILABLE_MESSAGE = (
    "Пока не удалось получить данные аккаунта. Проверь, включена ли в Dota 2 "
    "«Общедоступная история матчей». Если включил её только что — подожди немного и попробуй снова."
)
CHANGE_PROMPT = "Смена Dota-профиля\n\nОтправь новый код друга из Steam.\nВ Dota 2 включи «Общедоступная история матчей»."
CHANGE_CANCELLED_MESSAGE = "Смена аккаунта отменена."
INVALID_ACCOUNT_MESSAGE = "Не удалось определить Dota аккаунт.\n\nОтправь код друга Steam — только цифры, например:\n165682118"
ONBOARDING_MESSAGE = (
    "🚀 <b>Turbo Rating</b> — это ладдер для Turbo в Dota 2.\n\n"
    "Он позволяет оценивать свой скилл в Turbo примерно так же, как рейтинг в обычном "
    "рейтинговом режиме: за матчи ты получаешь или теряешь рейтинг, а игровая статистика "
    "влияет на итоговое изменение.\n\n"
    "🏆 Каждый месяц проходит новый сезон:\n"
    "— игроки соревнуются за места в таблице лидеров;\n"
    "— рейтинг сезона считается отдельно;\n"
    "— по итогам сезона лучшие игроки получают призы.\n\n"
    "Привяжи аккаунт, сыграй матчи и поднимайся выше в ладдере."
)
RATING_HELP_MESSAGE = (
    "Turbo Rating\n\n"
    "Стартовый TR рассчитывается по последним 20 Turbo-матчам.\n\n"
    "За результат матча:\n\n"
    "WIN → +25 TR\n"
    "LOSE → -25 TR\n\n"
    "За хорошую игру можно получить ещё до +10 TR.\n"
    "Учитываются участие в убийствах, урон героям и строениям, "
    "смерти и командный вклад.\n\n"
    "Пример хорошей игры:\n"
    "WIN: +25 за победу +5 за performance = +30 TR\n"
    "LOSE: -25 за поражение +5 за performance = -20 TR\n\n"
    "Победа или поражение всегда остаются главным фактором рейтинга."
)


class LinkDota(StatesGroup):
    waiting_for_account = State()


class ChangeDota(StatesGroup):
    confirming = State()
    waiting_for_account = State()


BOT_COMMANDS = [
    BotCommand(command="start", description="Начать работу"),
    BotCommand(command="add", description="Подключить Dota аккаунт"),
    BotCommand(command="rating", description="Профиль"),
    BotCommand(command="history", description="История матчей"),
    BotCommand(command="top", description="Таблица лидеров"),
    BotCommand(command="prizes", description="Призы сезона"),
    BotCommand(command="matches", description="История матчей"),
    BotCommand(command="sync", description="Обновить матчи"),
    BotCommand(command="profile", description="Мой профиль"),
]


def _player(message: Message):
    return db.get_telegram_player(message.from_user.id) if message.from_user else None


def _position(account_id: int) -> str:
    place = db.get_leaderboard_position(account_id)
    return f"#{place['position']}" if place else "пока нет"


def _week_ago() -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())


def _service_error(exc: Exception, fallback: str) -> str:
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    logger.error("Bot service request failed error=%s status=%s", type(exc).__name__, status)
    if isinstance(exc, MatchHistoryUnavailable):
        return HISTORY_UNAVAILABLE_MESSAGE
    if isinstance(exc, httpx.HTTPError):
        return OPENDOTA_ERROR_MESSAGE
    return fallback


def _leaderboard(account_id: int | None) -> str:
    at = season.now()
    leaderboard = db.get_leaderboard()
    final = db.get_final_standings()
    if final is not None:
        return format_season_results(final)
    if not leaderboard:
        return format_top([], {}, account_id, None, at=at)
    past_positions = {row["account_id"]: row["position"] for row in db.get_leaderboard_at(_week_ago())}
    place = db.get_leaderboard_position(account_id) if account_id is not None else None
    return format_top(leaderboard, past_positions, account_id, place, at=at)


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
    player = _player(message)
    if player is None:
        if message.from_user is None:
            return
        waiting = state is not None and await state.get_state() == LinkDota.waiting_for_account.state
        is_new = not db.is_known_telegram_user(message.from_user.id) and not waiting
        data = await state.get_data() if waiting else {}
        text = f"{ONBOARDING_MESSAGE}\n\n{LINK_PROMPT}" if is_new else LINK_PROMPT
        await message.answer(
            text, parse_mode="HTML", reply_markup=get_link_keyboard(retry_token=data.get("retry_token")),
        )
        if state is not None and not waiting:
            await state.clear()
            await state.set_state(LinkDota.waiting_for_account)
        db.remember_telegram_user(message.from_user.id)
        return
    await _exit_account_input(message, state)
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
        f"Turbo Rating — рейтинг Turbo среди друзей.\nПрисоединяйся: https://t.me/{me.username}",
        reply_markup=get_main_keyboard(_player(message) is not None),
    )


@router.message(F.text == RATING_HELP_BUTTON)
async def rating_help_command(message: Message, state: FSMContext | None = None) -> None:
    await _exit_account_input(message, state)
    await message.answer(
        RATING_HELP_MESSAGE,
        reply_markup=get_main_keyboard(_player(message) is not None),
    )


@router.message(F.text == TURBO_INFO_BUTTON)
async def turbo_info_command(message: Message) -> None:
    # Reading the description does not cancel an unfinished account link.
    await message.answer(ONBOARDING_MESSAGE, parse_mode="HTML", reply_markup=get_info_keyboard())
    if message.from_user is not None:
        db.remember_telegram_user(message.from_user.id)


@router.callback_query(F.data == RATING_HELP_CALLBACK)
async def rating_help_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.answer(RATING_HELP_MESSAGE)


@router.message(F.text == LINK_BUTTON)
async def link_command(message: Message, state: FSMContext) -> None:
    if _player(message) is not None:
        await change_command(message, state)
        return
    db.remember_telegram_user(message.from_user.id)
    await state.clear()
    await state.set_state(LinkDota.waiting_for_account)
    await message.answer(LINK_PROMPT, parse_mode="HTML", reply_markup=get_link_keyboard())


@router.callback_query(F.data.in_({FRIEND_CODE_HELP_CALLBACK, MATCH_HISTORY_HELP_CALLBACK}))
async def link_help_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    data = await state.get_data()
    text = FRIEND_CODE_HELP_MESSAGE if callback.data == FRIEND_CODE_HELP_CALLBACK else MATCH_HISTORY_HELP_MESSAGE
    await callback.message.answer(
        text, parse_mode="HTML",
        reply_markup=get_link_keyboard(
            retry_token=data.get("retry_token"), change_token=data.get("change_token"),
        ),
    )


@router.callback_query(F.data.startswith(LINK_RETRY_PREFIX))
async def retry_link_callback(callback: CallbackQuery, state: FSMContext, api_key: str | None = None) -> None:
    current = await state.get_state()
    data = await state.get_data()
    token = data.get("retry_token")
    if not (
        current in (LinkDota.waiting_for_account.state, ChangeDota.waiting_for_account.state)
        and token and callback.data == LINK_RETRY_PREFIX + token
        and data.get("pending_account_id") is not None
        and isinstance(callback.message, Message)
    ):
        await callback.answer("Эта проверка уже недоступна. Открой подключение Dota 2 и отправь код друга снова.")
        return
    await callback.answer()
    await _connect_account(
        callback.message, str(data["pending_account_id"]), api_key, state,
        telegram_id=callback.from_user.id,
    )


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
    if message.from_user is not None:
        db.remember_telegram_user(message.from_user.id)
    linked = _player(message) is not None
    if linked and not command.args and state is not None:
        await change_command(message, state)
        return
    if state is not None:
        await state.set_data({"change_token": uuid4().hex} if linked else {})
        await state.set_state(ChangeDota.waiting_for_account if linked else LinkDota.waiting_for_account)
    if not command.args:
        await message.answer(
            CHANGE_PROMPT if linked else LINK_PROMPT, parse_mode="HTML", reply_markup=get_link_keyboard(),
        )
        return
    await _connect_account(message, command.args, api_key, state)


async def _connect_account(
    message: Message, text: str, api_key: str | None, state: FSMContext | None,
    *, telegram_id: int | None = None,
) -> None:
    if telegram_id is None and message.from_user is not None:
        telegram_id = message.from_user.id
    if telegram_id is None:
        return
    data = await state.get_data() if state is not None else {}
    token = data.get("change_token")
    account_id = parse_dota_account_id(text)
    retry_token = uuid4().hex if state is not None and account_id is not None else None
    if state is not None:
        await state.update_data(pending_account_id=account_id, retry_token=retry_token)
    error_markup = get_link_keyboard(retry_token=retry_token, change_token=token)

    try:
        result = await link_dota_account(telegram_id, text, api_key=api_key)
    except InvalidDotaAccountError:
        await message.answer(INVALID_ACCOUNT_MESSAGE, reply_markup=error_markup)
        return
    except (httpx.HTTPError, MatchHistoryUnavailable) as exc:
        error = _service_error(exc, OPENDOTA_ERROR_MESSAGE)
        if isinstance(exc, MatchHistoryUnavailable) or (
            isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404
        ):
            error = ACCOUNT_UNAVAILABLE_MESSAGE
        await message.answer(error, reply_markup=error_markup)
        return
    except (ValueError, sqlite3.Error) as exc:
        fallback = ACCOUNT_UNAVAILABLE_MESSAGE if isinstance(exc, ValueError) else "Не удалось сохранить аккаунт и рейтинг. Попробуйте позже."
        await message.answer(_service_error(exc, fallback), reply_markup=error_markup)
        return

    if state is not None:
        await state.clear()
    rating = result.rating
    if rating is None and db.get_final_standings() is not None:
        await message.answer(
            f"Dota-профиль подключён.\n\n{_nickname(result.player)}\n\n"
            "Сезон завершён. Матчи сохраняются без начисления TR.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
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


@router.message(Command("prizes"))
@router.message(F.text == PRIZES_BUTTON)
async def prizes_command(message: Message, state: FSMContext | None = None) -> None:
    await _exit_account_input(message, state)
    at = season.now()
    leaderboard = db.get_leaderboard(limit=3)
    final = db.get_final_standings()
    await message.answer(
        format_prizes(final if final is not None else leaderboard, finished=final is not None, at=at),
        reply_markup=get_main_keyboard(_player(message) is not None),
    )


async def _load_rating(message: Message, account_id: int, api_key: str | None):
    try:
        rating = await initialize_rating(account_id, api_key=api_key)
        if rating is None:
            await message.answer("Сезон завершён. Рейтинг в этом сезоне не рассчитан.", reply_markup=MAIN_KEYBOARD)
        return rating
    except (httpx.HTTPError, sqlite3.Error, ValueError) as exc:
        await message.answer(_service_error(exc, "Не удалось загрузить рейтинг. Попробуйте позже."))
        return None


@router.message(Command("profile", "rating", "stats"))
@router.message(F.text.in_({PROFILE_BUTTON, RATING_BUTTON, "🏆 Рейтинг", STATS_BUTTON}))
async def profile_command(message: Message, api_key: str | None = None, state: FSMContext | None = None) -> None:
    player = await _linked_player(message, state)
    if player is None:
        return
    rating = await _load_rating(message, player["account_id"], api_key)
    if rating is None:
        return
    text = format_profile(
        player, rating, _position(player['account_id']), db.get_peak_rating(player['account_id']),
    )
    await message.answer(text, reply_markup=MAIN_KEYBOARD)


rating_command = profile_command


@router.message(Command("matches", "history"))
@router.message(F.text.in_({HISTORY_BUTTON, MATCHES_BUTTON, "📈 История", "📈 История TR"}))
async def history_command(message: Message, api_key: str | None = None, state: FSMContext | None = None) -> None:
    player = await _linked_player(message, state)
    if player is None:
        return
    account_id = player["account_id"]
    rating = await _load_rating(message, account_id, api_key)
    if rating is None:
        return
    matches = db.get_turbo_match_history(account_id, limit=10, since_timestamp=_week_ago())
    await message.answer(
        format_matches(matches), reply_markup=get_main_keyboard(True, include_info=False),
    )


matches_command = history_command


async def _sync_progress(message: Message) -> None:
    await asyncio.sleep(2)
    try:
        await message.answer("Синхронизирую матчи. OpenDota может отвечать с задержкой.")
    except Exception as exc:
        logger.warning("Sync progress message failed error=%s", type(exc).__name__)


@router.message(Command("sync"))
@router.message(F.text == SYNC_BUTTON)
async def sync_command(message: Message, api_key: str | None = None, state: FSMContext | None = None) -> None:
    player = await _linked_player(message, state)
    if player is None:
        return

    telegram_id = message.from_user.id
    if telegram_id in _sync_in_progress or is_sync_in_progress(player["account_id"]):
        await message.answer("Обновление уже выполняется. Подождите немного.", reply_markup=MAIN_KEYBOARD)
        return
    remaining = math.ceil(_sync_cooldowns.get(telegram_id, 0) - monotonic())
    if remaining > 0:
        await message.answer(
            f"Данные недавно обновлялись.\nПопробуйте через {remaining} сек.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    _sync_in_progress.add(telegram_id)
    _sync_cooldowns[telegram_id] = monotonic() + MANUAL_SYNC_COOLDOWN
    succeeded = False
    progress_task = asyncio.create_task(_sync_progress(message))
    try:
        result = await sync_player(player["account_id"], api_key=api_key)
        if result.already_in_progress:
            await message.answer("Обновление уже выполняется. Подождите немного.", reply_markup=MAIN_KEYBOARD)
            return
        succeeded = True
        _sync_cooldowns[telegram_id] = monotonic() + MANUAL_SYNC_COOLDOWN
    except (httpx.HTTPError, sqlite3.Error, ValueError) as exc:
        fallback = "Не удалось обновить матчи. Попробуйте позже." if isinstance(exc, sqlite3.Error) else OPENDOTA_ERROR_MESSAGE
        error = _service_error(exc, fallback)
        if isinstance(exc, httpx.TimeoutException):
            error = "Синхронизация не завершилась из-за временной ошибки API. Попробуйте ещё раз позже."
        await message.answer(error, reply_markup=MAIN_KEYBOARD)
        return
    finally:
        progress_task.cancel()
        with suppress(asyncio.CancelledError):
            await progress_task
        _sync_in_progress.discard(telegram_id)
        if not succeeded:
            _sync_cooldowns.pop(telegram_id, None)

    pending_notice = (
        f"\n\nPerformance пока недоступен для {result.performance_pending} матчей. "
        "Повторим расчёт при следующем обновлении."
    ) if result.performance_pending else ""
    if db.get_final_standings() is not None:
        rating = db.get_rating(player["account_id"])
        status = f"\n\nTurbo Rating: {rating['current_rating']:.0f}" if rating else ""
        await message.answer(
            f"Сезон завершён.\nНовых матчей сохранено: {result.new_count}.\nTR сезона зафиксирован.{status}",
            reply_markup=MAIN_KEYBOARD,
        )
    elif not result.rating_updates:
        rating = db.get_rating(player["account_id"])
        await message.answer(
            f"Матчи обновлены.\n\nTurbo Rating: {rating['current_rating']:.0f}{pending_notice}"
            if result.performance_pending else f"Данные актуальны.\n\nTurbo Rating: {rating['current_rating']:.0f}",
            reply_markup=MAIN_KEYBOARD,
        )
    else:
        updates = result.rating_updates
        new_count = sum(not update.is_correction for update in updates)
        corrected_count = len(updates) - new_count
        lines = [f"Обновлено.\n\nНовых Turbo: {new_count}"]
        if corrected_count:
            lines.append(f"Performance восстановлен: {corrected_count}")
        lines.append("")
        for update in updates[:50]:
            label = "Performance" if update.is_correction else ('WIN' if update.win else 'LOSE')
            line = f"{label}  {update.rating_delta:+.0f} TR"
            if update.performance_bonus > 0 and not update.is_correction:
                line += f" · performance +{update.performance_bonus}"
            lines.append(line)
        if len(updates) > 50:
            lines.append(f"… ещё матчей: {len(updates) - 50}")
        lines.extend(["", f"Rating:\n{updates[0].rating_before:.0f} → {updates[-1].rating_after:.0f}"])
        if len(updates) == 1:
            lines.extend(["", format_rating_breakdown(updates[0])])
        await message.answer("\n".join(lines) + pending_notice, reply_markup=MAIN_KEYBOARD)


# Register after menu handlers so navigation is never consumed as a Friend ID.
@router.message(LinkDota.waiting_for_account, F.text, ~F.text.startswith("/"))
@router.message(ChangeDota.waiting_for_account, F.text, ~F.text.startswith("/"))
async def account_input(message: Message, state: FSMContext, api_key: str | None = None) -> None:
    await _connect_account(message, message.text, api_key, state)


@router.message(LinkDota.waiting_for_account)
async def invalid_account_input(message: Message) -> None:
    await message.answer(INVALID_ACCOUNT_MESSAGE, reply_markup=get_link_keyboard())


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
