"""Turbo Rating mathematics and one-time historical calibration."""

import math
import os
from typing import Any

from app import db
from app.services.opendota import OpenDotaClient


BASE_RATING = 1000.0
ELO_SCALE = 400.0
K_FACTOR = 32.0
CALIBRATION_MATCHES = 20
PRIOR_WINS = 5
PRIOR_LOSSES = 5


def calculate_initial_rating(wins: int, matches: int) -> float:
    if type(wins) is not int or type(matches) is not int or not 0 <= wins <= matches:
        raise ValueError("Нужно 0 <= wins <= matches, оба значения целые.")
    p = (wins + PRIOR_WINS) / (matches + PRIOR_WINS + PRIOR_LOSSES)
    return BASE_RATING + ELO_SCALE * math.log10(p / (1 - p))


def calculate_expected_score(rating: float) -> float:
    if not math.isfinite(rating):
        raise ValueError("Рейтинг должен быть конечным числом.")
    exponent = (rating - BASE_RATING) / ELO_SCALE
    # Equivalent Elo formula without overflow for a very low rating.
    if exponent >= 0:
        return 1 / (1 + 10 ** -exponent)
    power = 10 ** exponent
    return power / (1 + power)


def calculate_new_rating(rating: float, win: bool) -> tuple[float, float, float]:
    if type(win) is not bool:
        raise ValueError("win должен быть bool.")
    expected = calculate_expected_score(rating)
    new_rating = rating + K_FACTOR * (int(win) - expected)
    return new_rating, new_rating - rating, expected


async def initialize_rating(
    account_id: int, *, api_key: str | None = None
) -> dict[str, Any]:
    player = db.get_player(account_id)
    if player is None:
        raise ValueError("Игрок ещё не добавлен в БД.")
    existing = db.get_rating(account_id)
    if existing is not None:
        return existing

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


def apply_rating_changes(account_id: int) -> list[dict[str, Any]]:
    return db.apply_pending_ratings(account_id, calculate_new_rating)
