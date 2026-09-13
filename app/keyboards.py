"""Persistent reply keyboard for the main bot actions."""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


RATING_BUTTON = "🏆 Рейтинг"
STATS_BUTTON = "📊 Статистика"
TOP_BUTTON = "🥇 Топ"
MATCHES_BUTTON = "🎮 Матчи"
SYNC_BUTTON = "🔄 Обновить"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=RATING_BUTTON), KeyboardButton(text=STATS_BUTTON)],
        [KeyboardButton(text=TOP_BUTTON), KeyboardButton(text=MATCHES_BUTTON)],
        [KeyboardButton(text=SYNC_BUTTON)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)
