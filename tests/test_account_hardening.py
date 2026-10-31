"""Parser formats, secrets handling, upsert semantics, CLI flows of the credentials accounts (temp dirs and mocks only)."""
import asyncio
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite

import youtube_bot
from config import AccountSettings, BrowserSettings, TargetSettings, mask_proxy_url, parse_proxy, resolve_account_proxy
from database import UNSET, AccountRecord, Database, Payload
from modules.account_parser import ParsedAccount, parse_account_line
from modules.fingerprint_manager import get_or_create_fingerprint, is_compatible
from modules.google_authenticator import GoogleAuthError, read_session_owner, write_session_owner
from modules.multi_account_orchestrator import MultiAccountOrchestrator
from modules.totp_helper import generate_totp, normalize_secret, validate_totp_secret
from modules.validator import _is_youtube_comment
from modules.wizard import mask_proxy
from modules.youtube_runner import YouTubeRunner
from secret_store import DPAPI_PREFIX, SecretStoreError, protect_secret, reveal_secret

SECRET = "JBSWY3DPEHPK3PXP"


def _line(tail: str = "") -> str:
    return f"bob@gmail.com:pw:{SECRET}{tail}"


class TestTotpSecretValidation(unittest.TestCase):
    def test_invalid_empty_and_foreign_secrets_are_rejected(self):
        for bad in ("", "   ", "not-base32!", "8080", "otpauth://totp/x?secret=ABC", "ABC1"):
            with self.assertRaises(ValueError, msg=bad):
                generate_totp(bad)
            with self.assertRaises(ValueError, msg=bad):
                validate_totp_secret(bad)

    def test_dashes_spaces_case_and_padding_are_normalised(self):
        self.assertEqual(normalize_secret("jbsw-y3dp ehpk 3pxp"), SECRET)
        self.assertEqual(generate_totp("jbsw-y3dp-ehpk-3pxp", at=100), generate_totp(SECRET, at=100))
        self.assertEqual(normalize_secret("JBSWY3DP======"), "JBSWY3DP")

    def test_impossible_length_is_a_value_error(self):
        with self.assertRaises(ValueError):
            generate_totp("JBSWY3DPE")  # 9 Base32 characters cannot be decoded


class TestProxyFormats(unittest.TestCase):
    def test_user_pass_at_host_port_and_bare_host_port(self):
        self.assertEqual(
            parse_proxy("user:pa:ss@1.2.3.4:8080"),
            {"server": "http://1.2.3.4:8080", "username": "user", "password": "pa:ss"},
        )
        self.assertEqual(parse_proxy("localhost:8080"), {"server": "http://localhost:8080"})
        self.assertEqual(parse_proxy("1.2.3.4:8080"), {"server": "http://1.2.3.4:8080"})

    def test_ipv6_stays_in_brackets(self):
        self.assertEqual(parse_proxy("[::1]:8080"), {"server": "http://[::1]:8080"})
        self.assertEqual(
            parse_proxy("http://u:p@[2001:db8::1]:3128"),
            {"server": "http://[2001:db8::1]:3128", "username": "u", "password": "p"},
        )
        self.assertEqual(parse_proxy("[::1]:80:u:p")["server"], "http://[::1]:80")

    def test_raw_format_keeps_special_characters_literally(self):
        self.assertEqual(parse_proxy("1.2.3.4:80:user:pa#ss?x/y")["password"], "pa#ss?x/y")
        self.assertEqual(parse_proxy("1.2.3.4:80:user:pa%41ss")["password"], "pa%41ss")  # no percent-decoding here
        self.assertEqual(parse_proxy("http://u:pa%41ss@h:80")["password"], "paAss")  # URLs are decoded as before

    def test_errors_never_contain_the_proxy_text(self):
        for bad in ("1.2.3.4:abc:user:SECRETPW", "SECRETPW", "http://u:SECRETPW@h:99999", "1.2.3.4:70000:u:SECRETPW", "http://u:SECRETPW@h"):
            with self.assertRaises(ValueError) as caught:
                parse_proxy(bad)
            self.assertNotIn("SECRETPW", str(caught.exception), bad)
            self.assertIsNone(caught.exception.__cause__, bad)

    def test_resolve_account_proxy_prefers_the_account(self):
        self.assertEqual(resolve_account_proxy("a:1", "g:2"), "a:1")
        self.assertEqual(resolve_account_proxy(None, "g:2"), "g:2")
        self.assertEqual(resolve_account_proxy("  ", "g:2"), "g:2")
        self.assertIsNone(resolve_account_proxy(None, None))


class TestMaskProxy(unittest.TestCase):
    def test_every_format_hides_the_password(self):
        for proxy in (
            "1.2.3.4:8080:user:SECRETPW",
            "proxy.io:3128:user:SECRETPW",
            "user:SECRETPW@1.2.3.4:8080",
            "http://user:SECRETPW@1.2.3.4:8080",
            "socks5://user:SECRETPW@[::1]:1080",
            "1.2.3.4:abc:user:SECRETPW",  # unparseable: masked entirely
        ):
            for masked in (mask_proxy(proxy), mask_proxy_url(proxy)):
                self.assertNotIn("SECRETPW", masked, proxy)
        self.assertEqual(mask_proxy("1.2.3.4:8080:user:SECRETPW"), "http://user:***@1.2.3.4:8080")
        self.assertEqual(mask_proxy("1.2.3.4:8080"), "http://1.2.3.4:8080")
        self.assertEqual(mask_proxy(None), "Прямое соединение (без прокси)")


class TestAccountLineFormats(unittest.TestCase):
    def test_proxy_formats_at_the_end_of_the_line(self):
        cases = {
            "user:pass@1.2.3.4:8080": "user:pass@1.2.3.4:8080",
            "localhost:8080": "localhost:8080",
            "1.2.3.4:8080": "1.2.3.4:8080",
            "[::1]:8080": "[::1]:8080",
            "http://u:p@h.io:80": "http://u:p@h.io:80",
        }
        for tail, expected in cases.items():
            account = parse_account_line(_line(f":{tail}"))
            self.assertEqual((account.password, account.totp_secret, account.proxy), ("pw", SECRET, expected), tail)
            parse_proxy(account.proxy)

    def test_recovery_email_before_a_user_pass_at_host_proxy(self):
        account = parse_account_line(_line(":rescue@mail.ru:user:pass@1.2.3.4:8080"))
        self.assertEqual((account.recovery_email, account.proxy), ("rescue@mail.ru", "user:pass@1.2.3.4:8080"))

    def test_a_mis_split_line_is_rejected_instead_of_stored(self):
        for bad in (
            "bob@gmail.com:pw:otpauth://totp/x?secret=ABC",
            "bob@gmail.com:pw:8080",
            "bob@gmail.com:pw:not_a_secret",
            _line(":1.2.3.4:99999"),
        ):
            with self.assertRaises(ValueError, msg=bad):
                parse_account_line(bad)

    def test_secret_is_stored_in_canonical_form(self):
        self.assertEqual(parse_account_line("bob@gmail.com:pw:jbsw y3dp").totp_secret, "JBSWY3DP")

    def test_empty_placeholders_and_trailing_colon(self):
        self.assertEqual(parse_account_line(_line(":")).totp_secret, SECRET)
        account = parse_account_line(_line("::1.2.3.4:8080:u:p"))
        self.assertEqual((account.recovery_email, account.proxy), (None, "1.2.3.4:8080:u:p"))

    def test_password_with_colons_and_at_signs(self):
        account = parse_account_line(f"bob@gmail.com:p:a@ss:{SECRET}")
        self.assertEqual(account.password, "p:a@ss")

    def test_errors_do_not_echo_the_line(self):
        with self.assertRaises(ValueError) as caught:
            parse_account_line("bob@gmail.com:SECRETPW:8080")
        self.assertNotIn("SECRETPW", str(caught.exception))


class TestSecretsAreNotPrinted(unittest.TestCase):
    def test_models_hide_password_and_totp_secret(self):
        record = AccountRecord(id=1, name="a", session_path="s", created_at="now", password="SECRETPW", totp_secret=SECRET)
        settings = AccountSettings(name="a", password="SECRETPW", totp_secret=SECRET)
        parsed = parse_account_line(_line().replace(":pw:", ":SECRETPW:"))
        for obj in (record, settings, parsed):
            for text in (repr(obj), str(obj), str(obj.model_dump()), obj.model_dump_json()):
                self.assertNotIn("SECRETPW", text, type(obj).__name__)
                self.assertNotIn(SECRET, text, type(obj).__name__)
        self.assertEqual((record.password, settings.password, parsed.password), ("SECRETPW",) * 3)  # still readable


class TestSecretStore(unittest.TestCase):
    def test_plain_values_pass_through_and_none_stays_none(self):
        self.assertIsNone(protect_secret(None))
        self.assertIsNone(reveal_secret(None))
        self.assertEqual(reveal_secret("legacy plain text"), "legacy plain text")  # written by an older version

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI")
    def test_dpapi_round_trip(self):
        encrypted = protect_secret("pässw:ord")
        self.assertTrue(encrypted.startswith(DPAPI_PREFIX))
        self.assertNotIn("ssw", encrypted)
        self.assertEqual(reveal_secret(encrypted), "pässw:ord")
        self.assertEqual(protect_secret(encrypted), encrypted)  # already protected

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI")
    def test_damaged_value_is_a_secret_store_error(self):
        for damaged in (DPAPI_PREFIX + "AAAA", DPAPI_PREFIX + "%%%"):
            with self.assertRaises(SecretStoreError):
                reveal_secret(damaged)


class _DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "metrics.db"


class TestUpsertKeepsData(_DbCase):
    async def test_omitted_fields_keep_proxy_active_flag_and_session(self):
        async with Database(self.path) as db:
            await db.upsert_account("bob", "s1", proxy_url="1.2.3.4:80:u:p", is_active=False, email="b@g.c", password="pw")
            again = await db.upsert_account("bob", email="b@g.c", password="pw", totp_secret=SECRET)
            self.assertEqual((again.session_path, again.proxy_url, again.is_active), ("s1", "1.2.3.4:80:u:p", False))
            self.assertEqual(again.totp_secret, SECRET)

    async def test_explicit_values_still_overwrite_and_none_removes_the_proxy(self):
        async with Database(self.path) as db:
            await db.upsert_account("bob", "s1", proxy_url="1.2.3.4:80", is_active=False)
            changed = await db.upsert_account("bob", "s2", proxy_url=None, is_active=True)
            self.assertEqual((changed.session_path, changed.proxy_url, changed.is_active), ("s2", None, True))

    async def test_new_account_defaults_and_session_path_is_required(self):
        async with Database(self.path) as db:
            created = await db.upsert_account("bob", "s1")
            self.assertEqual((created.proxy_url, created.is_active, created.auth_status), (None, True, "pending"))
            with self.assertRaises(ValueError):
                await db.upsert_account("nobody")
            self.assertIs(UNSET, UNSET)

    async def test_status_is_reset_only_by_a_changed_email_password_or_proxy(self):
        async with Database(self.path) as db:
            first = await db.upsert_account("bob", "s", proxy_url="1.2.3.4:80", email="b@g.c", password="pw", totp_secret=SECRET)
            await db.set_account_auth_status(first.id, "authenticated")
            same = await db.upsert_account("bob", "s", proxy_url="1.2.3.4:80", email="b@g.c", password="pw", recovery_email="r@m.ru")
            self.assertEqual(same.auth_status, "authenticated")
            self.assertEqual((await db.upsert_account("bob")).auth_status, "authenticated")
            for change in ({"email": "other@g.c"}, {"password": "new"}, {"proxy_url": "5.6.7.8:80"}, {"proxy_url": None}):
                await db.set_account_auth_status(first.id, "authenticated")
                self.assertEqual((await db.upsert_account("bob", **change)).auth_status, "pending", change)

    async def test_failed_status_can_be_reset_by_new_credentials(self):
        async with Database(self.path) as db:
            created = await db.upsert_account("bob", "s", email="b@g.c", password="pw")
            await db.set_account_auth_status(created.id, "failed")
            self.assertEqual((await db.upsert_account("bob", password="fixed")).auth_status, "pending")

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI")
    async def test_secrets_are_encrypted_in_the_file_and_decrypted_on_read(self):
        async with Database(self.path) as db:
            await db.upsert_account("bob", "s", email="b@g.c", password="SECRETPW", totp_secret=SECRET)
            account = await db.get_account("bob")
            self.assertEqual((account.password, account.totp_secret), ("SECRETPW", SECRET))
        with contextlib.closing(sqlite3.connect(self.path)) as raw_db:
            raw = raw_db.execute("SELECT password, totp_secret FROM accounts").fetchone()
        self.assertTrue(all(value.startswith(DPAPI_PREFIX) for value in raw))
        self.assertNotIn("SECRETPW", "".join(raw))

    async def test_plaintext_values_of_older_versions_stay_readable(self):
        async with Database(self.path) as db:
            await db.upsert_account("bob", "s", email="b@g.c")
        legacy = sqlite3.connect(self.path)
        legacy.execute("UPDATE accounts SET password = 'plain', totp_secret = ?", (SECRET,))
        legacy.commit()
        legacy.close()
        async with Database(self.path) as db:
            account = await db.get_account("bob")
            self.assertEqual((account.password, account.totp_secret), ("plain", SECRET))

    async def test_undecryptable_secret_is_dropped_with_a_warning_not_a_crash(self):
        async with Database(self.path) as db:
            await db.upsert_account("bob", "s", email="b@g.c")
        broken = sqlite3.connect(self.path)
        broken.execute("UPDATE accounts SET password = 'dpapi:AAAA'")
        broken.commit()
        broken.close()
        async with Database(self.path) as db:
            account = await db.get_account("bob")
            self.assertIsNone(account.password)

    async def test_two_connections_starting_together_do_not_fail_with_locked(self):
        async def start(path: Path) -> None:
            db = Database(path)
            await db.connect()
            try:
                await db.init_schema()
            finally:
                await db.close()

        for index in range(8):  # wizard and bot starting at the same moment
            path = self.root / f"race{index}.db"
            results = await asyncio.gather(start(path), start(path), return_exceptions=True)
            self.assertEqual([r for r in results if isinstance(r, Exception)], [])

    async def test_init_schema_retries_while_the_database_is_locked(self):
        db = Database(self.path)
        await db.connect()
        try:
            once = AsyncMock(side_effect=[aiosqlite.OperationalError("database is locked"), None])
            with patch.object(db, "_init_schema_once", once), patch("database.asyncio.sleep", AsyncMock()):
                await db.init_schema()
            self.assertEqual(once.await_count, 2)
            failing = AsyncMock(side_effect=aiosqlite.OperationalError("disk I/O error"))
            with patch.object(db, "_init_schema_once", failing), self.assertRaises(aiosqlite.OperationalError):
                await db.init_schema()
            self.assertEqual(failing.await_count, 1)
        finally:
            await db.close()


def _cfg(root: Path) -> SimpleNamespace:
    browser = BrowserSettings(
        session_root=root / "sessions",
        headless=True,
        targets=[TargetSettings(
            name="youtube", base_url="https://www.youtube.com", submit_input_selector="i",
            submit_button_selector="b", confirmation_selector="c",
        )],
    )
    return SimpleNamespace(app=SimpleNamespace(db_path=root / "metrics.db"), browser=browser)


class TestImportAccountsFile(_DbCase):
    def setUp(self):
        sleeps = patch("youtube_bot.asyncio.sleep", AsyncMock())
        self.sleep = sleeps.start()
        self.addCleanup(sleeps.stop)

    async def _import(self, lines, authenticate, **kwargs):
        file = self.root / "accounts.txt"
        file.write_text("\n".join(lines), encoding="utf-8")
        errors = io.StringIO()
        with patch("youtube_bot.load_config", return_value=_cfg(self.root)), \
             patch("youtube_bot._authenticate_account", authenticate), \
             contextlib.redirect_stderr(errors), contextlib.redirect_stdout(io.StringIO()):
            ok = await youtube_bot.import_accounts_file_cli(str(file), **kwargs)
        return ok, errors.getvalue()

    @staticmethod
    def _good(name: str, tail: str = "") -> str:
        return f"{name}@gmail.com:SECRETPW:{SECRET}{tail}"

    async def test_one_bad_line_and_one_failed_account_do_not_stop_the_rest(self):
        results = {"a@gmail.com": True, "b@gmail.com": False, "c@gmail.com": True}
        authenticate = AsyncMock(side_effect=lambda db, cfg, account: results[account.email])
        ok, errors = await self._import(
            ["# comment", "", self._good("a"), "broken line SECRETPW", self._good("b"), self._good("c")], authenticate
        )
        self.assertFalse(ok)
        self.assertEqual(authenticate.await_count, 3)
        self.assertIn("line 4", errors)
        self.assertNotIn("SECRETPW", errors)
        self.assertEqual(self.sleep.await_count, 2)  # a pause between consecutive sign-ins

    async def test_everything_fine_returns_true(self):
        ok, _ = await self._import([self._good("a")], AsyncMock(return_value=True))
        self.assertTrue(ok)

    async def test_second_import_skips_authenticated_accounts_unless_forced(self):
        async def authenticate(db, cfg, account):
            await db.set_account_auth_status(account.id, "authenticated")
            return True

        first = AsyncMock(side_effect=authenticate)
        await self._import([self._good("a"), self._good("b")], first)
        self.assertEqual(first.await_count, 2)

        second = AsyncMock(side_effect=authenticate)
        ok, _ = await self._import([self._good("a"), self._good("b")], second)
        self.assertTrue(ok)
        second.assert_not_awaited()

        forced = AsyncMock(side_effect=authenticate)
        await self._import([self._good("a"), self._good("b")], forced, force=True)
        self.assertEqual(forced.await_count, 2)

    async def test_changed_password_makes_the_account_sign_in_again(self):
        async def authenticate(db, cfg, account):
            await db.set_account_auth_status(account.id, "authenticated")
            return True

        await self._import([self._good("a")], AsyncMock(side_effect=authenticate))
        again = AsyncMock(side_effect=authenticate)
        await self._import([f"a@gmail.com:NEWPASSWORD:{SECRET}"], again)
        self.assertEqual(again.await_count, 1)

    async def test_reimport_without_proxy_keeps_the_stored_proxy_and_session(self):
        authenticate = AsyncMock(return_value=True)
        await self._import([self._good("a", ":1.2.3.4:8080:u:p")], authenticate)
        async with Database(self.root / "metrics.db") as db:
            await db.upsert_account("a@gmail.com", "custom-session", is_active=False)
        await self._import([self._good("a")], authenticate)
        async with Database(self.root / "metrics.db") as db:
            account = await db.get_account("a@gmail.com")
        self.assertEqual((account.proxy_url, account.session_path, account.is_active), ("1.2.3.4:8080:u:p", "custom-session", False))

    async def test_missing_file_is_an_error_code(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(await youtube_bot.import_accounts_file_cli(str(self.root / "missing.txt")))

    def test_pause_is_longer_for_accounts_without_their_own_proxy(self):
        bare = AccountRecord(id=1, name="a", session_path="s", created_at="now")
        proxied = AccountRecord(id=2, name="b", session_path="s", created_at="now", proxy_url="1.2.3.4:80")
        for _ in range(50):
            self.assertGreaterEqual(youtube_bot._pause_between_accounts(bare), 20.0)
            self.assertLessEqual(youtube_bot._pause_between_accounts(proxied), 15.0)


class TestAuthenticateAccount(_DbCase):
    async def _run(self, authenticate_effect, session_owner: str | None = None):
        cfg = _cfg(self.root)
        session = self.root / "sessions" / "bob"
        session.mkdir(parents=True, exist_ok=True)
        if session_owner:
            write_session_owner(session, session_owner)
        async with Database(self.root / "metrics.db") as db:
            account = await db.upsert_account("bob", str(session), email="bob@gmail.com", password="pw", totp_secret=SECRET)
            runner = MagicMock(start=AsyncMock(), close=AsyncMock(), context=MagicMock())
            authenticator = MagicMock()
            authenticator.return_value.authenticate = AsyncMock(side_effect=authenticate_effect)
            with patch("youtube_bot.YouTubeRunner", return_value=runner), patch("youtube_bot.GoogleAuthenticator", authenticator):
                result = await youtube_bot._authenticate_account(db, cfg, account)
            stored = (await db.get_account("bob")).auth_status
        return result, stored, authenticator.return_value.authenticate, runner, session

    async def test_success_marks_authenticated_and_remembers_the_owner(self):
        result, stored, authenticate, runner, session = await self._run([True])
        self.assertTrue(result)
        self.assertEqual(stored, "authenticated")
        self.assertEqual(read_session_owner(session), "bob@gmail.com")
        self.assertFalse(authenticate.await_args.kwargs["force_relogin"])
        runner.close.assert_awaited_once()

    async def test_google_refusal_is_failed(self):
        result, stored, _, runner, _ = await self._run(GoogleAuthError("неверный пароль"))
        self.assertFalse(result)
        self.assertEqual(stored, "failed")
        runner.close.assert_awaited_once()

    async def test_infrastructure_error_leaves_the_status_alone(self):
        for error in (RuntimeError("profile is locked"), OSError("proxy refused")):
            result, stored, _, runner, _ = await self._run(error)
            self.assertFalse(result)
            self.assertEqual(stored, "pending")
            runner.close.assert_awaited_once()

    async def test_session_of_another_account_forces_a_fresh_sign_in(self):
        _, _, authenticate, _, session = await self._run([True], session_owner="someone.else@gmail.com")
        self.assertTrue(authenticate.await_args.kwargs["force_relogin"])
        self.assertEqual(read_session_owner(session), "bob@gmail.com")

    async def test_account_without_credentials_is_not_started(self):
        async with Database(self.root / "metrics.db") as db:
            account = await db.upsert_account("legacy", "s")
            with patch("youtube_bot.YouTubeRunner") as runner:
                self.assertFalse(await youtube_bot._authenticate_account(db, _cfg(self.root), account))
            runner.assert_not_called()


class TestAccountsTable(_DbCase):
    async def test_table_shows_auth_status_and_masked_values_only(self):
        async with Database(self.root / "metrics.db") as db:
            await db.upsert_account(
                "bob", "sess", proxy_url="proxy.example.com:3128:loginname:SECRETPW", email="bob@gmail.com", password="SECRETPW"
            )
            account = await db.get_account("bob")
            await db.set_account_auth_status(account.id, "authenticated")
        out = io.StringIO()
        with patch("youtube_bot.load_config", return_value=_cfg(self.root)), contextlib.redirect_stdout(out):
            await youtube_bot.print_accounts_table()
        printed = out.getvalue()
        self.assertIn("authenticated", printed)
        self.assertIn("b***@gmail.com", printed)
        self.assertIn("loginname:***", printed)
        self.assertNotIn("bob@gmail.com", printed)
        self.assertNotIn("SECRETP", printed)
        self.assertEqual(youtube_bot.mask_email(None), "-")


class TestAddAccountFromStdin(unittest.TestCase):
    def _main(self, argv, stdin_text="", target="add_credentials_account_cli"):
        with patch("sys.argv", ["youtube_bot.py", *argv]), patch("sys.stdin", io.StringIO(stdin_text)), \
             patch(f"youtube_bot.{target}", new_callable=AsyncMock, return_value=True) as handler, \
             patch("youtube_bot.bind_to_kill_on_close_job"), patch("youtube_bot.setup_cli_logging"):
            code = None
            try:
                youtube_bot.main()
            except SystemExit as exc:
                code = exc.code
        return handler, code

    def test_dash_reads_the_account_line_from_stdin(self):
        handler, code = self._main(["--add-account", "-"], stdin_text=_line() + "\n")
        self.assertEqual(code, 0)
        self.assertEqual(handler.await_args.args[0], _line())

    def test_empty_stdin_is_an_error(self):
        handler, code = self._main(["--add-account", "-"], stdin_text="\n")
        self.assertEqual(code, 1)
        handler.assert_not_awaited()

    def test_credentials_in_argv_and_legacy_names_still_work(self):
        handler, code = self._main(["--add-account", _line()])
        self.assertEqual((code, handler.await_args.args[0]), (0, _line()))
        legacy, code = self._main(["--add-account", "plain_name"], target="add_account_cli")
        self.assertIsNone(code)
        self.assertEqual(legacy.await_args.args[0], "plain_name")

    def test_force_flag_reaches_the_import(self):
        handler, code = self._main(["--import-accounts-file", "f.txt", "--force"], target="import_accounts_file_cli")
        self.assertEqual(code, 0)
        self.assertTrue(handler.await_args.args[3])


class TestInteractiveLogin(_DbCase):
    async def _login(self, login_info: bool):
        cookies = [{"name": "LOGIN_INFO", "value": "x", "domain": ".youtube.com", "expires": -1}] if login_info else []
        page = MagicMock(is_closed=MagicMock(side_effect=[False, True]))
        page.goto = AsyncMock()
        context = MagicMock(pages=[page], cookies=AsyncMock(return_value=cookies))
        camoufox = MagicMock()
        camoufox.return_value.__aenter__ = AsyncMock(return_value=context)
        camoufox.return_value.__aexit__ = AsyncMock(return_value=None)
        cfg = _cfg(self.root)
        with patch("youtube_bot.load_config", return_value=cfg), \
             patch("camoufox.async_api.AsyncCamoufox", camoufox), \
             patch("modules.youtube_runner.get_or_create_fingerprint", return_value={"pinned": True}), \
             patch("youtube_bot.asyncio.sleep", AsyncMock()):
            await youtube_bot.interactive_login("unused.yaml", account_name="alice")
        async with Database(cfg.app.db_path) as db:
            return camoufox.call_args.kwargs, await db.get_account("alice")

    async def test_uses_runner_options_and_marks_the_account_authenticated(self):
        options, account = await self._login(login_info=True)
        self.assertEqual(options["fingerprint"], {"pinned": True})
        self.assertIs(options["firefox_user_prefs"]["security.webauth.webauthn"], False)
        self.assertFalse(options["headless"])
        self.assertEqual(account.auth_status, "authenticated")

    async def test_without_login_cookie_the_status_stays_pending(self):
        _, account = await self._login(login_info=False)
        self.assertEqual(account.auth_status, "pending")


class TestFingerprintOs(unittest.TestCase):
    def _generate(self, os_name: str):
        ua = {"windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0)", "macos": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15)"}
        return {"navigator": {"userAgent": ua[os_name]}, "id": os_name}

    def test_changed_os_makes_a_new_fingerprint_and_stores_the_os(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch("camoufox.fingerprints.generate_fingerprint", side_effect=lambda os: self._generate(os)) as generate:
            first = get_or_create_fingerprint(Path(temp), "windows")
            self.assertEqual(get_or_create_fingerprint(Path(temp), "windows"), first)
            second = get_or_create_fingerprint(Path(temp), "macos")
            self.assertNotEqual(second, first)
            self.assertEqual(generate.call_count, 2)
            self.assertEqual(json.loads((Path(temp) / "fingerprint.json").read_text(encoding="utf-8"))["os"], "macos")
            self.assertEqual(get_or_create_fingerprint(Path(temp), "macos"), second)
            self.assertEqual(generate.call_count, 2)

    def test_older_flat_file_is_read_and_its_os_is_derived_from_the_user_agent(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch("camoufox.fingerprints.generate_fingerprint", side_effect=lambda os: self._generate(os)) as generate:
            flat = self._generate("windows")
            (Path(temp) / "fingerprint.json").write_text(json.dumps(flat), encoding="utf-8")
            self.assertEqual(get_or_create_fingerprint(Path(temp), "windows"), flat)
            generate.assert_not_called()
            self.assertEqual(get_or_create_fingerprint(Path(temp), "macos"), self._generate("macos"))

    def test_write_is_atomic_and_force_new_replaces(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch("camoufox.fingerprints.generate_fingerprint", side_effect=[{"v": 1}, {"v": 2}]):
            self.assertEqual(get_or_create_fingerprint(Path(temp), "windows"), {"v": 1})
            self.assertEqual(sorted(p.name for p in Path(temp).iterdir()), ["fingerprint.json"])  # no leftover .tmp
            self.assertEqual(get_or_create_fingerprint(Path(temp), "windows", force_new=True), {"v": 2})

    def test_unwritable_target_still_returns_the_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch("camoufox.fingerprints.generate_fingerprint", return_value={"v": 1}), \
             patch("modules.fingerprint_manager.os.replace", side_effect=OSError("denied")):
            self.assertEqual(get_or_create_fingerprint(Path(temp), "windows"), {"v": 1})

    def test_fingerprint_the_installed_camoufox_cannot_convert_is_not_compatible(self):
        with patch("camoufox.fingerprints.from_fpgen", side_effect=KeyError("screen")):
            self.assertFalse(is_compatible({"navigator": {}}))
        with patch("camoufox.fingerprints.from_fpgen", return_value={}):
            self.assertTrue(is_compatible({"navigator": {}}))


def _runner(root: Path) -> YouTubeRunner:
    cfg = _cfg(root)
    return YouTubeRunner(cfg.browser, cfg.browser.targets[0])


class TestRunnerStart(_DbCase):
    def setUp(self):
        patcher = patch("modules.youtube_runner.get_or_create_fingerprint", return_value={"old": True})
        self.fingerprint = patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _browser(context):
        browser = MagicMock()
        browser.__aenter__ = AsyncMock(return_value=context)
        browser.__aexit__ = AsyncMock(return_value=None)
        return browser

    @staticmethod
    def _context():
        page = MagicMock(set_default_timeout=MagicMock(), set_default_navigation_timeout=MagicMock())
        return MagicMock(pages=[page])

    async def test_closed_page_releases_the_old_browser_before_a_new_start(self):
        runner = _runner(self.root)
        old = self._browser(self._context())
        runner._camoufox, runner._context = old, MagicMock()
        runner._page = MagicMock(is_closed=MagicMock(return_value=True))
        new = self._browser(self._context())
        with patch("modules.youtube_runner.AsyncCamoufox", return_value=new):
            await runner.start()
        old.__aexit__.assert_awaited_once()
        new.__aenter__.assert_awaited_once()

    async def test_incompatible_pinned_fingerprint_is_regenerated_once(self):
        runner = _runner(self.root)
        broken, fine = self._browser(self._context()), self._browser(self._context())
        broken.__aenter__ = AsyncMock(side_effect=KeyError("screen"))
        created = []
        with patch("modules.youtube_runner.AsyncCamoufox", side_effect=lambda **kw: created.append(kw) or (broken if len(created) == 1 else fine)), \
             patch("modules.youtube_runner.is_compatible", return_value=False):
            self.fingerprint.side_effect = [{"old": True}, {"new": True}]
            await runner.start()
        self.assertEqual([kw["fingerprint"] for kw in created], [{"old": True}, {"new": True}])
        self.assertTrue(self.fingerprint.call_args.kwargs["force_new"])

    async def test_launch_failure_with_a_compatible_fingerprint_is_not_retried(self):
        runner = _runner(self.root)
        broken = self._browser(self._context())
        broken.__aenter__ = AsyncMock(side_effect=RuntimeError("profile is locked"))
        with patch("modules.youtube_runner.AsyncCamoufox", return_value=broken) as factory, \
             patch("modules.youtube_runner.is_compatible", return_value=True), \
             self.assertRaisesRegex(RuntimeError, "profile is locked"):
            await runner.start()
        self.assertEqual(factory.call_count, 1)


class TestCommentIds(unittest.IsolatedAsyncioTestCase):
    async def test_all_four_comment_id_prefixes_are_extracted(self):
        for prefix in ("Ugw", "Ugx", "Ugy", "Ugz"):
            body = '{"actions":[{"createCommentAction":{"commentId":"%sQ1w.AbC-9_x"}}]}' % prefix
            response = MagicMock(text=AsyncMock(return_value=body))
            self.assertEqual(await YouTubeRunner._extract_remote_id(response), f"{prefix}Q1w.AbC-9_x")

    def test_validator_treats_every_youtube_comment_id_as_a_comment(self):
        base = dict(
            id=1, node_id=1, source_ref="vid12345678#p", source_text=None, text="t", language="en", status="submitted",
            error=None, created_at=0.0, submitted_at=0.0, validated_at=None,
        )
        for prefix in ("Ugw", "Ugx", "Ugy", "Ugz"):
            self.assertTrue(_is_youtube_comment(Payload(**base, remote_id=f"{prefix}ABC.9x")), prefix)
        self.assertFalse(_is_youtube_comment(Payload(**base, remote_id="yt_123")))


class TestOrchestratorAccounts(unittest.TestCase):
    def test_usable_accounts_rule(self):
        with tempfile.TemporaryDirectory() as temp:
            with_session = Path(temp) / "with"
            with_session.mkdir()
            (with_session / "cookies.sqlite").write_bytes(b"x")

            def account(status, email, session):
                return AccountRecord(id=1, name="a", session_path=str(session), created_at="now", auth_status=status, email=email)

            usable = MultiAccountOrchestrator._is_usable
            self.assertFalse(usable(account("failed", None, with_session)))
            self.assertTrue(usable(account("pending", None, Path(temp) / "none")))  # `default` / manual --login accounts
            self.assertFalse(usable(account("pending", "a@b.c", Path(temp) / "none")))  # credentials, never signed in
            self.assertTrue(usable(account("pending", "a@b.c", with_session)))
            self.assertTrue(usable(account("authenticated", "a@b.c", Path(temp) / "none")))

    def test_runner_proxy_falls_back_to_the_global_proxy_like_the_sign_in(self):
        orchestrator = MultiAccountOrchestrator.__new__(MultiAccountOrchestrator)
        orchestrator._runner_factory = None
        orchestrator._cfg = SimpleNamespace(browser=SimpleNamespace(proxy="9.9.9.9:80"))
        orchestrator._yt_target = SimpleNamespace()
        orchestrator._simulator = MagicMock()
        orchestrator._rng = MagicMock()
        bare = AccountRecord(id=1, name="a", session_path="s", created_at="now")
        own = AccountRecord(id=2, name="b", session_path="s", created_at="now", proxy_url="1.1.1.1:80")
        with patch("modules.multi_account_orchestrator.YouTubeRunner") as factory:
            orchestrator.create_runner(bare)
            orchestrator.create_runner(own)
        self.assertEqual([call.kwargs["proxy"] for call in factory.call_args_list], ["9.9.9.9:80", "1.1.1.1:80"])


class TestSimulatorWithoutMockImport(unittest.IsolatedAsyncioTestCase):
    def test_production_module_does_not_import_unittest(self):
        source = (Path(__file__).resolve().parent.parent / "modules" / "interaction_simulator.py").read_text(encoding="utf-8")
        self.assertNotIn("unittest", source)

    async def test_failed_key_release_does_not_type_the_character_twice(self):
        from modules.interaction_simulator import InteractionSimulator

        async def down(key):
            return None

        async def up(key):
            raise RuntimeError("release failed")

        typed = MagicMock()
        page = MagicMock(is_closed=MagicMock(return_value=False))
        page.keyboard = SimpleNamespace(down=down, up=up, type=typed)
        with patch("asyncio.sleep", AsyncMock()):
            await InteractionSimulator()._press_character(page, "a")
        typed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
