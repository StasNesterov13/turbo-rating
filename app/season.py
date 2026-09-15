"""Calendar months in Moscow; stored boundaries are [start, next month)."""

from datetime import datetime, timedelta, timezone


MOSCOW = timezone(timedelta(hours=3), "Europe/Moscow")
LEGACY_SEASON_ID = "2026-09"
INITIAL_RATING = 1000.0
PRIZES = {1: 3000, 2: 2000, 3: 1000}
MONTHS = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")


def now() -> datetime:
    return datetime.now(MOSCOW)


def season_id(at: datetime | None = None) -> str:
    return (at if at is not None else now()).astimezone(MOSCOW).strftime("%Y-%m")


def for_timestamp(timestamp: int) -> str:
    return season_id(datetime.fromtimestamp(timestamp, MOSCOW))


def bounds(identifier: str) -> tuple[datetime, datetime]:
    year, month = map(int, identifier.split("-"))
    start = datetime(year, month, 1, tzinfo=MOSCOW)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=MOSCOW)
    return start, end


def title(identifier: str) -> str:
    start, _ = bounds(identifier)
    return f"{MONTHS[start.month - 1]} {start.year}"


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


def format_countdown(at: datetime | None = None, *, identifier: str | None = None) -> str:
    at = at if at is not None else now()
    _, end = bounds(identifier or season_id(at))
    if at >= end:
        return "0 минут"
    seconds = int((end - at).total_seconds())
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{_unit(days, ('день', 'дня', 'дней'))} {_unit(hours, ('час', 'часа', 'часов'))}"
    if hours:
        return f"{_unit(hours, ('час', 'часа', 'часов'))} {_unit(minutes, ('минута', 'минуты', 'минут'))}"
    return _unit(minutes, ("минута", "минуты", "минут")) if minutes else "менее минуты"
