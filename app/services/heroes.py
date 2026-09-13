"""Hero names loaded once at startup and kept in memory for the process lifetime."""

import asyncio
import logging

import httpx


logger = logging.getLogger(__name__)
_hero_names: dict[int, str] = {}
_loaded = False
_load_lock = asyncio.Lock()


async def load_heroes() -> None:
    global _loaded
    if _loaded:
        return
    async with _load_lock:
        if _loaded:
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get("https://api.opendota.com/api/constants/heroes")
                response.raise_for_status()
                heroes = response.json()
            if not isinstance(heroes, dict):
                raise ValueError("Invalid hero constants")
            names = {}
            for hero in heroes.values():
                if isinstance(hero, dict) and type(hero.get("id")) is int:
                    name = hero.get("localized_name")
                    if isinstance(name, str) and name.strip():
                        names[hero["id"]] = " ".join(name.split())[:60]
            if not names:
                raise ValueError("Empty hero constants")
            _hero_names.update(names)
            logger.info("Hero names loaded count=%s", len(names))
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Hero constants unavailable error=%s; using fallback", type(exc).__name__)
        _loaded = True


def get_hero_name(hero_id: int | None) -> str:
    if hero_id is None:
        return "Неизвестный герой"
    return _hero_names.get(hero_id, f"Hero #{hero_id}")
