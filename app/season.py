"""The single prize season and its Moscow deadline."""

from datetime import datetime, timedelta, timezone


# Moscow is UTC+3 on this season's deadline; no system timezone dependency.
SEASON_END_AT = datetime(2026, 10, 1, 23, 59, 59, tzinfo=timezone(timedelta(hours=3), "Europe/Moscow"))
PRIZES = {1: 3000, 2: 2000, 3: 1000}


def now() -> datetime:
    return datetime.now(SEASON_END_AT.tzinfo)


def is_season_over(at: datetime | None = None) -> bool:
    return (at if at is not None else now()) >= SEASON_END_AT


def _unit(value: int, forms: tuple[str, str, str]) -> str:
    if 11 <= value % 100 <= 14:
        index = 2
    elif value % 10 == 1:
        index = 0
    elif 2 <= value % 10 <= 4:
        index = 1
    else:
        index = 2
    return f"{value} {forms[index]}"


def format_countdown(at: datetime | None = None) -> str:
    at = at if at is not None else now()
    if is_season_over(at):
        return "0 минут"
    seconds = int((SEASON_END_AT - at).total_seconds())
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{_unit(days, ('день', 'дня', 'дней'))} {_unit(hours, ('час', 'часа', 'часов'))}"
    if hours:
        return f"{_unit(hours, ('час', 'часа', 'часов'))} {_unit(minutes, ('минута', 'минуты', 'минут'))}"
    return _unit(minutes, ("минута", "минуты", "минут")) if minutes else "менее минуты"
