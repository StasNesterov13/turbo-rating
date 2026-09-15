"""Offline tests for leaderboard, account locks, autosync and notifications."""

import asyncio
from datetime import datetime, timezone
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
from app.autosync import run_autosync, sync_tracked_players
from app.notifications import format_rating_updates, notify_rating_updates
from app.services.opendota import OpenDotaClient
from app.services import sync as sync_service
from app.services.sync import RatingUpdate, SyncResult, sync_player
from scripts.test_rating import match, timestamp


class AutosyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch("app.season.now", return_value=datetime(2026, 9, 13, 12, tzinfo=timezone.utc)))
        temporary = tempfile.TemporaryDirectory(prefix="turbo-autosync-test-")
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(db, "DB_PATH", Path(temporary.name) / "test.db"))
        db.init_db()
        self.history = self.enterContext(patch.object(
            OpenDotaClient, "get_turbo_matches_before", new=AsyncMock(return_value=[])
        ))
        self.fetch = self.enterContext(patch.object(
            OpenDotaClient, "get_matches_for_sync", new=AsyncMock(return_value=[])
        ))
        self.enterContext(patch.object(OpenDotaClient, "get_match", new=AsyncMock(return_value={})))
        self.profile = self.enterContext(patch.object(OpenDotaClient, "get_player", new=AsyncMock(
            side_effect=lambda account_id: {"profile": {
                "account_id": account_id, "personaname": db.get_player(account_id)["nickname"],
            }},
        )))
        self.enterContext(patch.object(sync_service, "_last_sync_times", {}))
        self.bot = AsyncMock(spec=Bot)

    def player(self, account_id, rating=1000):
        db.add_player(account_id, f"Player {account_id}", timestamp(1000))
        db.create_rating(account_id, float(rating), [], 0)

    async def test_successful_empty_sync_refreshes_name_and_time_only(self):
        self.player(42)
        player, rating = db.get_player(42), db.get_rating(42)
        self.profile.side_effect = None
        self.profile.return_value = {"profile": {"account_id": 42, "personaname": "Renamed"}}
        with patch.object(sync_service.time, "time", return_value=1704067200):
            result = await sync_player(42)
        self.assertEqual(result.rating_updates, [])
        self.assertEqual(db.get_player(42), {**player, "nickname": "Renamed"})
        self.assertEqual(db.get_rating(42), rating)
        self.assertEqual(db.get_rating_history(42), [])
        self.assertEqual(sync_service.get_last_sync_time(42), 1704067200)
        self.profile.assert_awaited_once_with(42)
        await sync_player(42)
        self.assertEqual(db.get_player(42), {**player, "nickname": "Renamed"})

    async def test_failed_sync_preserves_name_rating_and_last_success_time(self):
        self.player(42)
        player, rating = db.get_player(42), db.get_rating(42)
        sync_service._last_sync_times[42] = 1234
        self.profile.side_effect = None
        self.profile.return_value = {"profile": {"account_id": 42, "personaname": "Not saved"}}
        self.fetch.side_effect = httpx.ReadTimeout("private-key")
        with self.assertRaises(httpx.ReadTimeout), self.assertLogs("app.services.sync", level="ERROR"):
            await sync_player(42)
        self.assertEqual(db.get_player(42), player)
        self.assertEqual(db.get_rating(42), rating)
        self.assertEqual(sync_service.get_last_sync_time(42), 1234)

    async def test_explicit_private_history_is_distinct_from_empty_history(self):
        self.player(42)
        player, rating = db.get_player(42), db.get_rating(42)
        self.profile.side_effect = None
        self.profile.return_value = {"profile": {"account_id": 42, "personaname": "Private", "fh_unavailable": True}}
        with self.assertRaises(sync_service.MatchHistoryUnavailable), self.assertLogs("app.services.sync", level="ERROR"):
            await sync_player(42)
        self.fetch.assert_not_awaited()
        self.assertEqual(db.get_player(42), player)
        self.assertEqual(db.get_rating(42), rating)
        self.assertIsNone(sync_service.get_last_sync_time(42))
        self.profile.return_value["profile"]["fh_unavailable"] = False
        result = await sync_player(42)
        self.assertEqual(result.new_count, 0)
        self.assertIsNotNone(sync_service.get_last_sync_time(42))
        self.profile.return_value = {"profile": {"account_id": 43, "fh_unavailable": True}}
        with self.assertRaises(sync_service.MatchHistoryUnavailable):
            await sync_service.ensure_player(43)
        self.assertIsNone(db.get_player(43))

    async def test_autosync_ignores_manual_user_cooldown(self):
        from app import bot as bot_module
        self.player(42)
        db.link_telegram_user(101, 42)
        self.enterContext(patch.object(bot_module, "_sync_cooldowns", {}))
        message = SimpleNamespace(from_user=SimpleNamespace(id=101), answer=AsyncMock())
        await bot_module.sync_command(message)
        await bot_module.sync_command(message)
        self.assertIn("Данные недавно обновлялись", message.answer.await_args.args[0])
        self.fetch.assert_awaited_once()
        await sync_tracked_players(self.bot)
        self.assertEqual(self.fetch.await_count, 2)

    async def test_leaderboard_unique_stable_sorted_and_limited(self):
        for account_id, rating in ((42, 1000), (43, 1200), (44, 1000)):
            self.player(account_id, rating)
            db.link_telegram_user(1000 + account_id, account_id)
        db.add_player(45, "No rating", timestamp(1000))
        db.link_telegram_user(101, 42)
        db.link_telegram_user(102, 42)
        self.assertEqual([p["account_id"] for p in db.get_leaderboard()], [43, 42, 44])
        self.assertEqual(db.get_tracked_account_ids(), [42, 43, 44])
        self.assertEqual(db.get_telegram_ids_by_account(42), [101, 102, 1042])
        for account_id in range(100, 125):
            self.player(account_id, 900)
            db.link_telegram_user(1000 + account_id, account_id)
        self.assertEqual(len(db.get_leaderboard(limit=100)), 20)
        self.assertEqual(len(db.get_leaderboard(limit=2)), 2)

    async def test_same_account_sync_returns_busy_without_a_second_request(self):
        self.player(42)
        entered, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        active = 0
        max_active = 0
        calls = 0

        async def fetch(*args):
            nonlocal active, max_active, calls
            calls += 1
            active += 1
            max_active = max(max_active, active)
            try:
                if calls == 1:
                    entered.set()
                    await release.wait()
                return [match(1, 1001)]
            finally:
                active -= 1

        async def second():
            second_started.set()
            return await sync_player(42)

        self.fetch.side_effect = fetch
        first_task = asyncio.create_task(sync_player(42))
        second_task = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            second_task = asyncio.create_task(second())
            await asyncio.wait_for(second_started.wait(), 3)
            self.assertEqual(calls, 1)
            release.set()
            first, repeated = await asyncio.wait_for(asyncio.gather(first_task, second_task), 5)
            self.assertEqual(max_active, 1)
            self.assertEqual(len(first.rating_updates), 1)
            self.assertEqual(repeated.rating_updates, [])
            self.assertTrue(repeated.already_in_progress)
            self.assertEqual(calls, 1)
            self.assertEqual(db.get_rating(42)["current_rating"], 1025)
        finally:
            release.set()
            await asyncio.gather(*(task for task in (first_task, second_task) if task), return_exceptions=True)

    async def test_manual_sync_during_autosync_is_busy_for_all_linked_users(self):
        from app import bot as bot_module
        self.player(42)
        db.link_telegram_user(101, 42)
        db.link_telegram_user(102, 42)
        self.enterContext(patch.object(bot_module, "_sync_cooldowns", {}))
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked(*args):
            entered.set()
            await release.wait()
            return [match(1, 1001)]

        self.fetch.side_effect = blocked
        task = asyncio.create_task(sync_tracked_players(self.bot))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            for telegram_id in (101, 102):
                message = SimpleNamespace(from_user=SimpleNamespace(id=telegram_id), answer=AsyncMock())
                await asyncio.wait_for(bot_module.sync_command(message), 1)
                self.assertIn("Обновление уже выполняется", message.answer.await_args.args[0])
                self.assertNotIn(telegram_id, bot_module._sync_cooldowns)
            self.fetch.assert_awaited_once()
            release.set()
            await asyncio.wait_for(task, 5)
            self.assertEqual(db.get_rating(42)["current_rating"], 1025)
            self.assertEqual(db.count_rated_matches(42), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_different_accounts_can_fetch_concurrently(self):
        self.player(42)
        self.player(43)
        both_entered = asyncio.Event()
        entered = set()

        async def fetch(account_id, tracking_started_at):
            entered.add(account_id)
            if len(entered) == 2:
                both_entered.set()
            await both_entered.wait()
            return [match(1, 1001)]

        self.fetch.side_effect = fetch
        results = await asyncio.wait_for(asyncio.gather(sync_player(42), sync_player(43)), 5)
        self.assertEqual(entered, {42, 43})
        self.assertEqual([len(r.rating_updates) for r in results], [1, 1])

    async def test_cancellation_releases_account_lock(self):
        self.player(42)
        entered = asyncio.Event()

        async def block(*args):
            entered.set()
            await asyncio.Event().wait()

        self.fetch.side_effect = block
        task = asyncio.create_task(sync_player(42))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.fetch.side_effect = None
        self.fetch.return_value = [match(1, 1001)]
        self.assertEqual(len((await asyncio.wait_for(sync_player(42), 3)).rating_updates), 1)

    async def test_updates_are_ordered_typed_and_turbo_only(self):
        self.player(42)
        self.fetch.return_value = [match(3, 1003, False), match(2, 1002, game_mode=22), match(1, 1001)]
        result = await sync_player(42)
        updates = result.rating_updates
        self.assertEqual(result.new_count, 3)
        self.assertTrue(all(isinstance(u, RatingUpdate) for u in updates))
        self.assertEqual([u.match_id for u in updates], [1, 3])
        self.assertEqual([u.start_time for u in updates], [timestamp(1001), timestamp(1003)])
        self.assertEqual([u.win for u in updates], [True, False])
        self.assertEqual(updates[0].hero_id, 44)
        self.assertEqual(updates[0].rating_before, 1000)
        self.assertEqual(updates[0].rating_after, updates[1].rating_before)
        self.assertEqual(updates[-1].rating_after, db.get_rating(42)["current_rating"])
        self.assertEqual((await sync_player(42)).rating_updates, [])

    async def test_autosync_aggregates_once_for_each_subscriber(self):
        self.player(42)
        self.player(43)
        db.link_telegram_user(101, 42)
        db.link_telegram_user(102, 42)
        self.fetch.return_value = [match(2, 1002, False), match(1, 1001)]
        await sync_tracked_players(self.bot)
        self.fetch.assert_awaited_once_with(42, timestamp(1000))
        self.assertEqual(self.bot.send_message.await_count, 2)
        first, second = self.bot.send_message.await_args_list
        self.assertEqual([first.args[0], second.args[0]], [101, 102])
        self.assertEqual(first.args[1], second.args[1])
        self.assertIn("🎮 Новые Turbo-матчи: 2", first.args[1])
        self.assertIn("Rating: 1000 → 1000", first.args[1])
        self.bot.send_message.reset_mock()
        await sync_tracked_players(self.bot)
        self.bot.send_message.assert_not_awaited()

    async def test_player_error_does_not_stop_next_player(self):
        for account_id in (42, 43):
            self.player(account_id)
            db.link_telegram_user(account_id, account_id)

        async def fetch(account_id, timestamp):
            if account_id == 42:
                raise httpx.ReadTimeout("private-url-api-key")
            return [match(1, 1001)]

        self.fetch.side_effect = fetch
        with self.assertLogs(level="ERROR") as logged:
            await sync_tracked_players(self.bot)
        self.assertNotIn("private-url-api-key", " ".join(logged.output))
        self.assertEqual([call.args[0] for call in self.fetch.await_args_list], [42, 43])
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_args.args[0], 43)

    async def test_notification_failure_does_not_stop_other_recipients(self):
        self.player(42)
        db.link_telegram_user(101, 42)
        db.link_telegram_user(102, 42)
        self.bot.send_message.side_effect = [RuntimeError("blocked"), True]
        update = RatingUpdate(1, 1001, 44, True, 1000, 16, 1016)
        with self.assertLogs("app.notifications", level="ERROR"):
            await notify_rating_updates(self.bot, 42, [update])
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_autosync_runs_immediately_and_sleeps_300(self):
        with patch("app.autosync.sync_tracked_players", new=AsyncMock()) as cycle, patch(
            "app.autosync.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)
        ) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await run_autosync(self.bot)
            cycle.assert_awaited_once_with(self.bot, api_key=None)
            sleep.assert_awaited_once_with(300)

    async def test_notification_formats_and_bounded_message_length(self):
        win = RatingUpdate(1, 1001, 44, True, 1000, 16, 1016)
        loss = RatingUpdate(2, 1002, 14, False, 1016, -12.08, 1003.92)
        self.assertIn("🟢 Победа в Turbo", format_rating_updates([win]))
        self.assertIn("🔴 Поражение в Turbo", format_rating_updates([loss]))
        self.assertIn("🟢 Turbo WIN: Hero #44  +16 TR", format_rating_updates([win, loss], manual=True))
        self.assertIn("Rating: 1000 → 1004", format_rating_updates([win, loss]))
        self.assertIn("-12 TR\n1016 → 1004", format_rating_updates([loss]))
        self.assertEqual(loss.rating_delta, -12.08)
        self.assertEqual(loss.rating_after, 1003.92)
        self.assertEqual(format_rating_updates([]), "")
        self.assertLess(len(format_rating_updates([win] * 1000).encode("utf-16-le")) // 2, 4096)

    async def test_top_add_and_manual_sync_responses(self):
        from aiogram.filters import CommandObject
        self.enterContext(patch("app.bot.MANUAL_SYNC_COOLDOWN", 0))
        self.enterContext(patch("app.bot._sync_cooldowns", {}))
        from app.bot import add_command, top_command, sync_command

        message = SimpleNamespace(from_user=SimpleNamespace(id=101), answer=AsyncMock())
        db.add_player(42, "Test Player", timestamp(1000))
        with patch.object(OpenDotaClient, "get_player", new=AsyncMock(return_value={
            "profile": {"account_id": 42, "personaname": "Test Player"}
        })):
            await add_command(message, CommandObject(command="add", args="42"))
            self.assertIn("Turbo Rating: 1000", message.answer.await_args.args[0])
            original_player, original_rating = db.get_player(42), db.get_rating(42)
            self.history.return_value = [match(i, 900 - i) for i in range(20)]
            await add_command(message, CommandObject(command="add", args="42"))
            self.assertEqual(db.get_player(42), original_player)
            self.assertEqual(db.get_rating(42), original_rating)
            self.history.assert_awaited_once()
        db.link_telegram_user(102, 42)
        self.player(43, 1200)
        db.link_telegram_user(103, 43)
        message.answer.reset_mock()
        await top_command(message)
        self.assertEqual(message.answer.await_args.args[0].split("\n\n💰 Призы:")[0],
                         "Turbo Rating\nСезон: Сентябрь 2026\n\n🥇 Player 43 — 1200  —\n🥈 Test Player — 1000  — ← вы")
        self.fetch.return_value = [match(2, 1002, False), match(1, 1001)]
        message.answer.reset_mock()
        with patch("app.notifications.notify_rating_updates", new=AsyncMock()) as notify:
            await sync_command(message)
            message.answer.assert_awaited_once()
            text = message.answer.await_args.args[0]
            self.assertIn("Новых Turbo: 2", text)
            self.assertIn("WIN  +25", text)
            self.assertIn("LOSE  -25", text)
            self.assertIn("Rating:\n1000 → 1000", text)
            notify.assert_not_awaited()
        self.fetch.return_value = [match(3, 1003, game_mode=22)]
        await sync_command(message)
        self.assertIn("Turbo Rating: 1000", message.answer.await_args.args[0])
        self.assertIn("Performance пока недоступен для 2 матчей", message.answer.await_args.args[0])
        self.assertEqual(db.count_player_matches(42), 3)


class RestartPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_fetches_more_than_20_and_rates_chronologically(self):
        self.enterContext(patch.object(OpenDotaClient, "get_match", new=AsyncMock(return_value={})))
        self.enterContext(patch("app.season.now", return_value=datetime(2026, 9, 13, 12, tzinfo=timezone.utc)))
        with tempfile.TemporaryDirectory(prefix="turbo-restart-test-") as directory:
            with patch.object(db, "DB_PATH", Path(directory) / "test.db"):
                db.init_db()
                db.add_player(42, "Restart", timestamp(1000))
                db.create_rating(42, 1000, [], 0)
                data = [match(i, 1120 - i) for i in range(125)]

                async def page(account_id, limit, offset=0):
                    return data[offset:offset + limit]

                with patch.object(OpenDotaClient, "get_player", new=AsyncMock(return_value={
                    "profile": {"account_id": 42, "personaname": "Restart"},
                })), patch.object(OpenDotaClient, "get_recent_matches", new=AsyncMock(side_effect=page)) as fetch:
                    result = await sync_player(42)
                    self.assertEqual([c.kwargs["offset"] for c in fetch.await_args_list], [0, 50, 100])
                    self.assertEqual(len(result.rating_updates), 121)
                    self.assertEqual([u.start_time for u in result.rating_updates], [timestamp(t) for t in range(1000, 1121)])
                    self.assertEqual(result.rating_updates[-1].rating_after, db.get_rating(42)["current_rating"])
                    self.assertEqual((await sync_player(42)).rating_updates, [])

    async def test_repeated_page_and_page_limit_fail_explicitly(self):
        async with OpenDotaClient() as client:
            with patch.object(client, "get_recent_matches", new=AsyncMock(return_value=[match(1, 1000)])):
                with self.assertRaisesRegex(ValueError, "повторяет страницу"):
                    await client.get_matches_for_sync(42, 1000, page_size=1)
                with self.assertRaisesRegex(ValueError, "лимит страниц"):
                    await client.get_matches_for_sync(42, 1000, page_size=1, max_pages=1)


class BotLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_task_is_cancelled_on_polling_exit(self):
        from app import bot as bot_module
        entered, stopped = asyncio.Event(), asyncio.Event()

        async def background(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async def polling(*args, **kwargs):
            self.assertFalse(kwargs["close_bot_session"])
            await entered.wait()

        with patch.object(bot_module, "run_autosync", side_effect=background), patch.object(
            bot_module.db, "init_db"
        ), patch.object(Dispatcher, "include_router"), patch.object(
            Dispatcher, "start_polling", side_effect=polling
        ), patch.object(bot_module, "load_heroes", new=AsyncMock()) as heroes, patch.object(
            Bot, "set_my_commands", new=AsyncMock()
        ) as menu:
            await asyncio.wait_for(bot_module.run_bot("123456:LOCAL_ONLY_TEST_TOKEN"), 3)
            heroes.assert_awaited_once()
            menu.assert_awaited_once_with(bot_module.BOT_COMMANDS)
        self.assertTrue(stopped.is_set())
        self.assertFalse(any(t.get_name() == "turbo-autosync" for t in asyncio.all_tasks()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
