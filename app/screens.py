"""Shared Telegram screen formatting using already loaded data."""

from datetime import datetime, timedelta, timezone

from app import season
from app.season import PRIZES, format_countdown
from app.services.heroes import get_hero_name


def format_nickname(player: dict) -> str:
    return " ".join((player.get("nickname") or "имя недоступно").split())[:80]


def format_home(player: dict, rating: dict | None, position: str) -> str:
    status = (
        f"{rating['current_rating']:.0f} TR · место {position}"
        if rating else "Рейтинг пока не рассчитан. Нажмите «Профиль»."
    )
    return f"Turbo Rating\n\n{format_nickname(player)}\n{status}"


def format_profile(player: dict, rating: dict, position: str, peak: float) -> str:
    connected = datetime.fromtimestamp(player["tracking_started_at"], timezone.utc).strftime("%d.%m.%Y")
    return (
        f"👤 Профиль\n\n{format_nickname(player)}\n"
        f"Dota ID: {player['account_id']}\n\n"
        f"Turbo Rating: {rating['current_rating']:.0f} TR\n"
        f"Место: {position}\n"
        f"Стартовый TR: {rating['initial_rating']:.0f}\n"
        f"Рекорд: {peak:.0f} TR\n\n"
        f"Дата подключения:\n{connected}"
    )


def _rank_movement(before: int | None, current: int) -> str:
    if before is None:
        return ""
    change = before - current
    return f"  ↑{change}" if change > 0 else f"  ↓{-change}" if change < 0 else "  —"


def format_top(
    leaderboard: list[dict], past_positions: dict[int, int],
    account_id: int | None, own_place: dict | None, *, at: datetime | None = None,
) -> str:
    lines = ["Turbo Rating", f"Сезон: {season.title(season.season_id(at))}", ""]
    if not leaderboard:
        lines.append("Рейтинг игроков пока пуст.")
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for position, player in enumerate(leaderboard, start=1):
        suffix = " ← вы" if player["account_id"] == account_id else ""
        movement = _rank_movement(past_positions.get(player["account_id"]), position)
        lines.append(
            f"{medals.get(position, f'{position}.')} {format_nickname(player)} — "
            f"{player['current_rating']:.0f}{movement}{suffix}"
        )
    if own_place and own_place["position"] > len(leaderboard):
        movement = _rank_movement(past_positions.get(account_id), own_place["position"])
        lines.extend(["", f"Ваше место: #{own_place['position']} — {own_place['current_rating']:.0f} TR{movement}"])
    return "\n".join(lines)


def _podium(standings: list[dict], *, prizes: bool = False) -> list[str]:
    lines = []
    for position, (medal, player) in enumerate(zip(("🥇", "🥈", "🥉"), standings), start=1):
        prize = f" — {PRIZES[position]:,} ₽".replace(",", " ") if prizes else ""
        lines.append(f"{medal} {format_nickname(player)} — {player['current_rating']:.0f} TR{prize}")
    return lines or ["Рейтинг игроков пока пуст."]


def format_season_results(standings: list[dict]) -> str:
    return "\n".join(["🏆 Итоги сезона", "", *_podium(standings)])


def format_prizes(
    standings: list[dict], *, finished: bool = False, at: datetime | None = None,
    previous_standings: list[dict] | None = None,
) -> str:
    identifier = season.season_id(at)
    end = season.bounds(identifier)[1] - timedelta(seconds=1)
    lines = ["💰 Призы сезона", f"Сезон: {season.title(identifier)}", ""]
    if finished:
        return "\n".join([*lines, "Сезон завершён.", "", "Победители:", *_podium(standings, prizes=True)])
    for position, medal in enumerate(("🥇", "🥈", "🥉"), start=1):
        lines.append(f"{medal} {position} место — {PRIZES[position]:,} ₽".replace(",", " "))
    lines.extend(["", "Сезон заканчивается:", f"{end:%d.%m.%Y} 23:59:59 МСК",
                  "", "До окончания:", format_countdown(at), "", "Текущий топ:", *_podium(standings)])
    if previous_standings is not None:
        previous_id = (previous_standings[0]["season_id"] if previous_standings
                       else season.season_id(season.bounds(identifier)[0] - timedelta(seconds=1)))
        lines.extend(["", f"Итоги: {season.title(previous_id)}", *_podium(previous_standings, prizes=True)])
    return "\n".join(lines)


def format_matches(matches: list[dict]) -> str:
    lines = ["📜 История матчей"]
    if not matches:
        lines.append("За последние 7 дней матчей нет.")
    for match in matches:
        date = datetime.fromtimestamp(match["start_time"], season.MOSCOW).strftime("%d.%m")
        result = {1: "WIN", 0: "LOSE", None: "Результат пока неизвестен"}[match["win"]]
        rating = (
            f"{match['rating_delta']:+.0f} TR → {match['rating_after']:.0f} TR"
            if match["rating_delta"] is not None else "TR не начислен."
        )
        if match["rating_delta"] is not None and (match.get("performance_bonus") or 0) > 0:
            rating = (
                f"{match['rating_delta']:+.0f} TR · performance +{match['performance_bonus']}"
                f" → {match['rating_after']:.0f} TR"
            )
        lines.append(f"{date}\n{result} · {get_hero_name(match['hero_id'])}\n{rating}")
    return "\n\n".join(lines)
