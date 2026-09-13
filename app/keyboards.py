"""Reply and inline keyboards for the main bot actions."""

from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup,
)


RATING_BUTTON = "🏆 Мой рейтинг"
STATS_BUTTON = "📊 Статистика"
TOP_BUTTON = "🥇 Топ"
MATCHES_BUTTON = "🎮 Матчи"
SYNC_BUTTON = "🔄 Обновить"
PROFILE_BUTTON = "👤 Профиль"
LINK_BUTTON = "➕ Привязать Dota"
CHANGE_BUTTON = "🔁 Сменить Dota"
SHARE_BUTTON = "🔗 Поделиться"
HISTORY_BUTTON = "📈 История"
PRIZES_BUTTON = "💰 Призы"
CHANGE_CONFIRM_PREFIX = "dota_change:confirm:"
CHANGE_CANCEL_PREFIX = "dota_change:cancel:"
VIEW_TOP_BUTTON = "🥇 Посмотреть топ"
RATING_HELP_BUTTON = "ℹ️ Как считается рейтинг"
FRIEND_CODE_HELP_CALLBACK = "dota_link:friend_code"
MATCH_HISTORY_HELP_CALLBACK = "dota_link:match_history"
LINK_RETRY_PREFIX = "dota_link:retry:"

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=RATING_BUTTON), KeyboardButton(text=TOP_BUTTON)],
        [KeyboardButton(text=HISTORY_BUTTON), KeyboardButton(text=MATCHES_BUTTON)],
        [KeyboardButton(text=SYNC_BUTTON), KeyboardButton(text=PROFILE_BUTTON)],
        [KeyboardButton(text=PRIZES_BUTTON)],
        [KeyboardButton(text=RATING_HELP_BUTTON)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

UNLINKED_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=LINK_BUTTON)],
        [KeyboardButton(text=VIEW_TOP_BUTTON), KeyboardButton(text=PRIZES_BUTTON)],
        [KeyboardButton(text=RATING_HELP_BUTTON)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def get_main_keyboard(linked: bool) -> ReplyKeyboardMarkup:
    return MAIN_KEYBOARD if linked else UNLINKED_KEYBOARD


def get_link_keyboard(
    *, retry_token: str | None = None, change_token: str | None = None,
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🔎 Как найти код друга", callback_data=FRIEND_CODE_HELP_CALLBACK)],
        [InlineKeyboardButton(text="⚙️ Как открыть историю матчей", callback_data=MATCH_HISTORY_HELP_CALLBACK)],
    ]
    if retry_token:
        rows.append([InlineKeyboardButton(text="🔄 Проверить снова", callback_data=LINK_RETRY_PREFIX + retry_token)])
    if change_token:
        rows.append([InlineKeyboardButton(text="Отмена", callback_data=CHANGE_CANCEL_PREFIX + change_token)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_change_keyboard(token: str, *, confirm: bool = False) -> InlineKeyboardMarkup:
    if not confirm:
        return get_link_keyboard(change_token=token)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="Сменить аккаунт", callback_data=CHANGE_CONFIRM_PREFIX + token,
        )],
        [InlineKeyboardButton(text="Отмена", callback_data=CHANGE_CANCEL_PREFIX + token)],
    ])
