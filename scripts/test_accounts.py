"""Offline account switching tests through Telegram dispatch and real SQLite."""

import asyncio
from datetime import datetime, timezone
import importlib
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(PROJECT_ROOT))

from app import db
from app.autosync import sync_tracked_players
from app.keyboards import CHANGE_BUTTON, LINK_BUTTON, MAIN_KEYBOARD, UNLINKED_KEYBOARD
from app.services.accounts import link_dota_account
from app.services.opendota import OpenDotaClient
from app.services.rating import apply_rating_changes, calculate_initial_rating
from app.services.sync import SyncResult
from scripts.test_rating import match


class AccountSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app import bot as bot_module
        self.module = importlib.reload(bot_module)
        temporary = tempfile.TemporaryDirectory(prefix="turbo-accounts-test-")
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(db, "DB_PATH", Path(temporary.name) / "test.db"))
        db.init_db()
        db.add_player(42, "Old Player", 1000)
        db.create_rating(42, 1074, [match(1, 900)], 1)
        db.save_match(42, match(2, 1001))
        apply_rating_changes(42)
        db.link_telegram_user(201, 42)
        self.dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
        self.dispatcher.include_router(self.module.router)
        self.addAsyncCleanup(self.dispatcher.storage.close)
        self.addAsyncCleanup(self.dispatcher.fsm.events_isolation.close)
        self.bot = await self.enterAsyncContext(Bot(token="123456:LOCAL_ONLY_TEST_TOKEN"))
        self.outgoing = self.enterContext(patch.object(Bot, "__call__", new=AsyncMock(return_value=True)))
        self.profile = self.enterContext(patch.object(OpenDotaClient, "get_player", new=AsyncMock(
            side_effect=lambda account_id: {"profile": {"account_id": account_id, "personaname": f"Player {account_id}"}},
        )))
        self.history = self.enterContext(patch.object(OpenDotaClient, "get_turbo_matches_before", new=AsyncMock(
            return_value=[match(i, 900 - i, i < 13) for i in range(20)],
        )))
        # Fail immediately if a test accidentally makes an unmocked HTTP request.
        self.enterContext(patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("Unexpected HTTP")))

    def snapshot(self):
        with db._connect() as connection:
            return list(connection.iterdump())

    def player_snapshot(self, account_id):
        return (db.get_player(account_id), db.get_rating(account_id),
                db.get_player_matches(account_id, include_calibration=True), db.get_rating_history(account_id))

    def context(self, user_id=201, chat_id=None):
        return self.dispatcher.fsm.get_context(bot=self.bot, chat_id=chat_id or user_id, user_id=user_id)

    async def send(self, text, user_id=201, chat_id=None):
        self.outgoing.reset_mock()
        message = Message(
            message_id=1, date=datetime.now(timezone.utc),
            chat=Chat(id=chat_id or user_id, type="private" if chat_id is None else "group"),
            from_user=User(id=user_id, is_bot=False, first_name="Test"), text=text,
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=1, message=message))
        responses = [call.args[0] for call in self.outgoing.await_args_list if isinstance(call.args[0], SendMessage)]
        return responses

    async def click(self, data, user_id=201, chat_id=None):
        self.outgoing.reset_mock()
        callback = CallbackQuery(
            id="test-callback", chat_instance="local", data=data,
            from_user=User(id=user_id, is_bot=False, first_name="Test"),
            message=Message(message_id=2, date=datetime.now(timezone.utc),
                            chat=Chat(id=chat_id or user_id, type="private" if chat_id is None else "group")),
        )
        await self.dispatcher.feed_update(self.bot, Update(update_id=2, callback_query=callback))
        return [call.args[0] for call in self.outgoing.await_args_list]

    async def begin_change(self):
        confirmation = (await self.send(CHANGE_BUTTON))[0]
        buttons = confirmation.reply_markup.inline_keyboard
        responses = await self.click(buttons[0][0].callback_data)
        prompt = next(response for response in responses if isinstance(response, EditMessageText))
        self.assertEqual(prompt.text, self.module.CHANGE_PROMPT)
        self.assertEqual(await self.context().get_state(), self.module.ChangeDota.waiting_for_account.state)
        return prompt

    async def test_menu_and_confirmation_do_not_mutate_database(self):
        before = self.snapshot()
        linked = (await self.send("/start"))[0]
        unlinked = (await self.send("/start", user_id=202))[0]
        self.assertEqual(linked.reply_markup, MAIN_KEYBOARD)
        self.assertEqual(unlinked.reply_markup, UNLINKED_KEYBOARD)
        self.assertIn(CHANGE_BUTTON, [b.text for row in linked.reply_markup.keyboard for b in row])
        self.assertNotIn(LINK_BUTTON, [b.text for row in linked.reply_markup.keyboard for b in row])
        confirmation = (await self.send(CHANGE_BUTTON))[0]
        self.assertEqual(confirmation.text,
                         "Сейчас подключён:\n\nOld Player\nDota ID: 42\n\nХотите привязать другой Dota-профиль?")
        self.assertEqual([b.text for row in confirmation.reply_markup.inline_keyboard for b in row],
                         ["Сменить аккаунт", "Отмена"])
        self.assertEqual(await self.context().get_state(), self.module.ChangeDota.confirming.state)
        # An ID before confirmation cannot switch the account.
        self.assertEqual((await self.send("43"))[0].text, self.module.CHANGE_CANCELLED_MESSAGE)
        self.assertEqual(self.snapshot(), before)
        self.profile.assert_not_awaited()

    async def test_inline_cancel_at_both_steps_preserves_entire_database(self):
        before = self.snapshot()
        for waiting in (False, True):
            prompt = await self.begin_change() if waiting else (await self.send(CHANGE_BUTTON))[0]
            cancel = prompt.reply_markup.inline_keyboard[-1][0].callback_data
            replies = await self.click(cancel)
            self.assertEqual(replies[-1].text, self.module.CHANGE_CANCELLED_MESSAGE)
            self.assertEqual(replies[-1].reply_markup, MAIN_KEYBOARD)
            self.assertIsNone(await self.context().get_state())
            self.assertEqual(await self.send("43"), [])
            self.assertEqual(self.snapshot(), before)
        self.profile.assert_not_awaited()
        self.history.assert_not_awaited()

    async def test_navigation_and_cancel_commands_exit_both_states(self):
        before = self.snapshot()
        for waiting in (False, True):
            for navigation in ("/start", "/top", "/profile", "/rating", "/stats", "ℹ️ Как считается рейтинг",
                               "Отмена", "/cancel", "/unknown"):
                with self.subTest(waiting=waiting, navigation=navigation):
                    if waiting:
                        await self.begin_change()
                    else:
                        await self.send(CHANGE_BUTTON)
                    replies = await self.send(navigation)
                    self.assertEqual(replies[0].text, self.module.CHANGE_CANCELLED_MESSAGE)
                    self.assertIsNone(await self.context().get_state())
                    self.assertEqual(self.snapshot(), before)

    async def test_old_or_foreign_callbacks_cannot_control_current_dialog(self):
        first = (await self.send(CHANGE_BUTTON))[0].reply_markup.inline_keyboard
        second = (await self.send(CHANGE_BUTTON))[0].reply_markup.inline_keyboard
        before = self.snapshot()
        for data in (first[0][0].callback_data, first[1][0].callback_data):
            replies = await self.click(data)
            self.assertEqual(len(replies), 1)
            self.assertIsInstance(replies[0], AnswerCallbackQuery)
            self.assertIn("диалог уже закрыт", replies[0].text)
        await self.send(CHANGE_BUTTON, chat_id=-100)
        await self.click(second[0][0].callback_data, chat_id=-100)
        self.assertEqual(await self.context(chat_id=-100).get_state(), self.module.ChangeDota.confirming.state)
        await self.click(second[0][0].callback_data, user_id=202)
        self.assertIsNone(await self.context(202).get_state())
        await self.click(second[0][0].callback_data)
        self.assertEqual(await self.context().get_state(), self.module.ChangeDota.waiting_for_account.state)
        self.assertEqual(self.snapshot(), before)
        self.profile.assert_not_awaited()

    async def test_invalid_id_and_profile_failures_preserve_old_link_and_allow_retry(self):
        await self.begin_change()
        before = self.snapshot()
        for invalid in ("bad", "0", "76561198125947846", None):
            self.assertEqual((await self.send(invalid))[0].text, self.module.INVALID_ACCOUNT_MESSAGE)
            self.assertEqual(self.snapshot(), before)
        self.profile.assert_not_awaited()
        failures = [httpx.ReadTimeout("private-url")]
        for status in (404, 429, 503):
            request = httpx.Request("GET", "https://example.test/profile")
            failures.append(httpx.HTTPStatusError("private-url", request=request,
                                                 response=httpx.Response(status, request=request)))
        for failure in failures:
            self.profile.side_effect = failure
            response = (await self.send("43"))[0]
            self.assertNotIn("private-url", response.text)
            self.assertNotIn("Готово", response.text)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(await self.context().get_state(), self.module.ChangeDota.waiting_for_account.state)
        self.profile.side_effect = None
        for payload in ({}, {"profile": None}, {"profile": {"account_id": 999}}):
            self.profile.return_value = payload
            self.assertIn("Проверьте Friend ID", (await self.send("43"))[0].text)
            self.assertEqual(self.snapshot(), before)
        self.profile.return_value = {"profile": {"account_id": 43, "personaname": "New Player"}}
        self.assertIn("Теперь подключён:", (await self.send("43"))[0].text)
        self.assertEqual(db.get_telegram_player(201)["account_id"], 43)

    async def test_current_id_is_read_only_in_dialog_and_add_even_without_rating(self):
        before = self.snapshot()
        with patch.object(db, "link_telegram_user", side_effect=AssertionError("Unnecessary write")):
            await self.begin_change()
            for value in ("42", "/add https://www.dotabuff.com/players/42"):
                response = (await self.send(value))[0]
                self.assertIn("Этот Dota-профиль уже подключён.", response.text)
                self.assertIn("Old Player", response.text)
                self.assertIn(f"Turbo Rating: {db.get_rating(42)['current_rating']:.0f}", response.text)
                self.assertEqual(response.reply_markup, MAIN_KEYBOARD)
                self.assertIsNone(await self.context().get_state())
                self.assertEqual(self.snapshot(), before)
        db.add_player(44, "Legacy", 900)
        db.link_telegram_user(202, 44)
        before = self.snapshot()
        self.assertIn("уже подключён", (await self.send("/add 44", user_id=202))[0].text)
        self.assertEqual(self.snapshot(), before)
        self.profile.assert_not_awaited()
        self.history.assert_not_awaited()

    async def test_new_account_initializes_before_linking_for_all_supported_formats(self):
        old = self.player_snapshot(42)
        for account_id, value in ((43, "43"), (44, "https://www.opendota.com/players/44"),
                                  (45, "https://www.dotabuff.com/players/45")):
            previous = db.get_telegram_player(201)["account_id"]
            original_link = db.link_telegram_user

            def verify_then_link(telegram_id, destination):
                self.assertEqual(db.get_telegram_player(telegram_id)["account_id"], previous)
                rating = db.get_rating(destination)
                self.assertAlmostEqual(rating["initial_rating"], calculate_initial_rating(13, 20))
                self.assertEqual(rating["calibration_matches"], 20)
                original_link(telegram_id, destination)

            await self.begin_change()
            started = int(time.time())
            with patch.object(db, "link_telegram_user", side_effect=verify_then_link) as link:
                response = (await self.send(value))[0]
                link.assert_called_once_with(201, account_id)
            self.profile.assert_awaited_with(account_id)
            tracking = db.get_player(account_id)["tracking_started_at"]
            self.assertGreaterEqual(tracking, started)
            self.assertLessEqual(tracking, int(time.time()))
            self.history.assert_awaited_with(account_id, tracking, limit=20)
            self.assertEqual(len(db.get_player_matches(account_id, include_calibration=True)), 20)
            self.assertEqual(db.get_rating_history(account_id), [])
            self.assertIn(f"Dota ID: {account_id}\n\nTurbo Rating: 1070\nМесто: #1", response.text)
            self.assertEqual(response.reply_markup, MAIN_KEYBOARD)
            self.assertIsNone(await self.context().get_state())
        self.assertEqual(self.player_snapshot(42), old)

    async def test_initialization_failure_keeps_link_and_retry_uses_original_tracking(self):
        old = self.player_snapshot(42)
        self.history.side_effect = httpx.ReadTimeout("private-url")
        await self.begin_change()
        self.assertIn("Попробуйте позже", (await self.send("43"))[0].text)
        self.assertEqual(db.get_telegram_player(201)["account_id"], 42)
        self.assertIsNone(db.get_rating(43))
        self.assertEqual(db.get_player_matches(43, include_calibration=True), [])
        self.assertEqual(db.get_tracked_account_ids(), [42])
        tracking = db.get_player(43)["tracking_started_at"]
        self.history.side_effect = None
        self.assertIn("Готово", (await self.send("43"))[0].text)
        self.assertEqual(db.get_player(43)["tracking_started_at"], tracking)
        self.assertEqual(self.player_snapshot(42), old)

    async def test_interrupted_initialization_never_replaces_old_link(self):
        entered = asyncio.Event()

        async def wait_for_history(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.history.side_effect = wait_for_history
        old = self.player_snapshot(42)
        task = asyncio.create_task(link_dota_account(201, "43"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(db.get_telegram_player(201)["account_id"], 42)
        self.assertEqual(self.player_snapshot(42), old)
        self.assertIsNone(db.get_rating(43))

    async def test_sqlite_failure_rolls_back_final_link_update(self):
        db.add_player(43, "Existing", 500)
        db.create_rating(43, 1087, [], 0)
        with db._connect() as connection:
            connection.executescript("""
                CREATE TRIGGER fail_switch AFTER UPDATE ON telegram_users
                BEGIN SELECT RAISE(ABORT, 'simulated failure after update'); END;
            """)
        before = self.snapshot()
        response = (await self.send("/add 43"))[0]
        self.assertIn("Не удалось сохранить аккаунт", response.text)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(db.get_telegram_player(201)["account_id"], 42)

    async def test_existing_account_and_return_preserve_rating_matches_and_calibration(self):
        db.add_player(43, "Player 43", 500)
        db.create_rating(43, 1087, [match(50, 450)], 1)
        db.save_match(43, match(51, 501, False))
        apply_rating_changes(43)
        before = {account_id: self.player_snapshot(account_id) for account_id in (42, 43)}
        # /add without arguments offers the same confirmation for linked users.
        self.assertIn("Сейчас подключён", (await self.send("/add"))[0].text)
        self.assertEqual(await self.context().get_state(), self.module.ChangeDota.confirming.state)
        for account_id in (43, 42):
            response = (await self.send(f"/add {account_id}"))[0]
            rating = before[account_id][1]["current_rating"]
            self.assertIn(f"Turbo Rating: {rating:.0f}", response.text)
            self.assertEqual(db.get_telegram_player(201)["account_id"], account_id)
            for stored_id in (42, 43):
                self.assertEqual(self.player_snapshot(stored_id), before[stored_id])
        self.history.assert_not_awaited()
        self.assertEqual([call.args[0] for call in self.profile.await_args_list], [43, 42])

    async def test_profile_rating_stats_top_and_autosync_follow_new_link(self):
        old = self.player_snapshot(42)
        await self.send("/add 43")
        for command in ("/profile", "/rating", "/stats"):
            response = (await self.send(command))[0].text
            self.assertIn("Player 43", response)
            self.assertNotIn("Old Player", response)
            self.assertIn("1070", response)
        top = (await self.send("/top"))[0].text
        self.assertEqual(top, "🥇 Turbo Rating\n\n🥇 Player 43 — 1070 ← вы")
        self.assertIsNone(db.get_leaderboard_position(42))
        self.assertEqual(db.get_tracked_account_ids(), [43])
        with patch("app.autosync.sync_player", new=AsyncMock(return_value=SyncResult(0, []))) as sync:
            await sync_tracked_players(self.bot)
            sync.assert_awaited_once_with(43, api_key=None)
        self.assertEqual(self.player_snapshot(42), old)

    async def test_old_account_with_other_users_remains_active_without_duplicates(self):
        db.link_telegram_user(202, 42)
        db.link_telegram_user(203, 42)
        await self.send("/add 43")
        self.assertEqual(db.get_tracked_account_ids(), [42, 43])
        self.assertEqual([row["account_id"] for row in db.get_leaderboard()], [42, 43])
        self.assertEqual(db.get_leaderboard_position(43)["position"], 2)
        top = (await self.send("/top"))[0].text
        self.assertEqual(top.count("Old Player"), 1)
        self.assertIn("🥈 Player 43 — 1070 ← вы", top)
        with patch("app.autosync.sync_player", new=AsyncMock(return_value=SyncResult(0, []))) as sync:
            await sync_tracked_players(self.bot)
            self.assertEqual([call.args[0] for call in sync.await_args_list], [42, 43])


if __name__ == "__main__":
    unittest.main(verbosity=2)
