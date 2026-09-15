"""Player registration and match synchronization, independent of Telegram."""

import asyncio
from dataclasses import dataclass, field
import logging
import os
import time
from typing import Any
from weakref import WeakValueDictionary

import httpx

from app import db
from app.services.opendota import OpenDotaClient
from app.services.rating import initialize_rating, apply_rating_changes, get_match_performance


logger = logging.getLogger(__name__)
# Running calls keep a strong reference; idle locks can be released.
_sync_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
# Display-only timestamps; a restart waits for the next successful sync.
_last_sync_times: dict[int, float] = {}


class MatchHistoryUnavailable(ValueError):
    """OpenDota explicitly reports that the player's history is unavailable."""


def is_sync_in_progress(account_id: int) -> bool:
    lock = _sync_locks.get(account_id)
    return lock is not None and lock.locked()


def get_last_sync_time(account_id: int) -> float | None:
    return _last_sync_times.get(account_id)


def _player_profile(player_data: dict[str, Any], account_id: int) -> dict[str, Any]:
    profile = player_data.get("profile")
    if not isinstance(profile, dict) or profile.get("account_id") != account_id:
        raise ValueError("Игрок не найден в OpenDota. Проверьте Dota ID.")
    if profile.get("fh_unavailable") is True:
        raise MatchHistoryUnavailable("OpenDota profile has fh_unavailable=true")
    return profile


@dataclass(frozen=True)
class RatingUpdate:
    match_id: int
    start_time: int
    hero_id: int | None
    win: bool
    rating_before: float
    rating_delta: float
    rating_after: float
    performance_bonus: int = 0
    is_correction: bool = False
    season_id: str | None = None


@dataclass
class SyncResult:
    received_count: int
    new_matches: list[dict[str, Any]]
    skipped_old: int = 0
    skipped_duplicates: int = 0
    rating_changes: list[dict[str, Any]] = field(default_factory=list)
    rating_updates: list[RatingUpdate] = field(default_factory=list)
    already_in_progress: bool = False
    performance_pending: int = 0

    @property
    def new_count(self) -> int:
        return len(self.new_matches)


async def ensure_player(
    account_id: int, *, api_key: str | None = None
) -> dict[str, Any]:
    """Verify the OpenDota profile and register the player; return the profile."""
    async with OpenDotaClient(
        api_key=api_key if api_key is not None else os.getenv("OPENDOTA_API_KEY") or None
    ) as client:
        player_data = await client.get_player(account_id)
    profile = _player_profile(player_data, account_id)
    db.add_player(account_id, profile.get("personaname"), int(time.time()))
    await initialize_rating(account_id, api_key=api_key)
    return profile


async def sync_player(account_id: int, *, api_key: str | None = None) -> SyncResult:
    """Run at most one sync per account; overlapping callers return immediately."""
    lock = _sync_locks.setdefault(account_id, asyncio.Lock())
    # There is no await between this check and acquisition of an unlocked lock.
    if lock.locked():
        logger.info("Sync already in progress account_id=%s", account_id)
        return SyncResult(received_count=0, new_matches=[], already_in_progress=True)
    async with lock:
        logger.info("Sync started account_id=%s", account_id)
        try:
            result = await _sync_player(account_id, api_key=api_key)
        except Exception as exc:
            # HTTP exception strings may contain an API key in the request URL.
            logger.error("Sync failed account_id=%s error=%s", account_id, type(exc).__name__)
            raise
        logger.info(
            "Sync completed account_id=%s received=%s new_matches=%s rating_updates=%s performance_pending=%s",
            account_id, result.received_count, result.new_count, len(result.rating_updates),
            result.performance_pending,
        )
        return result


async def _sync_player(account_id: int, *, api_key: str | None = None) -> SyncResult:
    db.ensure_current_season()
    player = db.get_player(account_id)
    if player is None:
        raise ValueError("Игрок ещё не добавлен в БД.")
    corrections = []
    async with OpenDotaClient(
        api_key=api_key if api_key is not None else os.getenv("OPENDOTA_API_KEY") or None
    ) as client:
        profile = _player_profile(await client.get_player(account_id), account_id)
        await initialize_rating(account_id, api_key=api_key)
        matches = await client.get_matches_for_sync(account_id, player["tracking_started_at"])

        for match in matches:
            if type(match.get("start_time")) is not int or type(match.get("match_id")) is not int:
                raise ValueError("В ответе OpenDota отсутствует корректный match_id или start_time.")

        new_match_ids = set()
        skipped_old = 0
        skipped_duplicates = 0
        for match in matches:
            if match["start_time"] < player["tracking_started_at"]:
                skipped_old += 1
            elif db.save_match(account_id, match):
                new_match_ids.add(match["match_id"])
            else:
                skipped_duplicates += 1

        # Commit the base first, also recovering saved, unrated matches. A slow
        # detail request or cancellation cannot lose this progress.
        rating_changes = apply_rating_changes(account_id)
        rated_ids = {change["match_id"] for change in rating_changes}
        for pending in db.get_pending_performance_matches(account_id):
            if pending["season_id"] != db.ensure_current_season()["season_id"]:
                break
            match_id = pending["match_id"]
            try:
                full_match = await client.get_match(match_id)
                performance = get_match_performance(full_match, account_id)
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "Performance unavailable account_id=%s match_id=%s error=%s",
                    account_id, match_id, type(exc).__name__,
                )
                performance = get_match_performance(None, account_id)
            correction = db.apply_match_performance(account_id, match_id, performance)
            if correction is not None:
                logger.info(
                    "Performance completed account_id=%s match_id=%s adjustment=%s",
                    account_id, match_id, correction["rating_delta"],
                )
                if match_id not in rated_ids:
                    corrections.append(correction)

    # New matches are reported with their full delta; old matches report only
    # the recovered adjustment. Build one continuous before/after summary.
    if rating_changes:
        history = {row["match_id"]: row for row in db.get_rating_history(account_id)}
        current = rating_changes[0]["rating_before"]
        for change in rating_changes:
            saved = history[change["match_id"]]
            for key in ("rating_delta", "performance_score", "performance_bonus", "performance_details"):
                change[key] = saved[key]
            change["rating_before"] = current
            current += change["rating_delta"]
            change["rating_after"] = current
        for correction in corrections:
            correction["rating_before"] = current
            current += correction["rating_delta"]
            correction["rating_after"] = current
    rating_changes.extend(corrections)
    # A request may span midnight. September changes stay in stored history;
    # do not present them as changes to the newly reset October balance.
    current_id = db.ensure_current_season()["season_id"]
    rating_changes = [change for change in rating_changes if change["season_id"] == current_id]

    stored = {
        match["match_id"]: match for match in db.get_player_matches(account_id)
    } if new_match_ids or rating_changes else {}
    new_matches = [match for match in stored.values() if match["match_id"] in new_match_ids]
    rating_updates = [
        RatingUpdate(
            match_id=change["match_id"], start_time=stored[change["match_id"]]["start_time"],
            hero_id=stored[change["match_id"]]["hero_id"], win=bool(change["result"]),
            rating_before=change["rating_before"], rating_delta=change["rating_delta"],
            rating_after=change["rating_after"],
            performance_bonus=change["performance_bonus"],
            is_correction=change.get("is_correction", False),
            season_id=change["season_id"],
        ) for change in rating_changes
    ]
    nickname = profile.get("personaname")
    if isinstance(nickname, str) and nickname.strip():
        db.update_player_nickname(account_id, nickname)
    _last_sync_times[account_id] = time.time()
    return SyncResult(
        received_count=len(matches),
        new_matches=new_matches,
        skipped_old=skipped_old,
        skipped_duplicates=skipped_duplicates,
        rating_changes=rating_changes,
        rating_updates=rating_updates,
        performance_pending=len(db.get_pending_performance_matches(account_id, batch_size=50)),
    )
