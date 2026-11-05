"""Wizard «Аккаунты»: import (file / paste), warm-up pick, warm-up of the chosen accounts, lifecycle table.

Pure logic is tested without a terminal; the screens run on scripted keys with a temporary DB. The browser
(AccountWarmer / sign-in) is always mocked. Credentials in this file are fake.
"""
from __future__ import annotations

import asyncio
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from database import AccountRecord
from modules.account_menu_logic import (
    clean_pasted_lines,
    count_account_lines,
    format_ago,
    format_duration,
    format_import_summary,
    format_warm_summary,
    lifecycle_rows,
    parse_warm_selection,
)
from modules.lifecycle_engine import AccountLifecycleEngine
from modules.wizard import (
    _setup_register,
    load_accounts,
)

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
PASSWORDS = ("pw-fake-1", "pw-fake-2", "pw-fake-3")
TOTP = "JBSWY3DPEHPK3PXP"


def good_line(n: int) -> str:
    return f"user{n}@example.com:{PASSWORDS[n - 1]}:{TOTP}"


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


class TestWarmSelection(unittest.TestCase):
    def test_all_none_and_numbers(self):
        cases = [
            ("a", [0, 1, 2]), ("A", [0, 1, 2]), ("все", [0, 1, 2]), ("Всех", [0, 1, 2]),
            ("", []), ("  ", []), ("n", []), ("нет", []),
            ("1 3", [0, 2]), ("3,1", [2, 0]), ("2", [1]), ("1 1 2", [0, 1]),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(parse_warm_selection(text, 3), expected)

    def test_garbage_and_out_of_range_raise(self):
        for text in ("x", "1 two", "0 1", "4", "1 9", "-1", "1.5"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_warm_selection(text, 3)

    def test_error_message_names_the_problem(self):
        with self.assertRaisesRegex(ValueError, "вне списка"):
            parse_warm_selection("5", 3)
        with self.assertRaisesRegex(ValueError, "Не распознано"):
            parse_warm_selection("1 abc", 3)


class TestSummaries(unittest.TestCase):
    def test_import_summary_counts_and_line_numbers_without_secrets(self):
        lines = format_import_summary(2, [(4, "аккаунт уже есть")], [(3, "формат: email:password:totp_secret")], 4)
        texts = [t for _, t in lines]
        self.assertEqual(texts[0], "Добавлено: 2 · пропущено: 1 · ошибок: 1")
        self.assertIn("строка 3: ошибка — формат: email:password:totp_secret", texts)
        self.assertIn("строка 4: пропущена — аккаунт уже есть", texts)
        self.assertEqual(lines[0][0], "ok")

    def test_import_summary_kinds_and_cap(self):
        self.assertEqual(format_import_summary(0, [], [(1, "x")], 1)[0][0], "err")
        self.assertEqual(format_import_summary(0, [(1, "x")], [], 1)[0][0], "warn")
        self.assertIn("нет строк", format_import_summary(0, [], [], 0)[0][1])
        many = format_import_summary(0, [], [(n, "x") for n in range(1, 21)], 20)
        self.assertEqual(len(many), 1 + 8 + 1)
        self.assertEqual(many[-1][1], "… ещё 12")

    def test_account_line_count_ignores_blank_and_comments(self):
        lines = clean_pasted_lines(["\ufeff# comment\r\n", "\n", good_line(1) + "\n", "  ", " # x", good_line(2)])
        self.assertEqual(count_account_lines(lines), 2)
        self.assertEqual(lines[0], "# comment")  # BOM and line end removed, positions kept

    def test_warm_summary(self):
        ok = SimpleNamespace(account_name="a", success=True)
        bad = SimpleNamespace(account_name="b", success=False)
        self.assertEqual(format_warm_summary([ok, ok]), ("ok", "Прогрев: 2 из 2 успешно"))
        self.assertEqual(format_warm_summary([ok, bad]), ("warn", "Прогрев: 1 из 2 успешно · не удалось: b"))
        self.assertEqual(format_warm_summary([])[0], "warn")

    def test_ago_and_duration(self):
        stamp = lambda **kw: (T0 - timedelta(**kw)).isoformat()  # noqa: E731
        self.assertEqual(format_ago(stamp(minutes=5), T0), "5м назад")
        self.assertEqual(format_ago(stamp(hours=3, minutes=10), T0), "3ч назад")
        self.assertEqual(format_ago(stamp(days=4), T0), "4д назад")
        self.assertEqual((format_ago(None), format_ago("junk")), ("—", "—"))
        self.assertEqual([format_duration(s) for s in (10, 2700, 7800)], ["<1м", "45м", "2ч 10м"])


class TestLifecycleRows(unittest.TestCase):
    def rows(self, *accounts, alive=True):
        return lifecycle_rows(accounts, lambda a: alive, T0)

    def test_stages_quota_and_heartbeat(self):
        rows = self.rows(
            _account(3, name="q"), _account(30, name="a"), _account(50, name="p", daily_comments_done=1),
            _account(100, name="m", daily_comments_done=2, last_warmup_at=(T0 - timedelta(hours=3)).isoformat()),
        )
        self.assertEqual([(r.name, r.stage, r.quota) for r in rows], [
            ("q", "карантин", "0/0"), ("a", "aging", "0/0"), ("p", "probation", "1/1"), ("m", "mature", "2/5"),
        ])
        self.assertEqual(rows[0].heartbeat, "ok · —")
        self.assertEqual(rows[3].heartbeat, "ok · 3ч назад")
        self.assertEqual([r.stage_style for r in rows], ["warn", "warn", "accent", "ok"])

    def test_dead_session_and_unauthenticated_account(self):
        dead = self.rows(_account(100), alive=False)[0]
        self.assertEqual((dead.heartbeat, dead.heartbeat_style), ("нет · —", "warn"))
        fresh = self.rows(_account(auth_status="pending", authenticated_at=None))[0]
        self.assertEqual((fresh.stage, fresh.quota, fresh.pause), ("—", "—", "нужен вход"))

    def test_circuit_breaker_limit_and_cooldown_marks(self):
        breaker = self.rows(_account(100, consecutive_errors=3))[0]
        self.assertEqual((breaker.stage, breaker.pause, breaker.pause_style), ("restricted", "breaker ×3", "err"))
        self.assertEqual(self.rows(_account(100, daily_comments_done=5))[0].pause, "лимит дня")
        recent = (T0 - timedelta(minutes=10)).isoformat()
        paused = self.rows(_account(100, daily_comments_done=1, last_comment_at=recent))[0]
        self.assertRegex(paused.pause, r"^пауза (\d+ч )?\d+м$")
        idle = self.rows(_account(100, daily_comments_done=1, last_comment_at=(T0 - timedelta(hours=5)).isoformat()))[0]
        self.assertEqual(idle.pause, "—")
        self.assertEqual(self.rows(_account(5))[0].pause, "ещё рано")

    def test_inactive_account_is_marked_and_beats_other_pauses(self):
        row = self.rows(_account(100, is_active=False, daily_comments_done=5))[0]
        self.assertEqual((row.pause, row.pause_style, row.quota), ("отключён", "dim", "5/5"))

    def test_quota_restarts_after_daily_reset(self):
        stale = _account(100, daily_comments_done=4, daily_limit_reset_at=(T0 - timedelta(hours=1)).isoformat())
        self.assertEqual(self.rows(stale)[0].quota, "0/5")

    def test_summarize_matches_engine(self):
        view = AccountLifecycleEngine.summarize(_account(6, daily_comments_done=1), T0)
        self.assertEqual((view.stage, round(view.age_hours), round(view.quarantine_left), view.quota_done), ("quarantine", 6, 18, 1))
        self.assertEqual(AccountLifecycleEngine.summarize(_account(auth_status="failed"), T0).stage, "-")


def _pools_rename(config_path: Path, old: str, new: str) -> None:
    from modules.wizard import _pools_db

    _pools_db(config_path, lambda db: db.rename_account(old, new))


class MenuCase(unittest.TestCase):
    """Temporary config/DB and scripted keys; `prompt_user` answers come from `inputs`."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "cfg.yaml"
        self.db_path = self.root / "t.db"
        with open("config.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["app"]["db_path"] = str(self.db_path)
        data["app"]["log_file"] = str(self.root / "t.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["browser"]["accounts"] = []
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()


    def accounts(self) -> dict[str, AccountRecord]:
        return {a.name: a for a in load_accounts(self.config_path)}

    def query(self, sql: str, *params) -> tuple:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def set_stage(self, name: str, hours_old: float, **columns) -> None:
        """Make an imported account authenticated `hours_old` hours ago."""
        stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat()
        columns = {"auth_status": "authenticated", "authenticated_at": stamp, **columns}
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                f"UPDATE accounts SET {', '.join(f'{c} = ?' for c in columns)} WHERE name = ?", (*columns.values(), name)
            )
            conn.commit()
        finally:
            conn.close()


class TestImportFlow(MenuCase):


    def test_cli_file_import_reports_utf16_too(self):
        from youtube_bot import import_accounts_file_cli

        path = self.root / "accounts16.txt"
        path.write_text(good_line(1) + "\n", encoding="utf-16")
        err = io.StringIO()
        with patch("sys.stderr", err):
            self.assertFalse(asyncio.run(import_accounts_file_cli(str(path), str(self.config_path))))
        self.assertIn("not UTF-8", err.getvalue())


class TestWarmupChoice(MenuCase):
    def added(self, n: int = 3) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=f"user{i}@example.com") for i in range(1, n + 1)]


class TestWarmupRun(MenuCase):
    """The shared warm-up path of the CLI with the browser replaced by a fake warmer."""

    def setUp(self):
        super().setUp()
        _setup_register(self.config_path, [good_line(1), good_line(2), good_line(3)])
        self.set_stage("user1@example.com", 2)  # quarantine
        self.set_stage("user2@example.com", 30)  # aging
        self.set_stage("user3@example.com", 100)  # mature: no warm-up needed
        self.warmed: list[str] = []

        async def warm_up(account, duration_seconds=45.0, target_video_url=None, db=None):
            self.warmed.append(account.name)
            return SimpleNamespace(account_name=account.name, success=True, details="fake")

        self.warmer = MagicMock()
        self.warmer.warm_up = warm_up
        self.patches = [
            patch("youtube_bot.AccountWarmer", return_value=self.warmer),
            patch("youtube_bot._pause_between_accounts", return_value=0),
        ]
        mocks = []
        for p in self.patches:
            mocks.append(p.start())
            self.addCleanup(p.stop)
        self.pause = mocks[1]

    def warm_selected(self, names: list[str]):
        from youtube_bot import warm_selected_cli

        return asyncio.run(warm_selected_cli(str(self.config_path), names, report=lambda line: None))


    def test_selected_accounts_only(self):
        import asyncio
        from youtube_bot import warm_selected_cli

        results = asyncio.run(warm_selected_cli(str(self.config_path), ["user2@example.com"], report=lambda line: None))
        self.assertEqual(self.warmed, ["user2@example.com"])
        self.assertEqual([r.success for r in results], [True])

    def test_selected_unauthenticated_signs_in_and_mature_is_skipped(self):
        import asyncio
        from youtube_bot import warm_selected_cli

        self.set_stage("user1@example.com", 0, auth_status="pending", authenticated_at=None)
        auth = AsyncMock(return_value=True)
        with patch("youtube_bot._authenticate_account", new=auth):
            results = asyncio.run(
                warm_selected_cli(str(self.config_path), ["user1@example.com", "user3@example.com"], report=lambda line: None)
            )
        auth.assert_awaited_once()
        self.assertEqual(self.warmed, [])  # the intro warm-up belongs to the sign-in itself
        self.assertEqual([(r.account_name, r.success) for r in results], [("user1@example.com", True), ("user3@example.com", True)])
        self.assertIn("skipped", results[1].details)

    def test_unknown_account_and_failed_sign_in_are_reported(self):
        self.set_stage("user1@example.com", 0, auth_status="pending", authenticated_at=None)
        with patch("youtube_bot._authenticate_account", new=AsyncMock(return_value=False)):
            results = self.warm_selected(["ghost@example.com", "user1@example.com"])
        self.assertEqual([(r.account_name, r.success, r.details) for r in results], [
            ("ghost@example.com", False, "account not found"), ("user1@example.com", False, "sign-in failed"),
        ])

    def test_successful_sign_in_is_not_claimed_to_include_a_warm_up(self):
        self.set_stage("user1@example.com", 0, auth_status="pending", authenticated_at=None)
        with patch("youtube_bot._authenticate_account", new=AsyncMock(return_value=True)):
            results = self.warm_selected(["user1@example.com"])
        self.assertEqual(results[0].details, "signed in")

    def test_pause_only_between_accounts_that_really_ran(self):
        self.warm_selected(["user3@example.com", "user1@example.com"])  # mature (skipped) first: nothing to wait for
        self.pause.assert_not_called()
        self.assertEqual(self.warmed, ["user1@example.com"])
        self.warm_selected(["user1@example.com", "user2@example.com"])
        self.pause.assert_called_once()
        self.pause.reset_mock()
        self.warm_selected(["user1@example.com", "user3@example.com", "user2@example.com"])  # a skip does not reset the wait
        self.pause.assert_called_once()


if __name__ == "__main__":
    unittest.main()
