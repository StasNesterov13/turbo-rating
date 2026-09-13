"""Shared Telegram screen formatting using already loaded data."""

from datetime import datetime, timezone

from app.services.game_modes import get_game_mode_name
from app.services.heroes import get_hero_name


def format_nickname(player: dict) -> str:
    return " ".join((player.get("nickname") or "имя недоступно").split())[:80]


def format_home(player: dict, rating: dict | None, position: str) -> str:
    status = (
        f"{rating['current_rating']:.0f} TR · место {position}"
        if rating else "Рейтинг пока не рассчитан. Нажмите «Мой рейтинг»."
    )
    return f"🏆 Turbo Rating\n\n{format_nickname(player)}\n{status}"


def format_rating(rating: dict, position: str, peak: float) -> str:
    return (
        "🏆 Мой рейтинг\n\n"
        f"{rating['current_rating']:.0f} TR\n"
        f"Место: {position}\n"
        f"Старт: {rating['initial_rating']:.0f} TR\n"
        f"Рекорд: {peak:.0f} TR"
    )


def format_profile(player: dict) -> str:
    connected = datetime.fromtimestamp(player["tracking_started_at"], timezone.utc).strftime("%d.%m.%Y")
    return (
        f"👤 Профиль\n\n{format_nickname(player)}\n\n"
        f"Dota ID: {player['account_id']}\n"
        f"Подключён: {connected}"
    )


def format_history(
    changes: dict[str, float], past_positions: dict[str, int | None],
    current_position: str, history: list[dict],
) -> str:
    lines = ["📈 История TR", ""]
    lines.extend(f"{label}: {change:+.0f} TR" for label, change in changes.items())
    lines.append("")
    for label, previous in past_positions.items():
        movement = f"#{previous} → {current_position}" if previous is not None else "ещё не зарегистрирован"
        lines.append(f"{label} назад: {movement}")
    lines.extend(["", "Последние изменения:", ""])
    for event in history:
        date = datetime.fromtimestamp(event["created_at"], timezone.utc).strftime("%d.%m")
        lines.append(f"{date}  {event['rating_delta']:+.0f}   {event['rating_after']:.0f} TR")
    if not history:
        lines.append("Начислений пока нет.")
    return "\n".join(lines)


def _rank_movement(before: int | None, current: int) -> str:
    if before is None:
        return ""
    change = before - current
    return f"  ↑{change}" if change > 0 else f"  ↓{-change}" if change < 0 else "  —"


def format_top(
    leaderboard: list[dict], past_positions: dict[int, int],
    account_id: int | None, own_place: dict | None,
) -> str:
    lines = ["🥇 Turbo Rating", ""]
    if not leaderboard:
        return "\n".join([*lines, "Рейтинг игроков пока пуст."])
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


def format_matches(matches: list[dict]) -> str:
    if not matches:
        return "Матчей пока нет. Нажмите «Обновить» после игры."
    lines = ["🎮 Последние матчи"]
    for match in matches:
        result = {1: "WIN", 0: "LOSE", None: "Результат пока неизвестен"}[match["win"]]
        mode = get_game_mode_name(match["game_mode"])
        duration = f"{match['duration'] / 60:.0f} мин" if match["duration"] is not None else "? мин"
        lines.append(f"{result}\n{get_hero_name(match['hero_id'])} · {mode} · {duration}")
    return "\n\n".join(lines)
