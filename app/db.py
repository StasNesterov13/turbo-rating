"""Local SQLite storage for tracked players and their matches."""

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Iterator


DB_PATH = Path(
    os.getenv("DB_PATH")
    or Path(__file__).resolve().parents[1] / "data" / "turbo_rating.db"
)


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
                account_id INTEGER PRIMARY KEY,
                nickname TEXT,
                tracking_started_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS matches (
                account_id INTEGER NOT NULL,
                match_id INTEGER NOT NULL,
                start_time INTEGER NOT NULL,
                game_mode INTEGER,
                hero_id INTEGER,
                player_slot INTEGER,
                radiant_win INTEGER,
                win INTEGER,
                duration INTEGER,
                created_at INTEGER NOT NULL,
                is_calibration INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (account_id, match_id)
            );

            CREATE TABLE IF NOT EXISTS telegram_users (
                telegram_id INTEGER PRIMARY KEY,
                account_id INTEGER NOT NULL REFERENCES players(account_id)
            );

            CREATE TABLE IF NOT EXISTS ratings (
                account_id INTEGER PRIMARY KEY REFERENCES players(account_id),
                initial_rating REAL NOT NULL,
                current_rating REAL NOT NULL,
                calibration_matches INTEGER NOT NULL,
                calibration_wins INTEGER NOT NULL,
                initialized_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rating_history (
                account_id INTEGER NOT NULL REFERENCES ratings(account_id),
                match_id INTEGER NOT NULL,
                rating_before REAL NOT NULL,
                expected_score REAL NOT NULL,
                result INTEGER NOT NULL,
                rating_delta REAL NOT NULL,
                rating_after REAL NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (account_id, match_id),
                FOREIGN KEY (account_id, match_id) REFERENCES matches(account_id, match_id)
            );
            """
        )
        connection.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(matches)")}
        if "is_calibration" not in columns:
            connection.execute(
                "ALTER TABLE matches ADD COLUMN is_calibration INTEGER NOT NULL DEFAULT 0"
            )


def add_player(
    account_id: int, nickname: str | None, tracking_started_at: int
) -> None:
    """Insert a player without changing an existing tracking start time."""
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO players (account_id, nickname, tracking_started_at, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (account_id) DO NOTHING
            """,
            (account_id, nickname, tracking_started_at, int(time.time())),
        )


def get_player(account_id: int) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM players WHERE account_id = ?", (account_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def get_match_win(match: dict[str, Any]) -> bool | None:
    """Determine the player's result; incomplete results remain unknown."""
    player_slot = match.get("player_slot")
    radiant_win = match.get("radiant_win")
    if type(player_slot) is not int or radiant_win not in (True, False):
        return None
    return radiant_win == (player_slot < 128)


def _save_match(
    connection: sqlite3.Connection, account_id: int, match: dict[str, Any],
    is_calibration: bool = False,
) -> bool:
    cursor = connection.execute(
        """
        INSERT INTO matches (
            account_id, match_id, start_time, game_mode, hero_id,
            player_slot, radiant_win, win, duration, created_at, is_calibration
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (account_id, match_id) DO NOTHING
        """,
        (
            account_id,
            match["match_id"],
            match["start_time"],
            match.get("game_mode"),
            match.get("hero_id"),
            match.get("player_slot"),
            match.get("radiant_win"),
            get_match_win(match),
            match.get("duration"),
            int(time.time()),
            int(is_calibration),
        ),
    )
    return cursor.rowcount == 1


def save_match(
    account_id: int, match: dict[str, Any], *, is_calibration: bool = False
) -> bool:
    """Insert a match; return False for a duplicate. Unknown results stay NULL."""
    with _connect() as connection:
        return _save_match(connection, account_id, match, is_calibration)


def match_exists(account_id: int, match_id: int) -> bool:
    with _connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM matches WHERE account_id = ? AND match_id = ?",
            (account_id, match_id),
        ).fetchone()
    return row is not None


def get_player_matches(
    account_id: int, limit: int | None = None, *, include_calibration: bool = False
) -> list[dict[str, Any]]:
    query = "SELECT * FROM matches WHERE account_id = ?"
    if not include_calibration:
        query += " AND is_calibration = 0"
    query += " ORDER BY start_time DESC, match_id DESC"
    parameters = [account_id]
    if limit is not None:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit должен быть положительным целым числом.")
        query += " LIMIT ?"
        parameters.append(limit)
    with _connect() as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [dict(row) for row in rows]


def link_telegram_user(telegram_id: int, account_id: int) -> None:
    """Link one Dota account per Telegram user; repeated links are idempotent."""
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO telegram_users (telegram_id, account_id) VALUES (?, ?)
            ON CONFLICT (telegram_id) DO UPDATE SET account_id = excluded.account_id
            """,
            (telegram_id, account_id),
        )


def get_telegram_player(telegram_id: int) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT players.* FROM players
            JOIN telegram_users ON telegram_users.account_id = players.account_id
            WHERE telegram_users.telegram_id = ?
            """,
            (telegram_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def count_player_matches(account_id: int) -> int:
    with _connect() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM matches WHERE account_id = ? AND is_calibration = 0",
            (account_id,),
        ).fetchone()[0]


def get_rating(account_id: int) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM ratings WHERE account_id = ?", (account_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def create_rating(
    account_id: int, initial_rating: float, calibration_matches: list[dict[str, Any]],
    calibration_wins: int,
) -> dict[str, Any]:
    """Atomically store calibration and rating once, including concurrent callers."""
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT * FROM ratings WHERE account_id = ?", (account_id,)
        ).fetchone()
        if existing is not None:
            return dict(existing)
        for match in calibration_matches:
            _save_match(connection, account_id, match, is_calibration=True)
            connection.execute(
                "UPDATE matches SET is_calibration = 1 WHERE account_id = ? AND match_id = ?",
                (account_id, match["match_id"]),
            )
        connection.execute(
            """
            INSERT INTO ratings (
                account_id, initial_rating, current_rating,
                calibration_matches, calibration_wins, initialized_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (account_id, initial_rating, initial_rating,
             len(calibration_matches), calibration_wins, int(time.time())),
        )
        return dict(connection.execute(
            "SELECT * FROM ratings WHERE account_id = ?", (account_id,)
        ).fetchone())


def apply_pending_ratings(
    account_id: int, calculate: Callable[[float, bool], tuple[float, float, float]]
) -> list[dict[str, Any]]:
    """Atomically rate eligible matches missing history, oldest first."""
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rating = connection.execute(
            "SELECT current_rating FROM ratings WHERE account_id = ?", (account_id,)
        ).fetchone()
        if rating is None:
            raise ValueError("Рейтинг ещё не инициализирован.")
        matches = connection.execute(
            """
            SELECT m.* FROM matches m
            JOIN players p ON p.account_id = m.account_id
            WHERE m.account_id = ? AND m.game_mode = 23 AND m.is_calibration = 0
                AND m.start_time >= p.tracking_started_at AND m.win IN (0, 1)
                AND NOT EXISTS (
                    SELECT 1 FROM rating_history h
                    WHERE h.account_id = m.account_id AND h.match_id = m.match_id
                )
            ORDER BY m.start_time ASC, m.match_id ASC
            """,
            (account_id,),
        ).fetchall()
        current = rating["current_rating"]
        changes = []
        for match in matches:
            new_rating, delta, expected = calculate(current, bool(match["win"]))
            change = dict(
                account_id=account_id, match_id=match["match_id"], rating_before=current,
                expected_score=expected, result=match["win"], rating_delta=delta,
                rating_after=new_rating, created_at=int(time.time()),
            )
            connection.execute(
                """
                INSERT INTO rating_history (
                    account_id, match_id, rating_before, expected_score,
                    result, rating_delta, rating_after, created_at
                ) VALUES (:account_id, :match_id, :rating_before, :expected_score,
                    :result, :rating_delta, :rating_after, :created_at)
                """,
                change,
            )
            changes.append(change)
            current = new_rating
        if changes:
            connection.execute(
                "UPDATE ratings SET current_rating = ? WHERE account_id = ?",
                (current, account_id),
            )
    return changes


def get_rating_history(account_id: int, limit: int | None = None) -> list[dict[str, Any]]:
    query = """
        SELECT h.*, m.hero_id, m.start_time FROM rating_history h
        JOIN matches m ON m.account_id = h.account_id AND m.match_id = h.match_id
        WHERE h.account_id = ? ORDER BY m.start_time DESC, m.match_id DESC
    """
    parameters = [account_id]
    if limit is not None:
        query += " LIMIT ?"
        parameters.append(limit)
    with _connect() as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def count_rated_matches(account_id: int) -> int:
    with _connect() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM rating_history WHERE account_id = ?", (account_id,)
        ).fetchone()[0]


def get_leaderboard(limit: int = 20) -> list[dict[str, Any]]:
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit должен быть положительным целым числом.")
    with _connect() as connection:
        return [dict(row) for row in connection.execute(
            """
            SELECT p.account_id, p.nickname, r.current_rating
            FROM ratings r JOIN players p ON p.account_id = r.account_id
            ORDER BY r.current_rating DESC, p.account_id ASC LIMIT ?
            """,
            (min(limit, 20),),
        ).fetchall()]


def get_tracked_account_ids() -> list[int]:
    with _connect() as connection:
        return [row[0] for row in connection.execute(
            "SELECT DISTINCT account_id FROM telegram_users ORDER BY account_id"
        ).fetchall()]


def get_telegram_ids_by_account(account_id: int) -> list[int]:
    with _connect() as connection:
        return [row[0] for row in connection.execute(
            "SELECT telegram_id FROM telegram_users WHERE account_id = ? ORDER BY telegram_id",
            (account_id,),
        ).fetchall()]


def get_turbo_stats(account_id: int) -> dict[str, Any]:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS matches,
                COALESCE(SUM(m.win = 1), 0) AS wins,
                COALESCE(SUM(m.win = 0), 0) AS losses
            FROM matches m JOIN players p ON p.account_id = m.account_id
            WHERE m.account_id = ? AND m.game_mode = 23 AND m.is_calibration = 0
                AND m.start_time >= p.tracking_started_at
            """,
            (account_id,),
        ).fetchone()
    stats = dict(row)
    completed = stats["wins"] + stats["losses"]
    stats["unknown"] = stats["matches"] - completed
    stats["winrate"] = stats["wins"] / completed * 100 if completed else 0.0
    return stats


def get_leaderboard_position(account_id: int) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT r.account_id, r.current_rating,
                1 + (SELECT COUNT(*) FROM ratings other
                     WHERE other.current_rating > r.current_rating
                        OR (other.current_rating = r.current_rating
                            AND other.account_id < r.account_id)) AS position
            FROM ratings r WHERE r.account_id = ?
            """,
            (account_id,),
        ).fetchone()
    return dict(row) if row is not None else None
