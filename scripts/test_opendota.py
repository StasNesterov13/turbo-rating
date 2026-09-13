"""Fetch a player and their last 10 available Turbo matches from OpenDota."""

import argparse
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import sys

import httpx
from dotenv import load_dotenv

# Allow both python -m scripts.test_opendota and python scripts/test_opendota.py.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.opendota import OpenDotaClient


async def check_api(account_id: int) -> None:
    async with OpenDotaClient(api_key=os.getenv("OPENDOTA_API_KEY") or None) as client:
        player = await client.get_player(account_id)
        matches = await client.get_recent_turbo_matches(account_id, limit=10)

    if len(matches) > 10:
        raise ValueError("OpenDota вернул больше 10 матчей.")
    start_times = [match.get("start_time") for match in matches]
    if any(type(value) is not int for value in start_times):
        raise ValueError("В ответе OpenDota отсутствует время начала матча.")
    if start_times != sorted(start_times, reverse=True):
        raise ValueError("Матчи пришли не в порядке от новых к старым.")

    profile = player.get("profile") or {}
    print(f"Игрок: {profile.get('personaname') or 'имя недоступно'}")
    print(f"Dota account_id: {account_id}")
    if profile.get("fh_unavailable") is True:
        print(
            "OpenDota не может получить полную историю матчей этого аккаунта.\n"
            "Проверь Expose Public Match Data в Dota 2."
        )
    print(f"Получено Turbo-матчей: {len(matches)} из 10")
    if not matches:
        print("Доступных Turbo-матчей нет. Проверьте публичность истории матчей в Dota 2.")
    if matches:
        print("Время матчей: UTC")
    for match in matches:
        start_time = datetime.fromtimestamp(
            match["start_time"], tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M")
        is_radiant = match["player_slot"] < 128
        result = "WIN" if is_radiant == match["radiant_win"] else "LOSE"
        duration_minutes = match["duration"] / 60
        print(
            f"{match['match_id']} | {start_time} | hero_id={match['hero_id']} | "
            f"{result} | {duration_minutes:.1f} min"
        )


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(
        description="Проверка OpenDota API: игрок и последние 10 Turbo-матчей."
    )
    parser.add_argument(
        "account_id",
        nargs="?",
        type=int,
        default=os.getenv("DOTA_ACCOUNT_ID") or None,
        help="Dota account_id (Steam32); по умолчанию DOTA_ACCOUNT_ID из .env",
    )
    args = parser.parse_args()
    if args.account_id is None:
        parser.error("Укажите account_id аргументом или заполните DOTA_ACCOUNT_ID в .env.")

    try:
        asyncio.run(check_api(args.account_id))
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        message = f"OpenDota API вернул HTTP {status}."
        if status == 429:
            message += " Лимит запросов исчерпан; повторите позже."
        print(message, file=sys.stderr)
        return 1
    except httpx.RequestError as exc:
        print(f"Не удалось подключиться к OpenDota API ({type(exc).__name__}).", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
