"""Persistent reply keyboard for the main bot actions."""

from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup,
)


RATING_BUTTON = "🏆 Мой рейтинг"
STATS_BUTTON = "📊 Статистика"
TOP_BUTTON = "🥇 Топ игроков"
MATCHES_BUTTON = "🎮 Матчи"
SYNC_BUTTON = "🔄 Обновить"
PROFILE_BUTTON = "👤 Профиль"
LINK_BUTTON = "➕ Привязать Dota"
CHANGE_BUTTON = "🔁 Сменить Dota"
SHARE_BUTTON = "🔗 Поделиться"
HISTORY_BUTTON = "📈 История TR"
CHANGE_CONFIRM_PREFIX = "dota_change:confirm:"
CHANGE_CANCEL_PREFIX = "dota_change:cancel:"
VIEW_TOP_BUTTON = "🥇 Посмотреть топ"
RATING_HELP_BUTTON = "ℹ️ Как считается рейтинг"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=RATING_BUTTON), KeyboardButton(text=TOP_BUTTON)],
        [KeyboardButton(text=STATS_BUTTON), KeyboardButton(text=MATCHES_BUTTON)],
        [KeyboardButton(text=SYNC_BUTTON), KeyboardButton(text=PROFILE_BUTTON)],
        [KeyboardButton(text=HISTORY_BUTTON)],
        [KeyboardButton(text=CHANGE_BUTTON), KeyboardButton(text=SHARE_BUTTON)],
        [KeyboardButton(text=RATING_HELP_BUTTON)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

UNLINKED_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=LINK_BUTTON)],
        [KeyboardButton(text=VIEW_TOP_BUTTON)],
        [KeyboardButton(text=RATING_HELP_BUTTON)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def get_main_keyboard(linked: bool) -> ReplyKeyboardMarkup:
    return MAIN_KEYBOARD if linked else UNLINKED_KEYBOARD


def get_change_keyboard(token: str, *, confirm: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if confirm:
        rows.append([InlineKeyboardButton(
            text="Сменить аккаунт", callback_data=CHANGE_CONFIRM_PREFIX + token,
        )])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data=CHANGE_CANCEL_PREFIX + token)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
