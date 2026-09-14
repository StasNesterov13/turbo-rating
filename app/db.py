"""Local SQLite storage for tracked players and their matches."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Iterator

from app import season


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

            CREATE TABLE IF NOT EXISTS bot_users (
                telegram_id INTEGER PRIMARY KEY,
                created_at INTEGER NOT NULL
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

            CREATE TABLE IF NOT EXISTS season_final_standings (
                season_end_at INTEGER PRIMARY KEY,
                finalized_at INTEGER NOT NULL,
                standings_json TEXT NOT NULL
            );
            """
        )
        connection.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(matches)")}
        if "is_calibration" not in columns:
            connection.execute(
                "ALTER TABLE matches ADD COLUMN is_calibration INTEGER NOT NULL DEFAULT 0"
            )
        history_columns = {row["name"] for row in connection.execute("PRAGMA table_info(rating_history)")}
        for name, definition in (
            ("performance_score", "REAL NULL"),
            ("performance_bonus", "INTEGER NOT NULL DEFAULT 0"),
            ("performance_details", "TEXT NULL"),
            ("performance_attempted_at", "INTEGER NULL"),
            ("performance_applied_at", "INTEGER NULL"),
            ("performance_adjustment", "REAL NOT NULL DEFAULT 0"),
        ):
            if name not in history_columns:
                connection.execute(f"ALTER TABLE rating_history ADD COLUMN {name} {definition}")
        _finalize_season(connection)


_LEADERBOARD_QUERY = """
    SELECT p.account_id, p.nickname, r.current_rating
    FROM ratings r JOIN players p ON p.account_id = r.account_id
    WHERE EXISTS (SELECT 1 FROM telegram_users t WHERE t.account_id = p.account_id)
    ORDER BY r.current_rating DESC, p.account_id ASC
"""


def _finalize_season(connection: sqlite3.Connection) -> list[dict[str, Any]] | None:
    """Read/fix the complete standings once, under the caller's write lock.

    A single snapshot row also records an empty season. Names, membership, ties
    and places below top-20 survive account switches, renames and restarts.
    """
    end_at = int(season.SEASON_END_AT.timestamp())
    row = connection.execute(
        "SELECT standings_json FROM season_final_standings WHERE season_end_at = ?", (end_at,),
    ).fetchone()
    if row is not None:
        return json.loads(row["standings_json"])
    if not season.is_season_over():
        return None
    standings = [dict(row, position=position) for position, row in enumerate(
        connection.execute(_LEADERBOARD_QUERY).fetchall(), start=1,
    )]
    connection.execute(
        "INSERT INTO season_final_standings VALUES (?, ?, ?)",
        (end_at, int(season.now().timestamp()), json.dumps(standings, ensure_ascii=False)),
    )
    return standings


def get_final_standings() -> list[dict[str, Any]] | None:
    """None while active; a persistent snapshot (possibly empty) once finished."""
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        return _finalize_season(connection)


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


def update_player_nickname(account_id: int, nickname: str) -> None:
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _finalize_season(connection)
        connection.execute(
            "UPDATE players SET nickname = ? WHERE account_id = ? AND nickname IS NOT ?",
            (nickname, account_id, nickname),
        )


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


def is_known_telegram_user(telegram_id: int) -> bool:
    """Remember visitors before linking, including users linked before this migration."""
    with _connect() as connection:
        return connection.execute(
            """SELECT 1 FROM bot_users WHERE telegram_id = ?
               UNION ALL SELECT 1 FROM telegram_users WHERE telegram_id = ? LIMIT 1""",
            (telegram_id, telegram_id),
        ).fetchone() is not None


def remember_telegram_user(telegram_id: int) -> None:
    with _connect() as connection:
        connection.execute(
            """INSERT INTO bot_users (telegram_id, created_at) VALUES (?, ?)
               ON CONFLICT (telegram_id) DO NOTHING""", (telegram_id, int(time.time())),
        )


def link_telegram_user(telegram_id: int, account_id: int) -> None:
    """Atomically replace one user's link, preserving all player data."""
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _finalize_season(connection)
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
) -> dict[str, Any] | None:
    """Atomically store calibration and rating once, including concurrent callers."""
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        final = _finalize_season(connection)
        existing = connection.execute(
            "SELECT * FROM ratings WHERE account_id = ?", (account_id,)
        ).fetchone()
        if existing is not None:
            return dict(existing)
        if final is not None:
            return None
        connection.execute("SAVEPOINT calibration")
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
        if season.is_season_over():
            connection.execute("ROLLBACK TO calibration")
            _finalize_season(connection)
            return None
        return dict(connection.execute(
            "SELECT * FROM ratings WHERE account_id = ?", (account_id,)
        ).fetchone())


_PENDING_RATINGS_QUERY = """
    SELECT m.* FROM matches m
    JOIN players p ON p.account_id = m.account_id
    WHERE m.account_id = ? AND m.game_mode = 23 AND m.is_calibration = 0
        AND m.start_time >= p.tracking_started_at AND m.win IN (0, 1)
        AND NOT EXISTS (
            SELECT 1 FROM rating_history h
            WHERE h.account_id = m.account_id AND h.match_id = m.match_id
        )
    ORDER BY m.start_time ASC, m.match_id ASC
"""


def get_pending_rating_matches(account_id: int) -> list[dict[str, Any]]:
    """Read unrated candidates; the base rating transaction rechecks them."""
    with _connect() as connection:
        return [dict(row) for row in connection.execute(_PENDING_RATINGS_QUERY, (account_id,))]


def get_pending_performance_matches(
    account_id: int, *, recent_limit: int = 50, batch_size: int = 20,
) -> list[dict[str, Any]]:
    """Retry missing scores among recent rated games, rotating failed attempts."""
    if any(type(value) is not int or value <= 0 for value in (recent_limit, batch_size)):
        raise ValueError("Лимиты performance должны быть положительными целыми числами.")
    with _connect() as connection:
        return [dict(row) for row in connection.execute(
            """
            SELECT * FROM (
                SELECT h.*, m.start_time FROM rating_history h
                JOIN matches m ON m.account_id = h.account_id AND m.match_id = h.match_id
                JOIN players p ON p.account_id = m.account_id
                WHERE h.account_id = ? AND m.game_mode = 23 AND m.is_calibration = 0
                    AND m.start_time >= p.tracking_started_at AND m.win IN (0, 1)
                ORDER BY m.start_time DESC, m.match_id DESC LIMIT ?
            ) WHERE performance_score IS NULL
            ORDER BY COALESCE(performance_attempted_at, 0), start_time, match_id
            LIMIT ?
            """, (account_id, recent_limit, batch_size),
        )]


def apply_match_performance(
    account_id: int, match_id: int, performance: dict[str, Any],
) -> dict[str, Any] | None:
    """Atomically fill a missing score and add only the previously unapplied bonus.

    Keep one history row per match and shift subsequent stored balances in actual
    insertion order. The adjustment timestamp preserves historical TR and gains.
    NULL remains pending; a calculated zero is final.
    """
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if _finalize_season(connection) is not None:
            return None
        connection.execute("SAVEPOINT performance_update")
        history = connection.execute(
            """SELECT rowid AS history_id, * FROM rating_history
               WHERE account_id = ? AND match_id = ? AND performance_score IS NULL""",
            (account_id, match_id),
        ).fetchone()
        if history is None:
            return None
        now = int(time.time())
        details = performance.get("performance_details")
        connection.execute(
            """UPDATE rating_history SET performance_attempted_at = ?, performance_details = ?
               WHERE account_id = ? AND match_id = ?""",
            (now, json.dumps(details, allow_nan=False) if details is not None else None, account_id, match_id),
        )
        change = None
        if performance.get("performance_score") is not None:
            bonus = performance["performance_bonus"]
            adjustment = bonus - history["performance_bonus"]
            current = connection.execute(
                "SELECT current_rating FROM ratings WHERE account_id = ?", (account_id,),
            ).fetchone()["current_rating"]
            connection.execute(
                """UPDATE rating_history SET performance_score = ?, performance_bonus = ?,
                       performance_applied_at = ?, performance_adjustment = ?,
                       rating_delta = rating_delta + ?, rating_after = rating_after + ?
                   WHERE account_id = ? AND match_id = ?""",
                (performance["performance_score"], bonus, now, adjustment,
                 adjustment, adjustment, account_id, match_id),
            )
            connection.execute(
                """UPDATE rating_history SET rating_before = rating_before + ?,
                       rating_after = rating_after + ? WHERE account_id = ? AND rowid > ?""",
                (adjustment, adjustment, account_id, history["history_id"]),
            )
            connection.execute(
                "UPDATE ratings SET current_rating = current_rating + ? WHERE account_id = ?",
                (adjustment, account_id),
            )
            change = dict(
                account_id=account_id, match_id=match_id, result=history["result"],
                rating_before=current, rating_delta=adjustment, rating_after=current + adjustment,
                performance_bonus=adjustment, is_correction=True,
                performance_score=performance["performance_score"],
                performance_details=json.dumps(details, allow_nan=False) if details is not None else None,
            )
        if season.is_season_over():
            connection.execute("ROLLBACK TO performance_update")
            _finalize_season(connection)
            return None
        return change


def apply_pending_ratings(
    account_id: int, calculate: Callable[[float, bool, int], tuple[float, float, float]],
    *, performance_by_match: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Atomically rate eligible matches missing history, oldest first."""
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if _finalize_season(connection) is not None:
            return []
        connection.execute("SAVEPOINT rating_batch")
        rating = connection.execute(
            "SELECT current_rating FROM ratings WHERE account_id = ?", (account_id,)
        ).fetchone()
        if rating is None:
            raise ValueError("Рейтинг ещё не инициализирован.")
        matches = connection.execute(_PENDING_RATINGS_QUERY, (account_id,)).fetchall()
        current = rating["current_rating"]
        changes = []
        for match in matches:
            performance = (performance_by_match or {}).get(match["match_id"], {})
            bonus = performance.get("performance_bonus", 0)
            new_rating, delta, expected = calculate(current, bool(match["win"]), bonus)
            details = performance.get("performance_details")
            change = dict(
                account_id=account_id, match_id=match["match_id"], rating_before=current,
                expected_score=expected, result=match["win"], rating_delta=delta,
                rating_after=new_rating, created_at=int(time.time()),
                performance_score=performance.get("performance_score"), performance_bonus=bonus,
                performance_details=json.dumps(details, allow_nan=False) if details is not None else None,
            )
            connection.execute(
                """
                INSERT INTO rating_history (
                    account_id, match_id, rating_before, expected_score,
                    result, rating_delta, rating_after, created_at,
                    performance_score, performance_bonus, performance_details
                ) VALUES (:account_id, :match_id, :rating_before, :expected_score,
                    :result, :rating_delta, :rating_after, :created_at,
                    :performance_score, :performance_bonus, :performance_details)
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
        # A calculation started before the deadline may finish after it.
        if season.is_season_over():
            connection.execute("ROLLBACK TO rating_batch")
            _finalize_season(connection)
            return []
    return changes


def get_rating_history(
    account_id: int, limit: int | None = None, *, by_recorded_time: bool = False,
) -> list[dict[str, Any]]:
    query = """
        SELECT h.*, m.hero_id, m.start_time FROM rating_history h
        JOIN matches m ON m.account_id = h.account_id AND m.match_id = h.match_id
        WHERE h.account_id = ?
    """
    order = "h.created_at DESC, h.rowid DESC" if by_recorded_time else "m.start_time DESC, m.match_id DESC"
    query += " ORDER BY " + order
    parameters = [account_id]
    if limit is not None:
        query += " LIMIT ?"
        parameters.append(limit)
    with _connect() as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def get_turbo_match_history(
    account_id: int, limit: int = 10, *, since_timestamp: int | None = None,
) -> list[dict[str, Any]]:
    """Read recent tracked Turbo matches with their original stored TR changes."""
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit должен быть положительным целым числом.")
    if since_timestamp is not None and type(since_timestamp) is not int:
        raise ValueError("since_timestamp должен быть целым Unix-временем.")
    query = """
        SELECT m.*, h.rating_delta, h.rating_after, h.performance_bonus FROM matches m
        JOIN players p ON p.account_id = m.account_id
        LEFT JOIN rating_history h ON h.account_id = m.account_id AND h.match_id = m.match_id
        WHERE m.account_id = ? AND m.game_mode = 23 AND m.is_calibration = 0
            AND m.start_time >= p.tracking_started_at
    """
    parameters = [account_id]
    if since_timestamp is not None:
        query += " AND m.start_time >= ?"
        parameters.append(since_timestamp)
    query += " ORDER BY m.start_time DESC, m.match_id DESC LIMIT ?"
    parameters.append(limit)
    with _connect() as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [dict(row) for row in rows]


def count_rated_matches(account_id: int) -> int:
    with _connect() as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM rating_history WHERE account_id = ?", (account_id,)
        ).fetchone()[0]


def get_leaderboard(limit: int = 20) -> list[dict[str, Any]]:
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit должен быть положительным целым числом.")
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        final = _finalize_season(connection)
        if final is not None:
            return [{key: row[key] for key in ("account_id", "nickname", "current_rating")}
                    for row in final[:min(limit, 20)]]
        return [dict(row) for row in connection.execute(
            _LEADERBOARD_QUERY + " LIMIT ?",
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


def get_leaderboard_position(account_id: int, *, rating: float | None = None) -> dict[str, Any] | None:
    """Rank an active player, optionally at another rating without changing it."""
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        final = _finalize_season(connection)
        if final is not None:
            return next((
                {key: row[key] for key in ("account_id", "current_rating", "position")}
                for row in final if row["account_id"] == account_id
            ), None)
        row = connection.execute(
            """
            WITH subject AS (
                SELECT account_id, COALESCE(?, current_rating) AS current_rating
                FROM ratings WHERE account_id = ?
            )
            SELECT r.account_id, r.current_rating,
                1 + (SELECT COUNT(*) FROM ratings other
                     WHERE other.account_id <> r.account_id
                       AND (other.current_rating > r.current_rating
                        OR (other.current_rating = r.current_rating
                            AND other.account_id < r.account_id))
                       AND EXISTS (SELECT 1 FROM telegram_users t
                                   WHERE t.account_id = other.account_id)) AS position
            FROM subject r
            WHERE EXISTS (SELECT 1 FROM telegram_users t
                          WHERE t.account_id = r.account_id)
            """,
            (rating, account_id),
        ).fetchone()
    return dict(row) if row is not None else None


def get_turbo_form(account_id: int) -> dict[str, Any]:
    """Recent completed Turbo results and the full current streak, newest first."""
    results = []
    streak = 0
    streak_win = None
    continuing = True
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT m.win FROM matches m JOIN players p ON p.account_id = m.account_id
            WHERE m.account_id = ? AND m.game_mode = 23 AND m.is_calibration = 0
                AND m.start_time >= p.tracking_started_at AND m.win IS NOT NULL
            ORDER BY m.start_time DESC, m.match_id DESC
            """, (account_id,),
        )
        for row in rows:
            win = bool(row["win"])
            if streak_win is None:
                streak_win = win
            if len(results) < 5:
                results.append("W" if win else "L")
            continuing = continuing and win == streak_win
            if continuing:
                streak += 1
            elif len(results) == 5:
                break
    return {"results": results, "streak": streak, "streak_win": streak_win}


# Base changes and recovered performance enter TR at their own recorded times.
_RATING_AT_QUERY = """
    SELECT p.account_id, p.nickname,
        r.initial_rating + COALESCE((
            SELECT SUM(
                CASE WHEN h.created_at <= :timestamp
                    THEN h.rating_delta - h.performance_adjustment ELSE 0 END
                + CASE WHEN h.performance_applied_at <= :timestamp
                    THEN h.performance_adjustment ELSE 0 END
            ) FROM rating_history h WHERE h.account_id = r.account_id
        ), 0) AS rating
    FROM ratings r JOIN players p ON p.account_id = r.account_id
    WHERE p.tracking_started_at <= :timestamp
"""


def get_rating_at(account_id: int, timestamp: int) -> float | None:
    """Saved TR at/before timestamp; None before registration or without a rating."""
    with _connect() as connection:
        row = connection.execute(
            _RATING_AT_QUERY + " AND p.account_id = :account_id",
            {"account_id": account_id, "timestamp": timestamp},
        ).fetchone()
    return row["rating"] if row is not None else None


def get_leaderboard_at(timestamp: int) -> list[dict[str, Any]]:
    """Reconstruct every currently active player's rank, without a top-20 limit."""
    with _connect() as connection:
        rows = connection.execute(
            _RATING_AT_QUERY + """
                AND EXISTS (SELECT 1 FROM telegram_users t WHERE t.account_id = p.account_id)
                ORDER BY rating DESC, p.account_id ASC
            """, {"timestamp": timestamp},
        ).fetchall()
    return [{**dict(row), "position": position} for position, row in enumerate(rows, 1)]


def get_rank_at(account_id: int, timestamp: int) -> int | None:
    return next((row["position"] for row in get_leaderboard_at(timestamp)
                 if row["account_id"] == account_id), None)


def get_peak_rating(account_id: int) -> float | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT MAX(r.initial_rating, COALESCE(MAX(h.rating_after), r.initial_rating)) AS peak
            FROM ratings r LEFT JOIN rating_history h ON h.account_id = r.account_id
            WHERE r.account_id = ? GROUP BY r.account_id
            """, (account_id,),
        ).fetchone()
    return row["peak"] if row is not None else None


def get_rating_change(account_id: int, since_timestamp: int) -> float | None:
    """Sum saved changes since the boundary (inclusive); initialization is not a gain."""
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT COALESCE(SUM(
                CASE WHEN h.created_at >= :since
                    THEN h.rating_delta - h.performance_adjustment ELSE 0 END
                + CASE WHEN h.performance_applied_at >= :since
                    THEN h.performance_adjustment ELSE 0 END
            ), 0.0) AS change
            FROM ratings r LEFT JOIN rating_history h
                ON h.account_id = r.account_id
            WHERE r.account_id = :account_id GROUP BY r.account_id
            """, {"since": since_timestamp, "account_id": account_id},
        ).fetchone()
    return row["change"] if row is not None else None
