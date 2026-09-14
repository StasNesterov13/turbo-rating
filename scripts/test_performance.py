"""Offline performance math, API fallback, audit migration and display regressions."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, call, patch

import httpx

from app import db, season
from app.notifications import format_rating_updates
from app.screens import format_matches
from app.services.opendota import OpenDotaClient
from app.services.rating import (
    PERFORMANCE_WEIGHTS, apply_rating_changes, calculate_new_rating,
    calculate_performance_bonus, calculate_performance_details,
    calculate_performance_score, calculate_rating_delta, get_match_performance,
)
from app.services.sync import sync_player
from scripts.test_rating import match


def full_match(kind="carry", *, account_id=42, dire=False, match_id=1):
    team = [dict(account_id=account_id if i == 0 else None, player_slot=i,
                 kills=kills, assists=20, deaths=8, hero_damage=500,
                 tower_damage=1000, obs_placed=10, sen_placed=10,
                 camps_stacked=10, hero_healing=1000)
            for i, kills in enumerate((10, 8, 8, 7, 7))]
    team[0].update(deaths=2, hero_damage=1000, tower_damage=900,
                   obs_placed=1, sen_placed=1, camps_stacked=1, hero_healing=100)
    if kind == "support":
        for player in team:
            player.update(kills=8, deaths=10, hero_damage=1000)
        team[0].update(assists=24, deaths=2, hero_damage=350, tower_damage=100,
                       obs_placed=10, sen_placed=10, camps_stacked=10, hero_healing=1000)
    elif kind == "weak":
        for player in team:
            player.update(kills=9, deaths=10, hero_damage=1000, camps_stacked=20)
        team[0].update(kills=4, assists=10, deaths=8, hero_damage=250, tower_damage=0,
                       obs_placed=3, sen_placed=0, camps_stacked=3, hero_healing=150)
    opponents = [dict(player, account_id=None, player_slot=i + 128,
                      kills=1000, deaths=1000, hero_damage=1000000, tower_damage=1000000,
                      obs_placed=1000, sen_placed=1000, camps_stacked=1000, hero_healing=1000000)
                 for i, player in enumerate(team)]
    if dire:
        for player in team + opponents:
            player["player_slot"] ^= 128
    return dict(match_id=match_id, game_mode=23, players=opponents + team)


class PerformanceMathTests(unittest.TestCase):
    def test_carry_support_and_weak_examples_on_both_sides(self):
        for kind, expected_score, bonus in (("carry", .6925, 5), ("support", .645, 5), ("weak", .215, 0)):
            for dire in (False, True):
                with self.subTest(kind=kind, dire=dire):
                    performance = get_match_performance(full_match(kind, dire=dire), 42)
                    self.assertAlmostEqual(performance["performance_score"], expected_score)
                    self.assertEqual(performance["performance_bonus"], bonus)
                    self.assertEqual(calculate_new_rating(100, True, bonus), (125 + bonus, 25 + bonus, .5))
                    self.assertEqual(calculate_new_rating(130, False, bonus), (105 + bonus, -25 + bonus, .5))

    def test_threshold_and_bonus_examples(self):
        for score, bonus in ((None, 0), (0, 0), (.22, 0), (.35, 0), (.50, 2), (.65, 5), (.82, 7), (1, 10)):
            self.assertEqual(calculate_performance_bonus(score), bonus)
        for score in (-100, 100, float("nan"), float("inf"), "bad", True):
            self.assertIn(calculate_performance_bonus(score), range(11))
        for bonus in range(-20, 30):
            self.assertTrue(25 <= calculate_rating_delta(True, bonus) <= 35)
            self.assertTrue(-25 <= calculate_rating_delta(False, bonus) <= -15)

    def test_missing_support_fields_renormalize_internal_weights(self):
        data = full_match()
        for player in data["players"]:
            player.pop("obs_placed")
            player.pop("sen_placed")
        data["players"][5]["camps_stacked"] = 5
        self.assertAlmostEqual(calculate_performance_details(data, 42)["support"], (.5 + .1) / 2)
        for player in data["players"]:
            player.pop("camps_stacked")
        self.assertAlmostEqual(calculate_performance_details(data, 42)["support"], .1)

    def test_missing_support_renormalizes_main_weights(self):
        data = full_match()
        for player in data["players"]:
            for field in ("obs_placed", "sen_placed", "camps_stacked", "hero_healing"):
                player[field] = None
        result = get_match_performance(data, 42)
        self.assertIsNone(result["performance_details"]["support"])
        self.assertAlmostEqual(result["performance_score"], .6725 / .8)
        self.assertEqual(result["performance_bonus"], 8)

    def test_partial_team_metrics_are_unavailable_not_zero(self):
        data = full_match()
        data["players"][-1].pop("hero_damage")
        data["players"][-1]["obs_placed"] = None
        details = calculate_performance_details(data, 42)
        self.assertIsNone(details["hero_damage"])
        self.assertAlmostEqual(details["support"], .1)
        self.assertAlmostEqual(calculate_performance_score(details), (.6925 - .2) / .8)

    def test_real_support_zeros_and_zero_denominators(self):
        data = full_match()
        for player in data["players"]:
            for field in ("kills", "deaths", "hero_damage", "tower_damage",
                          "obs_placed", "sen_placed", "camps_stacked", "hero_healing"):
                player[field] = 0
        result = get_match_performance(data, 42)
        self.assertEqual(result["performance_details"], dict(kill_participation=None,
                         hero_damage=None, tower_damage=None, survivability=1, support=0))
        self.assertIsNone(result["performance_score"])
        self.assertEqual(result["performance_bonus"], 0)

    def test_minimum_three_components_and_clamping(self):
        details = dict(hero_damage=1, tower_damage=1)
        self.assertIsNone(calculate_performance_score(details))
        details["survivability"] = 1
        self.assertEqual(calculate_performance_score(details), 1)
        self.assertEqual(calculate_performance_bonus(calculate_performance_score(details)), 10)
        details = dict.fromkeys(PERFORMANCE_WEIGHTS, 100)
        self.assertEqual(calculate_performance_score(details), 1)
        data = full_match()
        data["players"][5]["assists"] = 10000
        self.assertEqual(calculate_performance_details(data, 42)["kill_participation"], 1)

    def test_invalid_roster_account_mode_and_values_are_safe(self):
        for data in (None, {}, {"players": None}, {"players": [None]}, full_match(account_id=43),
                     dict(full_match(), game_mode=22), dict(full_match(), players=full_match()["players"][:-1])):
            self.assertEqual(get_match_performance(data, 42)["performance_bonus"], 0)
        for value in (None, "unknown", {}, [], True, -1, float("nan"), float("inf"), 10**400):
            data = full_match()
            for field in ("kills", "assists", "deaths", "hero_damage", "tower_damage",
                          "obs_placed", "sen_placed", "camps_stacked", "hero_healing"):
                data["players"][5][field] = value
            result = get_match_performance(data, 42)
            self.assertEqual(result["performance_bonus"], 0)
            json.dumps(result, allow_nan=False)
        data = full_match()
        data["players"][-1]["player_slot"] = 0
        self.assertEqual(get_match_performance(data, 42)["performance_bonus"], 0)

    def test_unrelated_stats_do_not_affect_score_or_mutate_input(self):
        data = full_match()
        before = deepcopy(data)
        expected = get_match_performance(data, 42)
        self.assertEqual(data, before)
        for player in data["players"]:
            player.update(gold_per_min=9999, xp_per_min=9999, last_hits=9999, net_worth=999999, hero_id=1)
        self.assertEqual(get_match_performance(data, 42), expected)

    def test_random_valid_and_incomplete_matches_stay_in_bounds(self):
        rng = random.Random(42)
        for _ in range(300):
            data = full_match(dire=rng.choice((True, False)))
            for player in data["players"]:
                for field in ("kills", "assists", "deaths", "hero_damage", "tower_damage",
                              "obs_placed", "sen_placed", "camps_stacked", "hero_healing"):
                    player[field] = rng.choice((None, 0, rng.randrange(1, 10000)))
            result = get_match_performance(data, 42)
            for score in [result["performance_score"], *result["performance_details"].values()]:
                self.assertTrue(score is None or 0 <= score <= 1)
            bonus = result["performance_bonus"]
            self.assertIn(bonus, range(11))
            self.assertTrue(25 <= calculate_rating_delta(True, bonus) <= 35)
            self.assertTrue(-25 <= calculate_rating_delta(False, bonus) <= -15)


class PerformanceSyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch("app.season.now", return_value=datetime(2026, 9, 13, tzinfo=timezone.utc)))
        temporary = tempfile.TemporaryDirectory(prefix="turbo-performance-test-")
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(db, "DB_PATH", Path(temporary.name) / "test.db"))
        db.init_db()
        db.add_player(42, "Player", 1000)
        db.create_rating(42, 100, [], 0)
        self.enterContext(patch.object(OpenDotaClient, "get_player", new=AsyncMock(return_value={
            "profile": {"account_id": 42, "personaname": "Player"},
        })))
        self.calibration = self.enterContext(patch.object(OpenDotaClient, "get_turbo_matches_before", new=AsyncMock()))
        self.recent = self.enterContext(patch.object(OpenDotaClient, "get_matches_for_sync", new=AsyncMock(return_value=[])))
        self.fetch = self.enterContext(patch.object(OpenDotaClient, "get_match", new=AsyncMock(side_effect=lambda mid: full_match(match_id=mid))))
        self.enterContext(patch.object(httpx.AsyncClient, "send", side_effect=AssertionError("Unexpected HTTP")))

    async def test_only_unrated_turbo_fetches_details_and_audits_chronologically(self):
        db.save_match(42, match(1, 1001))
        apply_rating_changes(42)
        old = db.get_rating_history(42)
        db.save_match(42, match(2, 1002), is_calibration=True)
        unknown = dict(match(7, 1007), radiant_win=None)
        self.recent.return_value = [match(4, 1004, False), match(3, 1003), match(3, 1003),
                                    match(1, 1001), match(2, 1002), match(5, 999), match(6, 1006, game_mode=22), unknown]
        first = await sync_player(42)
        self.assertEqual(self.fetch.await_args_list, [call(3), call(4)])
        self.assertEqual([u.rating_delta for u in first.rating_updates], [30, -20])
        self.assertEqual([u.performance_bonus for u in first.rating_updates], [5, 5])
        self.assertEqual(db.get_rating(42)["current_rating"], 135)
        for event in first.rating_changes:
            self.assertAlmostEqual(event["performance_score"], .6925)
            self.assertEqual(event["performance_bonus"], 5)
            self.assertAlmostEqual(json.loads(event["performance_details"])["support"], .1)
        self.assertEqual(db.get_rating_history(42)[2:], old)
        snapshot = db.get_rating(42), db.get_rating_history(42)
        self.assertEqual((await sync_player(42)).rating_updates, [])
        self.assertEqual(self.fetch.await_count, 2)
        self.assertEqual((db.get_rating(42), db.get_rating_history(42)), snapshot)
        self.calibration.assert_not_awaited()
        history = format_matches(db.get_turbo_match_history(42), {})
        self.assertIn("+30 TR · performance +5", history)
        self.assertIn("-20 TR · performance +5", history)

    async def test_unavailable_full_match_uses_base_once_and_never_backfills(self):
        errors = [httpx.ReadTimeout("timeout"), ValueError("bad JSON"),
                  httpx.HTTPStatusError("rate limit", request=httpx.Request("GET", "https://example.test"),
                                        response=httpx.Response(429))]
        self.recent.return_value = [match(i, 1000 + i, i != 2) for i in range(1, 4)]
        self.fetch.side_effect = errors
        result = await sync_player(42)
        self.assertEqual([u.rating_delta for u in result.rating_updates], [25, -25, 25])
        self.assertTrue(all(c["performance_score"] is None and c["performance_bonus"] == 0 for c in result.rating_changes))
        self.fetch.side_effect = lambda mid: full_match(match_id=mid)
        self.assertEqual((await sync_player(42)).rating_updates, [])
        self.assertEqual(self.fetch.await_count, 3)

    async def test_insufficient_components_fall_back_without_breaking_sync(self):
        data = full_match()
        for player in data["players"]:
            for field in ("hero_damage", "tower_damage", "obs_placed", "sen_placed", "camps_stacked", "hero_healing"):
                player.pop(field)
        self.fetch.side_effect = None
        self.fetch.return_value = data
        self.recent.return_value = [match(1, 1001)]
        result = await sync_player(42)
        self.assertEqual(result.rating_updates[0].rating_delta, 25)
        self.assertIsNone(result.rating_changes[0]["performance_score"])
        self.assertIsNotNone(json.loads(result.rating_changes[0]["performance_details"])["survivability"])

    async def test_cancellation_and_failed_transaction_recover_with_performance(self):
        self.recent.return_value = [match(1, 1001), match(2, 1002, False)]
        self.fetch.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await sync_player(42)
        self.assertEqual(db.get_rating_history(42), [])
        self.fetch.side_effect = lambda mid: full_match(match_id=mid)
        def fail_second(rating, win, bonus):
            if not win:
                raise RuntimeError("interrupted")
            return calculate_new_rating(rating, win, bonus)
        with patch("app.services.rating.calculate_new_rating", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                await sync_player(42)
        self.assertEqual(db.get_rating_history(42), [])
        self.assertEqual(db.get_rating(42)["current_rating"], 100)
        result = await sync_player(42)
        self.assertEqual(result.new_count, 0)
        self.assertEqual([u.rating_delta for u in result.rating_updates], [30, -20])

    async def test_season_closed_does_not_fetch_performance(self):
        self.recent.return_value = [match(1, 1001)]
        with patch.object(season, "now", return_value=season.SEASON_END_AT):
            self.assertEqual((await sync_player(42)).rating_updates, [])
        self.fetch.assert_not_awaited()

    async def test_notification_breakdown_and_no_zero_bonus_line(self):
        self.recent.return_value = [match(1, 1001), match(2, 1002, False)]
        result = await sync_player(42)
        for update, title, delta, ratings, base in (
            (result.rating_updates[0], "🟢 Победа в Turbo", "+30", "100 → 130", "+25"),
            (result.rating_updates[1], "🔴 Поражение в Turbo", "-20", "130 → 110", "-25"),
        ):
            self.assertEqual(format_rating_updates([update]), f"{title}\n\n{delta} TR\n{ratings}\n\nБаза: {base}\nИгра: +5")
        self.assertEqual(format_rating_updates(result.rating_updates).count("performance +5"), 2)
        self.recent.return_value = [match(3, 1003)]
        self.fetch.side_effect = lambda mid: full_match("weak", match_id=mid)
        result = await sync_player(42)
        text = format_rating_updates(result.rating_updates)
        self.assertIn("База: +25", text)
        self.assertNotIn("Игра:", text)

    async def test_manual_sync_shows_bonus_and_breakdown(self):
        from app import bot as bot_module
        db.link_telegram_user(101, 42)
        message = SimpleNamespace(from_user=SimpleNamespace(id=101), answer=AsyncMock())
        self.recent.return_value = [match(1, 1001)]
        with patch.object(bot_module, "_sync_cooldowns", {}), patch.object(bot_module, "_sync_in_progress", set()):
            await bot_module.sync_command(message)
        message.answer.assert_awaited_once()
        text = message.answer.await_args.args[0]
        for expected in ("+30 TR · performance +5", "100 → 130", "База: +25", "Игра: +5"):
            self.assertIn(expected, text)

    def test_existing_rating_history_migration_is_idempotent_and_preserves_values(self):
        db.save_match(42, match(1, 1001))
        with db._connect() as connection:
            connection.executescript("""
                DROP TABLE rating_history;
                CREATE TABLE rating_history (
                    account_id INTEGER NOT NULL REFERENCES ratings(account_id), match_id INTEGER NOT NULL,
                    rating_before REAL NOT NULL, expected_score REAL NOT NULL, result INTEGER NOT NULL,
                    rating_delta REAL NOT NULL, rating_after REAL NOT NULL, created_at INTEGER NOT NULL,
                    PRIMARY KEY (account_id, match_id),
                    FOREIGN KEY (account_id, match_id) REFERENCES matches(account_id, match_id));
                INSERT INTO rating_history VALUES (42, 1, 100, 0.51, 1, 16.25, 116.25, 1002);
                UPDATE ratings SET current_rating = 116.25 WHERE account_id = 42;
            """)
            legacy = dict(connection.execute("SELECT * FROM rating_history").fetchone())
        rating = db.get_rating(42)
        db.init_db()
        db.init_db()
        migrated = db.get_rating_history(42)[0]
        self.assertEqual({key: migrated[key] for key in legacy}, legacy)
        self.assertEqual([migrated[key] for key in ("performance_score", "performance_bonus", "performance_details")], [None, 0, None])
        self.assertEqual(db.get_rating(42), rating)
        self.assertEqual(apply_rating_changes(42), [])
        self.assertIn("+16 TR → 116 TR", format_matches(db.get_turbo_match_history(42), {}))
        with db._connect() as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])


class FullMatchClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_match_endpoint_validation_and_http_errors(self):
        async with OpenDotaClient() as client:
            response = httpx.Response(200, json=full_match(), request=httpx.Request("GET", client.BASE_URL + "matches/1"))
            with patch.object(client._client, "get", new=AsyncMock(return_value=response)) as fetch:
                self.assertEqual(await client.get_match(1), full_match())
                fetch.assert_awaited_once_with("matches/1")
                for invalid in (0, -1, True, "1", None):
                    with self.assertRaises(ValueError):
                        await client.get_match(invalid)
                self.assertEqual(fetch.await_count, 1)
                for payload in ([], None, {}, {"match_id": 2}):
                    fetch.return_value = httpx.Response(200, json=payload, request=response.request)
                    with self.assertRaises(ValueError):
                        await client.get_match(1)
                fetch.return_value = httpx.Response(503, request=response.request)
                with self.assertRaises(httpx.HTTPStatusError):
                    await client.get_match(1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
