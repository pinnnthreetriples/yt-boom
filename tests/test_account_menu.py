"""Wizard «Аккаунты»: import (file / paste), warm-up pick, warm-up of the chosen accounts, lifecycle table.

Pure logic is tested without a terminal; the screens run on scripted keys with a temporary DB. The browser
(AccountWarmer / sign-in) is always mocked. Credentials in this file are fake.
"""
from __future__ import annotations

import asyncio
import io
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from loguru import logger

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
    WizardBack,
    _read_secret_line,
    _setup_import,
    _setup_lifecycle,
    _setup_pick_warmup,
    _setup_warmup_all,
    load_accounts,
    step_account,
)
from tests.tui_keys import ENTER, scripted_keys

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

    def test_error_never_echoes_the_input(self):
        pasted = f"user1@example.com:{PASSWORDS[0]}:{TOTP}"  # leftovers of a paste can land in this prompt
        for text in (pasted, f"1 {pasted}"):
            with self.subTest(text=text), self.assertRaises(ValueError) as caught:
                parse_warm_selection(text, 3)
            for secret in (PASSWORDS[0], TOTP, "user1@example.com"):
                self.assertNotIn(secret, str(caught.exception))


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

    def run_keys(self, func, *keys: str, inputs: list | None = None, paste: list[str] | None = None):
        out = io.StringIO()
        prompt = {"side_effect": inputs} if inputs is not None else {"return_value": ""}
        secret = {"side_effect": paste} if paste is not None else {"side_effect": EOFError}
        with scripted_keys(*keys) as screen, patch("modules.wizard.prompt_user", **prompt), \
             patch("modules.wizard._read_secret_line", **secret), patch("sys.stdout", out), \
             patch("modules.wizard._pause"):
            result = func()
        return result, screen.getvalue() + out.getvalue()

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
    def paste(self, lines: list[str], *answers: str):
        """«Импорт» → «Вставить строки» (key 2) → the lines, an empty line ends the paste → warm-up answers."""
        return self.run_keys(
            lambda: _setup_import(self.config_path), "2", paste=[*lines, ""], inputs=list(answers),
        )

    def test_paste_adds_valid_skips_duplicates_and_reports_bad_lines(self):
        lines = [
            good_line(1),
            "# comment line",
            "no-separator-here",
            f"user2@example.com:{PASSWORDS[1]}:not-base32!",
            good_line(1),  # the same account twice
            good_line(3),
        ]
        with patch("youtube_bot.warm_selected_cli", new=AsyncMock(return_value=[])) as warm:
            notice, output = self.paste(lines, "n")
        self.assertEqual(sorted(a for a in self.accounts() if a.startswith("user")), ["user1@example.com", "user3@example.com"])
        self.assertEqual(notice, ("ok", "Импорт: добавлено 2 из 5"))
        self.assertIn("Добавлено: 2 · пропущено: 1 · ошибок: 2", output)
        self.assertIn("строка 5: пропущена — аккаунт уже есть", output)
        self.assertRegex(output, r"строка 3: ошибка")
        self.assertRegex(output, r"строка 4: ошибка")
        warm.assert_not_called()

    def test_secrets_never_reach_the_output(self):
        lines = [good_line(1), good_line(2), f"user3@example.com:{PASSWORDS[2]}:BAD!", "garbage-with-pw-fake-9"]
        logged, err = io.StringIO(), io.StringIO()
        sink = logger.add(logged, level="DEBUG")
        try:
            with patch("youtube_bot.warm_selected_cli", new=AsyncMock(return_value=[])), patch("sys.stderr", err):
                _, output = self.paste(lines, "n")
        finally:
            logger.remove(sink)
        for secret in (*PASSWORDS, TOTP, "pw-fake-9", "BAD!"):
            for where, text in (("output", output), ("loguru", logged.getvalue()), ("stderr", err.getvalue())):
                self.assertNotIn(secret, text, where)

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI")
    def test_stored_credentials_are_encrypted_not_raw(self):
        with patch("youtube_bot.warm_selected_cli", new=AsyncMock(return_value=[])):
            self.paste([good_line(1)], "n")
        stored = self.query("SELECT password, totp_secret FROM accounts WHERE name = 'user1@example.com'")
        self.assertTrue(all(value and PASSWORDS[0] not in value and TOTP not in value for value in stored))

    def test_file_mode(self):
        path = self.root / "accounts.txt"
        path.write_text("\ufeff# my accounts\n\n" + good_line(1) + "\nbroken\n" + good_line(2) + "\n", encoding="utf-8")
        with patch("youtube_bot.warm_selected_cli", new=AsyncMock(return_value=[])):
            notice, output = self.run_keys(lambda: _setup_import(self.config_path), "1", inputs=[str(path), "n"])
        self.assertEqual(notice, ("ok", "Импорт: добавлено 2 из 3"))
        self.assertIn("строка 4: ошибка", output)
        self.assertEqual(len([a for a in self.accounts() if a.startswith("user")]), 2)

    def test_missing_file_is_reported_not_raised(self):
        result, output = self.run_keys(lambda: _setup_import(self.config_path), "1", inputs=[str(self.root / "nope.txt")])
        self.assertIsNone(result)
        self.assertIn("Не удалось прочитать файл", output)

    def test_renamed_account_is_recognised_by_email_on_reimport(self):
        with patch("youtube_bot.warm_selected_cli", new=AsyncMock(return_value=[])):
            self.paste([good_line(1)], "n")
            _pools_rename(self.config_path, "user1@example.com", "nick")
            _, output = self.paste([good_line(1)], "n")
        self.assertEqual(sorted(a for a in self.accounts() if a.startswith(("user", "nick"))), ["nick"])
        self.assertIn("строка 1: пропущена — аккаунт уже есть", output)

    def test_utf16_file_is_reported_not_raised(self):
        path = self.root / "accounts16.txt"
        path.write_text(good_line(1) + "\n", encoding="utf-16")  # what `>` in Windows PowerShell 5 writes
        result, output = self.run_keys(lambda: _setup_import(self.config_path), "1", inputs=[str(path)])
        self.assertIsNone(result)
        self.assertIn("UTF-8", output)
        self.assertNotIn(PASSWORDS[0], output)

    def test_cli_file_import_reports_utf16_too(self):
        from youtube_bot import import_accounts_file_cli

        path = self.root / "accounts16.txt"
        path.write_text(good_line(1) + "\n", encoding="utf-16")
        err = io.StringIO()
        with patch("sys.stderr", err):
            self.assertFalse(asyncio.run(import_accounts_file_cli(str(path), str(self.config_path))))
        self.assertIn("not UTF-8", err.getvalue())

    def test_registration_failure_keeps_the_screen_alive_and_hints_at_partial_import(self):
        for error, kind in ((RuntimeError("db is locked"), "err"), (KeyboardInterrupt(), "warn")):
            with self.subTest(error=type(error).__name__), patch("modules.wizard._setup_register", side_effect=error):
                notice, _ = self.paste([good_line(1)], "n")
            self.assertEqual(notice[0], kind)
            self.assertIn("проверьте список", notice[1])
            self.assertNotIn("db is locked", notice[1])

    def test_input_cancel_raises_wizard_back(self):
        for error in (KeyboardInterrupt, EOFError):
            with self.subTest(error=error.__name__), patch("builtins.input", side_effect=error),                  patch("sys.stdout", io.StringIO()), self.assertRaises(WizardBack):
                _read_secret_line(" › ")

    def test_empty_paste_and_cancel(self):
        result, output = self.paste([])
        self.assertIsNone(result)
        self.assertIn("нет строк с аккаунтами", output)
        result, _ = self.run_keys(lambda: _setup_import(self.config_path), "0")
        self.assertIsNone(result)

    def test_existing_account_is_skipped_and_keeps_its_credentials(self):
        with patch("youtube_bot.warm_selected_cli", new=AsyncMock(return_value=[])):
            self.paste([good_line(1)], "n")
            before = self.query("SELECT password FROM accounts WHERE name = 'user1@example.com'")
            _, output = self.paste([f"user1@example.com:other-pw:{TOTP}"], "n")
        after = self.query("SELECT password FROM accounts WHERE name = 'user1@example.com'")
        self.assertEqual(before, after)
        self.assertIn("пропущено: 1", output)


class TestWarmupChoice(MenuCase):
    def added(self, n: int = 3) -> list[SimpleNamespace]:
        return [SimpleNamespace(name=f"user{i}@example.com") for i in range(1, n + 1)]

    def pick(self, *answers: str) -> tuple[list[str], str]:
        return self.run_keys(lambda: _setup_pick_warmup(self.added()), inputs=list(answers))

    def test_shows_numbered_list(self):
        _, output = self.pick("n")
        for number in (1, 2, 3):
            self.assertIn(f"{number}. user{number}@example.com", output)

    def test_all_none_and_chosen(self):
        names = [a.name for a in self.added()]
        self.assertEqual(self.pick("a")[0], names)
        self.assertEqual(self.pick("все")[0], names)
        self.assertEqual(self.pick("")[0], [])  # Enter = nobody (prompt_user default «n» is applied by the real prompt)
        self.assertEqual(self.pick("n")[0], [])
        self.assertEqual(self.pick("1 3")[0], [names[0], names[2]])

    def test_garbage_is_asked_again(self):
        names, output = self.pick("x", "7", "2")
        self.assertEqual(names, ["user2@example.com"])
        self.assertIn("Не распознано", output)
        self.assertIn("вне списка (1–3)", output)

    def test_ctrl_c_means_nobody(self):
        names, _ = self.pick(WizardBack())
        self.assertEqual(names, [])

    def test_import_warms_up_only_the_chosen_accounts(self):
        results = [SimpleNamespace(account_name="user1@example.com", success=True)]
        with patch("youtube_bot.warm_selected_cli", new=AsyncMock(return_value=results)) as warm:
            notice, output = self.run_keys(
                lambda: _setup_import(self.config_path), "2",
                paste=[good_line(1), good_line(2), good_line(3), ""], inputs=["1"],
            )
        warm.assert_awaited_once()
        self.assertEqual(warm.await_args.args[1], ["user1@example.com"])
        self.assertIn("Прогрев: 1 из 1 успешно", output)


class TestWarmupRun(MenuCase):
    """The shared warm-up path of the CLI with the browser replaced by a fake warmer."""

    def setUp(self):
        super().setUp()
        with patch("youtube_bot.warm_selected_cli", new=AsyncMock(return_value=[])):
            self.run_keys(
                lambda: _setup_import(self.config_path), "2", paste=[good_line(1), good_line(2), good_line(3), ""], inputs=["n"],
            )
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

    def test_warm_all_asks_confirmation_and_runs_quarantine_and_aging_only(self):
        result, output = self.run_keys(lambda: _setup_warmup_all(self.config_path), inputs=["y"])
        self.assertEqual(self.warmed, ["user1@example.com", "user2@example.com"])
        self.assertEqual(result, ("ok", "Прогрев: 2 из 2 успешно"))
        self.assertIn("[OK] user1@example.com: fake", output)

    def test_warm_all_declined_runs_nothing(self):
        result, _ = self.run_keys(lambda: _setup_warmup_all(self.config_path), inputs=["n"])
        self.assertEqual(self.warmed, [])
        self.assertEqual(result, ("info", "Отменено"))

    def test_warm_all_without_candidates(self):
        self.set_stage("user1@example.com", 100)
        self.set_stage("user2@example.com", 100)
        result, _ = self.run_keys(lambda: _setup_warmup_all(self.config_path), inputs=[])
        self.assertEqual(result[0], "warn")
        self.assertEqual(self.warmed, [])

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

    def test_unexpected_error_in_warm_up_shows_only_its_class(self):
        with patch("youtube_bot.warm_selected_cli", new=AsyncMock(side_effect=RuntimeError("secret-detail"))):
            from modules.wizard import _setup_run_warmup
            result, _ = self.run_keys(lambda: _setup_run_warmup(self.config_path, ["user1@example.com"]))
        self.assertEqual(result[0], "err")
        self.assertIn("RuntimeError", result[1])
        self.assertNotIn("secret-detail", result[1])

    def test_failed_warmup_is_summarised(self):
        async def failing(account, **kwargs):
            return SimpleNamespace(account_name=account.name, success=False, details="boom")

        self.warmer.warm_up = failing
        result, _ = self.run_keys(lambda: _setup_warmup_all(self.config_path), inputs=["y"])
        self.assertEqual(result[0], "warn")
        self.assertIn("не удалось: user1@example.com, user2@example.com", result[1])


class TestMenuIntegration(MenuCase):
    def test_new_rows_are_in_the_accounts_menu(self):
        _, output = self.run_keys(lambda: step_account(self.config_path, self.root / "sessions"), ENTER)
        for label in ("Импорт аккаунтов", "Прогрев", "Жизненный цикл"):
            self.assertIn(label, output)

    def test_lifecycle_screen_through_hotkey(self):
        with patch("youtube_bot.warm_selected_cli", new=AsyncMock(return_value=[])):
            self.run_keys(lambda: _setup_import(self.config_path), "2", paste=[good_line(1), good_line(2), ""], inputs=["n"])
        reset = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
        self.set_stage("user1@example.com", 3, daily_limit_reset_at=reset)
        self.set_stage("user2@example.com", 100, daily_comments_done=2, consecutive_errors=3, daily_limit_reset_at=reset)
        _, output = self.run_keys(lambda: step_account(self.config_path, self.root / "sessions"), "8", "4")
        self.assertRegex(output, r"Аккаунт +Стадия +Квота +Heartbeat +Пауза")
        self.assertRegex(output, r"user1@example\.com +карантин +0/0")
        self.assertRegex(output, r"user2@example\.com +restricted +2/5 .*breaker ×3")
        self.assertIn("нет · —", output)  # no cookies in the temporary profile
        for secret in (*PASSWORDS, TOTP):
            self.assertNotIn(secret, output)

    def test_lifecycle_table_is_capped_and_marks_inactive(self):
        accounts = [_account(100, id=n, name=f"acc{n:02}", is_active=n != 1) for n in range(1, 18)]
        with patch("modules.wizard._pools_db", return_value=accounts):
            _, output = self.run_keys(lambda: _setup_lifecycle(self.config_path))
        self.assertIn("acc15", output)
        self.assertNotIn("acc16", output)
        self.assertIn("… ещё 2", output)
        self.assertRegex(output, r"acc01 +mature +0/5 .*отключён")

    def test_lifecycle_without_accounts(self):
        with patch("modules.wizard._pools_db", return_value=[]):
            _, output = self.run_keys(lambda: _setup_lifecycle(self.config_path))
        self.assertIn("Аккаунтов нет", output)

    def test_import_row_registers_through_menu(self):
        with patch("youtube_bot.warm_selected_cli", new=AsyncMock(return_value=[])):
            _, output = self.run_keys(
                lambda: step_account(self.config_path, self.root / "sessions"), "5", "2", "4",
                paste=[good_line(1), ""], inputs=["n"],
            )
        self.assertIn("user1@example.com", [a.name for a in load_accounts(self.config_path)])
        self.assertIn("Импорт: добавлено 1 из 1", output)


if __name__ == "__main__":
    unittest.main()
