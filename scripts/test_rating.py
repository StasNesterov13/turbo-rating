"""Offline rating, SQLite, pagination and Telegram regression tests."""

import asyncio
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
import io
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from app import db
from app.services.opendota import OpenDotaClient
from app.services.rating import (
    calculate_initial_rating, calculate_rating_delta, calculate_new_rating,
    initialize_rating, apply_rating_changes,
)
from app.services.sync import sync_player, ensure_player


def match(match_id, start_time, win=True, game_mode=23, player_slot=0):
    return dict(
        match_id=match_id, start_time=start_time, game_mode=game_mode,
        player_slot=player_slot, radiant_win=win if player_slot < 128 else not win,
        hero_id=44, duration=1458,
    )


class MathematicsTests(unittest.TestCase):
    def test_no_history(self):
        self.assertEqual(calculate_initial_rating(0, 0), 1000)

    def test_balanced_history(self):
        self.assertEqual(calculate_initial_rating(10, 20), 1000)

    def test_twenty_wins(self):
        self.assertAlmostEqual(calculate_initial_rating(20, 20), 1279.59, places=2)

    def test_twenty_losses(self):
        self.assertAlmostEqual(calculate_initial_rating(0, 20), 720.41, places=2)

    def test_progressive_examples(self):
        for rating, win_delta, loss_delta in (
            (1000, 16, -12), (1200, 18, -13), (1400, 20, -14),
            (1600, 22, -15), (1800, 24, -16), (2000, 26, -17), (3000, 36, -22),
        ):
            for win, delta in ((True, win_delta), (False, loss_delta)):
                with self.subTest(rating=rating, win=win):
                    self.assertEqual(calculate_rating_delta(rating, win), delta)
                    result = calculate_new_rating(rating, win)
                    self.assertEqual(result, (rating + delta, delta, 0.5))
                    self.assertIsInstance(result[0], float)
                    self.assertIsInstance(result[1], float)

    def test_below_1000_keeps_base_delta_without_rating_floor(self):
        for rating in (-1e6, -1000, 0, 999.5, 1000):
            with self.subTest(rating=rating):
                self.assertEqual(calculate_new_rating(rating, True), (rating + 16, 16, 0.5))
                self.assertEqual(calculate_new_rating(rating, False), (rating - 12, -12, 0.5))

    def test_fractional_rating_and_delta_are_not_rounded(self):
        for win, expected_delta, expected_rating in (
            (True, 17.2345, 1140.6845), (False, -12.61725, 1110.83275),
        ):
            with self.subTest(win=win):
                new_rating, delta, _ = calculate_new_rating(1123.45, win)
                self.assertAlmostEqual(delta, expected_delta, places=10)
                self.assertAlmostEqual(new_rating, expected_rating, places=10)

    def test_progressive_growth_and_no_ceiling(self):
        previous_win, previous_loss = 0, 0
        for rating in (1000, 1200, 2000, 3000, 1e6, 1e9):
            win = calculate_rating_delta(rating, True)
            loss = -calculate_rating_delta(rating, False)
            self.assertGreater(win, loss)
            self.assertGreater(win, previous_win)
            self.assertGreater(loss, previous_loss)
            self.assertGreater(win - previous_win, loss - previous_loss)
            previous_win, previous_loss = win, loss
        self.assertEqual(calculate_new_rating(1e6, True), (1010006.0, 10006.0, 0.5))
        self.assertEqual(calculate_new_rating(1e9, False), (994999993.0, -5000007.0, 0.5))

    def test_invalid_rating_and_outcome_are_rejected(self):
        for rating in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(rating=rating), self.assertRaises(ValueError):
                calculate_new_rating(rating, True)
        for win in (0, 1, None, "WIN"):
            with self.subTest(win=win), self.assertRaises(ValueError):
                calculate_rating_delta(1000, win)


class RatingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch("app.season.now", return_value=datetime(2026, 9, 13, 12, tzinfo=timezone.utc)))
        temporary = tempfile.TemporaryDirectory(prefix="turbo-rating-test-")
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(db, "DB_PATH", Path(temporary.name) / "data" / "test.db"))
        db.init_db()
        db.add_player(42, "Test Player", 1000)
        self.history = self.enterContext(patch.object(
            OpenDotaClient, "get_turbo_matches_before", new=AsyncMock(return_value=[])
        ))
        self.recent = self.enterContext(patch.object(
            OpenDotaClient, "get_matches_for_sync", new=AsyncMock(return_value=[])
        ))
        self.enterContext(patch.object(OpenDotaClient, "get_player", new=AsyncMock(
            side_effect=lambda account_id: {"profile": {
                "account_id": account_id, "personaname": db.get_player(account_id)["nickname"],
            }},
        )))

    async def test_existing_player_lazy_initialization(self):
        original = db.get_player(42)
        self.assertIsNone(db.get_rating(42))
        result = await sync_player(42)
        self.assertEqual(result.new_count, 0)
        self.assertEqual(db.get_rating(42)["initial_rating"], 1000)
        self.assertEqual(db.get_player(42), original)
        self.history.assert_awaited_once_with(42, 1000, limit=20)

    async def test_calibration_saved_once_and_hidden(self):
        self.history.return_value = [match(i, 999 - i, i < 14) for i in range(20)]
        first = await initialize_rating(42)
        self.assertEqual((first["calibration_matches"], first["calibration_wins"]), (20, 14))
        self.assertEqual(first["initial_rating"], calculate_initial_rating(14, 20))
        stored = db.get_player_matches(42, include_calibration=True)
        self.assertEqual(len(stored), 20)
        self.assertTrue(all(m["is_calibration"] == 1 for m in stored))
        self.assertEqual(db.get_player_matches(42), [])
        self.assertEqual(db.count_player_matches(42), 0)
        self.assertEqual(apply_rating_changes(42), [])
        self.assertEqual(db.get_rating_history(42), [])
        self.history.return_value = [match(99, 500)]
        self.assertEqual(await initialize_rating(42), first)
        self.history.assert_awaited_once()
        self.assertFalse(db.match_exists(42, 99))

    async def test_non_turbo_has_no_effect(self):
        self.recent.return_value = [match(1, 1001, game_mode=22)]
        result = await sync_player(42)
        self.assertEqual(result.new_count, 1)
        self.assertEqual(db.get_rating(42)["current_rating"], 1000)
        self.assertEqual(db.get_rating_history(42), [])

    async def test_repeated_sync_is_idempotent(self):
        self.recent.return_value = [match(1, 1000), match(1, 1000)]
        first = await sync_player(42)
        self.assertEqual(first.new_count, 1)
        self.assertEqual(len(first.rating_changes), 1)
        self.assertEqual(db.get_rating(42)["current_rating"], 1016)
        old_rating = db.get_rating(42)
        old_history = db.get_rating_history(42)
        second = await sync_player(42)
        self.assertEqual(second.new_count, 0)
        self.assertEqual(second.rating_changes, [])
        self.assertEqual(db.get_rating(42), old_rating)
        self.assertEqual(db.get_rating_history(42), old_history)

    async def test_chronological_processing_without_rounding(self):
        self.recent.return_value = [match(3, 1003), match(2, 1002, False), match(1, 1001)]
        result = await sync_player(42)
        self.assertEqual([h["match_id"] for h in result.rating_changes], [1, 2, 3])
        for event, delta, after in zip(result.rating_changes, (16, -12.08, 16.0392), (1016, 1003.92, 1019.9592)):
            self.assertAlmostEqual(event["rating_delta"], delta, places=10)
            self.assertAlmostEqual(event["rating_after"], after, places=10)
            self.assertEqual(event["expected_score"], 0.5)
        current = db.get_rating(42)["current_rating"]
        self.assertAlmostEqual(current, 1019.9592, places=10)
        self.assertIsInstance(current, float)
        self.assertNotEqual(current, round(current))

    async def test_future_matches_preserve_legacy_history_and_calibration(self):
        self.history.return_value = [match(i, 900 - i, i < 10) for i in range(20)]
        await initialize_rating(42)
        legacy_matches = [match(101, 1001), match(102, 1002, False)]
        for game in legacy_matches:
            db.save_match(42, game)
        # Existing records from the old formula must keep all their stored values.
        with db._connect() as connection:
            connection.executemany(
                """INSERT INTO rating_history (account_id, match_id, rating_before,
                    expected_score, result, rating_delta, rating_after, created_at)
                    VALUES (42, ?, ?, ?, ?, ?, ?, ?)""",
                [(101, 1000.0, 0.5, 1, 16.0, 1016.0, 1001),
                 (102, 1016.0, 0.5230095872975, 0, -16.73630679352, 999.26369320648, 1002)],
            )
            connection.execute("UPDATE ratings SET current_rating = 999.26369320648 WHERE account_id = 42")
        old_rating = db.get_rating(42)
        old_history = db.get_rating_history(42)
        calibration = [row for row in db.get_player_matches(42, include_calibration=True) if row["is_calibration"]]
        self.assertEqual(apply_rating_changes(42), [])
        self.assertEqual(db.get_rating(42), old_rating)
        self.assertEqual(db.get_rating_history(42), old_history)

        self.recent.return_value = [*legacy_matches, match(103, 1003)]
        result = await sync_player(42)
        self.assertEqual([row["match_id"] for row in result.rating_changes], [103])
        event = result.rating_changes[0]
        self.assertEqual(event["rating_before"], old_rating["current_rating"])
        self.assertEqual(event["rating_delta"], 16.0)
        self.assertEqual(event["expected_score"], 0.5)
        self.assertEqual(db.get_rating(42), {**old_rating, "current_rating": old_rating["current_rating"] + 16})
        self.assertEqual(db.get_rating_history(42)[1:], old_history)
        self.assertEqual([row for row in db.get_player_matches(42, include_calibration=True) if row["is_calibration"]], calibration)
        new_history = db.get_rating_history(42)
        self.assertEqual((await sync_player(42)).rating_changes, [])
        self.assertEqual(db.get_rating_history(42), new_history)
        self.history.assert_awaited_once()

    async def test_old_matches_and_unknown_results_do_not_rate(self):
        unknown = match(3, 1002)
        unknown["radiant_win"] = None
        self.recent.return_value = [match(1, 999), match(2, 1000, False), unknown]
        await sync_player(42)
        self.assertFalse(db.match_exists(42, 1))
        self.assertEqual(db.get_rating(42)["current_rating"], 988)
        self.assertEqual(db.count_rated_matches(42), 1)

    async def test_party_match_is_independent_per_account(self):
        await initialize_rating(42)
        db.add_player(43, "Party Player", 1000)
        db.create_rating(43, 1200.0, [], 0)
        self.recent.return_value = [match(1, 1001)]
        await sync_player(42)
        await sync_player(43)
        self.assertEqual(db.get_rating(42)["current_rating"], 1016)
        self.assertEqual(db.get_rating(43)["current_rating"], 1218)
        self.assertEqual(db.count_rated_matches(42), 1)
        self.assertEqual(db.count_rated_matches(43), 1)

    async def test_transaction_rollback_and_recovery(self):
        await initialize_rating(42)
        db.save_match(42, match(1, 1001))
        db.save_match(42, match(2, 1002))
        calls = 0

        def fail_second(rating, win):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated interruption")
            return calculate_new_rating(rating, win)

        with self.assertRaises(RuntimeError):
            db.apply_pending_ratings(42, fail_second)
        self.assertEqual(db.get_rating(42)["current_rating"], 1000)
        self.assertEqual(db.get_rating_history(42), [])
        result = await sync_player(42)
        self.assertEqual(result.new_count, 0)
        self.assertEqual(len(result.rating_changes), 2)
        self.assertEqual(len((await sync_player(42)).rating_changes), 0)

    async def test_calibration_failure_is_atomic_and_retryable(self):
        with self.assertRaises(KeyError):
            db.create_rating(42, 1000, [match(1, 900), {"start_time": 899}], 1)
        self.assertIsNone(db.get_rating(42))
        self.assertFalse(db.match_exists(42, 1))
        self.history.side_effect = httpx.ReadTimeout("test")
        with self.assertRaises(httpx.ReadTimeout):
            await initialize_rating(42)
        self.assertIsNone(db.get_rating(42))
        self.history.side_effect = None
        await initialize_rating(42)
        self.assertIsNotNone(db.get_rating(42))

    async def test_concurrent_initialization_is_once(self):
        self.history.return_value = [match(1, 900)]
        first, second = await asyncio.gather(initialize_rating(42), initialize_rating(42))
        self.assertEqual(first, second)
        self.assertEqual(len(db.get_player_matches(42, include_calibration=True)), 1)
        self.assertEqual(db.create_rating(42, 2000, [match(2, 850)], 1), first)
        self.assertFalse(db.match_exists(42, 2))

    async def test_registration_and_manual_scripts(self):
        from scripts.test_opendota import check_api
        from scripts.test_sync import check_sync

        with patch.object(OpenDotaClient, "get_player", new=AsyncMock(return_value={
            "profile": {"account_id": 43, "personaname": "New Player"}
        })), patch.object(OpenDotaClient, "get_recent_turbo_matches", new=AsyncMock(return_value=[])):
            await ensure_player(43)
            self.assertIsNotNone(db.get_rating(43))
            original = db.get_player(43)
            with redirect_stdout(io.StringIO()):
                await check_sync(43)
                await check_api(43)
            self.assertEqual(db.get_player(43), original)

    async def test_telegram_commands(self):
        import importlib
        from aiogram import Bot, Dispatcher
        from aiogram.types import Chat, Message, Update, User
        from app import bot as bot_module
        importlib.reload(bot_module)
        self.enterContext(patch.object(bot_module, "MANUAL_SYNC_COOLDOWN", 0))
        from app.bot import router, ADD_ACCOUNT_MESSAGE

        dispatcher = Dispatcher()
        dispatcher.include_router(router)
        outgoing = AsyncMock(return_value=True)
        async with Bot(token="123456:LOCAL_ONLY_TEST_TOKEN") as bot:
            with patch.object(Bot, "__call__", outgoing), patch.object(
                OpenDotaClient, "get_player", new=AsyncMock(return_value={
                    "profile": {"account_id": 42, "personaname": "Test Player"}
                })
            ):
                async def command(text, user_id=100):
                    outgoing.reset_mock()
                    message = Message(
                        message_id=1, date=datetime.now(timezone.utc),
                        chat=Chat(id=user_id, type="private"),
                        from_user=User(id=user_id, is_bot=False, first_name="Test"), text=text,
                    )
                    await dispatcher.feed_update(bot, Update(update_id=1, message=message))
                    outgoing.assert_awaited_once()
                    return outgoing.await_args.args[0].text

                self.assertIn("Turbo Rating", await command("/start"))
                for name in ("/profile", "/matches", "/sync", "/rating"):
                    self.assertEqual(await command(name), ADD_ACCOUNT_MESSAGE)
                db.link_telegram_user(100, 42)
                self.assertIsNone(db.get_rating(42))
                profile = await command("/profile")
                self.assertIn("Dota ID: 42", profile)
                self.assertIn("Подключён: 01.01.1970", profile)
                self.assertNotIn("Rating", profile)
                self.assertIsNone(db.get_rating(42))
                self.assertIn("1000 TR", await command("/rating"))
                self.assertIsNotNone(db.get_rating(42))
                db.add_player(43, "Lazy Rating", 1000)
                db.link_telegram_user(101, 43)
                self.assertIn("1000 TR", await command("/rating", 101))
                self.assertIsNotNone(db.get_rating(43))
                original = db.get_player(42)
                self.assertIn("Test Player", await command("/add 42"))
                self.assertEqual(db.get_player(42), original)
                self.recent.return_value = [match(1, 1001)]
                self.assertIn(": 1", await command("/sync"))
                self.assertIn("WIN\nHero #44 · Turbo · 24 мин", await command("/matches"))
                rating_text = await command("/rating")
                self.assertIn("1016 TR", rating_text)
                self.assertNotIn("+16", rating_text)
                self.assertIn("Старт: 1000", rating_text)
                self.assertIn("Рекорд: 1016 TR", rating_text)
                self.assertNotIn("Последние", rating_text)
                self.assertEqual(await command("/stats"), rating_text)
                await command("/sync")
                self.assertEqual(await command("/rating"), rating_text)


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_offset_boundary_order_and_limit(self):
        data = [match(i, 1100 - i) for i in range(160)]
        async with OpenDotaClient() as client:
            def response(path, *, params):
                self.assertEqual(params["game_mode"], 23)
                self.assertEqual(params["significant"], 0)
                offset = params["offset"]
                return httpx.Response(200, json=data[offset:offset + params["limit"]],
                                      request=httpx.Request("GET", client.BASE_URL + path))
            with patch.object(client._client, "get", new=AsyncMock(side_effect=response)) as get:
                result = await client.get_turbo_matches_before(42, 1000)
                self.assertEqual([m["start_time"] for m in result], list(range(999, 979, -1)))
                self.assertEqual([c.kwargs["params"]["offset"] for c in get.await_args_list], [0, 50, 100])

    async def test_short_page_deduplication_and_max_pages(self):
        async with OpenDotaClient() as client:
            with patch.object(client, "get_recent_turbo_matches", new=AsyncMock(side_effect=[
                [match(1, 999), match(2, 998)], [match(2, 998)],
            ])) as fetch:
                result = await client.get_turbo_matches_before(42, 1000, page_size=2)
                self.assertEqual([m["match_id"] for m in result], [1, 2])
                self.assertEqual(fetch.await_count, 2)
            with patch.object(client, "get_recent_turbo_matches", new=AsyncMock(return_value=[
                match(1, 1000), match(2, 1001),
            ])) as fetch:
                self.assertEqual(await client.get_turbo_matches_before(42, 1000, page_size=2, max_pages=3), [])
                self.assertEqual(fetch.await_count, 3)


class MigrationTests(unittest.TestCase):
    def test_legacy_database_is_preserved(self):
        self.enterContext(patch("app.season.now", return_value=datetime(2026, 9, 13, 12, tzinfo=timezone.utc)))
        with tempfile.TemporaryDirectory(prefix="turbo-migration-test-") as directory:
            with patch.object(db, "DB_PATH", Path(directory) / "legacy.db"):
                with closing(sqlite3.connect(db.DB_PATH)) as connection:
                    connection.executescript("""
                        CREATE TABLE players (account_id INTEGER PRIMARY KEY, nickname TEXT,
                            tracking_started_at INTEGER NOT NULL, created_at INTEGER NOT NULL);
                        CREATE TABLE matches (account_id INTEGER NOT NULL, match_id INTEGER NOT NULL,
                            start_time INTEGER NOT NULL, game_mode INTEGER, hero_id INTEGER,
                            player_slot INTEGER, radiant_win INTEGER, win INTEGER, duration INTEGER,
                            created_at INTEGER NOT NULL, PRIMARY KEY (account_id, match_id));
                        CREATE TABLE telegram_users (telegram_id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL);
                        INSERT INTO players VALUES (42, 'Legacy', 1000, 1000);
                        INSERT INTO matches VALUES (42, 123, 1001, 23, 44, 0, 1, 1, 1500, 1002);
                        INSERT INTO telegram_users VALUES (100, 42);
                    """)
                db.init_db()
                db.init_db()
                self.assertEqual(db.get_telegram_player(100)["tracking_started_at"], 1000)
                self.assertEqual(db.get_player_matches(42)[0]["is_calibration"], 0)
                self.assertEqual(db.get_player_matches(42)[0]["match_id"], 123)
                with closing(sqlite3.connect(db.DB_PATH)) as connection:
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
