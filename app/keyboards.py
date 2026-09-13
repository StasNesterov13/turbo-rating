"""Persistent reply keyboard for the main bot actions."""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


RATING_BUTTON = "🏆 Мой рейтинг"
STATS_BUTTON = "📊 Статистика"
TOP_BUTTON = "🥇 Топ игроков"
MATCHES_BUTTON = "🎮 Матчи"
SYNC_BUTTON = "🔄 Обновить"
PROFILE_BUTTON = "👤 Профиль"
LINK_BUTTON = "➕ Привязать Dota"
VIEW_TOP_BUTTON = "🥇 Посмотреть топ"
RATING_HELP_BUTTON = "ℹ️ Как считается рейтинг"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=RATING_BUTTON), KeyboardButton(text=TOP_BUTTON)],
        [KeyboardButton(text=STATS_BUTTON), KeyboardButton(text=MATCHES_BUTTON)],
        [KeyboardButton(text=SYNC_BUTTON), KeyboardButton(text=PROFILE_BUTTON)],
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
