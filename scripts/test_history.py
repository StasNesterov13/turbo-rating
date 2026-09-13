"""Offline historical rating/rank queries and Telegram history screens."""

from datetime import datetime, timezone
import importlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User
import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from app import db
from app.keyboards import HISTORY_BUTTON, MAIN_KEYBOARD, UNLINKED_KEYBOARD
from app.notifications import notify_rating_updates
from app.services import heroes
from app.services.sync import RatingUpdate
from scripts.test_rating import match


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch("app.season.now", return_value=datetime(2026, 9, 13, 12, tzinfo=timezone.utc)))
        from app import bot as bot_module
        self.module = importlib.reload(bot_module)
        temporary = tempfile.TemporaryDirectory(prefix="turbo-history-test-")
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(db, "DB_PATH", Path(temporary.name) / "test.db"))
        db.init_db()
        self.now = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
        self.timestamp = int(self.now.timestamp())
        self.day = 86400
        self.midnight = int(self.now.replace(hour=0).timestamp())
        clock = self.enterContext(patch.object(self.module, "datetime", wraps=datetime))
        clock.now.return_value = self.now
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(self.module.router)
        self.addAsyncCleanup(self.dispatcher.storage.close)
        self.bot = await self.enterAsyncContext(Bot(token="123456:LOCAL_ONLY_TEST_TOKEN"))
        self.outgoing = self.enterContext(patch.object(Bot, "__call__", new=AsyncMock(return_value=True)))
        self.enterContext(patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("Unexpected HTTP")))
        self.enterContext(patch.object(heroes, "_hero_names", {44: "Phantom Assassin", 14: "Pudge"}))
        self.next_match = 1

    def player(self, account_id, initial=1000, *, registered=None, linked=True, nickname=None):
        registered = registered if registered is not None else self.timestamp - 60 * self.day
        db.add_player(account_id, nickname or f"Player {account_id}", registered)
        db.create_rating(account_id, initial, [], 0)
        if linked:
            db.link_telegram_user(account_id, account_id)

    def event(self, account_id, recorded_at, delta, *, match_id=None, played_at=None):
        match_id = match_id if match_id is not None else self.next_match
        self.next_match = max(self.next_match, match_id + 1)
        db.save_match(account_id, match(match_id, played_at if played_at is not None else recorded_at - 60, delta > 0))
        before = db.get_rating(account_id)["current_rating"]
        with db._connect() as connection:
            connection.execute(
                """INSERT INTO rating_history
                    (account_id, match_id, rating_before, expected_score, result,
                     rating_delta, rating_after, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (account_id, match_id, before, 0.5, int(delta > 0), delta, before + delta, recorded_at),
            )
            connection.execute("UPDATE ratings SET current_rating = ? WHERE account_id = ?", (before + delta, account_id))

    def snapshot(self):
        with db._connect() as connection:
            return list(connection.iterdump())

    async def send(self, text, user_id=42):
        self.outgoing.reset_mock()
        message = Message(message_id=1, date=self.now, chat=Chat(id=user_id, type="private"),
                          from_user=User(id=user_id, is_bot=False, first_name="Test"), text=text)
        await self.dispatcher.feed_update(self.bot, Update(update_id=1, message=message))
        self.outgoing.assert_awaited_once()
        return self.outgoing.await_args.args[0]

    def demo(self):
        self.player(42, 987, nickname="ДЕРЕВЕНСКИЙ")
        for days, delta in ((25, 200), (10, -147), (6, 40), (2, 14), (1, 12)):
            self.event(42, self.timestamp - days * self.day, delta)
        self.event(42, self.midnight, 1)
        self.event(42, self.timestamp - 3600, 17)
        for account_id, rating in enumerate((1187, 1154, 1087, 1070, 1050, 1030, 1010, 1000), 1):
            self.player(account_id, rating)

    async def test_rating_at_registration_and_before_after_events(self):
        registered = self.timestamp - 40 * self.day
        self.player(42, registered=registered)
        first, second = self.timestamp - 10 * self.day, self.timestamp - 5 * self.day
        self.event(42, first, 16, played_at=registered + 1)
        self.event(42, second, -17, played_at=registered + 2)
        before = self.snapshot()
        for timestamp, expected in ((registered - 1, None), (registered, 1000), (first - 1, 1000),
                                    (first, 1016), (second - 1, 1016), (second, 999), (self.timestamp, 999)):
            self.assertEqual(db.get_rating_at(42, timestamp), expected)
        self.assertIsNone(db.get_rating_at(999, self.timestamp))
        self.assertEqual(self.snapshot(), before)

    async def test_same_second_uses_recorded_order_including_late_backfill(self):
        self.player(42)
        self.event(42, self.timestamp - 1, 16, match_id=100)
        self.event(42, self.timestamp - 1, -17, match_id=5, played_at=self.timestamp - self.day)
        self.assertEqual(db.get_rating_at(42, self.timestamp - 1), 999)
        history = db.get_rating_history(42, limit=10, by_recorded_time=True)
        self.assertEqual([row["match_id"] for row in history], [5, 100])
        # The existing match-ordered history API retains its original behavior.
        self.assertEqual([row["match_id"] for row in db.get_rating_history(42)], [100, 5])

    async def test_peak_includes_initial_and_keeps_all_time_record(self):
        self.player(42, 1200)
        self.assertEqual(db.get_peak_rating(42), 1200)
        self.event(42, self.timestamp - 3, -100)
        self.assertEqual(db.get_peak_rating(42), 1200)
        self.event(42, self.timestamp - 2, 150)
        self.event(42, self.timestamp - 1, -300)
        self.assertEqual(db.get_peak_rating(42), 1250)
        self.assertIsNone(db.get_peak_rating(999))

    async def test_today_week_month_and_inclusive_period_boundaries(self):
        self.demo()
        self.assertEqual(db.get_rating_change(42, self.midnight), 18)
        self.assertEqual(db.get_rating_change(42, self.timestamp - 7 * self.day), 84)
        self.assertEqual(db.get_rating_change(42, self.timestamp - 30 * self.day), 137)
        self.assertEqual(db.get_rating_change(42, self.timestamp + 1), 0)
        self.assertIsNone(db.get_rating_change(999, self.midnight))
        self.player(99, 1500, registered=self.timestamp - 60)
        self.assertEqual(db.get_rating_change(99, self.timestamp - 30 * self.day), 0)
        for index, boundary in enumerate((self.midnight, self.timestamp - 7 * self.day,
                                          self.timestamp - 30 * self.day), 100):
            self.player(index)
            self.event(index, boundary - 1, 50)
            self.event(index, boundary, -10)
            self.event(index, boundary + 1, 3)
            self.assertEqual(db.get_rating_change(index, boundary), -7)

    async def test_historical_rank_uses_current_active_cohort_registration_and_exact_ties(self):
        self.player(42)
        self.player(1)
        self.player(43, 1000.001)
        self.player(99, 9000, linked=False)
        self.player(44, 2000, registered=self.timestamp - 6 * self.day)
        db.add_player(88, "Unrated", self.timestamp - 60 * self.day)
        db.link_telegram_user(88, 88)
        db.link_telegram_user(102, 42)
        before = self.snapshot()
        week = self.timestamp - 7 * self.day
        self.assertEqual([row["account_id"] for row in db.get_leaderboard_at(week)], [43, 1, 42])
        self.assertEqual(db.get_rank_at(42, week), 3)
        self.assertIsNone(db.get_rank_at(44, week))
        self.assertIsNone(db.get_rank_at(99, week))
        self.assertEqual(db.get_rating_at(99, week), 9000)
        self.assertEqual(db.get_rank_at(44, self.timestamp - 6 * self.day), 1)
        self.assertEqual(db.get_rank_at(42, self.timestamp), db.get_leaderboard_position(42)["position"])
        self.assertEqual(self.snapshot(), before)

    async def test_history_button_command_and_rating_summary(self):
        self.demo()
        before = self.snapshot()
        response = await self.send("/history")
        self.assertEqual(response.text, (await self.send(HISTORY_BUTTON)).text)
        for alias in ("/matches", "🎮 Матчи", "📈 История", "📈 История TR"):
            self.assertEqual(response.text, (await self.send(alias)).text)
        self.assertEqual(response.reply_markup, MAIN_KEYBOARD)
        self.assertEqual(response.text,
                         "📜 История матчей\n\n"
                         "13.09\nWIN · Phantom Assassin\n+17 TR → 1124 TR\n\n"
                         "12.09\nWIN · Phantom Assassin\n+1 TR → 1107 TR\n\n"
                         "12.09\nWIN · Phantom Assassin\n+12 TR → 1106 TR\n\n"
                         "11.09\nWIN · Phantom Assassin\n+14 TR → 1094 TR\n\n"
                         "07.09\nWIN · Phantom Assassin\n+40 TR → 1080 TR\n\n"
                         "03.09\nLOSE · Phantom Assassin\n-147 TR → 1040 TR\n\n"
                         "19.08\nWIN · Phantom Assassin\n+200 TR → 1187 TR\n\n"
                         "Сегодня: +18 TR\n7 дней: +84 TR\n30 дней: +137 TR")
        rating = (await self.send("/rating")).text
        self.assertEqual(rating, "👤 Профиль\n\nДЕРЕВЕНСКИЙ\nDota ID: 42\n\n"
                                 "Turbo Rating: 1124 TR\nМесто: #3\nСтартовый TR: 987\n"
                                 "Рекорд: 1187 TR\n\nДата подключения:\n15.07.2026")
        self.assertEqual(rating, (await self.send("/profile")).text)
        self.assertEqual(self.snapshot(), before)

    async def test_history_limit_empty_and_unlinked_users(self):
        response = await self.send("/history")
        self.assertEqual(response.text, self.module.ADD_ACCOUNT_MESSAGE)
        self.assertEqual(response.reply_markup, UNLINKED_KEYBOARD)
        self.player(42, 1187, registered=self.timestamp - self.day)
        empty = (await self.send(HISTORY_BUTTON)).text
        self.assertNotIn("Сейчас:", empty)
        self.assertNotIn("Рекорд:", empty)
        self.assertIn("Сегодня: +0 TR\n7 дней: +0 TR\n30 дней: +0 TR", empty)
        self.assertIn("Матчей пока нет.", empty)
        for index in range(12):
            self.event(42, self.timestamp - 12 + index, 1)
        text = (await self.send("/history")).text
        matches = text.split("\n\n")[1:-1]
        self.assertEqual(len(matches), 10)
        self.assertEqual(matches[0], "13.09\nWIN · Phantom Assassin\n+1 TR → 1199 TR")
        self.assertEqual(matches[-1], "13.09\nWIN · Phantom Assassin\n+1 TR → 1190 TR")

    async def test_match_history_filters_modes_and_calibration_and_preserves_unrated_games(self):
        self.player(42)
        self.player(43)
        self.event(42, self.timestamp - 3600, 16, match_id=1, played_at=self.timestamp - 2 * self.day)
        self.event(42, self.timestamp - 7200, -12, match_id=2, played_at=self.timestamp - self.day)
        self.event(43, self.timestamp, 25, match_id=50)
        unknown = match(50, self.timestamp - 1)
        unknown["radiant_win"] = None
        db.save_match(42, unknown)
        db.save_match(42, match(51, self.timestamp - 2))
        db.save_match(42, match(52, self.timestamp), is_calibration=True)
        db.save_match(42, match(53, self.timestamp, game_mode=1))
        db.save_match(42, match(54, self.timestamp - 70 * self.day))
        before = self.snapshot()
        history = db.get_turbo_match_history(42)
        self.assertEqual([row["match_id"] for row in history], [50, 51, 2, 1])
        self.assertEqual([row["rating_delta"] for row in history], [None, None, -12, 16])
        text = (await self.send("/matches")).text
        self.assertIn("Результат пока неизвестен · Phantom Assassin\nTR не начислен.", text)
        self.assertEqual(text.count("TR не начислен."), 2)
        self.assertIn("12.09\nLOSE · Phantom Assassin\n-12 TR → 1004 TR", text)
        self.assertIn("11.09\nWIN · Phantom Assassin\n+16 TR → 1016 TR", text)
        self.assertEqual(self.snapshot(), before)

    async def test_top_arrows_no_movement_for_new_player_and_own_marker(self):
        self.demo()
        self.player(99, 1090, registered=self.timestamp - 6 * self.day)
        top = (await self.send("/top")).text
        self.assertIn("🥇 Player 1 — 1187  —", top)
        self.assertIn("🥉 ДЕРЕВЕНСКИЙ — 1124  ↑3 ← вы", top)
        self.assertIn("5. Player 3 — 1087  ↓2", top)
        new_line = next(line for line in top.splitlines() if "Player 99" in line)
        self.assertEqual(new_line, "4. Player 99 — 1090")

    async def test_top_twenty_uses_all_historical_players_and_shows_position_below_limit(self):
        for account_id in range(1, 27):
            self.player(account_id)
        self.player(42, 900)
        # First player used to lead but dropped out of the current top twenty.
        self.event(1, self.timestamp - 1, -200)
        top = (await self.send("/top")).text
        self.assertIn("🥇 Player 2 — 1000  ↑1", top)
        self.assertIn("Ваше место: #26 — 900 TR  ↑1", top)
        self.assertEqual(len(db.get_leaderboard()), 20)
        self.assertEqual(len(db.get_leaderboard_at(self.timestamp - 7 * self.day)), 27)

    async def test_notification_rank_wraps_whole_batch_once(self):
        self.player(42, 1124)
        for account_id, rating in enumerate((1200, 1154, 1120, 1110), 1):
            self.player(account_id, rating)
        bot = AsyncMock(spec=Bot)
        updates = [RatingUpdate(1, self.timestamp - 1, 44, True, 1107, 10, 1117),
                   RatingUpdate(2, self.timestamp, 14, True, 1117, 7, 1124)]
        before = self.snapshot()
        await notify_rating_updates(bot, 42, updates)
        text = bot.send_message.await_args.args[1]
        self.assertEqual(text.count("Место:"), 1)
        self.assertTrue(text.endswith("Место:\n#5 → #3"))
        self.assertEqual(text.split("Место:\n")[1], "#5 → #3")
        await notify_rating_updates(bot, 42, [RatingUpdate(3, self.timestamp, 44, True, 1107, 17, 1124)])
        self.assertEqual(bot.send_message.await_args.args[1],
                         "🟢 Победа в Turbo\n\n+17 TR\n1107 → 1124\n\nМесто:\n#5 → #3")
        await notify_rating_updates(bot, 42, [RatingUpdate(4, self.timestamp, 44, False, 1124, -1, 1123)])
        self.assertTrue(bot.send_message.await_args.args[1].endswith("Место: #3"))
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
