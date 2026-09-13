"""Sync recent OpenDota matches into the local SQLite database."""

import argparse
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys

import httpx
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from app import db
from app.services.sync import ensure_player, sync_player


async def check_sync(account_id: int) -> None:
    db.init_db()
    profile = await ensure_player(account_id)
    player = db.get_player(account_id)
    if player is None:
        raise RuntimeError("Не удалось создать игрока в БД.")
    tracking_date = datetime.fromtimestamp(
        player["tracking_started_at"], tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")
    print(f"Игрок: {profile.get('personaname') or player['nickname'] or 'имя недоступно'}")
    print(f"Dota account_id: {account_id}")
    print(f"Tracking started: {tracking_date} UTC")
    if profile.get("fh_unavailable") is True:
        print(
            "OpenDota не может получить полную историю матчей этого аккаунта.\n"
            "Проверь Expose Public Match Data в Dota 2."
        )

    sync_result = await sync_player(account_id)
    print()
    print(f"Получено матчей из OpenDota: {sync_result.received_count}")
    print(f"Пропущено до начала отслеживания: {sync_result.skipped_old}")
    print(f"Пропущено дубликатов: {sync_result.skipped_duplicates}")
    print(f"Новых матчей: {sync_result.new_count}")
    game_modes = {1: "ALL PICK", 22: "ALL PICK", 23: "TURBO"}
    for match in sync_result.new_matches:
        mode = game_modes.get(match["game_mode"], f"GAME MODE {match['game_mode']}")
        result = {1: "WIN", 0: "LOSE", None: "UNKNOWN"}[match["win"]]
        print(f"{match['match_id']} | {mode} | {result}")
    print(f"Всего матчей игрока в БД: {db.count_player_matches(account_id)}")
    rating = db.get_rating(account_id)
    print(f"Turbo-матчей начислено в этом sync: {len(sync_result.rating_changes)}")
    if rating is not None:
        print(f"Turbo Rating: {rating['current_rating']:.0f}")


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Синхронизация матчей OpenDota в SQLite.")
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
        asyncio.run(check_sync(args.account_id))
    except httpx.HTTPStatusError as exc:
        print(f"OpenDota API вернул HTTP {exc.response.status_code}.", file=sys.stderr)
        return 1
    except httpx.RequestError as exc:
        print(f"Не удалось подключиться к OpenDota API ({type(exc).__name__}).", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError, ValueError, RuntimeError) as exc:
        print(f"Ошибка синхронизации: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
