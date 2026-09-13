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
from app.keyboards import (
    MAIN_KEYBOARD, UNLINKED_KEYBOARD, LINK_BUTTON, VIEW_TOP_BUTTON, RATING_HELP_BUTTON,
)
from app.notifications import format_rating_updates
from app.services import heroes
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
        self.assertIn("Ваше место:\n#27 — 1000", message.answer.await_args.args[0])
        db.link_telegram_user(101, 3)
        await top_command(message)
        self.assertIn("🥉 Player 3 — 1000 ← вы", message.answer.await_args.args[0])

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
                self.assertEqual(buttons, ["🏆 Мой рейтинг", "🥇 Топ игроков", "📊 Статистика", "🎮 Матчи", "🔄 Обновить", "👤 Профиль", RATING_HELP_BUTTON])
                for button, command in zip(buttons, ("/rating", "/top", "/stats", "/matches", "/sync", "/profile")):
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
                self.assertIn("Место: #1", profile)
                self.assertNotIn("Отслеживание", profile)
                stats = (await send("/stats")).text
                self.assertIn("Победы: 1\nПоражения: 2", stats)
                for command in ("/rating", "/stats", "/matches", "/sync", "/profile"):
                    response = await send(command, user_id=999)
                    self.assertEqual(response.text, bot_module.ADD_ACCOUNT_MESSAGE)
                    self.assertEqual(response.reply_markup, UNLINKED_KEYBOARD)
                for button in buttons:
                    if button in ("🥇 Топ игроков", RATING_HELP_BUTTON):
                        continue
                    response = await send(button, user_id=999)
                    self.assertEqual(response.text, bot_module.ADD_ACCOUNT_MESSAGE)
                    self.assertEqual(response.reply_markup, UNLINKED_KEYBOARD)
                self.assertEqual((await send("🏆 Рейтинг")).text, (await send("/rating")).text)
                self.assertEqual((await send("🥇 Топ")).text, (await send("/top")).text)
        self.assertEqual([cmd.command for cmd in bot_module.BOT_COMMANDS],
                         ["start", "add", "rating", "stats", "top", "matches", "sync", "profile"])

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
            self.assertIn("Как это работает:", response.text)
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
            self.assertIn("Ваш рейтинг: 953\nМесто: #2", repeat.text)
            self.assertIn("🥇 Leader — 1200", repeat.text)
            self.assertNotIn("Как это работает:", repeat.text)
            self.assertNotIn("Привяжите", repeat.text)
            self.assertEqual(repeat.reply_markup, MAIN_KEYBOARD)
        self.history.assert_awaited_once()

    async def test_rating_help_before_and_after_registration(self):
        await self.send(LINK_BUTTON)
        response = await self.send(RATING_HELP_BUTTON)
        self.assertIsNone(await self.state())
        self.assertEqual(response.reply_markup, UNLINKED_KEYBOARD)
        for passage in ("Как считается Turbo Rating", "до 20 последних Turbo-игр",
                        "Средняя точка — 1000 TR.", "WIN → рейтинг растёт",
                        "LOSE → рейтинг падает", "не влияют на рейтинг.",
                        "Учитывается только результат команды."):
            self.assertIn(passage, response.text)
        for technical in ("Elo", "K-factor", "expected_score"):
            self.assertNotIn(technical, response.text)
        db.link_telegram_user(201, 42)
        registered = await self.send(RATING_HELP_BUTTON)
        self.assertEqual(registered.text, response.text)
        self.assertEqual(registered.reply_markup, MAIN_KEYBOARD)
        start = await self.send("/start")
        self.assertIn("Ваш рейтинг: 1200", start.text)
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
        self.assertIn("Готово.", (await self.send("/add https://www.dotabuff.com/players/165682118")).text)
        self.assertEqual((db.get_player(165682118), db.get_rating(165682118)), original)
        self.history.assert_awaited_once()

    async def test_sync_buttons_show_turbo_updates_and_fresh_matches(self):
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
            self.assertIn("2 игры · 1 победа · 1 поражение", rating)
            response = await self.send("🔄 Обновить")
            current = db.get_rating(165682118)["current_rating"]
            self.assertEqual(response.text, f"Данные актуальны.\n\nTurbo Rating: {current:.0f}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
