"""Turbo Rating mathematics and one-time historical calibration."""

import math
import os
from typing import Any

from app import db, season
from app.services.opendota import OpenDotaClient


BASE_RATING = 1000.0
ELO_SCALE = 400.0
CALIBRATION_MATCHES = 20
PRIOR_WINS = 5
PRIOR_LOSSES = 5
BASE_WIN = 25
BASE_LOSS = -25
PERFORMANCE_BONUS_MIN = 0
PERFORMANCE_BONUS_MAX = 10
PERFORMANCE_THRESHOLD = 0.35
PERFORMANCE_WEIGHTS = {
    "kill_participation": 0.30,
    "hero_damage": 0.20,
    "tower_damage": 0.15,
    "survivability": 0.15,
    "support": 0.20,
}
SUPPORT_WEIGHTS = {"wards": 0.40, "stacks": 0.30, "healing": 0.30}


def _number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _weighted_score(values: dict[str, float | None], weights: dict[str, float]) -> float | None:
    available = [(weights[key], _clamp(value)) for key, value in values.items() if value is not None]
    if not available:
        return None
    return _clamp(sum(weight * value for weight, value in available) / sum(weight for weight, _ in available))


def calculate_performance_details(
    match: dict[str, Any] | None, account_id: int,
) -> dict[str, float | None]:
    """Compare only the player's complete five-player team; missing values stay unavailable."""
    details = dict.fromkeys(PERFORMANCE_WEIGHTS)
    if not isinstance(match, dict) or match.get("game_mode", 23) != 23:
        return details
    players = match.get("players")
    if not isinstance(players, list) or any(not isinstance(player, dict) for player in players):
        return details
    targets = [player for player in players if player.get("account_id") == account_id]
    if len(targets) != 1:
        return details
    player = targets[0]
    slot = player.get("player_slot")
    if type(slot) is not int or slot not in (*range(5), *range(128, 133)):
        return details
    slots = range(5) if slot < 128 else range(128, 133)
    team = [p for p in players if type(p.get("player_slot")) is int and p["player_slot"] in slots]
    if len(team) != 5 or {p["player_slot"] for p in team} != set(slots):
        return details
    index = team.index(player)

    def values(*fields: str) -> list[float] | None:
        result = []
        for teammate in team:
            numbers = [_number(teammate.get(field)) for field in fields]
            # Partial data cannot establish a reliable team maximum or sum.
            if any(number is None for number in numbers):
                return None
            total = sum(numbers)
            if not math.isfinite(total):
                return None
            result.append(total)
        return result

    def relative(*fields: str, zero_available: bool = False) -> float | None:
        numbers = values(*fields)
        if numbers is None:
            return None
        maximum = max(numbers)
        if maximum == 0:
            return 0.0 if zero_available else None
        return _clamp(numbers[index] / maximum)

    kills = values("kills")
    assists = _number(player.get("assists"))
    if kills is not None and assists is not None and 0 < sum(kills) < math.inf:
        details["kill_participation"] = _clamp((kills[index] + assists) / sum(kills))
    details["hero_damage"] = relative("hero_damage")
    details["tower_damage"] = relative("tower_damage")
    deaths = values("deaths")
    if deaths is not None:
        details["survivability"] = _clamp(1 - deaths[index] / max(deaths)) if max(deaths) else 1.0
    details["support"] = _weighted_score({
        "wards": relative("obs_placed", "sen_placed", zero_available=True),
        "stacks": relative("camps_stacked", zero_available=True),
        "healing": relative("hero_healing", zero_available=True),
    }, SUPPORT_WEIGHTS)
    return details


def calculate_performance_score(details: dict[str, float | None]) -> float | None:
    """Renormalize available components; fewer than three cannot earn a bonus."""
    available = {key: _number(details.get(key)) for key in PERFORMANCE_WEIGHTS}
    if sum(value is not None for value in available.values()) < 3:
        return None
    return _weighted_score(available, PERFORMANCE_WEIGHTS)


def calculate_performance_bonus(score: float | None) -> int:
    score = _number(score)
    if score is None or score <= PERFORMANCE_THRESHOLD:
        return PERFORMANCE_BONUS_MIN
    normalized = (_clamp(score) - PERFORMANCE_THRESHOLD) / (1 - PERFORMANCE_THRESHOLD)
    return max(PERFORMANCE_BONUS_MIN, min(PERFORMANCE_BONUS_MAX, round(normalized * PERFORMANCE_BONUS_MAX)))


def get_match_performance(match: dict[str, Any] | None, account_id: int) -> dict[str, Any]:
    details = calculate_performance_details(match, account_id)
    score = calculate_performance_score(details)
    return dict(performance_score=score, performance_bonus=calculate_performance_bonus(score),
                performance_details=details)


def calculate_initial_rating(wins: int, matches: int) -> float:
    if type(wins) is not int or type(matches) is not int or not 0 <= wins <= matches:
        raise ValueError("Нужно 0 <= wins <= matches, оба значения целые.")
    p = (wins + PRIOR_WINS) / (matches + PRIOR_WINS + PRIOR_LOSSES)
    return BASE_RATING + ELO_SCALE * math.log10(p / (1 - p))


def calculate_rating_delta(win: bool, performance_bonus: int = 0) -> float:
    if type(win) is not bool:
        raise ValueError("win должен быть bool.")
    if type(performance_bonus) is not int:
        raise ValueError("performance_bonus должен быть целым числом.")
    bonus = max(PERFORMANCE_BONUS_MIN, min(PERFORMANCE_BONUS_MAX, performance_bonus))
    return float((BASE_WIN if win else BASE_LOSS) + bonus)


def calculate_new_rating(rating: float, win: bool, performance_bonus: int = 0) -> tuple[float, float, float]:
    if not math.isfinite(rating):
        raise ValueError("Рейтинг должен быть конечным числом.")
    delta = calculate_rating_delta(win, performance_bonus)
    # Keep the existing return tuple and NOT NULL expected_score column.
    # This compatibility value has no effect on rating calculations.
    return float(rating) + delta, delta, 0.5


async def initialize_rating(
    account_id: int, *, api_key: str | None = None
) -> dict[str, Any] | None:
    player = db.get_player(account_id)
    if player is None:
        raise ValueError("Игрок ещё не добавлен в БД.")
    existing = db.get_rating(account_id)
    if existing is not None:
        return existing
    if db.ensure_current_season()["season_id"] > season.LEGACY_SEASON_ID:
        return db.create_rating(account_id, season.INITIAL_RATING, [], 0)

    async with OpenDotaClient(
        api_key=api_key if api_key is not None else os.getenv("OPENDOTA_API_KEY") or None
    ) as client:
        history = await client.get_turbo_matches_before(
            account_id, player["tracking_started_at"], limit=CALIBRATION_MATCHES
        )
    # Incomplete outcomes cannot be treated as losses or used for calibration.
    calibration = [match for match in history if db.get_match_win(match) is not None]
    wins = sum(db.get_match_win(match) is True for match in calibration)
    initial = calculate_initial_rating(wins, len(calibration))
    return db.create_rating(account_id, initial, calibration, wins)


def apply_rating_changes(
    account_id: int, performance_by_match: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return db.apply_pending_ratings(account_id, calculate_new_rating, performance_by_match=performance_by_match)
