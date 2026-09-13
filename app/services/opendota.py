"""Minimal asynchronous OpenDota API client."""

from types import TracebackType
from typing import Any, Self

import httpx


class OpenDotaClient:
    BASE_URL = "https://api.opendota.com/api/"
    TURBO_GAME_MODE = 23

    def __init__(self, api_key: str | None = None, timeout: float = 20.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={"Accept": "application/json"},
            params={"api_key": api_key} if api_key else None,
            timeout=timeout,
        )

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _validate_account_id(account_id: int) -> None:
        if type(account_id) is not int or not 0 < account_id < 2**32:
            raise ValueError(
                "account_id должен быть положительным 32-битным Dota ID, "
                "а не SteamID64."
            )

    async def get_player(self, account_id: int) -> dict[str, Any]:
        """Return the player data using a Dota account_id (Steam32)."""
        self._validate_account_id(account_id)
        response = await self._client.get(f"players/{account_id}")
        response.raise_for_status()
        player = response.json()
        if not isinstance(player, dict):
            raise ValueError("OpenDota вернул неожиданный формат данных игрока.")
        return player

    async def get_recent_matches(
        self, account_id: int, limit: int = 20, *, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return recent matches across all game modes, newest first."""
        self._validate_account_id(account_id)
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit должен быть положительным целым числом.")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset должен быть неотрицательным целым числом.")
        response = await self._client.get(
            f"players/{account_id}/matches",
            params={"limit": limit, "offset": offset, "significant": 0, "sort": "start_time"},
        )
        response.raise_for_status()
        matches = response.json()
        if not isinstance(matches, list) or any(
            not isinstance(match, dict) for match in matches
        ):
            raise ValueError("OpenDota вернул неожиданный формат матчей.")
        return matches

    async def get_matches_for_sync(
        self, account_id: int, tracking_started_at: int,
        *, page_size: int = 50, max_pages: int = 1000,
    ) -> list[dict[str, Any]]:
        """Read pages to the tracking boundary; the final page may include older games."""
        self._validate_account_id(account_id)
        if type(tracking_started_at) is not int or tracking_started_at <= 0:
            raise ValueError("tracking_started_at должен быть положительным Unix-временем.")
        if any(type(value) is not int or value <= 0 for value in (page_size, max_pages)):
            raise ValueError("page_size и max_pages должны быть положительными целыми числами.")
        found = {}
        for page_index in range(max_pages):
            page = await self.get_recent_matches(
                account_id, limit=page_size, offset=page_index * page_size
            )
            previous_count = len(found)
            for match in page:
                if type(match.get("start_time")) is not int or type(match.get("match_id")) is not int:
                    raise ValueError("OpenDota вернул матч без корректного времени или match_id.")
                found[match["match_id"]] = match
            if len(page) < page_size or any(m["start_time"] < tracking_started_at for m in page):
                return sorted(found.values(), key=lambda m: (m["start_time"], m["match_id"]), reverse=True)
            if len(found) == previous_count:
                raise ValueError("OpenDota повторяет страницу: синхронизация прервана.")
        raise ValueError("Достигнут лимит страниц OpenDota: история не дочитана, sync прерван.")

    async def get_recent_turbo_matches(
        self, account_id: int, limit: int = 10, *, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Return up to limit available Turbo matches, newest first."""
        self._validate_account_id(account_id)
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit должен быть положительным целым числом.")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset должен быть неотрицательным целым числом.")

        response = await self._client.get(
            f"players/{account_id}/matches",
            params={
                "game_mode": self.TURBO_GAME_MODE,
                "limit": limit,
                "offset": offset,
                # Turbo is excluded by the default significant-match filter.
                "significant": 0,
                "sort": "start_time",
            },
        )
        response.raise_for_status()
        matches = response.json()
        if not isinstance(matches, list) or any(
            not isinstance(match, dict)
            or match.get("game_mode") != self.TURBO_GAME_MODE
            for match in matches
        ):
            raise ValueError("OpenDota вернул неожиданный формат Turbo-матчей.")
        return matches

    async def get_turbo_matches_before(
        self, account_id: int, before_timestamp: int, limit: int = 20,
        *, page_size: int = 50, max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        """Find up to 20 historical Turbo matches using bounded offset pagination."""
        self._validate_account_id(account_id)
        if type(before_timestamp) is not int or before_timestamp <= 0:
            raise ValueError("before_timestamp должен быть положительным Unix-временем.")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("limit исторических матчей должен быть от 1 до 20.")
        if any(type(value) is not int or value <= 0 for value in (page_size, max_pages)):
            raise ValueError("page_size и max_pages должны быть положительными целыми числами.")
        found = {}
        for page_index in range(max_pages):
            page = await self.get_recent_turbo_matches(
                account_id, limit=page_size, offset=page_index * page_size
            )
            for match in page:
                if type(match.get("start_time")) is not int or type(match.get("match_id")) is not int:
                    raise ValueError("OpenDota вернул матч без корректного времени или match_id.")
                if match["start_time"] < before_timestamp:
                    found[match["match_id"]] = match
            if len(found) >= limit or len(page) < page_size:
                break
        return sorted(
            found.values(), key=lambda match: (match["start_time"], match["match_id"]),
            reverse=True,
        )[:limit]
