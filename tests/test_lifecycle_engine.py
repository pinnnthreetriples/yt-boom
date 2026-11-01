from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from database import AccountRecord, Database
from modules.lifecycle_engine import AccountLifecycleEngine
from modules.multi_account_orchestrator import MultiAccountOrchestrator

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)


def _account(hours_old: float = 0.0, **overrides) -> AccountRecord:
    fields = dict(
        id=1,
        name="acc",
        session_path="s",
        created_at=T0.isoformat(),
        auth_status="authenticated",
        authenticated_at=(T0 - timedelta(hours=hours_old)).isoformat(),
        daily_limit_reset_at=(T0 + timedelta(hours=6)).isoformat(),
    )
    fields.update(overrides)
    return AccountRecord(**fields)


class TestStageProgression(unittest.TestCase):
    def test_stage_follows_the_hours_since_sign_in(self):
        cases = [(0, "quarantine", 0), (23.9, "quarantine", 0), (25, "aging", 0), (49, "probation", 1), (73, "mature", 5)]
        for hours, stage, limit in cases:
            with self.subTest(hours=hours):
                self.assertEqual(AccountLifecycleEngine.calculate_stage(_account(hours), T0), (stage, limit))

    def test_boundaries_belong_to_the_next_stage(self):
        for hours, stage in [(24, "aging"), (48, "probation"), (72, "mature")]:
            self.assertEqual(AccountLifecycleEngine.calculate_stage(_account(hours), T0)[0], stage)

    def test_account_without_stamp_ages_from_its_creation(self):
        old = _account(authenticated_at=None, created_at=(T0 - timedelta(days=30)).isoformat())
        self.assertEqual(AccountLifecycleEngine.calculate_stage(old, T0)[0], "mature")

    def test_quarantine_hours_left(self):
        self.assertAlmostEqual(AccountLifecycleEngine.quarantine_hours_left(_account(6), T0), 18.0)
        self.assertEqual(AccountLifecycleEngine.quarantine_hours_left(_account(30), T0), 0.0)

    def test_mature_cooldown_is_random_inside_45_to_90_minutes_but_stable_per_comment(self):
        stamps = [(T0 - timedelta(minutes=n)).isoformat() for n in range(60)]
        values = [AccountLifecycleEngine.cooldown_sec("mature", stamp) for stamp in stamps]
        self.assertTrue(all(2700 <= v <= 5400 for v in values))
        self.assertGreater(len(set(values)), 30)  # differs from comment to comment
        self.assertEqual(AccountLifecycleEngine.cooldown_sec("mature", stamps[0]), values[0])  # same comment, same answer
        self.assertEqual(AccountLifecycleEngine.cooldown_sec("probation", stamps[0]), 10800)
        self.assertIsNone(AccountLifecycleEngine.cooldown_sec("quarantine", stamps[0]))

    def test_damaged_sign_in_stamp_does_not_make_an_account_mature(self):
        damaged = _account(authenticated_at="garbage", created_at=(T0 - timedelta(days=30)).isoformat())
        self.assertEqual(AccountLifecycleEngine.calculate_stage(damaged, datetime.now(timezone.utc))[0], "quarantine")


class TestEligibility(unittest.IsolatedAsyncioTestCase):
    async def check(self, account: AccountRecord, now: datetime = T0, db=None):
        return await AccountLifecycleEngine.evaluate_eligibility(account, db=db, now=now)

    async def test_quarantine_and_aging_only_warm_up(self):
        for hours, stage in [(1, "quarantine"), (30, "aging")]:
            result = await self.check(_account(hours))
            self.assertFalse(result.can_comment)
            self.assertEqual((result.stage, result.suggested_action), (stage, "warm_up"))

    async def test_unauthenticated_account_is_refused(self):
        result = await self.check(_account(100, auth_status="pending"))
        self.assertFalse(result.can_comment)
        self.assertIn("not authenticated", result.reason)

    async def test_probation_allows_one_comment_a_day(self):
        self.assertTrue((await self.check(_account(50))).can_comment)
        result = await self.check(_account(50, daily_comments_done=1, last_comment_at=(T0 - timedelta(hours=5)).isoformat()))
        self.assertFalse(result.can_comment)
        self.assertEqual(result.reason, "Daily limit reached")

    async def test_mature_allows_five_comments_a_day(self):
        long_ago = (T0 - timedelta(hours=2)).isoformat()
        self.assertTrue((await self.check(_account(100, daily_comments_done=4, last_comment_at=long_ago))).can_comment)
        result = await self.check(_account(100, daily_comments_done=5, last_comment_at=long_ago))
        self.assertFalse(result.can_comment)
        self.assertEqual(result.reason, "Daily limit reached")

    async def test_probation_cooldown_is_three_hours(self):
        # a second comment would exceed the day's limit of 1, so probation is only reachable here after a reset
        recent = _account(50, last_comment_at=(T0 - timedelta(hours=2, minutes=59)).isoformat())
        result = await self.check(recent)
        self.assertFalse(result.can_comment)
        self.assertEqual(result.reason, "Interval cooldown active")
        self.assertGreater(result.retry_after_sec, 0)
        ready = _account(50, last_comment_at=(T0 - timedelta(hours=3, minutes=1)).isoformat())
        self.assertTrue((await self.check(ready)).can_comment)

    async def test_mature_cooldown_is_forty_five_minutes(self):
        blocked = await self.check(_account(100, daily_comments_done=1, last_comment_at=(T0 - timedelta(minutes=44)).isoformat()))
        self.assertFalse(blocked.can_comment)
        self.assertEqual(blocked.reason, "Interval cooldown active")
        ok = await self.check(_account(100, daily_comments_done=1, last_comment_at=(T0 - timedelta(minutes=91)).isoformat()))
        self.assertTrue(ok.can_comment)

    async def test_daily_counter_restarts_after_the_reset_moment_with_timezone_aware_times(self):
        plus_three = timezone(timedelta(hours=3))
        reset_local = datetime(2026, 11, 1, 6, 0, tzinfo=plus_three)  # 03:00 UTC, before T0
        account = _account(
            100,
            daily_comments_done=5,
            daily_limit_reset_at=reset_local.isoformat(),
            last_comment_at=(T0 - timedelta(hours=3)).isoformat(),
        )
        db = AsyncMock(spec=Database)
        result = await self.check(account, db=db)
        self.assertTrue(result.can_comment)
        self.assertEqual(result.daily_comments_done, 0)
        saved = db.update_account_lifecycle.await_args.kwargs
        self.assertEqual(saved["daily_comments_done"], 0)
        next_reset = datetime.fromisoformat(saved["daily_limit_reset_at"])
        self.assertEqual(next_reset, datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc))

    async def test_counter_is_kept_before_the_reset_moment(self):
        account = _account(100, daily_comments_done=5, last_comment_at=(T0 - timedelta(hours=3)).isoformat())
        self.assertFalse((await self.check(account)).can_comment)

    async def test_three_consecutive_errors_restrict_the_account(self):
        db = AsyncMock(spec=Database)
        result = await self.check(_account(100, consecutive_errors=3), db=db)
        self.assertFalse(result.can_comment)
        self.assertEqual((result.stage, result.suggested_action), ("restricted", "restricted"))
        db.update_account_lifecycle.assert_awaited_once_with(1, lifecycle_stage="restricted")
        self.assertTrue((await self.check(_account(100, consecutive_errors=2))).can_comment)


class TestDatabaseLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_mark_authenticated_starts_the_quarantine_clock(self):
        async with Database(self.root / "m.db") as db:
            acc = await db.upsert_account("a", str(self.root / "a"))
            self.assertEqual((acc.lifecycle_stage, acc.authenticated_at, acc.consecutive_errors), ("quarantine", None, 0))
            await db.mark_account_authenticated(acc.id)
            acc = await db.get_account(acc.id)
        self.assertEqual(acc.auth_status, "authenticated")
        stamped = datetime.fromisoformat(acc.authenticated_at)
        self.assertEqual(datetime.fromisoformat(acc.quarantine_until) - stamped, timedelta(hours=24))
        reset = datetime.fromisoformat(acc.daily_limit_reset_at)
        self.assertEqual((reset.hour, reset.minute), (4, 0))
        self.assertGreater(reset, stamped)

    async def test_reauthentication_does_not_restart_the_clock(self):
        async with Database(self.root / "m.db") as db:
            acc = await db.upsert_account("a", str(self.root / "a"))
            await db.mark_account_authenticated(acc.id)
            first = (await db.get_account(acc.id)).authenticated_at
            await db.record_account_error(acc.id)
            await db.mark_account_authenticated(acc.id)
            again = await db.get_account(acc.id)
        self.assertEqual(again.authenticated_at, first)
        self.assertEqual(again.consecutive_errors, 0)

    async def test_comment_warmup_and_error_counters(self):
        async with Database(self.root / "m.db") as db:
            acc = await db.upsert_account("a", str(self.root / "a"))
            await db.record_account_error(acc.id)
            await db.record_account_error(acc.id)
            self.assertEqual((await db.get_account(acc.id)).consecutive_errors, 2)
            await db.record_account_comment_success(acc.id)
            await db.record_account_warmup_success(acc.id)
            acc = await db.get_account(acc.id)
            with self.assertRaises(Exception):
                await db.record_account_error(999)
        self.assertEqual((acc.total_replies, acc.daily_comments_done, acc.consecutive_errors), (1, 1, 0))
        self.assertEqual(acc.warmup_sessions_done, 1)
        self.assertIsNotNone(acc.last_comment_at)
        self.assertIsNotNone(acc.last_warmup_at)
        self.assertIsNotNone(acc.last_used_at)

    async def test_old_accounts_table_gets_the_lifecycle_columns(self):
        path = self.root / "old.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, session_path TEXT NOT NULL,"
            " proxy_url TEXT, is_active INTEGER NOT NULL DEFAULT 1, total_replies INTEGER NOT NULL DEFAULT 0,"
            " created_at TEXT NOT NULL, last_used_at TEXT)"
        )
        conn.execute("INSERT INTO accounts (name, session_path, created_at) VALUES ('legacy', 's', '2026-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()
        async with Database(path) as db:
            acc = await db.get_account("legacy")
        self.assertEqual((acc.lifecycle_stage, acc.daily_comments_limit, acc.warmup_sessions_done), ("quarantine", 0, 0))

    async def test_legacy_authenticated_account_keeps_its_age_after_migration(self):
        path = self.root / "legacy.db"
        async with Database(path) as db:
            acc = await db.upsert_account("old", str(self.root / "old"))
            await db.set_account_auth_status(acc.id, "authenticated")  # the way accounts were signed in before
            async with db._transaction() as conn:
                await conn.execute("UPDATE accounts SET created_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (acc.id,))
        async with Database(path) as db:  # the next start backfills the stamp
            acc = await db.get_account("old")
            self.assertEqual(acc.authenticated_at, "2026-01-01T00:00:00+00:00")
            await db.mark_account_authenticated(acc.id)  # a later re-login does not send it back to quarantine
            acc = await db.get_account("old")
        self.assertEqual(AccountLifecycleEngine.calculate_stage(acc)[0], "mature")

    async def test_fingerprint_sync_in_both_directions(self):
        session = self.root / "s"
        session.mkdir()
        payload = json.dumps({"format": 1, "os": "windows", "fingerprint": {"navigator": {"userAgent": "UA"}}})
        async with Database(self.root / "m.db") as db:
            acc = await db.upsert_account("a", str(session))
            (session / "fingerprint.json").write_text(payload, encoding="utf-8")
            await db.sync_account_fingerprint(acc.id, session)  # disk -> SQLite
            self.assertEqual((await db.get_account(acc.id)).fingerprint_data, payload)
            (session / "fingerprint.json").unlink()
            await db.sync_account_fingerprint(acc.id, session)  # SQLite -> disk
            self.assertEqual((session / "fingerprint.json").read_text(encoding="utf-8"), payload)
            (session / "fingerprint.json").write_text("{broken", encoding="utf-8")
            await db.sync_account_fingerprint(acc.id, session)  # damaged file never overwrites the stored value
            self.assertEqual((await db.get_account(acc.id)).fingerprint_data, payload)


class TestOrchestratorGate(unittest.IsolatedAsyncioTestCase):
    def _orchestrator(self, db, dry_run=False):
        cfg = MagicMock()
        cfg.browser.accounts = []
        warmer = MagicMock(warm_up=AsyncMock())
        orch = MultiAccountOrchestrator.__new__(MultiAccountOrchestrator)  # only the gate's attributes are needed
        orch._db, orch._cfg, orch._dry_run = db, cfg, dry_run
        orch._skipped_logged, orch._lifecycle_logged = set(), set()
        orch._warmer, orch._warmup_tasks, orch._warmup_failed_at = warmer, {}, {}
        orch._warmup_slot = asyncio.Semaphore(1)
        orch._runner_locks, orch._runners = defaultdict(asyncio.Lock), {}
        return orch, warmer

    async def test_young_accounts_are_skipped_and_warmed_up_in_the_background(self):
        young = _account(2, id=1, name="young")
        mature = _account(100, id=2, name="mature")
        db = AsyncMock(spec=Database)
        db.list_active_accounts.return_value = [young, mature]
        orch, warmer = self._orchestrator(db)

        usable = await orch._usable_accounts()
        await orch._finish_warmups()

        self.assertEqual([a.name for a in usable], ["mature"])
        warmer.warm_up.assert_awaited_once()
        self.assertEqual(warmer.warm_up.await_args.args[0].name, "young")

    async def test_recently_warmed_account_is_not_warmed_again(self):
        recent = _account(2, last_warmup_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
        db = AsyncMock(spec=Database)
        db.list_active_accounts.return_value = [recent]
        orch, warmer = self._orchestrator(db)

        self.assertEqual(await orch._usable_accounts(), [])
        await orch._finish_warmups()
        warmer.warm_up.assert_not_awaited()

    async def test_failed_warmup_is_not_retried_on_every_pick(self):
        db = AsyncMock(spec=Database)
        db.list_active_accounts.return_value = [_account(2)]
        orch, warmer = self._orchestrator(db)
        warmer.warm_up.return_value = MagicMock(success=False)

        for _ in range(4):
            await orch._usable_accounts()
            await orch._finish_warmups()
        self.assertEqual(warmer.warm_up.await_count, 1)

    async def test_warmup_waits_for_the_account_lock_and_frees_its_open_browser(self):
        db = AsyncMock(spec=Database)
        db.list_active_accounts.return_value = [_account(2, id=7)]
        orch, warmer = self._orchestrator(db)
        runner = MagicMock(close=AsyncMock())
        orch._runners[7] = runner

        async with orch._runner_locks[7]:  # a comment is being posted by this account
            await orch._usable_accounts()
            await asyncio.sleep(0)
            warmer.warm_up.assert_not_awaited()
            runner.close.assert_not_awaited()
        await orch._finish_warmups()

        runner.close.assert_awaited_once()
        self.assertNotIn(7, orch._runners)
        warmer.warm_up.assert_awaited_once()

    async def test_dry_run_never_starts_a_warmup(self):
        db = AsyncMock(spec=Database)
        db.list_active_accounts.return_value = [_account(2)]
        orch, warmer = self._orchestrator(db, dry_run=True)

        self.assertEqual(await orch._usable_accounts(), [])
        await orch._finish_warmups()
        warmer.warm_up.assert_not_awaited()

    async def test_accounts_that_were_never_signed_in_by_the_bot_are_not_gated(self):
        manual = _account(0, auth_status="pending", authenticated_at=None)
        db = AsyncMock(spec=Database)
        db.list_active_accounts.return_value = [manual]
        orch, _ = self._orchestrator(db)
        self.assertEqual(await orch._usable_accounts(), [manual])


if __name__ == "__main__":
    unittest.main()
