"""Offline Telegram UX tests using temporary SQLite and mocked network calls."""

import asyncio
from datetime import datetime, timezone
import importlib
from pathlib import Path
import sqlite3
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
from app.keyboards import (
    MAIN_KEYBOARD, UNLINKED_KEYBOARD, LINK_BUTTON, VIEW_TOP_BUTTON, RATING_HELP_BUTTON, SHARE_BUTTON, HISTORY_BUTTON,
)
from app.notifications import format_rating_updates, notify_rating_updates
from app.services import heroes, sync as sync_service
from app.services.game_modes import get_game_mode_name
from app.services.accounts import parse_dota_account_id
from app.services.opendota import OpenDotaClient
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
        from app import bot as bot_module
        temporary = tempfile.TemporaryDirectory(prefix="turbo-ux-test-")
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(db, "DB_PATH", Path(temporary.name) / "test.db"))
        self.enterContext(patch.object(heroes, "_hero_names", {
            44: "Phantom Assassin", 14: "Pudge", 35: "Sniper",
        }))
        self.enterContext(patch.object(sync_service, "_last_sync_times", {}))
        self.enterContext(patch.object(bot_module, "_sync_cooldowns", {}))
        self.enterContext(patch.object(bot_module, "_sync_in_progress", set()))
        db.init_db()
        db.add_player(42, "Test Player", 1000)
        db.create_rating(42, 1000, [], 0)
        db.link_telegram_user(101, 42)

    async def test_home_and_rating_show_distinct_summaries_without_changing_rating(self):
        from app.bot import start_command, rating_command
        for index, win in enumerate((False, True, False, True, True, True), 1):
            db.save_match(42, match(index, 1000 + index, win))
        db.save_match(42, match(7, 1100, False, game_mode=1))
        db.save_match(42, match(8, 1200, False), is_calibration=True)
        db.save_match(42, match(9, 999, False))
        unknown = match(10, 1300)
        unknown["radiant_win"] = None
        db.save_match(42, unknown)
        before = db.get_rating(42), db.get_rating_history(42)
        sync_service._last_sync_times[42] = 1704067200
        message = SimpleNamespace(from_user=SimpleNamespace(id=101), answer=AsyncMock())
        await start_command(message)
        text = message.answer.await_args.args[0]
        self.assertEqual(text, "🏆 Turbo Rating\n\nTest Player\n1000 TR · место #1")
        self.assertEqual(message.answer.await_args.kwargs["reply_markup"], MAIN_KEYBOARD)
        await rating_command(message)
        text = message.answer.await_args.args[0]
        self.assertEqual(text, "🏆 Мой рейтинг\n\n1000 TR\nМесто: #1\nСтарт: 1000 TR\n"
                               "Рекорд: 1000 TR")
        self.assertEqual((db.get_rating(42), db.get_rating_history(42)), before)

    async def test_home_excludes_matches_with_empty_or_populated_history(self):
        from app.bot import start_command
        message = SimpleNamespace(from_user=SimpleNamespace(id=101), answer=AsyncMock())
        await start_command(message)
        expected = "🏆 Turbo Rating\n\nTest Player\n1000 TR · место #1"
        self.assertEqual(message.answer.await_args.args[0], expected)
        for index in range(1, 8):
            db.save_match(42, match(index, 1000 + index))
        await start_command(message)
        self.assertEqual(message.answer.await_args.args[0], expected)
        # Equal start times are resolved by match_id, just like rating history.
        for index in (8, 9):
            db.save_match(42, match(index, 1007, False))
        await start_command(message)
        self.assertEqual(message.answer.await_args.args[0], expected)

    async def test_manual_cooldown_is_per_telegram_user_across_chats(self):
        from app import bot as bot_module
        db.link_telegram_user(102, 42)
        message = SimpleNamespace(from_user=SimpleNamespace(id=101), answer=AsyncMock())
        other_chat = SimpleNamespace(from_user=SimpleNamespace(id=101), answer=AsyncMock())
        other_user = SimpleNamespace(from_user=SimpleNamespace(id=102), answer=AsyncMock())
        with patch.object(bot_module, "monotonic", return_value=100) as clock, patch.object(
            bot_module, "sync_player", new=AsyncMock(return_value=SyncResult(0, []))
        ) as sync:
            await bot_module.sync_command(message)
            clock.return_value = 115.2
            await bot_module.sync_command(other_chat)
            self.assertEqual(other_chat.answer.await_args.args[0],
                             "Данные недавно обновлялись.\nПопробуйте через 15 сек.")
            sync.assert_awaited_once()
            await bot_module.sync_command(other_user)
            self.assertEqual(sync.await_count, 2)
            clock.return_value = 130
            await bot_module.sync_command(message)
            self.assertEqual(sync.await_count, 3)

    async def test_sync_errors_are_friendly_logged_and_allow_immediate_retry(self):
        from app import bot as bot_module
        message = SimpleNamespace(from_user=SimpleNamespace(id=101), answer=AsyncMock())
        request = httpx.Request("GET", "https://example.test/?api_key=private-key")
        failures = [
            httpx.ReadTimeout("private-key"),
            httpx.HTTPStatusError("private-key", request=request, response=httpx.Response(503, request=request)),
            sync_service.MatchHistoryUnavailable("fh_unavailable=true"),
            ValueError("malformed OpenDota history"), sqlite3.OperationalError("technical detail"),
        ]
        with patch.object(bot_module, "sync_player", new=AsyncMock()) as sync:
            for failure in failures:
                sync.side_effect = failure
                with self.assertLogs("app.bot", level="ERROR") as logged:
                    await bot_module.sync_command(message)
                text = message.answer.await_args.args[0]
                if isinstance(failure, sync_service.MatchHistoryUnavailable):
                    self.assertEqual(text, bot_module.HISTORY_UNAVAILABLE_MESSAGE)
                elif not isinstance(failure, sqlite3.Error):
                    self.assertEqual(text, bot_module.OPENDOTA_ERROR_MESSAGE)
                self.assertIn(type(failure).__name__, " ".join(logged.output))
                for secret in ("private-key", "traceback", "503", "technical detail"):
                    self.assertNotIn(secret, text)
                self.assertNotIn("private-key", " ".join(logged.output))
                self.assertNotIn(101, bot_module._sync_cooldowns)
            sync.side_effect = None
            sync.return_value = SyncResult(0, [])
            await bot_module.sync_command(message)
            self.assertIn("Данные актуальны", message.answer.await_args.args[0])

    async def test_slow_sync_cannot_duplicate_and_cancellation_clears_cooldown(self):
        from app import bot as bot_module
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        message = SimpleNamespace(from_user=SimpleNamespace(id=101), answer=AsyncMock())
        with patch.object(bot_module, "monotonic", return_value=100) as clock, patch.object(
            bot_module, "sync_player", new=AsyncMock(side_effect=blocked)
        ) as sync:
            task = asyncio.create_task(bot_module.sync_command(message))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                clock.return_value = 140
                await bot_module.sync_command(message)
                self.assertIn("Обновление уже выполняется", message.answer.await_args.args[0])
                sync.assert_awaited_once()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertNotIn(101, bot_module._sync_in_progress)
            self.assertNotIn(101, bot_module._sync_cooldowns)

    async def test_notifications_show_moved_unchanged_and_tied_positions_read_only(self):
        for account_id, rating in ((43, 1055), (44, 1100), (45, 1200)):
            db.add_player(account_id, str(account_id), 1000)
            db.create_rating(account_id, rating, [], 0)
            db.link_telegram_user(account_id, account_id)
        db.link_telegram_user(102, 43)  # Multiple subscribers cannot inflate rank.
        db.add_player(46, "Inactive", 1000)
        db.create_rating(46, 9000, [], 0)
        with db._connect() as connection:
            connection.execute("UPDATE ratings SET current_rating = 1070 WHERE account_id = 42")
        before = db.get_rating(42), db.get_rating_history(42)
        bot = AsyncMock(spec=Bot)
        for start, end, position in ((1053, 1070, "#4 → #3"), (1050, 1053, "#4"),
                                     (1053, 1055, "#4 → #3"), (1070, 1053, "#3 → #4")):
            await notify_rating_updates(bot, 42, [RatingUpdate(1, 1001, 44, end > start, start, end - start, end)])
            prefix = "Место:\n" if "→" in position else "Место: "
            self.assertTrue(bot.send_message.await_args.args[1].endswith(prefix + position))
        self.assertEqual((db.get_rating(42), db.get_rating_history(42)), before)
        self.assertEqual(format_rating_updates(
            [RatingUpdate(1, 1001, 44, True, 1053, 17, 1070)], position_before=4, position_after=3,
        ), "🟢 Победа в Turbo\n\n+17 TR\n1053 → 1070\n\nМесто:\n#4 → #3")

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

    async def test_rating_summary_excludes_history_without_recalculation(self):
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
        self.assertIn("Рекорд:", text)
        self.assertNotIn("7 дней:", text)
        self.assertIn("Старт: 1000 TR", text)
        self.assertIn("Место: #1", text)
        self.assertNotIn(HISTORY_BUTTON, text)
        self.assertNotIn("Последние изменения:", text)
        self.assertEqual((db.get_rating(42), db.get_rating_history(42)), before)

    async def test_leaderboard_position_stable_ties_and_below_top_twenty(self):
        from app.bot import top_command
        for account_id in range(1, 27):
            db.add_player(account_id, f"Player {account_id}", 1000)
            db.create_rating(account_id, 1000, [], 0)
            db.link_telegram_user(1000 + account_id, account_id)
        db.add_player(99, "Unrated", 1000)
        db.link_telegram_user(102, 42)
        self.assertEqual(db.get_leaderboard_position(42)["position"], 27)
        self.assertIsNone(db.get_leaderboard_position(99))
        self.assertEqual([row["account_id"] for row in db.get_leaderboard()], list(range(1, 21)))
        message = SimpleNamespace(from_user=SimpleNamespace(id=101), answer=AsyncMock())
        await top_command(message)
        self.assertIn("Ваше место: #27 — 1000 TR", message.answer.await_args.args[0])
        db.link_telegram_user(101, 3)
        await top_command(message)
        self.assertIn("🥉 Player 3 — 1000  — ← вы", message.answer.await_args.args[0])

    async def test_commands_and_buttons_through_dispatcher(self):
        from app import bot as bot_module
        # Each integration suite needs its own unattached Router instance.
        importlib.reload(bot_module)
        self.enterContext(patch.object(bot_module, "MANUAL_SYNC_COOLDOWN", 0))
        self.enterContext(patch.object(Bot, "me", new=AsyncMock(return_value=User(
            id=123456, is_bot=True, first_name="Turbo", username="turbo_rating_test_bot",
        ))))
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
                bot_module, "sync_player", new=AsyncMock(return_value=sync_result)
            ) as sync:
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
                self.assertEqual(buttons, ["🏆 Мой рейтинг", "🥇 Топ", HISTORY_BUTTON,
                                           "🎮 Матчи", "🔄 Обновить", "👤 Профиль", RATING_HELP_BUTTON])
                for button, command in zip(buttons, ("/rating", "/top", "/history", "/matches", "/sync", "/profile")):
                    self.assertEqual((await send(button)).text, (await send(command)).text)
                self.assertEqual(sync.await_count, 2)
                sync.assert_awaited_with(42, api_key=None)
                text = (await send("/matches")).text
                self.assertEqual(text.count(" мин"), 5)
                for value in ("Phantom Assassin", "Pudge", "All Pick", "Ranked All Pick", "Mode #77"):
                    self.assertIn(value, text)
                self.assertNotIn("hero_id", text)
                profile = (await send("/profile")).text
                self.assertEqual(profile, "👤 Профиль\n\nTest Player\n\nDota ID: 42\n"
                                          "Подключён: 01.01.1970")
                rating = (await send("/rating")).text
                for alias in ("/stats", "📊 Статистика"):
                    self.assertEqual((await send(alias)).text, rating)
                # Each screen keeps only its own details, including with populated history.
                for command, forbidden in {
                    "/start": ("Старт:", "Рекорд:", "7 дней", "30 дней", "Dota ID:", "Обновлено:", "Последние", "WIN", "LOSE"),
                    "/rating": ("Последние", "Сегодня:", "7 дней", "30 дней", "Dota ID:", "Test Player"),
                    "/history": ("Старт:", "Рекорд:", "Сейчас:", "Dota ID:", "Test Player"),
                    "/top": ("Старт:", "Рекорд:", "Последние", "Сегодня:", "Dota ID:"),
                    "/matches": ("TR", "Rating", "Место:", "Старт:", "Рекорд:", "7 дней"),
                    "/profile": ("TR", "Rating", "Место:", "Старт:", "Рекорд:", "Последние", "Сегодня:", "7 дней", "30 дней"),
                }.items():
                    screen = (await send(command)).text
                    for label in (*forbidden, "Winrate", "WR", "Turbo игр:", "Победы:", "Поражения:", "Серия:",
                                  "Как это работает:", "Как считается Turbo Rating", "Чтобы начать", "10W / 10L"):
                        with self.subTest(command=command, label=label):
                            self.assertNotIn(label, screen)
                for command in ("/rating", "/stats", "/history", "/matches", "/sync", "/profile"):
                    response = await send(command, user_id=999)
                    self.assertEqual(response.text, bot_module.ADD_ACCOUNT_MESSAGE)
                    self.assertEqual(response.reply_markup, UNLINKED_KEYBOARD)
                for button in buttons:
                    if button in ("🥇 Топ", RATING_HELP_BUTTON):
                        continue
                    response = await send(button, user_id=999)
                    self.assertEqual(response.text, bot_module.ADD_ACCOUNT_MESSAGE)
                    self.assertEqual(response.reply_markup, UNLINKED_KEYBOARD)
                self.assertEqual((await send("🏆 Рейтинг")).text, (await send("/rating")).text)
                self.assertEqual((await send("🥇 Топ")).text, (await send("/top")).text)
                self.assertEqual((await send("🥇 Топ игроков")).text, (await send("/top")).text)
                self.assertEqual((await send(HISTORY_BUTTON)).text, (await send("/history")).text)
                shared = await send(SHARE_BUTTON)
                self.assertEqual(shared.text,
                                 "🏆 Turbo Rating — рейтинг Turbo среди друзей.\nПрисоединяйся: https://t.me/turbo_rating_test_bot")
                self.assertEqual(shared.reply_markup, MAIN_KEYBOARD)
        self.assertEqual([cmd.command for cmd in bot_module.BOT_COMMANDS],
                         ["start", "add", "rating", "history", "top", "matches", "sync", "profile"])

    async def test_named_notifications_and_length_with_long_names(self):
        win = RatingUpdate(1, 1001, 44, True, 1000, 16, 1016)
        loss = RatingUpdate(2, 1002, 14, False, 1016, -17, 999)
        self.assertEqual(format_rating_updates([win]),
                         "🟢 Победа в Turbo\n\n+16 TR\n1000 → 1016")
        self.assertEqual(format_rating_updates([loss]),
                         "🔴 Поражение в Turbo\n\n-17 TR\n1016 → 999")
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


class AccountParsingTests(unittest.TestCase):
    def test_supported_formats(self):
        for text in ("165682118", " 165682118\n",
                     "https://www.opendota.com/players/165682118",
                     "https://www.dotabuff.com/players/165682118",
                     "https://opendota.com/players/165682118/?foo=bar#overview",
                     "http://dotabuff.com/players/165682118"):
            with self.subTest(text=text):
                self.assertEqual(parse_dota_account_id(text), 165682118)
        self.assertEqual(parse_dota_account_id("1"), 1)
        self.assertEqual(parse_dota_account_id("4294967295"), 2**32 - 1)

    def test_invalid_formats(self):
        for text in ("", "0", "-1", "+123", "12.5", "4294967296", "76561198125947846",
                     "hello", "１２３", "9" * 5000, "https://[bad",
                     "https://www.opendota.com.evil.test/players/165682118",
                     "https://www.opendota.com@evil.test/players/165682118",
                     "https://www.opendota.com/players/0",
                     "https://www.opendota.com/players/165682118/matches",
                     "https://steamcommunity.com/id/someone", "/add 165682118"):
            with self.subTest(text=text[:100]):
                self.assertIsNone(parse_dota_account_id(text))


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import bot as bot_module
        self.module = importlib.reload(bot_module)
        temporary = tempfile.TemporaryDirectory(prefix="turbo-onboarding-test-")
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(db, "DB_PATH", Path(temporary.name) / "test.db"))
        db.init_db()
        db.add_player(42, "Leader", 1000)
        db.create_rating(42, 1200, [], 0)
        db.link_telegram_user(101, 42)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(self.module.router)
        self.addAsyncCleanup(self.dispatcher.storage.close)
        self.bot = await self.enterAsyncContext(Bot(token="123456:LOCAL_ONLY_TEST_TOKEN"))
        self.outgoing = self.enterContext(patch.object(Bot, "__call__", new=AsyncMock(return_value=True)))
        self.profile = self.enterContext(patch.object(OpenDotaClient, "get_player", new=AsyncMock(
            return_value={"profile": {"account_id": 165682118, "personaname": "ДЕРЕВЕНСКИЙ"}}
        )))
        self.history = self.enterContext(patch.object(OpenDotaClient, "get_turbo_matches_before", new=AsyncMock(
            return_value=[match(i, 900 - i, i < 8) for i in range(20)]
        )))

    async def send(self, text, user_id=201, *, expect_answer=True):
        self.outgoing.reset_mock()
        message = Message(message_id=1, date=datetime.now(timezone.utc),
                          chat=Chat(id=user_id, type="private"),
                          from_user=User(id=user_id, is_bot=False, first_name="Test"), text=text)
        await self.dispatcher.feed_update(self.bot, Update(update_id=1, message=message))
        if expect_answer:
            self.outgoing.assert_awaited_once()
            return self.outgoing.await_args.args[0]
        self.outgoing.assert_not_awaited()

    async def state(self, user_id=201):
        context = self.dispatcher.fsm.get_context(bot=self.bot, chat_id=user_id, user_id=user_id)
        return await context.get_state()

    async def test_start_link_by_id_and_urls_then_repeat_start(self):
        original = None
        for user_id, value in enumerate(("165682118", "https://www.opendota.com/players/165682118",
                                         "https://www.dotabuff.com/players/165682118"), 201):
            response = await self.send("/start", user_id)
            self.assertEqual(response.text, "🏆 Turbo Rating\n\nРейтинг Turbo-игр среди друзей.\n\n"
                                            "Чтобы начать, привяжите Dota-профиль.")
            self.assertIn("Чтобы начать, привяжите Dota-профиль.", response.text)
            self.assertNotIn("Leader", response.text)
            self.assertNotIn("/add", response.text)
            self.assertEqual(response.reply_markup, UNLINKED_KEYBOARD)
            self.assertEqual(
                [button.text for row in response.reply_markup.keyboard for button in row],
                [LINK_BUTTON, VIEW_TOP_BUTTON, RATING_HELP_BUTTON],
            )
            self.assertEqual((await self.send(VIEW_TOP_BUTTON, user_id)).text,
                             (await self.send("/top", user_id)).text)
            self.assertEqual((await self.send(LINK_BUTTON, user_id)).text, self.module.LINK_PROMPT)
            self.assertEqual(await self.state(user_id), self.module.LinkDota.waiting_for_account.state)
            response = await self.send(value, user_id)
            self.profile.assert_awaited_with(165682118)
            self.assertIn("Готово.", response.text)
            self.assertIn("Turbo Rating: 953", response.text)
            self.assertIn("Место: #2", response.text)
            self.assertIn("по последним 20 Turbo-матчам", response.text)
            self.assertNotIn("OpenDota", response.text)
            self.assertNotIn("account_id", response.text)
            self.assertEqual(response.reply_markup, MAIN_KEYBOARD)
            self.assertIsNone(await self.state(user_id))
            self.assertEqual(db.get_telegram_player(user_id)["account_id"], 165682118)
            snapshot = db.get_player(165682118), db.get_rating(165682118)
            if original is None:
                original = snapshot
            self.assertEqual(snapshot, original)
            repeat = await self.send("/start", user_id)
            self.assertIn("ДЕРЕВЕНСКИЙ\n953 TR · место #2", repeat.text)
            self.assertEqual(repeat.text, "🏆 Turbo Rating\n\nДЕРЕВЕНСКИЙ\n953 TR · место #2")
            self.assertNotIn("Leader", repeat.text)
            self.assertNotIn("Как это работает:", repeat.text)
            self.assertNotIn("Привяжите", repeat.text)
            self.assertEqual(repeat.reply_markup, MAIN_KEYBOARD)
        self.history.assert_awaited_once()

    async def test_rating_help_before_and_after_registration(self):
        await self.send(LINK_BUTTON)
        response = await self.send(RATING_HELP_BUTTON)
        self.assertIsNone(await self.state())
        self.assertEqual(response.reply_markup, UNLINKED_KEYBOARD)
        self.assertEqual(response.text,
                         "🏆 Как считается Turbo Rating\n\n"
                         "Стартовый TR считается по последним 20 Turbo.\n\n"
                         "После подключения:\n\n"
                         "1000 TR → WIN +16 / LOSE -12\n1400 TR → WIN +20 / LOSE -14\n"
                         "1800 TR → WIN +24 / LOSE -16\n2000 TR → WIN +26 / LOSE -17\n\n"
                         "Чем выше TR, тем больше очков даёт победа.\nПобеда всегда ценнее поражения.")
        for technical in ("Elo", "K-factor", "expected_score", "expected score"):
            self.assertNotIn(technical, response.text)
        db.link_telegram_user(201, 42)
        registered = await self.send(RATING_HELP_BUTTON)
        self.assertEqual(registered.text, response.text)
        self.assertEqual(registered.reply_markup, MAIN_KEYBOARD)
        start = await self.send("/start")
        self.assertIn("1200 TR · место #1", start.text)
        self.assertNotIn("Как это работает:", start.text)
        self.assertIn(RATING_HELP_BUTTON, [b.text for row in start.reply_markup.keyboard for b in row])
        self.profile.assert_not_awaited()
        self.history.assert_not_awaited()

    async def test_invalid_input_and_network_failure_allow_retry(self):
        await self.send(LINK_BUTTON)
        for invalid in ("bad", "0", None):
            self.assertEqual((await self.send(invalid)).text, self.module.INVALID_ACCOUNT_MESSAGE)
            self.assertIsNotNone(await self.state())
        self.profile.assert_not_awaited()
        self.profile.side_effect = httpx.ReadTimeout("private-url")
        response = await self.send("165682118")
        self.assertNotIn("private-url", response.text)
        self.assertIsNone(db.get_telegram_player(201))
        self.assertIsNotNone(await self.state())
        self.profile.side_effect = None
        self.assertIn("Готово.", (await self.send("165682118")).text)
        self.assertIsNone(await self.state())

    async def test_waiting_is_per_user_and_menu_exits_linking(self):
        await self.send(LINK_BUTTON)
        await self.send("165682118", user_id=202, expect_answer=False)
        self.assertIsNotNone(await self.state(201))
        self.assertIsNone(await self.state(202))
        self.profile.assert_not_awaited()
        for navigation in ("/start", "🥇 Топ игроков", "🏆 Мой рейтинг"):
            await self.send(LINK_BUTTON)
            response = await self.send(navigation)
            self.assertNotEqual(response.text, self.module.INVALID_ACCOUNT_MESSAGE)
            self.assertIsNone(await self.state())
        self.profile.assert_not_awaited()

    async def test_add_without_argument_uses_same_link_flow(self):
        self.assertEqual((await self.send("/add")).text, self.module.LINK_PROMPT)
        self.assertIn("Готово.", (await self.send("165682118")).text)
        original = db.get_player(165682118), db.get_rating(165682118)
        self.assertIn("Этот Dota-профиль уже подключён.", (await self.send("/add https://www.dotabuff.com/players/165682118")).text)
        self.assertEqual((db.get_player(165682118), db.get_rating(165682118)), original)
        self.history.assert_awaited_once()

    async def test_sync_buttons_show_turbo_updates_and_fresh_matches(self):
        self.enterContext(patch.object(self.module, "MANUAL_SYNC_COOLDOWN", 0))
        await self.send("/add 165682118")
        start = db.get_player(165682118)["tracking_started_at"]
        with patch.object(OpenDotaClient, "get_matches_for_sync", new=AsyncMock(return_value=[
            match(100, start), match(101, start + 1, False), match(102, start + 2, game_mode=1),
        ])) as fetch:
            response = await self.send("🔄 Обновить")
            self.assertIn("Новых Turbo: 2", response.text)
            self.assertIn("WIN  +", response.text)
            self.assertIn("LOSE  -", response.text)
            self.assertIn("Rating:\n", response.text)
            for technical in ("API", "дубликат", "пропущено", "получено"):
                self.assertNotIn(technical, response.text)
            fetch.assert_awaited_once_with(165682118, start)
            matches = (await self.send("🎮 Матчи")).text
            self.assertIn("All Pick", matches)
            self.assertIn("Turbo", matches)
            self.assertEqual(db.count_rated_matches(165682118), 2)
            rating = (await self.send("🏆 Мой рейтинг")).text
            current = db.get_rating(165682118)["current_rating"]
            self.assertIn(f"{current:.0f} TR", rating)
            self.assertNotIn("Последние:", rating)
            self.assertNotIn("Последние:", (await self.send("/start")).text)
            response = await self.send("🔄 Обновить")
            current = db.get_rating(165682118)["current_rating"]
            self.assertEqual(response.text, f"Данные актуальны.\n\nTurbo Rating: {current:.0f}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
