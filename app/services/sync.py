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
# Waiting/running calls keep a strong reference; idle locks can be released.
_sync_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
# Display-only timestamps; a restart waits for the next successful sync.
_last_sync_times: dict[int, float] = {}


class MatchHistoryUnavailable(ValueError):
    """OpenDota explicitly reports that the player's history is unavailable."""


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


@dataclass
class SyncResult:
    received_count: int
    new_matches: list[dict[str, Any]]
    skipped_old: int = 0
    skipped_duplicates: int = 0
    rating_changes: list[dict[str, Any]] = field(default_factory=list)
    rating_updates: list[RatingUpdate] = field(default_factory=list)

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
    """Serialize synchronization per account within this application process."""
    lock = _sync_locks.setdefault(account_id, asyncio.Lock())
    async with lock:
        logger.info("Sync started account_id=%s", account_id)
        try:
            result = await _sync_player(account_id, api_key=api_key)
        except Exception as exc:
            # HTTP exception strings may contain an API key in the request URL.
            logger.error("Sync failed account_id=%s error=%s", account_id, type(exc).__name__)
            raise
        logger.info(
            "Sync completed account_id=%s received=%s new_matches=%s rating_updates=%s",
            account_id, result.received_count, result.new_count, len(result.rating_updates),
        )
        return result


async def _sync_player(account_id: int, *, api_key: str | None = None) -> SyncResult:
    player = db.get_player(account_id)
    if player is None:
        raise ValueError("Игрок ещё не добавлен в БД.")
    performance_by_match = {}
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

        # This also recovers saved, unrated matches after an interruption.
        # No network I/O is performed while holding the rating write transaction.
        if db.get_final_standings() is None:
            for pending in db.get_pending_rating_matches(account_id):
                match_id = pending["match_id"]
                try:
                    full_match = await client.get_match(match_id)
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning(
                        "Performance unavailable account_id=%s match_id=%s error=%s",
                        account_id, match_id, type(exc).__name__,
                    )
                    full_match = None
                performance_by_match[match_id] = get_match_performance(full_match, account_id)

    # Recover matches saved before an interrupted rating update as well as new ones.
    rating_changes = apply_rating_changes(account_id, performance_by_match)
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
    )
