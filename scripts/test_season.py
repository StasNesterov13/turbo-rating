"""Monthly rollover, real SQLite migrations/concurrency and Telegram screens."""

import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User
import httpx

from app import db, season
from app.autosync import run_season_rollover, sync_tracked_players
from app.keyboards import PRIZES_BUTTON
from app.services.opendota import OpenDotaClient
from app.services.rating import apply_rating_changes, calculate_new_rating, initialize_rating
from app.services.sync import sync_player
from scripts.test_rating import match


class CalendarTests(unittest.TestCase):
    def test_month_boundary_uses_moscow_not_utc(self):
        start, end = season.bounds("2026-09")
        self.assertEqual(end.isoformat(), "2026-10-01T00:00:00+03:00")
        self.assertEqual(end.astimezone(timezone.utc).isoformat(), "2026-09-30T21:00:00+00:00")
        self.assertEqual(season.for_timestamp(int(end.timestamp()) - 1), "2026-09")
        self.assertEqual(season.for_timestamp(int(end.timestamp())), "2026-10")
        self.assertEqual((end - start).days, 30)

    def test_year_boundary_and_leap_february(self):
        self.assertEqual(season.bounds("2026-12")[1], season.bounds("2027-01")[0])
        self.assertEqual((season.bounds("2028-02")[1] - season.bounds("2028-02")[0]).days, 29)
        self.assertEqual((season.bounds("2027-02")[1] - season.bounds("2027-02")[0]).days, 28)

    def test_countdown_units_and_boundary(self):
        end = season.bounds("2026-09")[1]
        for remaining, expected in (
            (timedelta(days=18, hours=6), "18 дней 6 часов"),
            (timedelta(days=21, hours=1), "21 день 1 час"),
            (timedelta(days=22, hours=2), "22 дня 2 часа"),
            (timedelta(days=11), "11 дней 0 часов"),
            (timedelta(hours=1, minutes=21), "1 час 21 минута"),
            (timedelta(hours=2, minutes=22), "2 часа 22 минуты"),
            (timedelta(minutes=11), "11 минут"),
            (timedelta(seconds=59), "менее минуты"),
            (timedelta(microseconds=1), "менее минуты"),
            (timedelta(0), "0 минут"),
            (timedelta(days=-2), "0 минут"),
        ):
            with self.subTest(remaining=remaining):
                self.assertEqual(season.format_countdown(end - remaining, identifier="2026-09"), expected)
        self.assertEqual(season.format_countdown(end), "31 день 0 часов")


class SeasonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import bot as bot_module
        self.module = importlib.reload(bot_module)
        temporary = tempfile.TemporaryDirectory(prefix="turbo-season-test-")
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(db, "DB_PATH", Path(temporary.name) / "test.db"))
        self.end_at = season.bounds("2026-09")[1]
        self.end = int(self.end_at.timestamp())
        self.clock = self.enterContext(patch.object(season, "now", return_value=self.end_at - timedelta(days=1)))
        self.enterContext(patch.object(db.time, "time", side_effect=lambda: self.clock.return_value.timestamp()))
        db.init_db()
        self.enterContext(patch.object(self.module, "_sync_cooldowns", {}))
        self.enterContext(patch.object(self.module, "MANUAL_SYNC_COOLDOWN", 0))
        self.profile = self.enterContext(patch.object(OpenDotaClient, "get_player", new=AsyncMock(
            side_effect=lambda account_id: {"profile": {"account_id": account_id, "personaname": f"Updated {account_id}"}},
        )))
        self.history = self.enterContext(patch.object(OpenDotaClient, "get_turbo_matches_before", new=AsyncMock(return_value=[])))
        self.fetch = self.enterContext(patch.object(OpenDotaClient, "get_matches_for_sync", new=AsyncMock(return_value=[])))
        self.details = self.enterContext(patch.object(OpenDotaClient, "get_match", new=AsyncMock(return_value={})))
        self.enterContext(patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("Unexpected HTTP")))
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(self.module.router)
        self.addAsyncCleanup(self.dispatcher.storage.close)
        self.bot = await self.enterAsyncContext(Bot(token="123456:LOCAL_ONLY_TEST_TOKEN"))
        self.outgoing = self.enterContext(patch.object(Bot, "__call__", new=AsyncMock(return_value=True)))

    def player(self, account_id, rating=1000, *, linked=True):
        db.add_player(account_id, f"Player {account_id}", self.end - 30 * 86400)
        db.create_rating(account_id, rating, [], 0)
        if linked:
            db.link_telegram_user(account_id, account_id)

    def october(self):
        self.clock.return_value = self.end_at + timedelta(minutes=5)

    def snapshot(self):
        with db._connect() as connection:
            return [tuple(row) for row in connection.execute("SELECT * FROM season_final_standings ORDER BY season_end_at")]

    async def send(self, text, user_id=1):
        self.outgoing.reset_mock()
        message = Message(message_id=1, date=self.clock.return_value, chat=Chat(id=user_id, type="private"),
                          from_user=User(id=user_id, is_bot=False, first_name="Test"), text=text)
        await self.dispatcher.feed_update(self.bot, Update(update_id=1, message=message))
        self.outgoing.assert_awaited_once()
        return self.outgoing.await_args.args[0].text

    async def test_rollover_snapshots_ratings_places_and_stats_then_starts_at_1000(self):
        self.player(1, 1435)
        self.player(2, 1190)
        db.save_match(1, match(1, self.end - 600))
        db.save_match(1, match(2, self.end - 500, False))
        apply_rating_changes(1)
        old_history = db.get_rating_history(1)
        self.october()
        with self.assertLogs("app.db", level="INFO") as logs:
            self.assertEqual(db.ensure_current_season()["season_id"], "2026-10")
        for event in ("rollover started", "snapshot completed", "Season created", "rollover completed"):
            self.assertTrue(any(event in line for line in logs.output))
        final = db.get_final_standings("2026-09")
        self.assertEqual([(r["final_rating"], r["final_position"]) for r in final], [(1435, 1), (1190, 2)])
        self.assertEqual((final[0]["matches_played"], final[0]["wins"], final[0]["losses"]), (2, 1, 1))
        self.assertEqual([db.get_rating(i)["current_rating"] for i in (1, 2)], [1000, 1000])
        self.assertEqual(db.get_rating(1)["initial_rating"], 1000)
        self.assertEqual(db.get_rating_history(1), old_history)
        self.assertIsNone(db.get_final_standings("2026-10"))

    async def test_repeated_rollover_does_not_reset_already_earned_october_rating(self):
        self.player(1, 1435)
        self.october()
        db.ensure_current_season()
        db.save_match(1, match(1, self.end + 300))
        apply_rating_changes(1)
        before = self.snapshot()
        for _ in range(3):
            db.ensure_current_season()
            db.init_db()
        self.assertEqual(db.get_rating(1)["current_rating"], 1025)
        self.assertEqual(self.snapshot(), before)
        with db._connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM seasons WHERE season_id = '2026-10'").fetchone()[0], 1)

    async def test_before_midnight_late_sync_is_stored_in_september_without_rating(self):
        self.player(1, 1435)
        self.october()
        self.fetch.return_value = [match(1, self.end - 600)]
        for _ in range(2):
            result = await sync_player(1)
            self.assertEqual(result.rating_updates, [])
        self.assertEqual(db.get_player_matches(1)[0]["season_id"], "2026-09")
        self.assertEqual(db.get_rating(1)["current_rating"], 1000)
        self.assertEqual(db.get_final_standings("2026-09")[0]["final_rating"], 1435)
        self.assertEqual(db.get_rating_history(1), [])
        self.assertIsNone(db.get_turbo_match_history(1)[0]["rating_delta"])
        self.details.assert_not_awaited()

    async def test_after_midnight_win_and_loss_start_from_1000_without_floor(self):
        self.player(1, 1435)
        self.player(2, 1190)
        self.october()
        for account_id, win, expected in ((1, True, 1025), (2, False, 975)):
            self.fetch.return_value = [match(1, self.end + 300, win)]
            result = await sync_player(account_id)
            self.assertEqual(result.rating_updates[0].rating_before, 1000)
            self.assertEqual(db.get_rating(account_id)["current_rating"], expected)
            self.assertEqual(db.get_rating_history(account_id)[0]["season_id"], "2026-10")

    async def test_restart_and_multiple_missed_months(self):
        self.player(1, 1435)
        self.clock.return_value = season.bounds("2027-01")[0]
        db.init_db()
        self.assertEqual(db.get_rating(1)["current_rating"], 1000)
        self.assertEqual(db.ensure_current_season()["season_id"], "2027-01")
        self.assertEqual(db.get_final_standings("2026-09")[0]["final_rating"], 1435)
        self.assertEqual(db.get_final_standings("2026-12")[0]["final_rating"], 1000)
        self.assertEqual(len(self.snapshot()), 4)

    async def test_top_profile_prizes_and_seven_day_history_across_rollover(self):
        self.player(2, 1435)
        self.player(1, 1190)
        db.save_match(2, match(1, self.end - 600))
        apply_rating_changes(2)
        self.october()
        self.assertIn("Сезон: Октябрь 2026", await self.send("/top"))
        top = db.get_leaderboard()
        self.assertEqual([(r["account_id"], r["current_rating"]) for r in top], [(1, 1000), (2, 1000)])
        self.assertEqual(db.get_leaderboard_position(1)["position"], 1)
        self.assertIn("Turbo Rating: 1000 TR", await self.send("/profile"))
        prizes = await self.send("/prizes")
        self.assertIn("31.10.2026 23:59:59 МСК", prizes)
        self.assertIn("Итоги: Сентябрь 2026\n🥇 Player 2 — 1460 TR — 3 000 ₽", prizes)
        self.assertEqual(prizes, await self.send(PRIZES_BUTTON))
        history = await self.send("/history", 2)
        self.assertIn("30.09", history)
        self.assertIn("+25 TR → 1460 TR", history)
        self.assertNotIn("→ 1025", history)

    async def test_late_performance_and_rename_cannot_mutate_snapshot_or_balances(self):
        self.player(1, 1435)
        db.save_match(1, match(1, self.end - 600))
        apply_rating_changes(1)
        self.october()
        db.ensure_current_season()
        before = self.snapshot()
        old_history = db.get_rating_history(1)
        db.save_match(1, match(2, self.end + 1))
        apply_rating_changes(1)
        self.assertIsNone(db.apply_match_performance(1, 1, {"performance_score": 1, "performance_bonus": 10}))
        self.assertEqual(db.get_rating(1)["current_rating"], 1025)
        self.assertEqual(db.get_rating_history(1)[1], old_history[0])
        db.update_player_nickname(1, "New name")
        self.player(2)
        db.link_telegram_user(1, 2)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(db.get_leaderboard_at(self.end - 1)[0]["nickname"], "Player 1")
        self.assertEqual(db.get_final_standings("2026-09")[0]["final_rating"], 1460)

    async def test_transaction_crossing_midnight_rolls_back_before_snapshot(self):
        self.player(1, 1435)
        for mid in (1, 2):
            db.save_match(1, match(mid, self.end - 100 + mid))
        def calculate(current, win, bonus):
            self.october()
            return calculate_new_rating(current, win, bonus)
        self.assertEqual(db.apply_pending_ratings(1, calculate), [])
        self.assertEqual(db.get_rating_history(1), [])
        self.assertEqual(db.get_final_standings("2026-09")[0]["final_rating"], 1435)
        self.assertEqual(db.get_rating(1)["current_rating"], 1000)

    async def test_delayed_api_response_and_backwards_clock_cannot_reopen_season(self):
        self.player(1, 1435)
        async def delayed(*args):
            self.october()
            return [match(1, self.end - 600), match(2, self.end + 300)]
        self.fetch.side_effect = delayed
        result = await sync_player(1)
        self.assertEqual([u.match_id for u in result.rating_updates], [2])
        self.assertEqual(db.get_rating(1)["current_rating"], 1025)
        db.add_player(2, "Late calibration", self.end - 86400)
        self.clock.return_value = self.end_at - timedelta(minutes=1)
        self.assertEqual((await initialize_rating(2))["initial_rating"], 1000)
        self.history.assert_not_awaited()

    async def test_calibration_in_flight_at_rollover_becomes_1000(self):
        db.add_player(1, "Late", self.end - 86400)
        async def delayed(*args, **kwargs):
            self.october()
            return [match(1, self.end - 2 * 86400)]
        self.history.side_effect = delayed
        rating = await initialize_rating(1)
        self.assertEqual((rating["initial_rating"], rating["calibration_matches"], rating["season_id"]), (1000, 0, "2026-10"))
        self.assertEqual(db.get_player_matches(1, include_calibration=True), [])

    async def test_new_october_player_initializes_without_api_calibration(self):
        self.october()
        self.assertIn("Каждый новый сезон начинается с 1000 TR", await self.send("/add 55", 55))
        rating = db.get_rating(55)
        self.assertEqual((rating["initial_rating"], rating["current_rating"]), (1000, 1000))
        self.history.assert_not_awaited()

    async def test_concurrent_rollover_and_sync_apply_exactly_once(self):
        for account_id in range(1, 24):
            self.player(account_id, 1435)
        self.october()
        db.save_match(1, match(1, self.end + 1))
        await asyncio.gather(*(asyncio.to_thread(fn, *args) for fn, args in
            [(db.ensure_current_season, ())] * 4 + [(apply_rating_changes, (1,))] * 4))
        self.assertEqual(len(self.snapshot()), 1)
        self.assertEqual(db.get_rating(1)["current_rating"], 1025)
        self.assertEqual(db.count_rated_matches(1), 1)
        self.assertEqual(len(db.get_leaderboard()), 20)
        self.assertEqual(db.get_leaderboard_position(23)["position"], 23)
        self.assertEqual([r["final_position"] for r in db.get_final_standings("2026-09")], list(range(1, 24)))

    async def test_failed_creation_or_reset_rolls_back_snapshot_and_closure(self):
        self.player(1, 1435)
        self.october()
        for trigger in (
            "CREATE TRIGGER fail BEFORE INSERT ON seasons WHEN NEW.season_id = '2026-10' BEGIN SELECT RAISE(ABORT, 'test'); END",
            "CREATE TRIGGER fail BEFORE UPDATE ON ratings BEGIN SELECT RAISE(ABORT, 'test'); END",
        ):
            with db._connect() as connection:
                connection.execute(trigger)
            with self.assertRaises(sqlite3.IntegrityError):
                db.ensure_current_season()
            with db._connect() as connection:
                self.assertIsNone(connection.execute("SELECT closed_at FROM seasons").fetchone()[0])
                self.assertEqual(connection.execute("SELECT current_rating FROM ratings").fetchone()[0], 1435)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM seasons").fetchone()[0], 1)
                connection.execute("DROP TRIGGER fail")
            self.assertEqual(self.snapshot(), [])
        self.assertEqual(db.ensure_current_season()["season_id"], "2026-10")

    async def test_unlinked_rating_preserved_without_prize_position(self):
        self.player(1, 1435, linked=False)
        self.october()
        self.assertEqual(db.get_final_standings("2026-09"), [])
        result = db.get_season_results("2026-09")[0]
        self.assertEqual(result["final_rating"], 1435)
        self.assertIsNone(result["final_position"])
        self.assertEqual(db.get_rating(1)["current_rating"], 1000)
        self.assertEqual(db.get_peak_rating(1), 1435)

    async def test_rating_periods_and_archived_balance_do_not_mix_months(self):
        self.player(1, 1435)
        before_match = int(self.clock.return_value.timestamp()) - 1
        db.save_match(1, match(1, before_match))
        apply_rating_changes(1)
        self.october()
        self.assertEqual(db.get_rating_at(1, before_match), 1435)
        self.assertEqual(db.get_rating_at(1, self.end - 1), 1460)
        self.assertEqual(db.get_rating_at(1, self.end), 1000)
        self.assertIsNone(db.get_rating_at(1, self.end - 31 * 86400))
        db.save_match(1, match(2, self.end + 300, False))
        apply_rating_changes(1)
        self.assertEqual(db.get_rating_change(1, self.end - 7 * 86400), -25)
        self.assertEqual(db.get_rating_at(1, self.end + 300), 975)
        self.assertEqual(db.get_peak_rating(1), 1460)

    async def test_snapshots_cannot_be_updated_or_deleted(self):
        self.player(1, 1435)
        self.october()
        db.ensure_current_season()
        before = self.snapshot()
        for query in ("UPDATE season_final_standings SET standings_json = '[]'", "DELETE FROM season_final_standings"):
            with self.assertRaises(sqlite3.IntegrityError):
                with db._connect() as connection:
                    connection.execute(query)
        self.assertEqual(self.snapshot(), before)

    async def test_empty_season_api_failure_and_background_recovery(self):
        self.october()
        await sync_tracked_players(AsyncMock(spec=Bot))
        self.assertEqual(db.get_final_standings("2026-09"), [])
        self.assertEqual(len(self.snapshot()), 1)
        self.player(1)
        self.clock.return_value = season.bounds("2026-11")[0]
        self.profile.side_effect = httpx.ReadTimeout("Unavailable")
        with self.assertLogs("app.autosync", level="ERROR"):
            await sync_tracked_players(AsyncMock(spec=Bot))
        self.assertEqual(db.ensure_current_season()["season_id"], "2026-11")
        self.clock.return_value = season.bounds("2026-12")[0]
        task = asyncio.create_task(run_season_rollover())
        await asyncio.sleep(0)
        self.assertEqual(db.get_rating(1)["season_id"], "2026-12")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class MigrationTests(unittest.TestCase):
    def test_existing_september_balances_survive_repeatable_migration(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(db, "DB_PATH", Path(directory) / "legacy.db"), \
                patch.object(season, "now", return_value=datetime(2026, 9, 15, tzinfo=season.MOSCOW)) as clock:
            with closing(sqlite3.connect(db.DB_PATH)) as connection:
                connection.executescript("""
                    CREATE TABLE players (account_id INTEGER PRIMARY KEY, nickname TEXT,
                        tracking_started_at INTEGER NOT NULL, created_at INTEGER NOT NULL);
                    CREATE TABLE ratings (account_id INTEGER PRIMARY KEY, initial_rating REAL NOT NULL,
                        current_rating REAL NOT NULL, calibration_matches INTEGER NOT NULL,
                        calibration_wins INTEGER NOT NULL, initialized_at INTEGER NOT NULL);
                    INSERT INTO players VALUES (1, 'A', 0, 0), (2, 'B', 0, 0);
                    INSERT INTO ratings VALUES (1, 1100.25, 1435.75, 20, 13, 0), (2, 987, 1190, 20, 8, 0);
                """)
            db.init_db()
            db.link_telegram_user(1, 1)
            db.link_telegram_user(2, 2)
            before = [db.get_rating(i) for i in (1, 2)]
            db.init_db()
            self.assertEqual([db.get_rating(i) for i in (1, 2)], before)
            self.assertEqual([r["current_rating"] for r in before], [1435.75, 1190])
            self.assertEqual([r["season_id"] for r in before], ["2026-09"] * 2)
            self.assertIsNone(db.get_final_standings())
            clock.return_value = season.bounds("2026-10")[0]
            db.init_db()
            self.assertEqual([r["final_rating"] for r in db.get_final_standings("2026-09")], [1435.75, 1190])
            self.assertEqual([db.get_rating(i)["current_rating"] for i in (1, 2)], [1000, 1000])
            with db._connect() as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
