import asyncio
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from config import AccountSettings, parse_proxy
from database import Database
from modules.account_parser import looks_like_credentials, parse_account_line
from modules.fingerprint_manager import get_or_create_fingerprint
from modules.google_authenticator import GoogleAuthenticator
from modules.totp_helper import generate_fresh_totp, generate_totp, get_remaining_seconds

# RFC 6238 appendix B: SHA-1 secret "12345678901234567890", 8 digits; the last 6 are what the 6-digit code shows.
_RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()
_RFC_VECTORS = {59: "94287082", 1111111109: "07081804", 1111111111: "14050471", 1234567890: "89005924", 2000000000: "69279037"}


class TestTotp(unittest.TestCase):
    def test_rfc6238_vectors(self):
        for moment, expected in _RFC_VECTORS.items():
            self.assertEqual(generate_totp(_RFC_SECRET, at=moment), expected[-6:], moment)

    def test_secret_lengths_without_padding(self):
        for length in (16, 26, 32):
            secret = ("JBSWY3DPEHPK3PXP" * 2)[:length]
            self.assertNotIn("=", secret)
            code = generate_totp(secret, at=1_700_000_000)
            self.assertRegex(code, r"^\d{6}$")
            padded = secret + "=" * (-len(secret) % 8)
            self.assertEqual(code, generate_totp(padded, at=1_700_000_000))

    def test_spaces_and_case_are_ignored(self):
        self.assertEqual(
            generate_totp("jbsw y3dp ehpk 3pxp", at=100),
            generate_totp("JBSWY3DPEHPK3PXP", at=100),
        )

    def test_remaining_seconds(self):
        self.assertEqual(get_remaining_seconds(at=60), 30)
        self.assertEqual(get_remaining_seconds(at=89), 1)

    def test_drift_guard_waits_for_next_window(self):
        async def run(remaining: int) -> list[float]:
            sleeps: list[float] = []

            async def fake_sleep(seconds: float) -> None:
                sleeps.append(seconds)

            with patch("modules.totp_helper.get_remaining_seconds", return_value=remaining), \
                 patch("modules.totp_helper.asyncio.sleep", fake_sleep):
                await generate_fresh_totp(_RFC_SECRET)
            return sleeps

        self.assertEqual(asyncio.run(run(3)), [3.5])
        self.assertEqual(asyncio.run(run(4)), [])
        self.assertEqual(asyncio.run(run(20)), [])


class TestProxyFormats(unittest.TestCase):
    def test_raw_ip_port_user_pass(self):
        self.assertEqual(
            parse_proxy("1.2.3.4:8080:user:secret"),
            {"server": "http://1.2.3.4:8080", "username": "user", "password": "secret"},
        )

    def test_raw_format_with_hostname(self):
        proxy = parse_proxy("proxy.example.com:3128:bob:pw")
        self.assertEqual(proxy["server"], "http://proxy.example.com:3128")
        self.assertEqual(proxy["username"], "bob")

    def test_standard_formats_unchanged(self):
        self.assertEqual(parse_proxy("http://u:p@h.io:9000"), {"server": "http://h.io:9000", "username": "u", "password": "p"})
        self.assertEqual(parse_proxy("socks5://h.io:1080"), {"server": "socks5://h.io:1080"})
        self.assertIsNone(parse_proxy(None))
        with self.assertRaises(ValueError):
            parse_proxy("no-port-here")

    def test_account_settings_accept_raw_proxy_and_credentials(self):
        acc = AccountSettings(name="a", proxy_url="1.2.3.4:8080:u:p", email="a@b.c", totp_secret="JBSWY3DP")
        self.assertEqual(acc.playwright_proxy()["server"], "http://1.2.3.4:8080")
        self.assertIsNone(acc.password)
        self.assertIsNone(acc.recovery_email)


class TestAccountLineParsing(unittest.TestCase):
    def test_email_pass_totp(self):
        acc = parse_account_line("bob@gmail.com:pw123:JBSWY3DPEHPK3PXP")
        self.assertEqual((acc.email, acc.password, acc.totp_secret), ("bob@gmail.com", "pw123", "JBSWY3DPEHPK3PXP"))
        self.assertIsNone(acc.recovery_email)
        self.assertIsNone(acc.proxy)
        self.assertEqual(acc.default_name, "bob@gmail.com")

    def test_with_recovery_email(self):
        acc = parse_account_line("bob@gmail.com:pw:JBSWY3DP:rescue@mail.ru")
        self.assertEqual(acc.recovery_email, "rescue@mail.ru")
        self.assertEqual(acc.totp_secret, "JBSWY3DP")

    def test_with_scheme_proxy(self):
        acc = parse_account_line("bob@gmail.com:pw:JBSWY3DP:http://u:p@1.2.3.4:8080")
        self.assertEqual(acc.proxy, "http://u:p@1.2.3.4:8080")
        self.assertEqual(acc.totp_secret, "JBSWY3DP")

    def test_with_raw_proxy(self):
        acc = parse_account_line("bob@gmail.com:pw:JBSWY3DP:1.2.3.4:8080:user:pass")
        self.assertEqual(acc.proxy, "1.2.3.4:8080:user:pass")
        self.assertEqual((acc.password, acc.totp_secret), ("pw", "JBSWY3DP"))

    def test_recovery_email_and_proxy_together(self):
        acc = parse_account_line("bob@gmail.com:pw:JBSWY3DP:rescue@mail.ru:1.2.3.4:8080:user:pass")
        self.assertEqual(acc.recovery_email, "rescue@mail.ru")
        self.assertEqual(acc.proxy, "1.2.3.4:8080:user:pass")

    def test_proxy_argument_is_used_when_line_has_none(self):
        acc = parse_account_line("bob@gmail.com:pw:JBSWY3DP", proxy="1.2.3.4:8080:u:p")
        self.assertEqual(acc.proxy, "1.2.3.4:8080:u:p")
        inline = parse_account_line("bob@gmail.com:pw:JBSWY3DP:5.6.7.8:80:a:b", proxy="1.2.3.4:8080:u:p")
        self.assertEqual(inline.proxy, "5.6.7.8:80:a:b")

    def test_password_with_colons(self):
        acc = parse_account_line("bob@gmail.com:p:a:ss:JBSWY3DP")
        self.assertEqual((acc.password, acc.totp_secret), ("p:a:ss", "JBSWY3DP"))

    def test_invalid_lines(self):
        for bad in ("", "just-a-name", "bob@gmail.com:onlypass", "nomail:pw:JBSWY3DP", "bob@gmail.com::JBSWY3DP"):
            with self.assertRaises(ValueError, msg=bad):
                parse_account_line(bad)

    def test_looks_like_credentials_keeps_legacy_names(self):
        self.assertTrue(looks_like_credentials("bob@gmail.com:pw:JBSWY3DP"))
        self.assertFalse(looks_like_credentials("my_account"))
        self.assertFalse(looks_like_credentials("work-acc-2"))


class TestAccountsMigration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "metrics.db"

    async def test_new_columns_added_to_old_accounts_table(self):
        old = sqlite3.connect(self.path)
        old.execute(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, "
            "session_path TEXT NOT NULL, proxy_url TEXT, is_active INTEGER NOT NULL DEFAULT 1, "
            "total_replies INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, last_used_at TEXT)"
        )
        old.execute("INSERT INTO accounts (name, session_path, created_at) VALUES ('legacy', 's', 'now')")
        old.commit()
        old.close()
        async with Database(self.path) as db:
            pass
        async with Database(self.path) as db:  # idempotent
            accounts = await db.list_accounts()
        self.assertEqual([a.name for a in accounts], ["legacy"])
        self.assertEqual(accounts[0].auth_status, "pending")
        self.assertIsNone(accounts[0].email)
        check = sqlite3.connect(self.path)
        try:
            columns = {row[1] for row in check.execute("PRAGMA table_info(accounts)")}
        finally:
            check.close()
        self.assertTrue({"email", "password", "totp_secret", "recovery_email", "auth_status", "fingerprint_data", "geo_country"} <= columns)

    async def test_upsert_stores_credentials_and_old_callers_keep_them(self):
        async with Database(self.path) as db:
            created = await db.upsert_account(
                "bob", "s", proxy_url="1.2.3.4:80:u:p", email="bob@gmail.com", password="pw",
                totp_secret="JBSWY3DP", recovery_email="r@mail.ru",
            )
            self.assertEqual(created.auth_status, "pending")
            again = await db.upsert_account("bob", "s2", proxy_url=None)  # a legacy call without credentials
            self.assertEqual((again.email, again.password, again.totp_secret, again.recovery_email),
                             ("bob@gmail.com", "pw", "JBSWY3DP", "r@mail.ru"))
            self.assertEqual(again.session_path, "s2")
            await db.set_account_auth_status(created.id, "authenticated")
            listed = await db.list_accounts()
            self.assertEqual(listed[0].auth_status, "authenticated")
            self.assertEqual(listed[0].email, "bob@gmail.com")
            await db.set_account_geo_country(created.id, "Kyrgyzstan")
            updated = await db.list_accounts()
            self.assertEqual(updated[0].geo_country, "Kyrgyzstan")


class TestFingerprintManager(unittest.TestCase):
    def setUp(self):
        patcher = patch("modules.fingerprint_manager.host_gpu", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_generated_once_then_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "acc"
            fake = {"navigator": {"userAgent": "x"}, "screen": {"width": 1920}}
            with patch("camoufox.fingerprints.generate_fingerprint", return_value=fake) as generate:
                first = get_or_create_fingerprint(session, "windows")
                second = get_or_create_fingerprint(session, "windows")
            self.assertEqual(first, fake)
            self.assertEqual(second, fake)
            generate.assert_called_once_with(os="windows")
            saved = json.loads((session / "fingerprint.json").read_text(encoding="utf-8"))
            self.assertEqual((saved["format"], saved["os"], saved["fingerprint"]), (1, "windows", fake))

    def test_corrupt_file_is_regenerated(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "fingerprint.json").write_text("{not json", encoding="utf-8")
            with patch("camoufox.fingerprints.generate_fingerprint", return_value={"a": 1}):
                self.assertEqual(get_or_create_fingerprint(Path(temp)), {"a": 1})

    def test_runner_passes_saved_fingerprint(self):
        from config import BrowserSettings, TargetSettings
        from modules.youtube_runner import YouTubeRunner

        with tempfile.TemporaryDirectory() as temp:
            cfg = BrowserSettings(
                session_root=Path(temp),
                targets=[TargetSettings(
                    name="youtube", base_url="https://www.youtube.com", submit_input_selector="i",
                    submit_button_selector="b", confirmation_selector="c",
                )],
            )
            runner = YouTubeRunner(cfg, cfg.targets[0])
            with patch("modules.youtube_runner.get_or_create_fingerprint", return_value={"pinned": True}):
                options = runner._launch_options()
            self.assertEqual(options["fingerprint"], {"pinned": True})
            self.assertIs(options["firefox_user_prefs"]["security.webauth.webauthn"], False)


class TestIsSessionAuthenticated(unittest.IsolatedAsyncioTestCase):
    def _auth(self) -> GoogleAuthenticator:
        return GoogleAuthenticator(MagicMock())

    @staticmethod
    def _context(cookies: list[dict]) -> MagicMock:
        return MagicMock(cookies=AsyncMock(return_value=cookies))

    async def test_login_info_present(self):
        context = self._context([
            {"name": "VISITOR_INFO1_LIVE", "value": "v", "domain": ".youtube.com", "expires": -1},
            {"name": "LOGIN_INFO", "value": "AFmmF2sw", "domain": ".youtube.com", "expires": 4_102_444_800},
        ])
        self.assertTrue(await self._auth().is_session_authenticated(context))

    async def test_session_cookie_without_expiry_is_valid(self):
        context = self._context([{"name": "LOGIN_INFO", "value": "x", "domain": ".youtube.com", "expires": -1}])
        self.assertTrue(await self._auth().is_session_authenticated(context))

    async def test_login_info_missing(self):
        context = self._context([{"name": "VISITOR_INFO1_LIVE", "value": "v", "domain": ".youtube.com", "expires": -1}])
        self.assertFalse(await self._auth().is_session_authenticated(context))

    async def test_login_info_on_other_domain_or_expired_or_empty(self):
        for cookie in (
            {"name": "LOGIN_INFO", "value": "x", "domain": ".google.com", "expires": -1},
            {"name": "LOGIN_INFO", "value": "x", "domain": ".youtube.com", "expires": 1},
            {"name": "LOGIN_INFO", "value": "", "domain": ".youtube.com", "expires": -1},
        ):
            self.assertFalse(await self._auth().is_session_authenticated(self._context([cookie])), cookie)

    async def test_cookie_read_error_means_not_authenticated(self):
        context = MagicMock(cookies=AsyncMock(side_effect=RuntimeError("closed")))
        self.assertFalse(await self._auth().is_session_authenticated(context))

    async def test_authenticate_skips_form_when_already_signed_in(self):
        context = self._context([{"name": "LOGIN_INFO", "value": "x", "domain": ".youtube.com", "expires": -1}])
        context.new_page = AsyncMock()
        self.assertTrue(await self._auth().authenticate(context, "a@b.c", "pw", "JBSWY3DP"))
        context.new_page.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
