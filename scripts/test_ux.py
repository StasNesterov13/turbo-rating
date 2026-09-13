"""Offline Telegram UX tests using temporary SQLite and mocked network calls."""

import asyncio
from datetime import datetime, timezone
import importlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, Update, User
import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from app import db
from app.keyboards import MAIN_KEYBOARD
from app.notifications import format_rating_updates
from app.services import heroes
from app.services.game_modes import get_game_mode_name
from app.services.rating import apply_rating_changes
from app.services.sync import RatingUpdate, SyncResult
from scripts.test_rating import match


class HeroTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(heroes, "_hero_names", {}))
        self.enterContext(patch.object(heroes, "_loaded", False))
        self.enterContext(patch.object(heroes, "_load_lock", asyncio.Lock()))
        self.client = AsyncMock()
        factory = self.enterContext(patch.object(heroes.httpx, "AsyncClient"))
        factory.return_value.__aenter__.return_value = self.client

    def response(self, data, status=200):
        return httpx.Response(status, json=data, request=httpx.Request("GET", "https://example.test/heroes"))

    async def test_names_cached_once_and_unknown_fallback(self):
        self.client.get.return_value = self.response({
            "44": {"id": 44, "localized_name": "Phantom Assassin"},
            "14": {"id": 14, "localized_name": "Pudge"},
        })
        await heroes.load_heroes()
        await heroes.load_heroes()
        for _ in range(50):
            self.assertEqual(heroes.get_hero_name(44), "Phantom Assassin")
        self.assertEqual(heroes.get_hero_name(14), "Pudge")
        self.assertEqual(heroes.get_hero_name(99999), "Hero #99999")
        self.assertEqual(heroes.get_hero_name(None), "Неизвестный герой")
        self.client.get.assert_awaited_once_with("https://api.opendota.com/api/constants/heroes")

    async def test_simultaneous_load_fetches_once(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def fetch(*args):
            entered.set()
            await release.wait()
            return self.response({"44": {"id": 44, "localized_name": "Phantom Assassin"}})

        self.client.get.side_effect = fetch
        first = asyncio.create_task(heroes.load_heroes())
        await asyncio.wait_for(entered.wait(), 2)
        second = asyncio.create_task(heroes.load_heroes())
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.client.get.assert_awaited_once()

    async def test_unavailable_or_invalid_constants_keep_fallback(self):
        for failure in (httpx.ReadTimeout("secret-url"), self.response({}, 503),
                        self.response([]), self.response({"44": {"id": 44}})):
            with self.subTest(failure=type(failure).__name__):
                heroes._loaded = False
                self.client.get.reset_mock(side_effect=True)
                if isinstance(failure, Exception):
                    self.client.get.side_effect = failure
                else:
                    self.client.get.return_value = failure
                with self.assertLogs("app.services.heroes", level="WARNING") as logged:
                    await heroes.load_heroes()
                await heroes.load_heroes()
                self.assertNotIn("secret-url", " ".join(logged.output))
                self.assertEqual(heroes.get_hero_name(44), "Hero #44")
                self.client.get.assert_awaited_once()

    async def test_bot_starts_when_constants_fail(self):
        from app import bot as bot_module
        self.client.get.side_effect = httpx.ConnectError("offline")
        with patch.object(db, "init_db"), patch.object(Dispatcher, "include_router"), patch.object(
            Dispatcher, "start_polling", new=AsyncMock()
        ) as polling, patch.object(Bot, "set_my_commands", new=AsyncMock()) as menu, patch.object(
            bot_module, "run_autosync", new=AsyncMock()
        ), self.assertLogs("app.services.heroes", level="WARNING"):
            await bot_module.run_bot("123456:LOCAL_ONLY_TEST_TOKEN")
        polling.assert_awaited_once()
        menu.assert_awaited_once()
        self.assertEqual(heroes.get_hero_name(44), "Hero #44")


class UXTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="turbo-ux-test-")
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(db, "DB_PATH", Path(temporary.name) / "test.db"))
        self.enterContext(patch.object(heroes, "_hero_names", {
            44: "Phantom Assassin", 14: "Pudge", 35: "Sniper",
        }))
        db.init_db()
        db.add_player(42, "Test Player", 1000)
        db.create_rating(42, 1000, [], 0)
        db.link_telegram_user(101, 42)

    async def test_stats_only_turbo_since_tracking_without_calibration(self):
        for game in (match(1, 1000), match(2, 1001), match(3, 1002, False),
                     match(4, 999), match(5, 1003, game_mode=1)):
            db.save_match(42, game)
        db.save_match(42, match(6, 1004), is_calibration=True)
        stats = db.get_turbo_stats(42)
        self.assertEqual((stats["matches"], stats["wins"], stats["losses"]), (3, 2, 1))
        self.assertAlmostEqual(stats["winrate"], 200 / 3)
        unknown = match(7, 1005)
        unknown["radiant_win"] = None
        db.save_match(42, unknown)
        stats = db.get_turbo_stats(42)
        self.assertEqual((stats["matches"], stats["unknown"]), (4, 1))
        self.assertAlmostEqual(stats["winrate"], 200 / 3)
        self.assertEqual(db.get_turbo_stats(999)["winrate"], 0)

    async def test_last_five_ratings_have_hero_names_without_recalculation(self):
        from app.bot import rating_command
        for index in range(1, 7):
            game = match(index, 1000 + index, index % 2 == 0)
            game["hero_id"] = 14 if index % 2 else 44
            db.save_match(42, game)
        db.save_match(42, match(7, 2000, game_mode=1))
        db.save_match(42, match(8, 2001), is_calibration=True)
        apply_rating_changes(42)
        before = db.get_rating(42), db.get_rating_history(42)
        history = db.get_rating_history(42, limit=5)
        self.assertEqual([row["match_id"] for row in history], [6, 5, 4, 3, 2])
        self.assertEqual([row["hero_id"] for row in history], [44, 14, 44, 14, 44])
        message = SimpleNamespace(from_user=SimpleNamespace(id=101), answer=AsyncMock())
        with patch("app.services.rating.apply_rating_changes", side_effect=AssertionError("recalculated")):
            await rating_command(message)
            await rating_command(message)
        text = message.answer.await_args.args[0]
        self.assertEqual(text.count("Phantom Assassin"), 3)
        self.assertEqual(text.count("Pudge"), 2)
        self.assertEqual((db.get_rating(42), db.get_rating_history(42)), before)

    async def test_leaderboard_position_stable_ties_and_below_top_twenty(self):
        from app.bot import top_command
        for account_id in range(1, 27):
            db.add_player(account_id, f"Player {account_id}", 1000)
            db.create_rating(account_id, 1000, [], 0)
        db.add_player(99, "Unrated", 1000)
        db.link_telegram_user(102, 42)
        self.assertEqual(db.get_leaderboard_position(42)["position"], 27)
        self.assertIsNone(db.get_leaderboard_position(99))
        self.assertEqual([row["account_id"] for row in db.get_leaderboard()], list(range(1, 21)))
        message = SimpleNamespace(from_user=SimpleNamespace(id=101), answer=AsyncMock())
        await top_command(message)
        self.assertIn("Ваше место: #27 — 1000", message.answer.await_args.args[0])
        db.link_telegram_user(101, 3)
        await top_command(message)
        self.assertIn("👉 🥉 Player 3 — 1000", message.answer.await_args.args[0])

    async def test_commands_and_buttons_through_dispatcher(self):
        from app import bot as bot_module
        # Each integration suite needs its own unattached Router instance.
        importlib.reload(bot_module)
        dispatcher = Dispatcher()
        dispatcher.include_router(bot_module.router)
        for index in range(1, 7):
            game = match(index, 1000 + index, index % 2 == 0, game_mode={4: 1, 5: 22, 6: 77}.get(index, 23))
            game["hero_id"] = 14 if index % 2 else 44
            db.save_match(42, game)
        apply_rating_changes(42)
        outgoing = AsyncMock(return_value=True)
        sync_result = SyncResult(received_count=0, new_matches=[], rating_changes=[])
        async with Bot(token="123456:LOCAL_ONLY_TEST_TOKEN") as bot:
            with patch.object(Bot, "__call__", outgoing), patch.object(
                bot_module, "ensure_player", new=AsyncMock(return_value={"personaname": "Test Player"})
            ), patch.object(bot_module, "sync_player", new=AsyncMock(return_value=sync_result)) as sync:
                async def send(text, user_id=101):
                    outgoing.reset_mock()
                    message = Message(message_id=1, date=datetime.now(timezone.utc),
                                      chat=Chat(id=user_id, type="private"),
                                      from_user=User(id=user_id, is_bot=False, first_name="Test"), text=text)
                    await dispatcher.feed_update(bot, Update(update_id=1, message=message))
                    outgoing.assert_awaited_once()
                    return outgoing.await_args.args[0]

                for command in ("/start", "/add 42"):
                    response = await send(command)
                    self.assertEqual(response.reply_markup, MAIN_KEYBOARD)
                buttons = [button.text for row in MAIN_KEYBOARD.keyboard for button in row]
                self.assertEqual(buttons, ["🏆 Рейтинг", "📊 Статистика", "🥇 Топ", "🎮 Матчи", "🔄 Обновить"])
                for button, command in zip(buttons, ("/rating", "/stats", "/top", "/matches", "/sync")):
                    self.assertEqual((await send(button)).text, (await send(command)).text)
                self.assertEqual(sync.await_count, 2)
                sync.assert_awaited_with(42, api_key=None)
                text = (await send("/matches")).text
                self.assertEqual(text.count(" мин"), 5)
                for value in ("Phantom Assassin", "Pudge", "All Pick", "Ranked All Pick", "Mode #77"):
                    self.assertIn(value, text)
                self.assertNotIn("hero_id", text)
                profile = (await send("/profile")).text
                self.assertIn("Turbo игр: 3", profile)
                self.assertIn("Winrate: 33.3%", profile)
                self.assertIn("01.01.1970", profile)
                stats = (await send("/stats")).text
                self.assertIn("Победы: 1\nПоражения: 2", stats)
                for command in ("/rating", "/stats", "/matches", "/sync", "/profile"):
                    self.assertEqual((await send(command, user_id=999)).text, bot_module.ADD_ACCOUNT_MESSAGE)
        self.assertEqual([cmd.command for cmd in bot_module.BOT_COMMANDS],
                         ["start", "add", "rating", "stats", "top", "matches", "sync"])

    async def test_named_notifications_and_length_with_long_names(self):
        win = RatingUpdate(1, 1001, 44, True, 1000, 16, 1016)
        loss = RatingUpdate(2, 1002, 14, False, 1016, -17, 999)
        self.assertEqual(format_rating_updates([win]),
                         "🟢 Победа в Turbo\n\nPhantom Assassin\n\n+16 TR\n1000 → 1016")
        self.assertEqual(format_rating_updates([loss]),
                         "🔴 Поражение в Turbo\n\nPudge\n\n-17 TR\n1016 → 999")
        group = format_rating_updates([win, loss])
        self.assertIn("🟢 Phantom Assassin  +16 TR", group)
        self.assertIn("🔴 Pudge  -17 TR", group)
        self.assertTrue(group.endswith("Rating: 1000 → 999"))
        with patch.dict(heroes._hero_names, {44: "🟢" * 60}):
            for manual in (True, False):
                text = format_rating_updates([win] * 1000, manual=manual)
                self.assertLess(len(text.encode("utf-16-le")) // 2, 3800)
                self.assertIn("ещё матчей:", text)

    async def test_game_modes(self):
        self.assertEqual([get_game_mode_name(mode) for mode in (23, 1, 22, 77)],
                         ["Turbo", "All Pick", "Ranked All Pick", "Mode #77"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
