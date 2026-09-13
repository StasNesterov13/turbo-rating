"""Offline prize screens and irreversible season closure using real SQLite."""

import asyncio
from datetime import timedelta, timezone
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User
import httpx

from app import db, season
from app.autosync import sync_tracked_players
from app.keyboards import LINK_BUTTON, MAIN_KEYBOARD, PRIZES_BUTTON, UNLINKED_KEYBOARD
from app.services.opendota import OpenDotaClient
from app.services.rating import apply_rating_changes, calculate_new_rating, initialize_rating
from app.services.sync import sync_player
from scripts.test_rating import match


class CountdownTests(unittest.TestCase):
    def test_moscow_deadline_and_exact_boundary(self):
        self.assertEqual(season.SEASON_END_AT.astimezone(timezone.utc).isoformat(), "2026-10-01T20:59:59+00:00")
        self.assertFalse(season.is_season_over(season.SEASON_END_AT - timedelta(microseconds=1)))
        self.assertTrue(season.is_season_over(season.SEASON_END_AT))

    def test_countdown_units_declensions_and_nonnegative_boundary(self):
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
                self.assertEqual(season.format_countdown(season.SEASON_END_AT - remaining), expected)


class SeasonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import bot as bot_module
        self.module = importlib.reload(bot_module)
        temporary = tempfile.TemporaryDirectory(prefix="turbo-season-test-")
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(db, "DB_PATH", Path(temporary.name) / "test.db"))
        self.clock = self.enterContext(patch.object(season, "now", return_value=
            season.SEASON_END_AT - timedelta(days=18, hours=6)))
        db.init_db()
        self.end = int(season.SEASON_END_AT.timestamp())
        self.enterContext(patch.object(self.module, "_sync_cooldowns", {}))
        self.enterContext(patch.object(self.module, "MANUAL_SYNC_COOLDOWN", 0))
        self.profile = self.enterContext(patch.object(OpenDotaClient, "get_player", new=AsyncMock(
            side_effect=lambda account_id: {"profile": {"account_id": account_id, "personaname": f"Updated {account_id}"}},
        )))
        self.history = self.enterContext(patch.object(OpenDotaClient, "get_turbo_matches_before", new=AsyncMock(return_value=[])))
        self.fetch = self.enterContext(patch.object(OpenDotaClient, "get_matches_for_sync", new=AsyncMock(return_value=[])))
        self.enterContext(patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("Unexpected HTTP")))
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(self.module.router)
        self.addAsyncCleanup(self.dispatcher.storage.close)
        self.bot = await self.enterAsyncContext(Bot(token="123456:LOCAL_ONLY_TEST_TOKEN"))
        self.outgoing = self.enterContext(patch.object(Bot, "__call__", new=AsyncMock(return_value=True)))

    def player(self, account_id, rating=1000, nickname=None, *, linked=True):
        db.add_player(account_id, nickname or f"Player {account_id}", self.end - 60 * 86400)
        db.create_rating(account_id, rating, [], 0)
        if linked:
            db.link_telegram_user(account_id, account_id)

    def podium(self):
        for account_id, rating, name in ((1, 1284, "Player One"), (2, 1210, "Stas"),
                                         (3, 1178, "Player Three"), (4, 1000, "Fourth")):
            self.player(account_id, rating, name)

    def close_season(self):
        self.clock.return_value = season.SEASON_END_AT

    def snapshot_row(self):
        with db._connect() as connection:
            return [tuple(row) for row in connection.execute("SELECT * FROM season_final_standings")]

    async def send(self, text, user_id=2):
        self.outgoing.reset_mock()
        message = Message(message_id=1, date=self.clock.return_value, chat=Chat(id=user_id, type="private"),
                          from_user=User(id=user_id, is_bot=False, first_name="Test"), text=text)
        await self.dispatcher.feed_update(self.bot, Update(update_id=1, message=message))
        self.outgoing.assert_awaited_once()
        return self.outgoing.await_args.args[0]

    async def test_prizes_command_button_current_top_three_and_no_writes(self):
        self.podium()
        with db._connect() as connection:
            before = list(connection.iterdump())
        expected = ("💰 Призы сезона\n\n🥇 1 место — 3 000 ₽\n🥈 2 место — 2 000 ₽\n🥉 3 место — 1 000 ₽\n\n"
                    "Сезон заканчивается:\n1 октября 2026\n\nДо окончания:\n18 дней 6 часов\n\n"
                    "Текущий топ:\n🥇 Player One — 1284 TR\n🥈 Stas — 1210 TR\n🥉 Player Three — 1178 TR")
        for user_id, keyboard in ((2, MAIN_KEYBOARD), (999, UNLINKED_KEYBOARD)):
            for text in (PRIZES_BUTTON, "/prizes"):
                response = await self.send(text, user_id)
                self.assertEqual(response.text, expected)
                self.assertEqual(response.reply_markup, keyboard)
        with db._connect() as connection:
            self.assertEqual(list(connection.iterdump()), before)
        self.history.assert_not_awaited()

    async def test_prizes_navigation_exits_account_input(self):
        await self.send(LINK_BUTTON, 999)
        self.assertIn("Текущий топ:", (await self.send(PRIZES_BUTTON, 999)).text)
        state = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=999, user_id=999)
        self.assertIsNone(await state.get_state())

    async def test_top_keeps_ranking_and_prizes_screen_has_countdown(self):
        self.podium()
        text = (await self.send("/top")).text
        self.assertTrue(text.startswith("Turbo Rating"))
        self.assertIn("🥈 Stas — 1210", text)
        self.assertIn("← вы", text)
        self.assertIn("4. Fourth — 1000", text)
        prizes = (await self.send("/prizes")).text
        self.assertIn("🥇 1 место — 3 000 ₽", prizes)
        self.assertIn("🥈 2 место — 2 000 ₽", prizes)
        self.assertIn("🥉 3 место — 1 000 ₽", prizes)
        self.assertIn("До окончания:\n18 дней 6 часов", prizes)
        self.assertIsNone(db.get_final_standings())

    async def test_empty_and_partial_podium_before_and_after_deadline(self):
        self.assertIn("Рейтинг игроков пока пуст.", (await self.send("/prizes")).text)
        self.assertEqual("Turbo Rating\n\nРейтинг игроков пока пуст.", (await self.send("/top")).text)
        self.player(1)
        text = (await self.send("/prizes")).text.split("Текущий топ:")[1]
        self.assertEqual(text.count(" TR"), 1)
        self.close_season()
        text = (await self.send("/prizes")).text
        self.assertIn("🥇 Player 1 — 1000 TR — 3 000 ₽", text)
        self.assertNotIn("🥈", text)

    async def test_live_top_tracks_rating_until_last_instant(self):
        self.player(1, 1000)
        self.player(2, 1005)
        self.clock.return_value = season.SEASON_END_AT - timedelta(microseconds=1)
        db.save_match(1, match(100, self.end - 100))
        self.assertEqual(len(apply_rating_changes(1)), 1)
        self.assertEqual(db.get_rating(1)["current_rating"], 1025)
        self.assertIn("🥇 Player 1 — 1025 TR", (await self.send("/prizes")).text)
        self.assertEqual(self.snapshot_row(), [])

    async def test_deadline_stops_pending_and_new_matches_for_every_player(self):
        self.podium()
        db.save_match(1, match(1, self.end - 100))
        apply_rating_changes(1)
        ratings = [db.get_rating(i) for i in range(1, 5)]
        histories = [db.get_rating_history(i) for i in range(1, 5)]
        db.save_match(1, match(2, self.end - 50))  # Delayed pre-deadline match.
        self.close_season()
        for i in range(1, 5):
            db.save_match(i, match(3, self.end + 10))
            self.assertEqual(apply_rating_changes(i), [])
        self.assertEqual([db.get_rating(i) for i in range(1, 5)], ratings)
        self.assertEqual([db.get_rating_history(i) for i in range(1, 5)], histories)
        self.assertEqual(len(db.get_final_standings()), 4)

    async def test_rating_transaction_crossing_deadline_rolls_back_whole_batch(self):
        self.player(1)
        for match_id in (1, 2):
            db.save_match(1, match(match_id, self.end - 100 + match_id))
        calls = 0

        def calculate(current, win):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.close_season()
            return calculate_new_rating(current, win)

        self.assertEqual(db.apply_pending_ratings(1, calculate), [])
        self.assertEqual(db.get_rating(1)["current_rating"], 1000)
        self.assertEqual(db.get_rating_history(1), [])
        self.assertEqual(db.count_player_matches(1), 2)
        self.assertEqual(db.get_final_standings()[0]["current_rating"], 1000)

    async def test_sync_started_before_deadline_finishes_without_rating(self):
        self.player(1)

        async def delayed_fetch(*args):
            self.close_season()
            return [match(1, self.end - 10)]

        self.fetch.side_effect = delayed_fetch
        result = await sync_player(1)
        self.assertEqual(result.new_count, 1)
        self.assertEqual(result.rating_updates, [])
        self.assertEqual(db.get_rating(1)["current_rating"], 1000)
        self.assertEqual(db.get_rating_history(1), [])

    async def test_calibration_response_after_deadline_does_not_create_season_rating(self):
        db.add_player(1, "Late", self.end - 86400)

        async def delayed_history(*args, **kwargs):
            self.close_season()
            return [match(1, self.end - 2 * 86400)]

        self.history.side_effect = delayed_history
        self.assertIsNone(await initialize_rating(1))
        self.assertIsNone(db.get_rating(1))
        self.assertEqual(db.get_player_matches(1, include_calibration=True), [])
        self.assertEqual(db.get_final_standings(), [])

    async def test_new_player_after_deadline_can_link_and_sync_without_calibration(self):
        self.close_season()
        self.assertIn("Dota-профиль подключён", (await self.send("/add 55", 55)).text)
        tracking = db.get_player(55)["tracking_started_at"]
        self.fetch.return_value = [match(1, max(tracking, self.end + 1))]
        self.assertIn("Новых матчей сохранено: 1", (await self.send("/sync", 55)).text)
        self.assertIn("Рейтинг в этом сезоне не рассчитан", (await self.send("/rating", 55)).text)
        self.assertIsNone(db.get_rating(55))
        self.assertEqual(db.count_player_matches(55), 1)
        self.assertEqual(db.get_rating_history(55), [])
        self.assertEqual(db.get_final_standings(), [])
        self.history.assert_not_awaited()

    async def test_final_screens_use_stored_winners(self):
        self.podium()
        self.close_season()
        top = (await self.send("/top")).text
        self.assertEqual(top, "🏆 Итоги сезона\n\n🥇 Player One — 1284 TR\n🥈 Stas — 1210 TR\n🥉 Player Three — 1178 TR")
        prizes = (await self.send("/prizes")).text
        self.assertEqual(prizes, "💰 Призы сезона\n\nСезон завершён.\n\nПобедители:\n"
                                "🥇 Player One — 1284 TR — 3 000 ₽\n🥈 Stas — 1210 TR — 2 000 ₽\n🥉 Player Three — 1178 TR — 1 000 ₽")
        self.assertEqual((await self.send(PRIZES_BUTTON)).text, prizes)
        db.update_player_nickname(1, "Changed")
        db.link_telegram_user(1, 4)
        self.assertEqual((await self.send("/top")).text, top)
        self.assertEqual((await self.send("/prizes")).text, prizes)

    async def test_first_rename_and_relink_at_deadline_freeze_previous_cohort(self):
        self.podium()
        self.player(9, 3000, "Unlinked", linked=False)
        self.close_season()
        db.link_telegram_user(1, 9)  # First operation since the deadline.
        db.update_player_nickname(2, "Renamed")
        final = db.get_final_standings()
        self.assertEqual([row["account_id"] for row in final], [1, 2, 3, 4])
        self.assertEqual(final[1]["nickname"], "Stas")
        self.assertEqual(db.get_leaderboard_position(1)["position"], 1)
        self.assertIsNone(db.get_leaderboard_position(9))

    async def test_first_rename_at_deadline_keeps_old_name(self):
        self.player(1)
        self.close_season()
        db.update_player_nickname(1, "New name")
        self.assertEqual(db.get_final_standings()[0]["nickname"], "Player 1")

    async def test_repeated_autosync_saves_matches_without_changing_finals_or_history(self):
        self.podium()
        before = [db.get_rating(i) for i in range(1, 5)]
        self.close_season()
        self.fetch.return_value = [match(1, self.end - 10), match(2, self.end + 10)]
        bot = AsyncMock(spec=Bot)
        await sync_tracked_players(bot)
        snapshot = self.snapshot_row()
        for _ in range(2):
            await sync_tracked_players(bot)
        self.assertEqual(self.snapshot_row(), snapshot)
        self.assertEqual([db.get_rating(i) for i in range(1, 5)], before)
        self.assertEqual([db.count_player_matches(i) for i in range(1, 5)], [2] * 4)
        self.assertTrue(all(db.get_rating_history(i) == [] for i in range(1, 5)))
        self.assertEqual([row["nickname"] for row in db.get_final_standings()][:3], ["Player One", "Stas", "Player Three"])
        bot.send_message.assert_not_awaited()

    async def test_restart_ties_top_twenty_and_snapshot_written_only_once(self):
        for i in reversed(range(1, 24)):
            self.player(i)
        db.link_telegram_user(100, 1)
        self.close_season()
        finals = await asyncio.gather(*(asyncio.to_thread(db.get_final_standings) for _ in range(8)))
        self.assertTrue(all(final == finals[0] for final in finals))
        self.assertEqual([row["account_id"] for row in finals[0]], list(range(1, 24)))
        snapshot = self.snapshot_row()
        self.assertEqual(len(snapshot), 1)
        self.clock.return_value += timedelta(days=1)
        db.init_db()
        self.assertEqual(self.snapshot_row(), snapshot)
        self.assertEqual(db.get_final_standings(), finals[0])
        self.assertEqual(len(db.get_leaderboard()), 20)
        self.assertEqual(db.get_leaderboard_position(23)["position"], 23)
        # Once closed, a backwards clock change cannot reopen this season.
        self.clock.return_value = season.SEASON_END_AT - timedelta(days=1)
        db.save_match(1, match(1, self.end - 100))
        self.assertEqual(apply_rating_changes(1), [])
        self.assertEqual(self.snapshot_row(), snapshot)

    async def test_empty_season_is_finalized_even_without_tracked_players(self):
        self.close_season()
        await sync_tracked_players(AsyncMock(spec=Bot))
        self.assertEqual(db.get_final_standings(), [])
        snapshot = self.snapshot_row()
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0][2], "[]")
        db.init_db()
        self.assertEqual(self.snapshot_row(), snapshot)
        self.assertIn("Сезон завершён.", (await self.send("/prizes")).text)

    async def test_migration_after_downtime_preserves_existing_data_and_fixes_standings(self):
        self.podium()
        db.save_match(1, match(1, self.end - 100))
        apply_rating_changes(1)
        tables = ("players", "matches", "ratings", "rating_history", "telegram_users")
        with db._connect() as connection:
            before = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                      for table in tables}
            connection.execute("DROP TABLE season_final_standings")
        self.close_season()
        db.init_db()
        self.assertEqual(len(db.get_final_standings()), 4)
        with db._connect() as connection:
            after = {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                     for table in tables}
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(after, before)

    async def test_autosync_fixes_standings_even_when_every_api_request_fails(self):
        self.podium()
        self.close_season()
        self.profile.side_effect = httpx.ReadTimeout("Unavailable")
        with self.assertLogs("app.autosync", level="ERROR"):
            await sync_tracked_players(AsyncMock(spec=Bot))
        self.assertEqual([row["account_id"] for row in db.get_final_standings()], [1, 2, 3, 4])
        self.assertEqual(len(self.snapshot_row()), 1)
        self.assertTrue(all(db.get_rating_history(i) == [] for i in range(1, 5)))


if __name__ == "__main__":
    unittest.main()
