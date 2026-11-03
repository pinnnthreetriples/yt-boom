"""Per-account work settings: age mode, in work, auto warm-up, restriction release; engine, gate, DB, import, CLI, UI."""
from __future__ import annotations

import asyncio
import contextlib
import io
import re
import sqlite3
import sys
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from loguru import logger

import youtube_bot
from config import load_config
from database import AccountRecord, Database
from modules.account_menu_logic import (
    account_card_rows,
    account_status_label,
    lifecycle_rows,
    next_comment_text,
)
from modules.account_parser import parse_account_line
from modules.lifecycle_engine import (
    AccountLifecycleEngine,
    explain_block,
    release_restriction,
)
from modules.multi_account_orchestrator import MultiAccountOrchestrator
from modules.proxy_library import sync_config_accounts
from modules.wizard import (
    MENU_WIDTH,
    _setup_account_actions,
    _setup_account_rows,
    _setup_bulk_settings,
    _setup_lifecycle,
    _setup_pick_mode,
    load_accounts,
)
from tests.test_account_menu import MenuCase, good_line
from tests.tui_keys import DOWN, ENTER, ESC, SPACE, scripted_keys

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _account(hours_old: float = 0.0, base: datetime = T0, **overrides) -> AccountRecord:
    fields = dict(
        id=1,
        name="acc",
        session_path="s",
        created_at=base.isoformat(),
        auth_status="authenticated",
        authenticated_at=(base - timedelta(hours=hours_old)).isoformat(),
        daily_limit_reset_at=(base + timedelta(hours=6)).isoformat(),
    )
    fields.update(overrides)
    return AccountRecord(**fields)


def _live(hours_old: float = 0.0, **overrides) -> AccountRecord:
    return _account(hours_old, base=datetime.now(timezone.utc), **overrides)


class TestModeStages(unittest.TestCase):
    def test_auto_is_the_default_and_follows_the_age(self):
        self.assertEqual(_account(0).lifecycle_mode, "auto")
        self.assertTrue(_account(0).auto_warmup)
        self.assertEqual(AccountLifecycleEngine.calculate_stage(_account(2), T0), ("quarantine", 0))

    def test_mature_mode_skips_the_young_stages(self):
        for hours in (0, 10, 30, 50, 100):
            with self.subTest(hours=hours):
                acc = _account(hours, lifecycle_mode="mature")
                self.assertEqual(AccountLifecycleEngine.calculate_stage(acc, T0), ("mature", 5))

    def test_unlimited_mode_is_its_own_stage_without_a_limit(self):
        for hours in (0, 30, 100):
            acc = _account(hours, lifecycle_mode="unlimited")
            self.assertEqual(AccountLifecycleEngine.calculate_stage(acc, T0), ("unlimited", 0))

    def test_unknown_stored_mode_behaves_as_auto(self):
        acc = _account(2, lifecycle_mode="weird")
        self.assertEqual(AccountLifecycleEngine.calculate_stage(acc, T0)[0], "quarantine")

    def test_summary_keeps_the_real_age_and_stage_for_display(self):
        view = AccountLifecycleEngine.summarize(_account(5, lifecycle_mode="mature"), T0)
        self.assertEqual((view.stage, view.mode, view.age_stage, view.quota_limit), ("mature", "mature", "quarantine", 5))
        self.assertAlmostEqual(view.age_hours, 5.0)
        view = AccountLifecycleEngine.summarize(_account(5, lifecycle_mode="unlimited"), T0)
        self.assertEqual((view.stage, view.quota_limit, view.age_stage), ("unlimited", None, "quarantine"))

    def test_breaker_shows_as_restricted_in_every_mode(self):
        for mode in ("auto", "mature", "unlimited"):
            view = AccountLifecycleEngine.summarize(_account(100, lifecycle_mode=mode, consecutive_errors=3), T0)
            self.assertEqual(view.stage, "restricted")


class TestModeEligibility(unittest.IsolatedAsyncioTestCase):
    async def check(self, account, now=T0, db=None):
        return await AccountLifecycleEngine.evaluate_eligibility(account, db=db, now=now)

    async def test_mature_mode_comments_at_once_with_mature_limits(self):
        result = await self.check(_account(1, lifecycle_mode="mature"))
        self.assertTrue(result.can_comment)
        self.assertEqual((result.stage, result.daily_limit), ("mature", 5))

    async def test_mature_mode_keeps_five_a_day_and_the_interval(self):
        long_ago = (T0 - timedelta(hours=2)).isoformat()
        full = await self.check(_account(1, lifecycle_mode="mature", daily_comments_done=5, last_comment_at=long_ago))
        self.assertEqual((full.can_comment, full.reason), (False, "Daily limit reached"))
        recent = await self.check(
            _account(1, lifecycle_mode="mature", daily_comments_done=1, last_comment_at=(T0 - timedelta(minutes=30)).isoformat())
        )
        self.assertEqual((recent.can_comment, recent.reason), (False, "Interval cooldown active"))

    async def test_unlimited_has_no_daily_limit_and_no_interval(self):
        acc = _account(
            0, lifecycle_mode="unlimited", daily_comments_done=40, last_comment_at=(T0 - timedelta(seconds=5)).isoformat()
        )
        result = await self.check(acc)
        self.assertTrue(result.can_comment)
        self.assertEqual(result.stage, "unlimited")

    async def test_circuit_breaker_still_stops_mature_and_unlimited(self):
        for mode in ("mature", "unlimited"):
            result = await self.check(_account(100, lifecycle_mode=mode, consecutive_errors=3))
            self.assertEqual((result.can_comment, result.stage, result.suggested_action), (False, "restricted", "restricted"))

    async def test_auto_rules_are_unchanged(self):
        young = await self.check(_account(2))
        self.assertEqual((young.can_comment, young.suggested_action), (False, "warm_up"))
        self.assertTrue((await self.check(_account(100))).can_comment)

    async def test_unlimited_stage_is_saved_for_the_wizard(self):
        db = AsyncMock(spec=Database)
        await self.check(_account(0, lifecycle_mode="unlimited"), db=db)
        saved = db.update_account_lifecycle.await_args.kwargs
        self.assertEqual(saved["lifecycle_stage"], "unlimited")


class TestExplainBlock(unittest.IsolatedAsyncioTestCase):
    async def reason(self, account):
        verdict = await AccountLifecycleEngine.evaluate_eligibility(account, now=T0)
        return explain_block(account, verdict, T0)

    async def test_young_account_text_names_the_way_out(self):
        text = await self.reason(_account(14))
        self.assertIn("молодой аккаунт", text)
        self.assertIn("«Режим»", text)
        self.assertNotIn("Автопрогрев выключен", text)

    async def test_young_account_with_auto_warmup_off_says_so(self):
        self.assertIn("Автопрогрев выключен", await self.reason(_account(14, auto_warmup=False)))

    async def test_restricted_text_points_at_the_release(self):
        self.assertIn("Снять ограничение", await self.reason(_account(100, consecutive_errors=3)))

    async def test_limit_and_cooldown_texts(self):
        full = await self.reason(_account(100, daily_comments_done=5, last_comment_at=(T0 - timedelta(hours=3)).isoformat()))
        self.assertIn("дневной лимит 5/5", full)
        wait = await self.reason(_account(100, daily_comments_done=1, last_comment_at=(T0 - timedelta(minutes=10)).isoformat()))
        self.assertIn("пауза между комментариями", wait)


class TestDatabaseModes(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_old_accounts_become_auto_and_warm_up_on(self):
        path = self.root / "old.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, session_path TEXT NOT NULL,"
            " proxy_url TEXT, is_active INTEGER NOT NULL DEFAULT 1, total_replies INTEGER NOT NULL DEFAULT 0,"
            " created_at TEXT NOT NULL, last_used_at TEXT)"
        )
        conn.execute("INSERT INTO accounts (name, session_path, created_at) VALUES ('legacy', 's', '2026-01-01T00:00:00+00:00')")
        conn.execute("INSERT INTO accounts (name, session_path, is_active, created_at) VALUES ('off', 's', 0, '2026-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()
        async with Database(path) as db:
            legacy, off = await db.get_account("legacy"), await db.get_account("off")
        self.assertEqual((legacy.lifecycle_mode, legacy.auto_warmup, legacy.is_active), ("auto", True, True))
        self.assertEqual((off.lifecycle_mode, off.auto_warmup, off.is_active), ("auto", True, False))  # is_active untouched

    async def test_migration_is_repeatable(self):
        path = self.root / "m.db"
        async with Database(path) as db:
            acc = await db.upsert_account("a", str(self.root / "a"))
            await db.set_account_lifecycle_mode(acc.id, "mature")
        async with Database(path) as db:  # a second start must not reset or fail
            self.assertEqual((await db.get_account("a")).lifecycle_mode, "mature")

    async def test_setters_and_unknown_mode(self):
        async with Database(self.root / "m.db") as db:
            acc = await db.upsert_account("a", str(self.root / "a"))
            await db.set_account_lifecycle_mode(acc.id, "unlimited")
            await db.set_account_auto_warmup(acc.id, False)
            await db.set_account_active(acc.id, False)
            got = await db.get_account(acc.id)
            with self.assertRaises(ValueError):
                await db.set_account_lifecycle_mode(acc.id, "bogus")
            with self.assertRaises(Exception):
                await db.set_account_active(999, True)
            # upserting the same account again keeps the settings
            again = await db.upsert_account("a", str(self.root / "a"))
        self.assertEqual((got.lifecycle_mode, got.auto_warmup, got.is_active), ("unlimited", False, False))
        self.assertEqual((again.lifecycle_mode, again.auto_warmup), ("unlimited", False))

    async def test_inactive_account_is_not_listed_as_active(self):
        async with Database(self.root / "m.db") as db:
            acc = await db.upsert_account("a", str(self.root / "a"))
            await db.set_account_active(acc.id, False)
            self.assertEqual(await db.list_active_accounts(), [])

    async def test_release_restriction_resets_errors_and_derives_the_stage(self):
        async with Database(self.root / "m.db") as db:
            acc = await db.upsert_account("a", str(self.root / "a"))
            await db.mark_account_authenticated(acc.id)
            for _ in range(3):
                await db.record_account_error(acc.id)
            await db.update_account_lifecycle(acc.id, lifecycle_stage="restricted")
            restricted = await db.get_account(acc.id)
            await release_restriction(db, restricted)
            freed = await db.get_account(acc.id)
        self.assertEqual(restricted.consecutive_errors, 3)
        self.assertEqual((freed.consecutive_errors, freed.lifecycle_stage), (0, "quarantine"))  # a fresh account: young

    async def test_release_in_mature_mode_gives_mature(self):
        async with Database(self.root / "m.db") as db:
            acc = await db.upsert_account("a", str(self.root / "a"))
            await db.mark_account_authenticated(acc.id)
            await db.set_account_lifecycle_mode(acc.id, "mature")
            for _ in range(3):
                await db.record_account_error(acc.id)
            await release_restriction(db, await db.get_account(acc.id))
            freed = await db.get_account(acc.id)
        self.assertEqual((freed.consecutive_errors, freed.lifecycle_stage, freed.daily_comments_limit), (0, "mature", 5))


class TestOrchestratorModes(unittest.IsolatedAsyncioTestCase):
    def _orchestrator(self, db, dry_run=False):
        cfg = MagicMock()
        cfg.browser.accounts = []
        warmer = MagicMock(warm_up=AsyncMock())
        orch = MultiAccountOrchestrator.__new__(MultiAccountOrchestrator)
        orch._db, orch._cfg, orch._dry_run = db, cfg, dry_run
        orch._skipped_logged, orch._lifecycle_logged = set(), set()
        orch._warmer, orch._warmup_tasks, orch._warmup_failed_at = warmer, {}, {}
        orch._warmup_slot = asyncio.Semaphore(1)
        orch._runner_locks, orch._runners = defaultdict(asyncio.Lock), {}
        return orch, warmer

    async def usable(self, *accounts):
        db = AsyncMock(spec=Database)
        db.list_active_accounts.return_value = list(accounts)
        orch, warmer = self._orchestrator(db)
        result = await orch._usable_accounts()
        await orch._finish_warmups()
        return orch, result, warmer

    async def test_young_account_in_mature_mode_is_used(self):
        _, usable, warmer = await self.usable(_live(2, name="bought", lifecycle_mode="mature"))
        self.assertEqual([a.name for a in usable], ["bought"])
        warmer.warm_up.assert_not_awaited()

    async def test_young_account_in_unlimited_mode_is_used_even_past_the_old_limits(self):
        acc = _live(0, name="free", lifecycle_mode="unlimited", daily_comments_done=99,
                    last_comment_at=datetime.now(timezone.utc).isoformat())
        _, usable, _ = await self.usable(acc)
        self.assertEqual([a.name for a in usable], ["free"])

    async def test_auto_warmup_off_never_starts_a_background_warmup(self):
        _, usable, warmer = await self.usable(_live(2, auto_warmup=False))
        self.assertEqual(usable, [])  # still gated by its age...
        warmer.warm_up.assert_not_awaited()  # ...but no warm-up is started

    async def test_auto_warmup_on_still_warms_a_young_account(self):
        _, _, warmer = await self.usable(_live(2, auto_warmup=True))
        warmer.warm_up.assert_awaited_once()

    async def test_inactive_account_is_never_warmed_by_the_orchestrator(self):
        db = AsyncMock(spec=Database)
        orch, warmer = self._orchestrator(db)
        orch._schedule_warmup(_live(2, is_active=False))
        await orch._finish_warmups()
        warmer.warm_up.assert_not_awaited()

    async def test_account_switched_off_during_a_run_is_not_picked_again(self):
        db = AsyncMock(spec=Database)
        db.get_account.return_value = _live(100, is_active=False)
        orch, _ = self._orchestrator(db)
        self.assertFalse(await orch._may_comment_now(_live(100)))
        db.get_account.return_value = _live(100)
        self.assertTrue(await orch._may_comment_now(_live(100)))

    async def test_skip_log_is_russian_and_names_the_way_out(self):
        logged = io.StringIO()
        sink = logger.add(logged, level="WARNING", format="{message}")
        try:
            await self.usable(_live(5, name="newbie", auto_warmup=False))
        finally:
            logger.remove(sink)
        text = logged.getvalue()
        self.assertIn("[LIFECYCLE] newbie: пропущен", text)
        self.assertIn("«Режим»", text)
        self.assertIn("Автопрогрев выключен", text)


class TestParserAndImport(unittest.IsolatedAsyncioTestCase):
    def test_mode_token_in_colon_and_free_form_lines(self):
        base = "u@x.com:pw:JBSWY3DPEHPK3PXP"
        self.assertEqual(parse_account_line(base).lifecycle_mode, None)
        self.assertEqual(parse_account_line(base + ":mode=mature").lifecycle_mode, "mature")
        self.assertEqual(parse_account_line(base + ":country=PL:mode=UNLIMITED").lifecycle_mode, "unlimited")
        self.assertEqual(parse_account_line(base + ":mode=auto:country=PL").expected_country, "PL")
        free = parse_account_line("u@x.com pw JBSWY3DPEHPK3PXP mode=mature country=PL")
        self.assertEqual((free.lifecycle_mode, free.expected_country), ("mature", "PL"))

    def test_unknown_mode_is_a_line_error_without_the_password(self):
        with self.assertRaises(ValueError) as caught:
            parse_account_line("u@x.com:pw-secret-1:JBSWY3DPEHPK3PXP:mode=turbo")
        self.assertIn("mode", str(caught.exception))
        self.assertNotIn("pw-secret-1", str(caught.exception))

    async def test_import_default_mode_and_line_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = MagicMock()
            cfg.browser.session_root = Path(tmp)
            lines = [
                "a@x.com:pw1:JBSWY3DPEHPK3PXP",
                "b@x.com:pw2:JBSWY3DPEHPK3PXP:mode=unlimited",
                "c@x.com:pw3:JBSWY3DPEHPK3PXP:mode=auto",
            ]
            async with Database(Path(tmp) / "m.db") as db:
                result = await youtube_bot.register_account_lines(db, cfg, lines, skip_existing=True, default_mode="mature")
                modes = {a.name: (await db.get_account(a.name)).lifecycle_mode for a in result.added}
                plain = await youtube_bot.register_account_lines(db, cfg, ["d@x.com:pw4:JBSWY3DPEHPK3PXP"])
                plain_mode = (await db.get_account("d@x.com")).lifecycle_mode
        self.assertEqual(modes, {"a@x.com": "mature", "b@x.com": "unlimited", "c@x.com": "auto"})
        self.assertEqual((len(plain.added), plain_mode), (1, "auto"))


class CliCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "t.db"
        self.config_path = self.root / "cfg.yaml"
        with open("config.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["app"]["db_path"] = str(self.db_path)
        data["app"]["log_file"] = str(self.root / "t.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["browser"]["accounts"] = [
            {"name": "bob", "session_path": str(self.root / "sessions" / "bob"), "proxy_url": None, "is_active": True}
        ]
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.db(lambda db: db.upsert_account("bob", str(self.root / "sessions" / "bob")))

    def tearDown(self):
        self.temp_dir.cleanup()

    def db(self, fn):
        async def _run():
            async with Database(self.db_path) as db:
                return await fn(db)

        return asyncio.run(_run())

    def bob(self) -> AccountRecord:
        return self.db(lambda db: db.get_account("bob"))


class TestCliFunctions(CliCase):
    def run_cli(self, coro):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            ok = asyncio.run(coro)
        return ok, out.getvalue(), err.getvalue()

    def test_set_mode(self):
        ok, out, _ = self.run_cli(youtube_bot.set_mode_cli("bob", "mature", str(self.config_path)))
        self.assertTrue(ok)
        self.assertIn("mature", out)
        self.assertEqual(self.bob().lifecycle_mode, "mature")

    def test_set_mode_rejects_unknown_mode_and_unknown_account(self):
        ok, _, err = self.run_cli(youtube_bot.set_mode_cli("bob", "turbo", str(self.config_path)))
        self.assertFalse(ok)
        self.assertIn("Unknown mode", err)
        self.assertEqual(self.bob().lifecycle_mode, "auto")
        ok, _, err = self.run_cli(youtube_bot.set_mode_cli("nobody", "mature", str(self.config_path)))
        self.assertFalse(ok)
        self.assertIn("not found", err)

    def test_auto_warmup_switch(self):
        self.run_cli(youtube_bot.set_auto_warmup_cli("bob", False, str(self.config_path)))
        self.assertFalse(self.bob().auto_warmup)
        self.run_cli(youtube_bot.set_auto_warmup_cli("bob", True, str(self.config_path)))
        self.assertTrue(self.bob().auto_warmup)

    def test_set_active_survives_a_config_sync(self):
        self.run_cli(youtube_bot.set_active_cli("bob", False, str(self.config_path)))
        self.assertFalse(self.bob().is_active)
        cfg = load_config(self.config_path)  # config.yaml still says is_active: true
        self.db(lambda db: sync_config_accounts(db, cfg.browser.accounts))
        self.assertFalse(self.bob().is_active)
        self.run_cli(youtube_bot.set_active_cli("bob", True, str(self.config_path)))
        self.assertTrue(self.bob().is_active)

    def test_reset_restricted(self):
        async def break_it(db):
            acc = await db.get_account("bob")
            await db.mark_account_authenticated(acc.id)
            for _ in range(3):
                await db.record_account_error(acc.id)

        self.db(break_it)
        ok, out, _ = self.run_cli(youtube_bot.reset_restricted_cli("bob", str(self.config_path)))
        self.assertTrue(ok)
        self.assertIn("restriction lifted", out)
        self.assertEqual(self.bob().consecutive_errors, 0)
        _, out, _ = self.run_cli(youtube_bot.reset_restricted_cli("bob", str(self.config_path)))
        self.assertIn("not restricted", out)

    def test_lifecycle_table_shows_mode_work_and_auto_warmup(self):
        self.db(lambda db: db.set_account_lifecycle_mode(1, "unlimited"))
        self.db(lambda db: db.set_account_auto_warmup(1, False))
        self.db(lambda db: db.set_account_active(1, False))
        _, out, _ = self.run_cli(youtube_bot.lifecycle_cli(str(self.config_path)))
        self.assertRegex(out, r"Mode +\| Active +\| AutoWarm")
        self.assertRegex(out, r"bob +\|.*\| unlimited +\| NO +\| off")


class TestCliFlags(CliCase):
    def main_with(self, *args: str):
        argv = ["youtube_bot.py", "--config", str(self.config_path), *args]
        with patch.object(sys, "argv", argv), patch("youtube_bot.bind_to_kill_on_close_job"), \
             patch("youtube_bot.setup_cli_logging"), contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                youtube_bot.main()
        return caught.exception.code

    def test_flags_change_the_account(self):
        self.assertEqual(self.main_with("--set-mode", "bob", "Mature"), 0)
        self.assertEqual(self.bob().lifecycle_mode, "mature")
        self.assertEqual(self.main_with("--auto-warmup", "bob", "off"), 0)
        self.assertFalse(self.bob().auto_warmup)
        self.assertEqual(self.main_with("--set-active", "bob", "off"), 0)
        self.assertFalse(self.bob().is_active)
        self.assertEqual(self.main_with("--set-active", "bob", "on"), 0)
        self.assertTrue(self.bob().is_active)

    def test_bad_values_fail_without_changes(self):
        self.assertEqual(self.main_with("--set-mode", "bob", "turbo"), 1)
        self.assertEqual(self.main_with("--auto-warmup", "bob", "maybe"), 1)
        self.assertEqual(self.main_with("--set-active", "nobody", "off"), 1)
        self.assertEqual(self.main_with("--reset-restricted", "nobody"), 1)
        bob = self.bob()
        self.assertEqual((bob.lifecycle_mode, bob.auto_warmup, bob.is_active), ("auto", True, True))

    def test_reset_restricted_flag(self):
        self.assertEqual(self.main_with("--reset-restricted", "bob"), 0)


class TestUiText(unittest.TestCase):
    def status(self, account):
        return account_status_label(account, T0)

    def test_row_text_per_state(self):
        self.assertEqual(self.status(_account(10)), ("◐ карантин 14ч", "warn"))
        self.assertEqual(self.status(_account(30))[0], "◐ aging 18ч")
        self.assertEqual(self.status(_account(50)), ("◑ новичок 0/1", "accent"))
        self.assertEqual(self.status(_account(100, daily_comments_done=2)), ("✓ взрослый 2/5", "ok"))
        self.assertEqual(self.status(_account(5, lifecycle_mode="mature")), ("✓ прогрет 0/5", "ok"))
        self.assertEqual(self.status(_account(5, lifecycle_mode="unlimited")), ("∞ без лимитов", "accent"))
        self.assertEqual(self.status(_account(100, is_active=False)), ("– не в работе", "dim"))
        self.assertEqual(self.status(_account(100, consecutive_errors=3)), ("✗ ограничен", "err"))
        self.assertEqual(self.status(_account(authenticated_at=None, auth_status="pending")), ("· без возраста", "dim"))
        # a mode chosen before the first sign-in is shown too
        unauth = dict(authenticated_at=None, auth_status="pending")
        self.assertEqual(self.status(_account(lifecycle_mode="unlimited", **unauth))[0], "∞ без лимитов")
        self.assertEqual(self.status(_account(lifecycle_mode="mature", **unauth))[0], "✓ прогрет")

    def test_switched_off_wins_over_restricted(self):
        self.assertEqual(self.status(_account(100, is_active=False, consecutive_errors=3))[0], "– не в работе")

    def test_row_text_fits_its_column(self):
        for acc in (_account(10), _account(30), _account(100, daily_comments_done=5), _account(5, lifecycle_mode="mature")):
            self.assertLessEqual(len(self.status(acc)[0]), 15)

    def test_next_comment_texts(self):
        self.assertEqual(next_comment_text(_account(100), T0), ("сейчас", "ok"))
        self.assertEqual(next_comment_text(_account(5, lifecycle_mode="unlimited"), T0)[0], "сразу, без пауз")
        self.assertIn("через 43ч", next_comment_text(_account(5), T0)[0])
        wait = next_comment_text(_account(100, daily_comments_done=1, last_comment_at=(T0 - timedelta(minutes=10)).isoformat()), T0)
        self.assertTrue(wait[0].startswith("через "))
        full = next_comment_text(_account(100, daily_comments_done=5, last_comment_at=(T0 - timedelta(hours=3)).isoformat()), T0)
        self.assertIn("после сброса лимита через 6ч", full[0])
        self.assertEqual(next_comment_text(_account(100, is_active=False), T0)[0], "аккаунт не в работе")
        self.assertIn("Снять ограничение", next_comment_text(_account(100, consecutive_errors=3), T0)[0])

    def test_card_shows_real_age_next_to_a_forced_mode(self):
        rows = {label: value for label, value, _ in account_card_rows(
            _account(14, lifecycle_mode="mature", daily_comments_done=2, auto_warmup=False,
                     last_warmup_at=(T0 - timedelta(hours=3)).isoformat(), warmup_sessions_done=4), T0)}
        self.assertEqual(rows["Стадия"], "прогрет (вручную)")
        self.assertEqual(rows["Возраст"], "14 ч с первого входа · по часам: карантин (молодой)")
        self.assertEqual(rows["Лимит сегодня"], "2/5 сегодня")
        self.assertEqual(rows["Режим"], "Уже прогрет")
        self.assertEqual(rows["Автопрогрев"], "выкл")
        self.assertEqual(rows["В работе"], "да")
        self.assertEqual(rows["Последний прогрев"], "3ч назад · всего 4")

    def test_card_for_auto_and_unlimited(self):
        auto = {label: value for label, value, _ in account_card_rows(_account(10), T0)}
        self.assertEqual((auto["Стадия"], auto["Лимит сегодня"], auto["Режим"]), ("карантин (молодой)", "комментарии запрещены", "По возрасту"))
        free = {label: value for label, value, _ in account_card_rows(_account(10, lifecycle_mode="unlimited", daily_comments_done=7), T0)}
        self.assertEqual((free["Стадия"], free["Лимит сегодня"]), ("без лимитов", "без лимита (сегодня 7)"))

    def test_lifecycle_rows_carry_mode_work_and_warmup(self):
        rows = lifecycle_rows(
            [_account(100, name="a"), _account(5, name="b", lifecycle_mode="mature", is_active=False, auto_warmup=False),
             _account(5, name="c", lifecycle_mode="unlimited", daily_comments_done=3)],
            lambda a: True, T0,
        )
        self.assertEqual([(r.mode, r.in_work, r.auto_warmup) for r in rows],
                         [("По возрасту", "да", "вкл"), ("Уже прогрет", "нет", "выкл"), ("Без лимитов", "да", "вкл")])
        self.assertEqual((rows[1].stage, rows[2].stage, rows[2].quota), ("прогрет", "∞", "3/∞"))


class TestWizardScreens(MenuCase):
    def seed(self, count: int = 2) -> None:
        with patch("youtube_bot.warm_selected_cli", new=AsyncMock(return_value=[])):
            from modules.wizard import _setup_import

            self.run_keys(
                lambda: _setup_import(self.config_path), "2", ENTER,
                paste=[good_line(n) for n in range(1, count + 1)] + [""], inputs=["n"],
            )

    def entry(self, name: str):
        acc = self.accounts()[name]
        return acc, True

    def actions(self, name: str, *keys: str, inputs: list | None = None):
        entry = self.entry(name)
        return self.run_keys(
            lambda: _setup_account_actions(self.config_path, self.root / "sessions", [entry], entry), *keys, inputs=inputs
        )

    def test_account_rows_show_status_and_keep_the_width(self):
        self.seed(2)
        reset = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        self.set_stage("user1@example.com", 100, daily_comments_done=2, daily_limit_reset_at=reset)
        accounts = [(a, True) for a in load_accounts(self.config_path)]
        accounts[1][0].lifecycle_mode = "unlimited"
        rows = _setup_account_rows(accounts)
        self.assertIn("✓ взрослый 2/5", rows[0][2])
        self.assertIn("∞ без лимитов", rows[1][2])
        for key, label, value, _ in rows:
            self.assertLessEqual(len(label) + len(value) + 3, MENU_WIDTH, (label, value))

    def test_account_menu_shows_the_card_and_new_rows(self):
        self.seed(1)
        _, output = self.actions("user1@example.com", ESC)
        plain = ANSI.sub("", output)
        for text in ("Стадия", "Возраст", "Лимит сегодня", "Следующий коммент", "Последний прогрев", "Режим", "Автопрогрев",
                     "В работе: да", "Режим: По возрасту", "Автопрогрев: вкл", "Прогреть сейчас"):
            self.assertIn(text, plain)
        self.assertNotIn("Снять ограничение", plain)  # only for a restricted account

    def test_in_work_toggle_saves_and_reports(self):
        self.seed(1)
        notice, _ = self.actions("user1@example.com", "5", ESC)
        self.assertFalse(self.accounts()["user1@example.com"].is_active)
        self.assertEqual(notice[0], "ok")
        self.assertIn("выключен", notice[1])

    def test_mode_choice_saves(self):
        self.seed(1)
        notice, _ = self.actions("user1@example.com", "6", "2", ESC)
        self.assertEqual(self.accounts()["user1@example.com"].lifecycle_mode, "mature")
        self.assertIn("Уже прогрет", notice[1])

    def test_mode_choice_can_be_cancelled(self):
        self.seed(1)
        self.actions("user1@example.com", "6", "0", ESC)
        self.assertEqual(self.accounts()["user1@example.com"].lifecycle_mode, "auto")

    def test_auto_warmup_toggle(self):
        self.seed(1)
        self.actions("user1@example.com", "7", ESC)
        self.assertFalse(self.accounts()["user1@example.com"].auto_warmup)

    def test_release_restriction_needs_confirmation(self):
        self.seed(1)
        self.set_stage("user1@example.com", 100, consecutive_errors=3)
        _, output = self.actions("user1@example.com", "9", ESC, inputs=["n"])
        self.assertEqual(self.accounts()["user1@example.com"].consecutive_errors, 3)
        self.assertIn("Снять ограничение", ANSI.sub("", output))
        notice, _ = self.actions("user1@example.com", "9", ESC, inputs=["y"])
        self.assertEqual(self.accounts()["user1@example.com"].consecutive_errors, 0)
        self.assertEqual(notice, ("ok", "«user1@example.com»: ограничение снято"))

    def test_warm_now_runs_one_manual_warmup_after_confirmation(self):
        self.seed(1)
        with patch("youtube_bot.warm_up_cli", new=AsyncMock(return_value=True)) as warm:
            notice, _ = self.actions("user1@example.com", "8", ESC, inputs=["y"])
        warm.assert_awaited_once()
        self.assertEqual(warm.await_args.args[0], "user1@example.com")
        self.assertEqual(notice[0], "ok")
        with patch("youtube_bot.warm_up_cli", new=AsyncMock(return_value=True)) as warm:
            self.actions("user1@example.com", "8", ESC, inputs=["n"])
        warm.assert_not_awaited()

    def test_bulk_mode_for_ticked_accounts(self):
        self.seed(3)
        accounts = [(a, True) for a in load_accounts(self.config_path)]
        # tick 1 and 3, «Готово», «Режим…» (1), «Без лимитов» (3)
        notice, _ = self.run_keys(
            lambda: _setup_bulk_settings(self.config_path, accounts), SPACE, DOWN, DOWN, SPACE, DOWN, ENTER, "1", "3"
        )
        modes = {n: a.lifecycle_mode for n, a in self.accounts().items()}
        self.assertEqual((modes["user1@example.com"], modes["user2@example.com"], modes["user3@example.com"]),
                         ("unlimited", "auto", "unlimited"))
        self.assertEqual(notice, ("ok", "режим «Без лимитов»: 2 из 2 акк."))

    def test_bulk_in_work_and_auto_warmup_for_all(self):
        self.seed(2)
        accounts = [(a, True) for a in load_accounts(self.config_path)]
        self.run_keys(lambda: _setup_bulk_settings(self.config_path, accounts), SPACE, DOWN, SPACE, DOWN, ENTER, "3")
        self.assertEqual([a.is_active for a in self.accounts().values()], [False, False])
        self.run_keys(lambda: _setup_bulk_settings(self.config_path, accounts), SPACE, DOWN, SPACE, DOWN, ENTER, "5")
        self.assertEqual([a.auto_warmup for a in self.accounts().values()], [False, False])

    def test_bulk_cancel_changes_nothing(self):
        self.seed(1)
        accounts = [(a, True) for a in load_accounts(self.config_path)]
        notice, _ = self.run_keys(lambda: _setup_bulk_settings(self.config_path, accounts), SPACE, ESC)
        self.assertIsNone(notice)
        notice, _ = self.run_keys(lambda: _setup_bulk_settings(self.config_path, accounts), DOWN, ENTER)
        self.assertIsNone(notice)

    def test_lifecycle_table_has_mode_work_and_warmup_line(self):
        self.seed(2)
        self.set_stage("user1@example.com", 100)
        async def modes(db):
            a, b = await db.get_account("user1@example.com"), await db.get_account("user2@example.com")
            await db.set_account_lifecycle_mode(a.id, "mature")
            await db.set_account_auto_warmup(b.id, False)
            await db.set_account_active(b.id, False)
        asyncio.run(self._with_db(modes))
        _, output = self.run_keys(lambda: _setup_lifecycle(self.config_path))
        plain = ANSI.sub("", output)
        self.assertRegex(plain, r"режим: Уже прогрет · в работе: да · автопрогрев: вкл")
        self.assertRegex(plain, r"режим: По возрасту · в работе: нет · автопрогрев: выкл")

    async def _with_db(self, fn):
        async with Database(self.db_path) as db:
            return await fn(db)


class TestImportQuestion(MenuCase):
    def paste(self, mode_keys: tuple[str, ...], lines: list[str]):
        from modules.wizard import _setup_import

        with patch("youtube_bot.warm_selected_cli", new=AsyncMock(return_value=[])):
            return self.run_keys(
                lambda: _setup_import(self.config_path), "2", *mode_keys, paste=[*lines, ""], inputs=["n"],
            )

    def test_question_applies_the_picked_mode_to_every_imported_account(self):
        self.paste(("2",), [good_line(1), good_line(2)])
        self.assertEqual({a.lifecycle_mode for a in self.accounts().values() if a.name.startswith("user")}, {"mature"})

    def test_default_answer_is_by_age(self):
        self.paste((ENTER,), [good_line(1)])
        self.assertEqual(self.accounts()["user1@example.com"].lifecycle_mode, "auto")

    def test_line_mode_beats_the_answer(self):
        self.paste(("3",), [good_line(1) + ":mode=auto", good_line(2)])
        modes = {n: a.lifecycle_mode for n, a in self.accounts().items() if n.startswith("user")}
        self.assertEqual(modes, {"user1@example.com": "auto", "user2@example.com": "unlimited"})

    def test_cancelling_the_question_cancels_the_import(self):
        notice, _ = self.paste(("0",), [good_line(1)])
        self.assertEqual(notice[0], "info")
        self.assertFalse([n for n in self.accounts() if n.startswith("user")])

    def test_pick_mode_returns_keys(self):
        for key, mode in (("1", "auto"), ("2", "mature"), ("3", "unlimited"), ("0", None)):
            with self.subTest(key=key), scripted_keys(key):
                self.assertEqual(_setup_pick_mode(), mode)


if __name__ == "__main__":
    unittest.main()
