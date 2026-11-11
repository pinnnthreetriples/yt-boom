"""Google Chrome (patchright) next to Camoufox: paths, proxy rule, profile prep, heartbeat state, runner, DB, UI helpers."""

import json
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from pydantic import ValidationError

from config import (
    CHROME_SOCKS5_AUTH_ERROR,
    AccountSettings,
    BrowserSettings,
    TargetSettings,
    get_account_session_path,
    validate_proxy_for_engine,
)
from database import Database, RecordNotFoundError
from modules import chrome_launcher
from modules.chrome_launcher import (
    COUNTRY_TO_TIMEZONE,
    ensure_chrome_executable,
    get_chrome_args,
    prepare_chrome_profile,
    resolve_geo_timezone,
)
from modules.chrome_session import (
    HEARTBEAT_STATE_FILE,
    chrome_login_info_expiry,
    load_chrome_heartbeat_state,
    save_chrome_heartbeat_state,
)
from modules.session_heartbeat import (
    check_session_heartbeat,
    check_session_offline,
    chrome_client_hints,
    chrome_headers,
    chrome_impersonate_profile,
)
from modules.youtube_runner import YouTubeRunner

CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def _cookie(name, value, domain, expires=-1):
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": expires}


def _context(cookies):
    context = MagicMock()
    context.cookies = AsyncMock(return_value=cookies)
    context.close = AsyncMock()
    context.new_page = AsyncMock()
    return context


class TestSessionPathAndProxy(unittest.TestCase):
    def test_account_session_path_resolution(self):
        root = Path("test_sessions")
        self.assertEqual(get_account_session_path(root, "alice", "camoufox"), root / "alice")
        self.assertEqual(get_account_session_path(root, "alice", "chrome"), root / "chrome" / "alice")
        self.assertEqual(get_account_session_path("test_sessions", "bob", "chrome"), Path("test_sessions/chrome/bob"))
        self.assertNotEqual(
            get_account_session_path(root, "alice", "camoufox"), get_account_session_path(root, "alice", "chrome")
        )

    def test_socks5_with_auth_rejected_for_chrome(self):
        for proxy in ("socks5://user:pass@10.0.0.1:1080", "socks5://user@10.0.0.1:1080"):
            with self.subTest(proxy=proxy):
                with self.assertRaises(ValueError) as caught:
                    validate_proxy_for_engine(proxy, "chrome")
                self.assertEqual(str(caught.exception), CHROME_SOCKS5_AUTH_ERROR)
                with self.assertRaises(ValidationError) as invalid:
                    AccountSettings(name="a", proxy_url=proxy, engine="chrome")
                self.assertIn("Движок Chrome не поддерживает SOCKS5 с авторизацией", str(invalid.exception))
                self.assertNotIn("pass", str(invalid.exception).replace("авторизацией", ""))

    def test_other_proxy_and_engine_combinations_are_allowed(self):
        for proxy, engine in (
            ("socks5://10.0.0.1:1080", "chrome"),
            ("http://user:pass@10.0.0.1:8080", "chrome"),
            ("10.0.0.1:8080:user:pass", "chrome"),
            ("socks5://user:pass@10.0.0.1:1080", "camoufox"),
            (None, "chrome"),
        ):
            with self.subTest(proxy=proxy, engine=engine):
                validate_proxy_for_engine(proxy, engine)
                AccountSettings(name="a", proxy_url=proxy, engine=engine)

    def test_engine_defaults_and_unknown_engine(self):
        self.assertEqual(AccountSettings(name="a").engine, "camoufox")
        with self.assertRaises(ValidationError):
            AccountSettings(name="a", engine="webkit")
        browser = BrowserSettings(targets=[TargetSettings(
            name="youtube", base_url="https://www.youtube.com", submit_input_selector="#a",
            submit_button_selector="#b", confirmation_selector="#c",
        )])
        self.assertEqual(browser.default_engine, "camoufox")


class TestPrepareChromeProfile(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name) / "chrome" / "alice"

    def test_stale_locks_are_removed_and_preferences_are_created(self):
        self.profile.mkdir(parents=True)
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile"):
            (self.profile / name).write_text("x")
        prepare_chrome_profile(self.profile)
        for name in ("SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile"):
            self.assertFalse((self.profile / name).exists(), name)
        prefs = json.loads((self.profile / "Default" / "Preferences").read_text(encoding="utf-8"))
        self.assertEqual(prefs["profile"]["exit_type"], "Normal")
        self.assertTrue(prefs["profile"]["exited_cleanly"])
        self.assertFalse(prefs["profile"]["password_manager_enabled"])
        self.assertFalse(prefs["credentials_enable_service"])
        self.assertFalse(prefs["signin"]["allowed"])
        self.assertFalse(prefs["signin"]["allowed_on_next_startup"])
        self.assertFalse((self.profile / "Default" / "Preferences.tmp").exists())

    def test_preferences_are_deep_merged_not_replaced(self):
        default = self.profile / "Default"
        default.mkdir(parents=True)
        (default / "Preferences").write_text(json.dumps({
            "profile": {"name": "Person 1", "exit_type": "Crashed"},
            "signin": {"keep_me": 1},
            "intl": {"selected_languages": "pl,en"},
        }), encoding="utf-8")
        prepare_chrome_profile(self.profile)
        prefs = json.loads((default / "Preferences").read_text(encoding="utf-8"))
        self.assertEqual(prefs["profile"]["name"], "Person 1")
        self.assertEqual(prefs["profile"]["exit_type"], "Normal")
        self.assertEqual(prefs["signin"]["keep_me"], 1)
        self.assertFalse(prefs["signin"]["allowed"])
        self.assertEqual(prefs["intl"], {"selected_languages": "pl,en"})

    def test_damaged_preferences_are_rebuilt(self):
        default = self.profile / "Default"
        default.mkdir(parents=True)
        (default / "Preferences").write_text("{not json", encoding="utf-8")
        prepare_chrome_profile(self.profile)
        self.assertFalse(json.loads((default / "Preferences").read_text(encoding="utf-8"))["signin"]["allowed"])
        (default / "Preferences").write_text("[1, 2]", encoding="utf-8")  # valid JSON, not an object
        prepare_chrome_profile(self.profile)
        self.assertIn("profile", json.loads((default / "Preferences").read_text(encoding="utf-8")))

    def test_a_lock_held_by_a_running_chrome_is_reported(self):
        self.profile.mkdir(parents=True)
        (self.profile / "lockfile").write_text("x")
        with patch.object(Path, "unlink", side_effect=PermissionError(32, "in use")):
            with self.assertRaises(RuntimeError) as caught:
                prepare_chrome_profile(self.profile)
        self.assertIn("WinError 32", str(caught.exception))
        self.assertIn(str(self.profile), str(caught.exception))

    def test_chrome_args(self):
        args = get_chrome_args("de-DE")
        self.assertIn("--hide-crash-restore-bubble", args)
        self.assertIn("--lang=de-DE", args)
        self.assertIn("--disable-quic", args)
        self.assertIn("--webrtc-ip-handling-policy=disable_non_proxied_udp", args)
        self.assertIn("--lang=en-US", get_chrome_args())


class TestEnsureChromeExecutable(unittest.TestCase):
    def test_installed_chrome_is_found_without_installing(self):
        with tempfile.TemporaryDirectory() as temp:
            exe = Path(temp) / chrome_launcher.CHROME_RELATIVE_PATH
            exe.parent.mkdir(parents=True)
            exe.write_text("")
            with patch.dict("os.environ", {"ProgramFiles": temp}, clear=False), \
                    patch("modules.chrome_launcher.subprocess.run") as run:
                self.assertEqual(ensure_chrome_executable(), exe)
            run.assert_not_called()

    def test_missing_chrome_is_installed_through_patchright(self):
        with patch("modules.chrome_launcher.find_chrome_executable", side_effect=[None, Path("C:/chrome.exe")]), \
                patch("modules.chrome_launcher.subprocess.run", return_value=MagicMock(returncode=0)) as run:
            self.assertEqual(ensure_chrome_executable(), Path("C:/chrome.exe"))
        self.assertEqual(run.call_args.args[0], [sys.executable, "-m", "patchright", "install", "chrome"])

    def test_failed_install_raises(self):
        with patch("modules.chrome_launcher.find_chrome_executable", return_value=None), \
                patch("modules.chrome_launcher.subprocess.run", return_value=MagicMock(returncode=1, stderr="boom", stdout="")):
            with self.assertRaises(RuntimeError) as caught:
                ensure_chrome_executable()
        self.assertIn("установите Google Chrome вручную", str(caught.exception))


class TestResolveGeoTimezone(unittest.IsolatedAsyncioTestCase):
    async def test_exit_country_gives_timezone_and_locale(self):
        for country, timezone_id, locale in (
            ("US", "America/New_York", "en-US"), ("DE", "Europe/Berlin", "de-DE"), ("GB", "Europe/London", "en-GB"),
            ("FR", "Europe/Paris", "fr-FR"), ("RU", "Europe/Moscow", "ru-RU"), ("TR", "Europe/Istanbul", "tr-TR"),
        ):
            with self.subTest(country=country), patch("modules.geo_guard.resolve_exit_country", AsyncMock(return_value=country)):
                self.assertEqual(await resolve_geo_timezone("http://p:1"), (timezone_id, locale))

    async def test_account_country_and_locale_are_the_fallback(self):
        with patch("modules.geo_guard.resolve_exit_country", AsyncMock(return_value=None)):
            self.assertEqual(await resolve_geo_timezone(None, "Turkey"), ("Europe/Istanbul", "tr-TR"))
            self.assertEqual(await resolve_geo_timezone(None, "TR", "en-GB"), ("Europe/Istanbul", "en-GB"))

    async def test_explicit_locale_wins_over_the_country_default(self):
        with patch("modules.geo_guard.resolve_exit_country", AsyncMock(return_value="DE")):
            self.assertEqual(await resolve_geo_timezone(None, None, "pl-PL"), ("Europe/Berlin", "pl-PL"))

    async def test_unknown_country_keeps_the_system_timezone(self):
        with patch("modules.geo_guard.resolve_exit_country", AsyncMock(return_value=None)):
            self.assertEqual(await resolve_geo_timezone(None), (None, "en-US"))
        with patch("modules.geo_guard.resolve_exit_country", AsyncMock(return_value="ZW")):
            self.assertEqual(await resolve_geo_timezone(None), (None, "en-US"))

    def test_table_covers_the_named_countries(self):
        for code in ("US", "DE", "GB", "FR", "RU", "TR"):
            self.assertIn(code, COUNTRY_TO_TIMEZONE)


class TestChromeHeartbeatState(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name) / "chrome" / "alice"

    async def test_chrome_heartbeat_state_dpapi(self):
        context = _context([
            _cookie("LOGIN_INFO", "login-secret", ".youtube.com", 4102444800),
            _cookie("SID", "sid-secret", ".google.com"),
            _cookie("__Secure-3PSID", "psid", "accounts.google.com"),
            _cookie("tracker", "ad", ".doubleclick.net"),
            _cookie("session", "x", "example.com"),
        ])
        self.assertTrue(await save_chrome_heartbeat_state(self.profile, context, CHROME_UA))
        context.cookies.assert_awaited_once_with()  # no domain filter: every domain is collected, then filtered here

        raw = (self.profile / HEARTBEAT_STATE_FILE).read_bytes()
        self.assertFalse((self.profile / (HEARTBEAT_STATE_FILE + ".tmp")).exists())
        if sys.platform == "win32":  # DPAPI: no cookie value or UA is readable in the file
            self.assertTrue(raw.startswith(b"dpapi:"))
            for secret in (b"login-secret", b"sid-secret", CHROME_UA.encode()):
                self.assertNotIn(secret, raw)

        cookies, user_agent = load_chrome_heartbeat_state(self.profile)
        self.assertEqual(cookies, {"LOGIN_INFO": "login-secret", "SID": "sid-secret", "__Secure-3PSID": "psid"})
        self.assertEqual(user_agent, CHROME_UA)
        self.assertEqual(chrome_login_info_expiry(self.profile), (True, 4102444800.0))

    async def test_a_context_without_google_cookies_keeps_the_previous_state(self):
        await save_chrome_heartbeat_state(self.profile, _context([_cookie("LOGIN_INFO", "v", ".youtube.com")]), CHROME_UA)
        self.assertFalse(await save_chrome_heartbeat_state(self.profile, _context([_cookie("a", "b", "example.com")]), None))
        self.assertEqual(load_chrome_heartbeat_state(self.profile)[0], {"LOGIN_INFO": "v"})

    async def test_a_missing_user_agent_keeps_the_saved_one(self):
        context = _context([_cookie("LOGIN_INFO", "v", ".youtube.com")])
        await save_chrome_heartbeat_state(self.profile, context, CHROME_UA)
        await save_chrome_heartbeat_state(self.profile, context, None)
        self.assertEqual(load_chrome_heartbeat_state(self.profile)[1], CHROME_UA)

    async def test_youtube_cookie_wins_over_the_google_one_of_the_same_name(self):
        context = _context([_cookie("SAPISID", "yt", ".youtube.com"), _cookie("SAPISID", "g", ".google.com")])
        await save_chrome_heartbeat_state(self.profile, context, None)
        self.assertEqual(load_chrome_heartbeat_state(self.profile)[0], {"SAPISID": "yt"})

    def test_nothing_saved_or_unreadable_is_empty(self):
        self.assertEqual(load_chrome_heartbeat_state(self.profile), ({}, None))
        self.profile.mkdir(parents=True)
        (self.profile / HEARTBEAT_STATE_FILE).write_bytes(b"\xff\xfe not a state")
        self.assertEqual(load_chrome_heartbeat_state(self.profile), ({}, None))
        self.assertEqual(chrome_login_info_expiry(self.profile), (False, None))


class TestChromeHeartbeatCheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name) / "chrome" / "alice"

    async def _save(self, cookies=None):
        cookies = cookies or [_cookie("LOGIN_INFO", "v", ".youtube.com", 4102444800)]
        await save_chrome_heartbeat_state(self.profile, _context(cookies), CHROME_UA)

    def _session(self, text='"LOGGED_IN":true'):
        response = MagicMock(status_code=200, text=text)
        session = MagicMock()
        session.get = AsyncMock(return_value=response)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        return session

    async def test_chrome_account_replays_the_saved_cookies_ua_and_hints(self):
        await self._save()
        session = self._session()
        with patch("modules.session_heartbeat.AsyncSession", return_value=session) as factory:
            status = await check_session_heartbeat(self.profile, "alice", None, locale="de-DE", engine="chrome")
        self.assertTrue(status.is_alive)
        kwargs = factory.call_args.kwargs
        self.assertRegex(kwargs["impersonate"], r"^chrome\d+$")
        headers = kwargs["headers"]
        self.assertEqual(headers["User-Agent"], CHROME_UA)
        self.assertEqual(headers["Origin"], "https://www.youtube.com")
        self.assertEqual(headers["Accept-Language"], "de-DE,de;q=0.9")
        self.assertIn('"Google Chrome";v="131"', headers["sec-ch-ua"])
        self.assertEqual(headers["sec-ch-ua-platform"], '"Windows"')
        self.assertEqual(session.get.call_args.kwargs["cookies"], {"LOGIN_INFO": "v"})

    async def test_chrome_account_without_saved_state_is_dead_and_makes_no_request(self):
        with patch("modules.session_heartbeat.AsyncSession") as factory:
            status = await check_session_heartbeat(self.profile, "alice", engine="chrome")
        self.assertFalse(status.is_alive)
        factory.assert_not_called()

    async def test_camoufox_branch_still_reads_cookies_sqlite_and_impersonates_firefox(self):
        profile = Path(self.temp.name) / "alice"
        profile.mkdir()
        conn = sqlite3.connect(profile / "cookies.sqlite")
        conn.execute("CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, expiry INTEGER)")
        conn.execute("INSERT INTO moz_cookies VALUES ('LOGIN_INFO', 'v', '.youtube.com', 4102444800)")
        conn.commit()
        conn.close()
        session = self._session()
        with patch("modules.session_heartbeat.AsyncSession", return_value=session) as factory:
            status = await check_session_heartbeat(profile, "alice")
        self.assertTrue(status.is_alive)
        self.assertTrue(factory.call_args.kwargs["impersonate"].startswith("firefox"))

    async def test_offline_check_reads_the_chrome_state(self):
        await self._save()
        status = check_session_offline(self.profile, "alice", "chrome")
        self.assertTrue(status.is_alive)
        self.assertIsNotNone(status.days_remaining)
        self.assertFalse(check_session_offline(self.profile, "alice").is_alive)  # as Camoufox: no cookies.sqlite there

    async def test_expired_login_info_is_dead_offline(self):
        await self._save([_cookie("LOGIN_INFO", "v", ".youtube.com", 1000000000)])
        self.assertFalse(check_session_offline(self.profile, "alice", "chrome").is_alive)

    def test_impersonation_follows_the_user_agent_major_version(self):
        self.assertRegex(chrome_impersonate_profile(CHROME_UA), r"^chrome\d+$")
        self.assertRegex(chrome_impersonate_profile(None), r"^chrome\d+$")
        found = re.fullmatch(r"chrome(\d+)", chrome_impersonate_profile("Chrome/124.0.0.0"))
        self.assertIsNotNone(found)
        self.assertLessEqual(int(found.group(1)), 124)
        self.assertEqual(chrome_client_hints("Mozilla/5.0 Firefox/130.0"), {})

    def test_a_headless_user_agent_is_not_replayed_as_headless(self):
        headless = CHROME_UA.replace("Chrome/", "HeadlessChrome/")
        headers = chrome_headers(headless, "en-US")
        self.assertEqual(headers["User-Agent"], CHROME_UA)
        self.assertIn('"Google Chrome";v="131"', headers["sec-ch-ua"])


def _browser_settings(root: Path, **extra) -> BrowserSettings:
    target = TargetSettings(
        name="youtube", base_url="https://www.youtube.com",
        item_url_template="https://www.youtube.com/watch?v={external_id}",
        submit_input_selector="#reply-dialog #contenteditable-root",
        submit_button_selector="#reply-dialog #submit-button button",
        confirmation_selector="ytd-comment-view-model",
    )
    return BrowserSettings(headless=True, session_root=root, targets=[target], **extra)


class TestYouTubeRunnerChrome(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = _browser_settings(self.root)
        self.session_dir = self.root / "chrome" / "alice"

    def _fake_patchright(self, launch=None):
        self.page = MagicMock()
        self.page.evaluate = AsyncMock(return_value=CHROME_UA)
        self.page.set_default_timeout = MagicMock()
        self.page.set_default_navigation_timeout = MagicMock()
        self.page.is_closed = MagicMock(return_value=False)
        self.context = _context([_cookie("LOGIN_INFO", "v", ".youtube.com")])
        self.context.pages = [self.page]
        self.driver = MagicMock()
        self.driver.stop = AsyncMock()
        self.driver.chromium.launch_persistent_context = launch or AsyncMock(return_value=self.context)
        starter = MagicMock()
        starter.start = AsyncMock(return_value=self.driver)
        module = MagicMock()
        module.async_playwright = MagicMock(return_value=starter)
        return patch.dict(sys.modules, {"patchright": MagicMock(), "patchright.async_api": module})

    def _runner(self, **kwargs):
        return YouTubeRunner(self.cfg, self.cfg.targets[0], session_dir=self.session_dir, account_name="alice",
                             engine="chrome", **kwargs)

    def _patches(self):
        return (
            patch("modules.youtube_runner.ensure_chrome_executable", return_value=Path("chrome.exe")),
            patch("modules.youtube_runner.resolve_geo_timezone", AsyncMock(return_value=("Europe/Berlin", "de-DE"))),
        )

    async def test_start_launches_chrome_with_the_account_profile_and_geo(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            runner = self._runner(proxy="http://u:p@10.0.0.1:8080", locale="pl-PL")
            page = await runner.start()
        self.assertIs(page, self.page)
        kwargs = self.driver.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(kwargs["user_data_dir"], str(self.session_dir))
        self.assertEqual(kwargs["channel"], "chrome")
        self.assertTrue(kwargs["no_viewport"])
        self.assertTrue(kwargs["headless"])
        self.assertEqual(kwargs["timezone_id"], "Europe/Berlin")
        self.assertEqual(kwargs["locale"], "de-DE")
        self.assertIn("--lang=de-DE", kwargs["args"])
        self.assertIn("--hide-crash-restore-bubble", kwargs["args"])
        self.assertEqual(kwargs["proxy"], {"server": "http://10.0.0.1:8080", "username": "u", "password": "p"})
        self.assertTrue((self.session_dir / "Default" / "Preferences").is_file())  # prepare_chrome_profile ran
        self.assertEqual(runner._cached_user_agent, CHROME_UA)
        self.assertTrue(runner.is_running)
        self.assertEqual(runner.engine, "chrome")

    async def test_the_account_locale_and_country_reach_the_geo_lookup(self):
        ensure, _ = self._patches()
        geo = AsyncMock(return_value=(None, "pl-PL"))
        with self._fake_patchright(), ensure, patch("modules.youtube_runner.resolve_geo_timezone", geo):
            await self._runner(proxy=None, locale="pl-PL", geo_country="PL").start()
        geo.assert_awaited_once_with(None, "PL", "pl-PL")

    async def test_close_saves_the_heartbeat_state_closes_the_context_and_stops_the_driver(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            runner = self._runner(proxy=None)
            await runner.start()
            await runner.close()
        self.context.close.assert_awaited_once()
        self.driver.stop.assert_awaited_once()
        self.assertIsNone(runner.context)
        self.assertFalse(runner.is_running)
        cookies, user_agent = load_chrome_heartbeat_state(self.session_dir)
        self.assertEqual(cookies, {"LOGIN_INFO": "v"})
        self.assertEqual(user_agent, CHROME_UA)

    async def test_a_failed_state_save_does_not_block_the_close(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            runner = self._runner(proxy=None)
            await runner.start()
            self.context.cookies = AsyncMock(side_effect=RuntimeError("closed"))
            await runner.close()
        self.context.close.assert_awaited_once()
        self.driver.stop.assert_awaited_once()

    async def test_a_failed_launch_stops_the_driver(self):
        ensure, geo = self._patches()
        with self._fake_patchright(launch=AsyncMock(side_effect=RuntimeError("no chrome"))), ensure, geo:
            runner = self._runner(proxy=None)
            with self.assertRaises(RuntimeError):
                await runner.start()
        self.driver.stop.assert_awaited_once()
        self.assertIsNone(runner._patchright_driver)

    async def test_socks5_proxy_with_login_is_refused_before_anything_starts(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure as ensure_mock, geo:
            runner = self._runner(proxy="socks5://u:p@10.0.0.1:1080")
            with self.assertRaises(ValueError):
                await runner.start()
        ensure_mock.assert_not_called()

    async def test_camoufox_runner_is_unchanged(self):
        runner = YouTubeRunner(self.cfg, self.cfg.targets[0], session_dir=self.root / "alice")
        self.assertEqual(runner.engine, "camoufox")
        with patch("modules.youtube_runner.AsyncCamoufox") as camoufox, \
                patch("modules.youtube_runner.get_or_create_fingerprint", return_value={"pinned": True}):
            camoufox.return_value.__aenter__ = AsyncMock(return_value=self._camoufox_context())
            camoufox.return_value.__aexit__ = AsyncMock(return_value=None)
            await runner.start()
            await runner.close()
        camoufox.assert_called_once()
        self.assertFalse((self.root / "alice" / HEARTBEAT_STATE_FILE).exists())

    @staticmethod
    def _camoufox_context():
        page = MagicMock()
        page.set_default_timeout = MagicMock()
        page.set_default_navigation_timeout = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        return MagicMock(pages=[page])


class TestAccountEngineInDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "test.db")
        await self.db.connect()
        await self.db.init_schema()
        self.addAsyncCleanup(self.db.close)

    async def test_engine_defaults_to_camoufox(self):
        account = await self.db.upsert_account("alice", session_path=str(self.root / "sessions" / "alice"))
        self.assertEqual(account.engine, "camoufox")

    async def test_set_account_engine_moves_the_session_path_both_ways(self):
        sessions = self.root / "sessions"
        account = await self.db.upsert_account("alice", session_path=str(sessions / "alice"))
        chrome = await self.db.set_account_engine(account.id, "chrome")
        self.assertEqual((chrome.engine, Path(chrome.session_path)), ("chrome", sessions / "chrome" / "alice"))
        again = await self.db.set_account_engine(account.id, "chrome")
        self.assertEqual(again.session_path, chrome.session_path)  # idempotent: no nested chrome/chrome
        back = await self.db.set_account_engine(account.id, "camoufox")
        self.assertEqual((back.engine, Path(back.session_path)), ("camoufox", sessions / "alice"))
        stored = await self.db.get_account("alice")
        self.assertEqual(stored.engine, "camoufox")

    async def test_set_account_engine_rejects_unknown_engine_account_and_socks5_login(self):
        account = await self.db.upsert_account(
            "alice", session_path=str(self.root / "alice"), proxy_url="socks5://u:p@10.0.0.1:1080"
        )
        with self.assertRaises(ValueError):
            await self.db.set_account_engine(account.id, "chrome")
        self.assertEqual((await self.db.get_account("alice")).engine, "camoufox")
        with self.assertRaises(ValueError):
            await self.db.set_account_engine(account.id, "webkit")
        with self.assertRaises(RecordNotFoundError):
            await self.db.set_account_engine(9999, "chrome")

    async def test_a_chrome_account_refuses_a_socks5_proxy_with_login(self):
        account = await self.db.upsert_account("alice", session_path=str(self.root / "alice"))
        await self.db.set_account_engine(account.id, "chrome")
        with self.assertRaises(ValueError):
            await self.db.upsert_account("alice", proxy_url="socks5://u:p@10.0.0.1:1080")
        await self.db.upsert_account("alice", proxy_url="http://u:p@10.0.0.1:8080")

    async def test_migration_adds_the_engine_column_to_an_old_accounts_table(self):
        old = self.root / "old.db"
        conn = sqlite3.connect(old)
        conn.execute(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, session_path TEXT NOT NULL,"
            " proxy_url TEXT, is_active INTEGER NOT NULL DEFAULT 1, total_replies INTEGER NOT NULL DEFAULT 0,"
            " created_at TEXT NOT NULL, last_used_at TEXT)"
        )
        conn.execute("INSERT INTO accounts (name, session_path, created_at) VALUES ('legacy', 'test_sessions/legacy', 'now')")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()
        db = Database(old)
        await db.connect()
        try:
            await db.init_schema()
            await db.init_schema()  # a second start must not fail on the existing column
            legacy = await db.get_account("legacy")
        finally:
            await db.close()
        self.assertEqual((legacy.engine, legacy.session_path), ("camoufox", "test_sessions/legacy"))
        check = sqlite3.connect(old)
        columns = {row[1]: row for row in check.execute("PRAGMA table_info(accounts)")}
        check.close()
        self.assertIn("engine", columns)
        self.assertEqual(columns["engine"][4], "'camoufox'")


class TestEngineInTheWizard(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_icons_have_an_emoji_and_a_plain_form(self):
        from modules.ui.icons import engine_icon, icon_cells, other_engine

        self.assertEqual(engine_icon("camoufox", plain=False), "🦊")
        self.assertEqual(engine_icon("chrome", plain=False), "🌐")
        self.assertEqual(engine_icon("camoufox", plain=True), "[Fx]")
        self.assertEqual(engine_icon("chrome", plain=True), "[Cr]")
        self.assertEqual((icon_cells(False), icon_cells(True)), (2, 4))
        self.assertEqual((other_engine("chrome"), other_engine("camoufox")), ("camoufox", "chrome"))
        with patch.dict("os.environ", {"YT_PLAIN_UI": "1"}):
            self.assertEqual(engine_icon("chrome"), "[Cr]")

    def test_chrome_session_check_reads_the_saved_state(self):
        import asyncio

        from modules.wizard import check_account_session

        profile = self.root / "chrome" / "alice"
        self.assertFalse(check_account_session(profile, "chrome")[0])
        asyncio.run(save_chrome_heartbeat_state(profile, _context([_cookie("SID", "v", ".google.com")]), None))
        self.assertEqual(check_account_session(profile, "chrome")[1], "Гостевой режим (вход в аккаунт НЕ выполнен)")
        asyncio.run(save_chrome_heartbeat_state(profile, _context([_cookie("LOGIN_INFO", "v", ".youtube.com", 4102444800)]), None))
        self.assertTrue(check_account_session(profile, "chrome")[0])
        self.assertFalse(check_account_session(profile)[0])  # as Camoufox the folder has no cookies.sqlite

    def test_switching_the_engine_is_mirrored_into_config_yaml(self):
        from modules.wizard import _setup_engine_in_config

        path = self.root / "config.yaml"
        path.write_text(yaml.safe_dump({"browser": {"accounts": [
            {"name": "alice", "session_path": "test_sessions/alice"}, {"name": "bob", "session_path": "test_sessions/bob"},
        ]}}), encoding="utf-8")
        _setup_engine_in_config(path, "alice", "chrome", "test_sessions/chrome/alice")
        accounts = yaml.safe_load(path.read_text(encoding="utf-8"))["browser"]["accounts"]
        self.assertEqual(accounts[0], {"name": "alice", "session_path": "test_sessions/chrome/alice", "engine": "chrome"})
        self.assertEqual(accounts[1], {"name": "bob", "session_path": "test_sessions/bob"})

    def test_the_switch_asks_before_leaving_a_signed_in_profile(self):
        from modules import wizard

        acc = MagicMock(engine="camoufox")
        acc.id, acc.name = 1, "alice"
        with patch.object(wizard, "prompt_user", return_value="n") as ask, patch.object(wizard, "_pools_db") as db:
            self.assertIsNone(wizard._setup_switch_engine(self.root / "config.yaml", acc, logged_in=True))
        self.assertIn("отдельной авторизации в Google", ask.call_args.args[0])
        db.assert_not_called()
        updated = MagicMock(engine="chrome", session_path="test_sessions/chrome/alice")
        with patch.object(wizard, "prompt_user", return_value="y"), \
                patch.object(wizard, "_pools_db", return_value=updated) as db, \
                patch.object(wizard, "_setup_engine_in_config") as mirror:
            notice = wizard._setup_switch_engine(self.root / "config.yaml", acc, logged_in=True)
        self.assertEqual(notice[0], "ok")
        db.assert_called_once()
        mirror.assert_called_once_with(self.root / "config.yaml", "alice", "chrome", "test_sessions/chrome/alice")
        with patch.object(wizard, "prompt_user") as ask, patch.object(wizard, "_pools_db", return_value=updated):
            with patch.object(wizard, "_setup_engine_in_config"):
                wizard._setup_switch_engine(self.root / "config.yaml", acc, logged_in=False)
        ask.assert_not_called()  # nothing to lose: no question


if __name__ == "__main__":
    unittest.main()
