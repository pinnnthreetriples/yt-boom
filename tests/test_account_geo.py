"""Ожидаемая страна и локаль аккаунта: БД, формат импорта, проверка перед входом, локаль браузера и heartbeat (без сети)."""
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import youtube_bot
from config import BrowserSettings, TargetSettings
from database import UNSET, AccountRecord, Database
from modules import geo_guard
from modules.account_parser import parse_account_line
from modules.google_authenticator import GoogleAuthError
from modules.multi_account_orchestrator import MultiAccountOrchestrator
from modules.session_heartbeat import check_account_heartbeat
from modules.youtube_runner import YouTubeRunner

SECRET = "JBSWY3DPEHPK3PXP"


def _target() -> TargetSettings:
    return TargetSettings(
        name="youtube", base_url="https://www.youtube.com", submit_input_selector="i",
        submit_button_selector="b", confirmation_selector="c",
    )


def _browser(root: Path, **extra) -> BrowserSettings:
    return BrowserSettings(session_root=root / "sessions", targets=[_target()], **extra)


def _cfg(root: Path) -> MagicMock:
    cfg = MagicMock()
    cfg.browser = _browser(root)
    cfg.app.db_path = root / "metrics.db"
    return cfg


class _DbCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        geo_guard._EXIT_CACHE.clear()
        geo_guard._warned.clear()


class TestGeoColumns(_DbCase):
    async def test_migration_adds_the_columns_to_an_existing_database_and_keeps_rows(self):
        path = self.root / "old.db"
        old = sqlite3.connect(path)
        old.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, session_path TEXT NOT NULL,
                proxy_url TEXT, is_active INTEGER NOT NULL DEFAULT 1, total_replies INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, last_used_at TEXT
            );
            INSERT INTO accounts (name, session_path, proxy_url, created_at) VALUES ('legacy', 's', 'h:1', 'now');
            """
        )
        old.commit()
        old.close()
        async with Database(path) as db:
            pass
        async with Database(path) as db:  # idempotent
            accounts = await db.list_accounts()
        self.assertEqual([(a.name, a.proxy_url, a.expected_country, a.locale) for a in accounts], [("legacy", "h:1", None, None)])
        check = sqlite3.connect(path)
        try:
            columns = {row[1] for row in check.execute("PRAGMA table_info(accounts)")}
        finally:
            check.close()
        self.assertTrue({"expected_country", "locale"} <= columns)

    async def test_upsert_stores_normalises_and_keeps_unset_values(self):
        async with Database(self.root / "m.db") as db:
            created = await db.upsert_account("a", "s", expected_country=" pl ", locale=" pl-PL ")
            self.assertEqual((created.expected_country, created.locale), ("PL", "pl-PL"))
            kept = await db.upsert_account("a", proxy_url="h:1")  # UNSET keeps both
            self.assertEqual((kept.expected_country, kept.locale), ("PL", "pl-PL"))
            changed = await db.upsert_account("a", expected_country="DE", locale=UNSET)
            self.assertEqual((changed.expected_country, changed.locale), ("DE", "pl-PL"))
            cleared = await db.upsert_account("a", expected_country=None, locale=None)
            self.assertEqual((cleared.expected_country, cleared.locale), (None, None))

    async def test_changing_expected_country_or_locale_does_not_reset_the_login(self):
        async with Database(self.root / "m.db") as db:
            account = await db.upsert_account("a", "s", email="a@b.c", password="pw")
            await db.set_account_auth_status(account.id, "authenticated")
            again = await db.upsert_account("a", expected_country="PL", locale="pl-PL")
        self.assertEqual(again.auth_status, "authenticated")


class TestImportLineFormat(unittest.TestCase):
    def test_line_without_the_new_fields_is_unchanged(self):
        parsed = parse_account_line(f"bob@gmail.com:pw:{SECRET}:1.2.3.4:8080")
        self.assertEqual((parsed.proxy, parsed.expected_country, parsed.locale), ("1.2.3.4:8080", None, None))

    def test_trailing_country_and_locale_tokens(self):
        for tail, proxy in (("", None), (":socks5://u:p@1.2.3.4:1080", "socks5://u:p@1.2.3.4:1080"), (":1.2.3.4:8080:u:p", "1.2.3.4:8080:u:p")):
            with self.subTest(tail=tail):
                parsed = parse_account_line(f"bob@gmail.com:pw:{SECRET}{tail}:country=Poland:locale=pl-pl")
                self.assertEqual((parsed.proxy, parsed.expected_country, parsed.locale), (proxy, "PL", "pl-PL"))
                self.assertEqual(parsed.password, "pw")
        only_country = parse_account_line(f"bob@gmail.com:pw:{SECRET}:country=PL")
        self.assertEqual((only_country.expected_country, only_country.locale), ("PL", None))

    def test_keyword_defaults_apply_and_the_line_wins(self):
        parsed = parse_account_line(f"bob@gmail.com:pw:{SECRET}", country="Germany", locale="de_DE")
        self.assertEqual((parsed.expected_country, parsed.locale), ("DE", "de-DE"))
        parsed = parse_account_line(f"bob@gmail.com:pw:{SECRET}:country=PL", country="DE", locale="de-DE")
        self.assertEqual((parsed.expected_country, parsed.locale), ("PL", "de-DE"))

    def test_garbage_is_rejected_with_a_clear_message_and_no_secrets(self):
        for tail, word in ((":country=Atlantis", "country"), (":locale=polish", "locale"), (":locale=xx-YY", "locale")):
            with self.subTest(tail=tail):
                with self.assertRaises(ValueError) as caught:
                    parse_account_line(f"bob@gmail.com:SECRETPW:{SECRET}{tail}")
                self.assertIn(word, str(caught.exception))
                self.assertNotIn("SECRETPW", str(caught.exception))
        with self.assertRaises(ValueError):
            parse_account_line(f"bob@gmail.com:pw:{SECRET}", country="Atlantis")


class TestAuthenticateGuard(_DbCase):
    async def _run(self, *, expected, exit_country, proxy="socks5://u:p@1.2.3.4:1080", auth_status=None, session_owner=None):
        cfg = _cfg(self.root)
        session = self.root / "sessions" / "bob"
        session.mkdir(parents=True, exist_ok=True)
        async with Database(self.root / "metrics.db") as db:
            account = await db.upsert_account(
                "bob", str(session), proxy_url=proxy, email="bob@gmail.com", password="pw",
                totp_secret=SECRET, expected_country=expected,
            )
            if auth_status:
                await db.set_account_auth_status(account.id, auth_status)
                account = await db.get_account("bob")
            runner = MagicMock(start=AsyncMock(), close=AsyncMock(), context=MagicMock())
            authenticator = MagicMock()
            authenticator.return_value.authenticate = AsyncMock(return_value=True)
            authenticator.return_value.detected_country = None
            resolve = AsyncMock(return_value=exit_country)
            with patch("youtube_bot.YouTubeRunner", return_value=runner) as runner_cls, \
                 patch("youtube_bot.GoogleAuthenticator", authenticator), \
                 patch("youtube_bot._intro_warm_up", AsyncMock()), \
                 patch("modules.geo_guard.resolve_exit_country", resolve), \
                 patch("youtube_bot.read_session_owner", return_value=session_owner), \
                 patch("youtube_bot.write_session_owner"):
                result = await youtube_bot._authenticate_account(db, cfg, account)
            stored = await db.get_account("bob")
        return result, stored, runner_cls, authenticator, resolve

    async def test_mismatch_aborts_before_the_login_and_does_not_start_quarantine(self):
        result, stored, runner_cls, authenticator, _ = await self._run(expected="PL", exit_country="US")
        self.assertFalse(result)
        runner_cls.assert_not_called()
        authenticator.assert_not_called()
        self.assertEqual(stored.auth_status, "pending")
        self.assertIsNone(stored.authenticated_at)

    async def test_unknown_exit_country_also_aborts(self):
        result, stored, runner_cls, _, _ = await self._run(expected="PL", exit_country=None)
        self.assertFalse(result)
        runner_cls.assert_not_called()
        self.assertEqual(stored.auth_status, "pending")

    async def test_abort_message_names_expected_and_actual_country(self):
        from loguru import logger

        messages: list[str] = []
        sink = logger.add(lambda m: messages.append(str(m)), level="ERROR", format="{message}")
        try:
            await self._run(expected="PL", exit_country="US")
            await self._run(expected="PL", exit_country=None)
        finally:
            logger.remove(sink)
        joined = "\n".join(messages)
        self.assertIn("PL", joined)
        self.assertIn("US", joined)
        self.assertIn("не удалось определить", joined)

    async def test_matching_country_proceeds_to_the_login(self):
        result, stored, runner_cls, _, resolve = await self._run(expected="PL", exit_country="PL")
        self.assertTrue(result)
        runner_cls.assert_called_once()
        resolve.assert_awaited_once_with("socks5://u:p@1.2.3.4:1080")
        self.assertEqual(stored.auth_status, "authenticated")

    async def test_no_expected_country_behaves_as_before_without_any_lookup(self):
        result, stored, runner_cls, _, resolve = await self._run(expected=None, exit_country="US", proxy=None)
        self.assertTrue(result)
        resolve.assert_not_awaited()
        self.assertEqual(stored.auth_status, "authenticated")

    async def test_already_authenticated_account_only_warns(self):
        from loguru import logger

        messages: list[str] = []
        sink = logger.add(lambda m: messages.append(str(m)), level="WARNING", format="{message}")
        try:
            result, _, runner_cls, _, _ = await self._run(expected="PL", exit_country="US", auth_status="authenticated")
        finally:
            logger.remove(sink)
        self.assertTrue(result)
        runner_cls.assert_called_once()
        self.assertTrue(any("PL" in m and "US" in m for m in messages))

    async def test_foreign_session_forces_the_guard_even_when_authenticated(self):
        result, _, runner_cls, _, _ = await self._run(
            expected="PL", exit_country="US", auth_status="authenticated", session_owner="other@gmail.com"
        )
        self.assertFalse(result)
        runner_cls.assert_not_called()

    async def test_post_login_country_is_stored_as_iso_and_disagreement_warns(self):
        from loguru import logger

        cfg = _cfg(self.root)
        session = self.root / "sessions" / "bob"
        session.mkdir(parents=True, exist_ok=True)
        messages: list[str] = []
        sink = logger.add(lambda m: messages.append(str(m)), level="WARNING", format="{message}")
        try:
            async with Database(self.root / "metrics.db") as db:
                account = await db.upsert_account(
                    "bob", str(session), proxy_url="socks5://u:p@1.2.3.4:1080", email="bob@gmail.com",
                    password="pw", totp_secret=SECRET, expected_country="PL",
                )
                authenticator = MagicMock()
                authenticator.return_value.authenticate = AsyncMock(return_value=True)
                authenticator.return_value.detected_country = "United States"
                runner = MagicMock(start=AsyncMock(), close=AsyncMock(), context=MagicMock())
                with patch("youtube_bot.YouTubeRunner", return_value=runner), \
                     patch("youtube_bot.GoogleAuthenticator", authenticator), \
                     patch("youtube_bot._intro_warm_up", AsyncMock()), \
                     patch("modules.geo_guard.resolve_exit_country", AsyncMock(return_value="PL")), \
                     patch("youtube_bot.write_session_owner"):
                    self.assertTrue(await youtube_bot._authenticate_account(db, cfg, account))
                stored = await db.get_account("bob")
        finally:
            logger.remove(sink)
        self.assertEqual(stored.geo_country, "US")
        joined = "\n".join(messages)
        self.assertIn("Country version", joined)
        self.assertIn("US", joined)
        self.assertEqual(stored.auth_status, "authenticated")  # never blocks after the login

    async def test_unrecognised_post_login_country_keeps_the_raw_text(self):
        cfg = _cfg(self.root)
        session = self.root / "sessions" / "bob"
        session.mkdir(parents=True, exist_ok=True)
        async with Database(self.root / "metrics.db") as db:
            account = await db.upsert_account("bob", str(session), email="bob@gmail.com", password="pw", totp_secret=SECRET)
            authenticator = MagicMock()
            authenticator.return_value.authenticate = AsyncMock(return_value=True)
            authenticator.return_value.detected_country = "Atlantis"
            runner = MagicMock(start=AsyncMock(), close=AsyncMock(), context=MagicMock())
            with patch("youtube_bot.YouTubeRunner", return_value=runner), \
                 patch("youtube_bot.GoogleAuthenticator", authenticator), \
                 patch("youtube_bot._intro_warm_up", AsyncMock()), \
                 patch("youtube_bot.write_session_owner"):
                await youtube_bot._authenticate_account(db, cfg, account)
            self.assertEqual((await db.get_account("bob")).geo_country, "Atlantis")

    async def test_login_failure_still_marks_failed_after_a_passed_guard(self):
        cfg = _cfg(self.root)
        session = self.root / "sessions" / "bob"
        session.mkdir(parents=True, exist_ok=True)
        async with Database(self.root / "metrics.db") as db:
            account = await db.upsert_account(
                "bob", str(session), email="bob@gmail.com", password="pw", totp_secret=SECRET, expected_country="PL"
            )
            authenticator = MagicMock()
            authenticator.return_value.authenticate = AsyncMock(side_effect=GoogleAuthError("неверный пароль"))
            runner = MagicMock(start=AsyncMock(), close=AsyncMock(), context=MagicMock())
            with patch("youtube_bot.YouTubeRunner", return_value=runner), \
                 patch("youtube_bot.GoogleAuthenticator", authenticator), \
                 patch("modules.geo_guard.resolve_exit_country", AsyncMock(return_value="PL")):
                self.assertFalse(await youtube_bot._authenticate_account(db, cfg, account))
            self.assertEqual((await db.get_account("bob")).auth_status, "failed")


class TestInteractiveLoginGuard(_DbCase):
    async def test_mismatch_aborts_before_the_browser_opens(self):
        cfg = _cfg(self.root)
        async with Database(cfg.app.db_path) as db:
            await db.upsert_account("bob", str(self.root / "s"), proxy_url="socks5://u:p@1.2.3.4:1080", expected_country="PL")
        with patch("youtube_bot.load_config", return_value=cfg), \
             patch("youtube_bot.YouTubeRunner") as runner_cls, \
             patch("modules.geo_guard.resolve_exit_country", AsyncMock(return_value="US")):
            await youtube_bot.interactive_login("unused.yaml", account_name="bob")
        runner_cls.assert_not_called()
        async with Database(cfg.app.db_path) as db:
            self.assertEqual((await db.get_account("bob")).auth_status, "pending")


class TestWarnOnce(_DbCase):
    async def test_warns_once_per_account_and_never_for_unset_expectation(self):
        from loguru import logger

        messages: list[str] = []
        sink = logger.add(lambda m: messages.append(str(m)), level="WARNING", format="{message}")
        resolve = AsyncMock(return_value="US")
        try:
            with patch("modules.geo_guard.resolve_exit_country", resolve):
                for _ in range(3):
                    await geo_guard.warn_if_exit_mismatch("bob", "PL", "socks5://h:1")
                await geo_guard.warn_if_exit_mismatch("kiri", None, None)
        finally:
            logger.remove(sink)
        self.assertEqual(len(messages), 1)
        resolve.assert_awaited_once()

    async def test_orchestrator_warns_when_it_creates_a_runner_for_an_account_with_an_expected_country(self):
        orchestrator = MagicMock(_runners={}, _concurrency=2, create_runner=MagicMock())
        orchestrator._cfg.browser = _browser(self.root)
        account = AccountRecord(
            id=1, name="a", session_path="s", created_at="now", proxy_url="socks5://u:p@1.2.3.4:1080", expected_country="PL"
        )
        with patch("modules.geo_guard.resolve_exit_country", AsyncMock(return_value="US")) as resolve:
            await MultiAccountOrchestrator._get_runner(orchestrator, account)
        resolve.assert_awaited_once_with("socks5://u:p@1.2.3.4:1080")
        orchestrator.create_runner.assert_called_once_with(account)
        plain = AccountRecord(id=2, name="b", session_path="s", created_at="now")
        with patch("modules.geo_guard.resolve_exit_country", AsyncMock()) as resolve:
            await MultiAccountOrchestrator._get_runner(orchestrator, plain)
        resolve.assert_not_awaited()


class TestLocaleOverride(_DbCase):
    def _runner(self, **kwargs) -> YouTubeRunner:
        return YouTubeRunner(_browser(self.root, locale="en-US"), _target(), session_dir=self.root / "p", **kwargs)

    def _options(self, runner: YouTubeRunner) -> dict:
        with patch("modules.youtube_runner.get_or_create_fingerprint", return_value={"pinned": True}):
            return runner._launch_options()

    def test_account_locale_reaches_the_launch_options(self):
        self.assertEqual(self._options(self._runner(locale="pl-PL"))["locale"], "pl-PL")

    def test_without_an_account_locale_the_global_one_is_used(self):
        self.assertEqual(self._options(self._runner())["locale"], "en-US")
        self.assertEqual(self._options(self._runner(locale=None))["locale"], "en-US")

    def test_geoip_stays_on(self):
        self.assertTrue(self._options(self._runner(locale="pl-PL"))["geoip"])

    def test_orchestrator_passes_the_account_locale(self):
        orchestrator = MagicMock()
        orchestrator._runner_factory = None
        orchestrator._cfg.browser = _browser(self.root)
        orchestrator._yt_target = _target()
        account = AccountRecord(id=1, name="a", session_path=str(self.root / "a"), created_at="now", locale="pl-PL")
        runner = MultiAccountOrchestrator.create_runner(orchestrator, account)
        self.assertEqual(self._options(runner)["locale"], "pl-PL")

    async def test_heartbeat_uses_the_account_locale_for_accept_language(self):
        account = AccountRecord(id=1, name="a", session_path=str(self.root / "a"), created_at="now", locale="pl-PL")
        with patch("modules.session_heartbeat.check_session_heartbeat", new_callable=AsyncMock) as check:
            await check_account_heartbeat(account, locale="en-US")
        self.assertEqual(check.await_args.kwargs["locale"], "pl-PL")
        plain = AccountRecord(id=2, name="b", session_path=str(self.root / "b"), created_at="now")
        with patch("modules.session_heartbeat.check_session_heartbeat", new_callable=AsyncMock) as check:
            await check_account_heartbeat(plain, locale="en-US")
        self.assertEqual(check.await_args.kwargs["locale"], "en-US")


class TestInputSurface(_DbCase):
    async def test_import_file_stores_the_new_fields_and_keeps_old_lines_working(self):
        file = self.root / "accounts.txt"
        file.write_text(
            f"a@gmail.com:pw:{SECRET}:socks5://u:p@1.2.3.4:1080:country=Poland:locale=pl-PL\n"
            f"b@gmail.com:pw:{SECRET}:1.2.3.4:8080\n",
            encoding="utf-8",
        )
        with patch("youtube_bot.load_config", return_value=_cfg(self.root)), \
             patch("youtube_bot._authenticate_account", AsyncMock(return_value=True)), \
             patch("youtube_bot.asyncio.sleep", AsyncMock()):
            self.assertTrue(await youtube_bot.import_accounts_file_cli(str(file)))
        async with Database(self.root / "metrics.db") as db:
            a, b = await db.get_account("a@gmail.com"), await db.get_account("b@gmail.com")
        self.assertEqual((a.expected_country, a.locale, a.proxy_url), ("PL", "pl-PL", "socks5://u:p@1.2.3.4:1080"))
        self.assertEqual((b.expected_country, b.locale), (None, None))

    async def test_reimport_without_the_fields_keeps_them(self):
        file = self.root / "accounts.txt"
        file.write_text(f"a@gmail.com:pw:{SECRET}:country=PL:locale=pl-PL\n", encoding="utf-8")
        with patch("youtube_bot.load_config", return_value=_cfg(self.root)), \
             patch("youtube_bot._authenticate_account", AsyncMock(return_value=True)):
            await youtube_bot.import_accounts_file_cli(str(file))
            file.write_text(f"a@gmail.com:pw:{SECRET}\n", encoding="utf-8")
            await youtube_bot.import_accounts_file_cli(str(file))
        async with Database(self.root / "metrics.db") as db:
            a = await db.get_account("a@gmail.com")
        self.assertEqual((a.expected_country, a.locale), ("PL", "pl-PL"))

    async def test_invalid_line_is_reported_without_the_password_and_the_rest_goes_on(self):
        file = self.root / "accounts.txt"
        file.write_text(f"a@gmail.com:SECRETPW:{SECRET}:country=Atlantis\nb@gmail.com:pw:{SECRET}\n", encoding="utf-8")
        errors = io.StringIO()
        with patch("youtube_bot.load_config", return_value=_cfg(self.root)), \
             patch("youtube_bot._authenticate_account", AsyncMock(return_value=True)), \
             contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(await youtube_bot.import_accounts_file_cli(str(file)))
        self.assertIn("line 1", errors.getvalue())
        self.assertNotIn("SECRETPW", errors.getvalue())
        async with Database(self.root / "metrics.db") as db:
            self.assertIsNotNone(await db.get_account("b@gmail.com"))
            self.assertIsNone(await db.get_account("a@gmail.com"))

    async def test_add_account_flags_set_country_and_locale_of_a_plain_account(self):
        with patch("youtube_bot.load_config", return_value=_cfg(self.root)):
            await youtube_bot.add_account_cli("kiri", session_path="s", country="Poland", locale="pl_pl")
        async with Database(self.root / "metrics.db") as db:
            kiri = await db.get_account("kiri")
        self.assertEqual((kiri.expected_country, kiri.locale), ("PL", "pl-PL"))

    async def test_add_account_flags_reject_garbage_before_touching_the_database(self):
        for kwargs in ({"country": "Atlantis"}, {"locale": "polish"}):
            with self.subTest(kwargs=kwargs), patch("youtube_bot.load_config") as load,                  contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                await youtube_bot.add_account_cli("kiri", **kwargs)
            self.assertEqual(caught.exception.code, 1)
            load.assert_not_called()

    def test_flags_reach_the_handlers(self):
        for spec, target in ((f"a@gmail.com:pw:{SECRET}", "add_credentials_account_cli"), ("plain", "add_account_cli")):
            with self.subTest(target=target), \
             patch("sys.argv", ["youtube_bot.py", "--add-account", spec, "--country", "PL", "--locale", "pl-PL"]), \
             patch(f"youtube_bot.{target}", new_callable=AsyncMock, return_value=True) as handler,                  patch("youtube_bot.bind_to_kill_on_close_job"), patch("youtube_bot.setup_cli_logging"):
                try:
                    youtube_bot.main()
                except SystemExit:
                    pass
            self.assertEqual((handler.await_args.kwargs["country"], handler.await_args.kwargs["locale"]), ("PL", "pl-PL"))


class TestListAccounts(_DbCase):
    async def test_table_shows_expected_country_and_locale(self):
        cfg = _cfg(self.root)
        async with Database(cfg.app.db_path) as db:
            await db.upsert_account("bob", "sess", expected_country="PL", locale="pl-PL")
            await db.upsert_account("kiri", "sess2")
        out = io.StringIO()
        with patch("youtube_bot.load_config", return_value=cfg), contextlib.redirect_stdout(out):
            await youtube_bot.print_accounts_table()
        printed = out.getvalue()
        self.assertIn("pl-PL", printed)
        bob_row = next(line for line in printed.splitlines() if line.startswith(("1 ", "1  ")) and "bob" in line)
        self.assertIn("PL", bob_row)


if __name__ == "__main__":
    unittest.main()
