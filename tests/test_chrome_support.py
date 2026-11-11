"""Google Chrome (patchright) next to Camoufox: paths, proxy rule, profile prep, heartbeat state, runner, DB, UI helpers."""

import asyncio
import asyncio
import json
import re
import socket
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml
from loguru import logger
from pydantic import ValidationError

from config import (
    CHROME_SOCKS5_AUTH_ERROR,
    AccountSettings,
    BrowserSettings,
    TargetSettings,
    get_account_session_path,
    validate_proxy_for_engine,
)
from database import AccountRecord, Database, RecordNotFoundError
from modules import chrome_launcher
from modules.chrome_launcher import (
    COUNTRY_TO_TIMEZONE,
    ChromeGeo,
    accept_languages_for,
    city_geolocation,
    ensure_chrome_executable,
    get_chrome_args,
    get_window_size,
    prepare_chrome_profile,
    resolve_chrome_geo,
    resolve_geo_timezone,
    socks5_host_resolver_rule,
)
from modules.chrome_session import (
    HEARTBEAT_STATE_FILE,
    chrome_login_info_expiry,
    load_chrome_client_hints,
    load_chrome_heartbeat_state,
    save_chrome_heartbeat_state,
)
from modules.geo_guard import ExitGeo, GeoLockError, NetworkGuardError
from modules.session_heartbeat import (
    check_account_heartbeat,
    check_session_heartbeat,
    check_session_offline,
    chrome_brands_for_major,
    chrome_client_hints,
    chrome_headers,
    chrome_impersonate_profile,
)
from modules.youtube_runner import YouTubeRunner
from secret_store import protect_secret

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
        self.assertEqual(prefs["intl"]["selected_languages"], "pl,en")  # a foreign key of the section stays
        self.assertEqual(prefs["intl"]["accept_languages"], "en-US,en")

    def test_languages_follow_the_locale(self):
        for locale, accept in (
            ("ru-RU", "ru-RU,ru,en-US,en"), ("en-GB", "en-GB,en"), ("en-US", "en-US,en"), ("de-DE", "de-DE,de,en-US,en"),
        ):
            with self.subTest(locale=locale):
                self.assertEqual(accept_languages_for(locale), accept)
                prepare_chrome_profile(self.profile, locale)
                prefs = json.loads((self.profile / "Default" / "Preferences").read_text(encoding="utf-8"))
                self.assertEqual(prefs["intl"]["accept_languages"], accept)
                self.assertEqual(prefs["spellcheck"]["dictionaries"], [locale])
                self.assertNotIn(" spellcheck", prefs)  # no key with a typo
                self.assertEqual([key for key in prefs if key != key.strip() or " " in key], [])

    def test_default_locale_and_underscore_form(self):
        prepare_chrome_profile(self.profile)
        prefs = json.loads((self.profile / "Default" / "Preferences").read_text(encoding="utf-8"))
        self.assertEqual(prefs["intl"]["accept_languages"], "en-US,en")
        self.assertEqual(accept_languages_for("pl_PL"), "pl-PL,pl,en-US,en")
        self.assertEqual(accept_languages_for("en"), "en")

    def test_a_stale_system_language_is_overwritten_and_wrong_types_are_replaced(self):
        default = self.profile / "Default"
        default.mkdir(parents=True)
        (default / "Preferences").write_text(json.dumps({
            "intl": {"accept_languages": "ru-RU,ru", "other": 1},
            "spellcheck": ["not", "a", "dict"],
            "webrtc": "broken",
            "net": 5,
            "dns_prefetching": None,
        }), encoding="utf-8")
        prepare_chrome_profile(self.profile, "de-DE")
        prefs = json.loads((default / "Preferences").read_text(encoding="utf-8"))
        self.assertEqual(prefs["intl"], {"accept_languages": "de-DE,de,en-US,en", "other": 1})
        self.assertEqual(prefs["spellcheck"], {"dictionaries": ["de-DE"]})
        self.assertIsInstance(prefs["webrtc"], dict)
        self.assertIsInstance(prefs["net"], dict)
        self.assertIsInstance(prefs["dns_prefetching"], dict)

    def test_webrtc_and_dns_are_locked_in_the_profile_too(self):
        default = self.profile / "Default"
        default.mkdir(parents=True)
        (default / "Preferences").write_text(json.dumps({
            "webrtc": {"keep_me": 1, "ip_handling_policy": "default"}, "net": {"keep_me": 2},
        }), encoding="utf-8")
        prepare_chrome_profile(self.profile)
        prefs = json.loads((default / "Preferences").read_text(encoding="utf-8"))
        self.assertEqual(prefs["webrtc"], {
            "keep_me": 1, "ip_handling_policy": "disable_non_proxied_udp",
            "multiple_routes_enabled": False, "nonproxied_udp_enabled": False,
        })
        self.assertEqual(prefs["net"], {"keep_me": 2, "network_prediction_options": 2})
        self.assertEqual(prefs["dns_prefetching"], {"enabled": False})

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

    def _warnings_of(self, action):
        messages: list[str] = []
        sink_id = logger.add(lambda message: messages.append(message.record["message"]), level="WARNING")
        try:
            with self.assertRaises(RuntimeError) as caught:
                action()
        finally:
            logger.remove(sink_id)
        return messages, caught.exception

    def test_a_busy_lock_logs_a_warning_before_the_error(self):
        self.profile.mkdir(parents=True)
        (self.profile / "lockfile").write_text("x")
        with patch.object(Path, "unlink", side_effect=PermissionError(32, "in use")):
            messages, error = self._warnings_of(lambda: prepare_chrome_profile(self.profile))
        self.assertEqual(len(messages), 1)
        self.assertIn("занят живым процессом Chrome", messages[0])
        self.assertIn(str(self.profile), messages[0])
        self.assertIn("lockfile", messages[0])
        self.assertIsInstance(error, RuntimeError)

    def test_a_busy_preferences_file_logs_a_warning_and_leaves_no_tmp_file(self):
        with patch("modules.chrome_launcher.os.replace", side_effect=PermissionError(32, "in use")):
            messages, error = self._warnings_of(lambda: prepare_chrome_profile(self.profile))
        self.assertEqual(len(messages), 1)
        self.assertIn("Preferences", messages[0])
        self.assertIn("WinError 32", str(error))
        self.assertFalse((self.profile / "Default" / "Preferences.tmp").exists())

    def test_chrome_args(self):
        args = get_chrome_args("de-DE")
        self.assertIn("--hide-crash-restore-bubble", args)
        self.assertIn("--lang=de-DE", args)
        self.assertIn("--lang=en-US", get_chrome_args())
        for flag in (
            "--disable-quic", "--webrtc-ip-handling-policy=disable_non_proxied_udp", "--enforce-webrtc-ip-permission-check",
            "--dns-prefetch-disable", "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, args)

    def test_there_is_exactly_one_disable_features_switch(self):
        switches = [arg for arg in get_chrome_args("de-DE", "alice", "socks5://10.0.0.1:1080") if arg.startswith("--disable-features=")]
        self.assertEqual(len(switches), 1)  # a second one would replace the first in Chrome
        features = switches[0].split("=", 1)[1].split(",")
        self.assertNotIn("WebAuthentication", features)  # without it window.PublicKeyCredential stays
        for feature in ("IdentityConsistency", "SigninInterceptBubbleV2", "SyncPromo", "OptimizationHints",
                        "PasskeyUnlockManager", "CalculateNativeWinOcclusion"):
            self.assertIn(feature, features)
        self.assertEqual(len(features), len(set(features)))

    def test_no_resolver_rule_without_a_socks5_proxy(self):
        for proxy in (None, "http://u:p@10.0.0.1:8080", "10.0.0.1:8080:u:p", "https://10.0.0.1:443"):
            with self.subTest(proxy=proxy):
                self.assertFalse([arg for arg in get_chrome_args(proxy_url=proxy) if arg.startswith("--host-resolver-rules")])


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


def _exit_geo_patch(geo: ExitGeo | None):
    """Patch of the exit-IP lookup (`create=True`: the function is written in geo_guard in parallel)."""
    return patch("modules.geo_guard.resolve_exit_geo", AsyncMock(return_value=geo), create=True)


def _exit_geo(country=None, timezone=None, latitude=None, longitude=None) -> ExitGeo:
    return ExitGeo(ip="203.0.113.7", country=country, timezone=timezone, latitude=latitude, longitude=longitude)


class TestResolveGeoTimezone(unittest.IsolatedAsyncioTestCase):
    async def test_exit_country_gives_timezone_and_locale(self):
        for country, timezone_id, locale in (
            ("US", "America/New_York", "en-US"), ("DE", "Europe/Berlin", "de-DE"), ("GB", "Europe/London", "en-GB"),
            ("FR", "Europe/Paris", "fr-FR"), ("RU", "Europe/Moscow", "ru-RU"), ("TR", "Europe/Istanbul", "tr-TR"),
        ):
            with self.subTest(country=country), _exit_geo_patch(_exit_geo(country)):
                self.assertEqual(await resolve_geo_timezone("http://p:1"), (timezone_id, locale))

    async def test_account_country_and_locale_are_the_fallback(self):
        with _exit_geo_patch(None):
            self.assertEqual(await resolve_geo_timezone(None, "Turkey"), ("Europe/Istanbul", "tr-TR"))
            self.assertEqual(await resolve_geo_timezone(None, "TR", "en-GB"), ("Europe/Istanbul", "en-GB"))

    async def test_explicit_locale_wins_over_the_country_default(self):
        with _exit_geo_patch(_exit_geo("DE")):
            self.assertEqual(await resolve_geo_timezone(None, None, "pl-PL"), ("Europe/Berlin", "pl-PL"))

    async def test_unknown_country_keeps_the_system_timezone(self):
        with _exit_geo_patch(None):
            self.assertEqual(await resolve_geo_timezone(None), (None, "en-US"))
        with _exit_geo_patch(_exit_geo("ZW")):
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
        self.assertIsNone(load_chrome_client_hints(self.profile))

    async def test_real_client_hints_are_saved_and_loaded(self):
        context = _context([_cookie("LOGIN_INFO", "v", ".youtube.com")])
        hints = {"brands": [{"brand": "Not_A Brand", "version": "99"}, {"brand": "Chromium", "version": "131"},
                            {"brand": "Google Chrome", "version": "131"}], "mobile": False, "platform": "Windows"}
        self.assertTrue(await save_chrome_heartbeat_state(self.profile, context, CHROME_UA, hints))
        loaded = load_chrome_client_hints(self.profile)
        self.assertEqual(loaded["brands"], hints["brands"])
        self.assertEqual((loaded["mobile"], loaded["platform"], loaded["major"], loaded["source"]), (False, "Windows", 131, "browser"))
        # the cookie and UA API is unchanged by the hints
        self.assertEqual(load_chrome_heartbeat_state(self.profile), ({"LOGIN_INFO": "v"}, CHROME_UA))
        if sys.platform == "win32":
            self.assertNotIn(b"Not_A Brand", (self.profile / HEARTBEAT_STATE_FILE).read_bytes())

    async def test_without_hints_the_user_agent_major_is_stored(self):
        await save_chrome_heartbeat_state(self.profile, _context([_cookie("LOGIN_INFO", "v", ".youtube.com")]), CHROME_UA)
        loaded = load_chrome_client_hints(self.profile)
        self.assertEqual(loaded, {"brands": [], "mobile": False, "platform": "Windows", "major": 131, "source": "user_agent"})
        await save_chrome_heartbeat_state(self.profile, _context([_cookie("LOGIN_INFO", "v", ".youtube.com")]), None)
        self.assertEqual(load_chrome_client_hints(self.profile), loaded)  # a save with nothing new changes nothing

    async def test_neither_user_agent_nor_hints_stores_no_hints(self):
        await save_chrome_heartbeat_state(self.profile, _context([_cookie("LOGIN_INFO", "v", ".youtube.com")]), None)
        self.assertIsNone(load_chrome_client_hints(self.profile))

    async def test_a_later_save_keeps_the_real_hints_while_they_match_the_user_agent(self):
        context = _context([_cookie("LOGIN_INFO", "v", ".youtube.com")])
        hints = {"brands": [{"brand": "Chromium", "version": "131"}, {"brand": "Google Chrome", "version": "131"}],
                 "mobile": False, "platform": "Windows"}
        await save_chrome_heartbeat_state(self.profile, context, CHROME_UA, hints)
        await save_chrome_heartbeat_state(self.profile, context, None)  # nothing new: everything stays
        await save_chrome_heartbeat_state(self.profile, context, CHROME_UA)  # same UA, no hints: still the real ones
        kept = load_chrome_client_hints(self.profile)
        self.assertEqual((kept["source"], kept["brands"]), ("browser", hints["brands"]))
        # the browser updated: hints of the old version must not outlive their user agent
        await save_chrome_heartbeat_state(self.profile, context, CHROME_UA.replace("131", "134"))
        replaced = load_chrome_client_hints(self.profile)
        self.assertEqual((replaced["source"], replaced["brands"], replaced["major"]), ("user_agent", [], 134))

    async def test_unusable_hints_are_ignored(self):
        context = _context([_cookie("LOGIN_INFO", "v", ".youtube.com")])
        for bad in ({"brands": []}, {"brands": "Chromium"}, {"brands": [{"brand": "", "version": "1"}]}, ["x"], {}):
            with self.subTest(bad=bad):
                await save_chrome_heartbeat_state(self.profile, context, CHROME_UA, bad)
                self.assertEqual(load_chrome_client_hints(self.profile)["source"], "user_agent")

    def test_a_state_file_from_before_the_hints_is_still_read(self):
        self.profile.mkdir(parents=True)
        old = {"cookies": [{"name": "LOGIN_INFO", "value": "v", "domain": ".youtube.com", "expires": 4102444800}],
               "user_agent": CHROME_UA, "updated_at": "2026-01-01T00:00:00+00:00"}
        (self.profile / HEARTBEAT_STATE_FILE).write_bytes(protect_secret(json.dumps(old)).encode("utf-8"))
        self.assertEqual(load_chrome_heartbeat_state(self.profile), ({"LOGIN_INFO": "v"}, CHROME_UA))
        self.assertIsNone(load_chrome_client_hints(self.profile))
        self.assertEqual(chrome_login_info_expiry(self.profile), (True, 4102444800.0))


class TestChromeHeartbeatCheck(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.profile = Path(self.temp.name) / "chrome" / "alice"
        guard = patch("modules.session_heartbeat.ensure_network_ready", new_callable=AsyncMock, return_value=None)
        self.guard = guard.start()  # the network guard asks the internet: never in these tests
        self.addCleanup(guard.stop)

    async def _save(self, cookies=None, user_agent=CHROME_UA, client_hints=None):
        cookies = cookies or [_cookie("LOGIN_INFO", "v", ".youtube.com", 4102444800)]
        await save_chrome_heartbeat_state(self.profile, _context(cookies), user_agent, client_hints)

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
        self.assertNotIn("Origin", headers)  # a top-level GET navigation carries none
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

    # --- sec-ch-ua follows the browser's version -------------------------------------------------------------

    def test_the_brand_list_of_a_major_matches_what_curl_cffi_captured_from_real_chrome(self):
        captured = {  # curl_cffi's default `sec-ch-ua` of these profiles, read off its fingerprint data
            120: '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
            124: '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            131: '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            133: '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
            142: '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
            146: '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
            150: '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
        }
        for major, header in captured.items():
            with self.subTest(major=major):
                ua = CHROME_UA.replace("131", str(major))
                self.assertEqual(chrome_client_hints(ua)["sec-ch-ua"], header)
                self.assertEqual(len(chrome_brands_for_major(major)), 3)

    def test_real_brands_make_the_exact_header_in_their_own_order(self):
        hints = {"brands": [{"brand": "Chromium", "version": "134"}, {"brand": "Not:A-Brand", "version": "24"},
                            {"brand": "Google Chrome", "version": "134"}], "mobile": False, "platform": "Windows"}
        result = chrome_client_hints(CHROME_UA.replace("131", "134"), hints)
        self.assertEqual(result["sec-ch-ua"], '"Chromium";v="134", "Not:A-Brand";v="24", "Google Chrome";v="134"')
        self.assertEqual(result["sec-ch-ua-mobile"], "?0")
        self.assertEqual(result["sec-ch-ua-platform"], '"Windows"')

    def test_headless_brands_never_reach_http(self):
        hints = {"brands": [{"brand": "HeadlessChrome", "version": "134"}, {"brand": "Chromium", "version": "134"},
                            {"brand": "Not.A/Brand", "version": "24"}], "mobile": True, "platform": "Windows"}
        header = chrome_client_hints(CHROME_UA.replace("131", "134"), hints)
        self.assertEqual(header["sec-ch-ua"], '"Google Chrome";v="134", "Chromium";v="134", "Not.A/Brand";v="24"')
        self.assertNotIn("Headless", " ".join(header.values()))
        self.assertEqual(header["sec-ch-ua-mobile"], "?0")  # a desktop profile, whatever the flag says

    def test_the_platform_follows_the_hints_then_the_user_agent(self):
        linux_ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
        self.assertEqual(chrome_client_hints(linux_ua)["sec-ch-ua-platform"], '"Linux"')
        hints = {"brands": [{"brand": "Chromium", "version": "134"}], "platform": "Windows"}
        self.assertEqual(chrome_client_hints(linux_ua, hints)["sec-ch-ua-platform"], '"Windows"')

    async def test_the_request_header_carries_the_major_of_the_saved_user_agent(self):
        await self._save(user_agent=CHROME_UA.replace("131", "134"))
        session = self._session()
        with patch("modules.session_heartbeat.AsyncSession", return_value=session) as factory:
            await check_session_heartbeat(self.profile, "alice", engine="chrome")
        headers = factory.call_args.kwargs["headers"]
        self.assertEqual(re.findall(r'v="(\d+)"', headers["sec-ch-ua"]).count("134"), 2)
        self.assertNotIn("131", headers["sec-ch-ua"])
        self.assertEqual(headers["sec-ch-ua-mobile"], "?0")

    async def test_the_request_header_uses_the_saved_real_brands(self):
        hints = {"brands": [{"brand": "Chromium", "version": "134"}, {"brand": "Not:A-Brand", "version": "24"},
                            {"brand": "HeadlessChrome", "version": "134"}], "mobile": False, "platform": "Windows"}
        await self._save(user_agent=CHROME_UA.replace("131", "134").replace("Chrome/", "HeadlessChrome/"), client_hints=hints)
        session = self._session()
        with patch("modules.session_heartbeat.AsyncSession", return_value=session) as factory:
            await check_session_heartbeat(self.profile, "alice", engine="chrome")
        headers = factory.call_args.kwargs["headers"]
        self.assertEqual(headers["sec-ch-ua"], '"Chromium";v="134", "Not:A-Brand";v="24", "Google Chrome";v="134"')
        self.assertNotIn("Headless", " ".join(headers.values()))

    async def test_a_state_without_a_user_agent_still_sends_a_windows_chrome(self):
        await self._save(user_agent=None)
        session = self._session()
        with patch("modules.session_heartbeat.AsyncSession", return_value=session) as factory:
            await check_session_heartbeat(self.profile, "alice", engine="chrome")
        kwargs = factory.call_args.kwargs
        major = kwargs["impersonate"].removeprefix("chrome")
        self.assertIn(f"Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0", kwargs["headers"]["User-Agent"])
        self.assertEqual(kwargs["headers"]["sec-ch-ua-platform"], '"Windows"')
        self.assertIn(f'"Google Chrome";v="{major}"', kwargs["headers"]["sec-ch-ua"])

    def test_tls_profile_edge_cases(self):
        def profile(ua):
            return int(re.fullmatch(r"chrome(\d+)", chrome_impersonate_profile(ua)).group(1))

        from curl_cffi.requests import BrowserType
        known = sorted(int(m.group(1)) for v in BrowserType if (m := re.fullmatch(r"chrome(\d+)", str(v.value))))
        self.assertEqual(profile("Chrome/999.0.0.0"), known[-1])  # newer than every profile: the newest
        self.assertEqual(profile("Chrome/1.0.0.0"), known[0])  # older than every profile: the oldest
        self.assertEqual(profile(f"Chrome/{known[-1]}.0.0.0"), known[-1])
        self.assertEqual(profile(f"Chrome/{known[-1] - 1}.0.0.0"), known[-2])
        self.assertEqual(profile("Mozilla/5.0 Firefox/130.0"), known[-1])  # not Chrome: the newest
        self.assertEqual(profile("Chrome/134.0.0.0"), max(v for v in known if v <= 134))  # 133a is never picked

    # --- the request never leaves without the account's proxy ------------------------------------------------

    async def test_the_account_proxy_is_what_curl_gets_and_the_guard_checks(self):
        await self._save()
        session = self._session()
        with patch("modules.session_heartbeat.AsyncSession", return_value=session) as factory:
            await check_session_heartbeat(self.profile, "alice", "socks5://u:p@1.2.3.4:1080", engine="chrome")
        self.assertEqual(factory.call_args.kwargs["proxy"], "socks5h://u:p@1.2.3.4:1080")
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.guard.assert_awaited_once_with("socks5://u:p@1.2.3.4:1080", require_proxy=False)

    async def test_an_invalid_account_proxy_sends_no_request(self):
        await self._save()
        with patch("modules.session_heartbeat.AsyncSession", side_effect=AssertionError("network used")) as factory:
            status = await check_session_heartbeat(self.profile, "alice", "socks5://user:SECRETPW@nohost", engine="chrome")
        factory.assert_not_called()
        self.guard.assert_not_awaited()
        self.assertFalse(status.is_alive)
        self.assertTrue(status.login_info_present)
        self.assertIn("request not sent", status.details)
        self.assertNotIn("SECRETPW", status.details)

    async def test_chrome_account_goes_through_its_own_proxy_and_never_falls_back(self):
        await self._save()
        account = AccountRecord(id=1, name="alice", session_path=str(self.profile), created_at="now", engine="chrome",
                                proxy_url="http://own:80")
        session = self._session()
        with patch("modules.session_heartbeat.AsyncSession", return_value=session) as factory:
            status = await check_account_heartbeat(account)
        self.assertTrue(status.is_alive)
        self.assertEqual(factory.call_args.kwargs["proxy"], "http://own:80")
        with patch("modules.session_heartbeat.AsyncSession", side_effect=AssertionError("network used")) as factory:
            bad = await check_account_heartbeat(account.model_copy(update={"proxy_url": "not a proxy"}))
        factory.assert_not_called()  # a broken proxy of the account never turns into a direct request
        self.assertFalse(bad.is_alive)

    async def test_chrome_check_stops_when_the_guard_refuses_or_a_proxy_is_required(self):
        await self._save()
        for proxy, require, message in (("http://1.2.3.4:80", False, "IP совпадает с IP хоста"), (None, True, "нужен прокси")):
            with self.subTest(proxy=proxy):
                self.guard.reset_mock()
                self.guard.side_effect = NetworkGuardError(message)
                with patch("modules.session_heartbeat.AsyncSession", side_effect=AssertionError("network used")) as factory:
                    status = await check_session_heartbeat(self.profile, "alice", proxy, engine="chrome", require_proxy=require)
                factory.assert_not_called()
                self.assertFalse(status.is_alive)
                self.assertEqual(status.details, f"сеть: {message}")
                self.guard.assert_awaited_once_with(proxy, require_proxy=require)

    async def test_chrome_without_any_proxy_goes_out_directly_without_a_guard(self):
        await self._save()
        session = self._session()
        with patch("modules.session_heartbeat.AsyncSession", return_value=session) as factory:
            status = await check_session_heartbeat(self.profile, "alice", engine="chrome")
        self.assertTrue(status.is_alive)
        self.assertIsNone(factory.call_args.kwargs["proxy"])
        self.guard.assert_not_awaited()

    async def test_chrome_heartbeat_reads_neither_cookies_sqlite_nor_the_camoufox_fingerprint(self):
        await self._save()
        (self.profile / "cookies.sqlite").write_bytes(b"not a database")
        session = self._session()
        with patch("modules.session_heartbeat.AsyncSession", return_value=session), \
             patch("modules.session_heartbeat.read_youtube_cookies", side_effect=AssertionError("cookies.sqlite read")), \
             patch("modules.session_heartbeat.read_login_info_metadata", side_effect=AssertionError("cookies.sqlite read")), \
             patch("modules.session_heartbeat.fingerprint_headers", side_effect=AssertionError("camoufox headers")):
            status = await check_session_heartbeat(self.profile, "alice", engine="chrome")
        self.assertTrue(status.is_alive)

    async def test_a_local_server_sees_one_sec_ch_ua_and_it_is_ours(self):
        """Real curl_cffi against 127.0.0.1 (no internet): our headers replace the impersonation defaults, not duplicate them."""
        from curl_cffi.requests import AsyncSession

        captured: list[str] = []
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)

        def serve():
            conn, _ = server.accept()
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            captured.append(data.decode("latin1"))
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            conn.close()

        thread = threading.Thread(target=serve)
        thread.start()
        self.addCleanup(server.close)
        hints = {"brands": [{"brand": "Chromium", "version": "134"}, {"brand": "Not:A-Brand", "version": "24"},
                            {"brand": "Google Chrome", "version": "134"}], "mobile": False, "platform": "Windows"}
        user_agent = CHROME_UA.replace("131", "134")
        headers = chrome_headers(user_agent, "de-DE", hints)
        async with AsyncSession(impersonate=chrome_impersonate_profile(user_agent), headers=headers, trust_env=False) as session:
            await session.get(f"http://127.0.0.1:{server.getsockname()[1]}/", cookies={"LOGIN_INFO": "v"})
        await asyncio.to_thread(thread.join, 5)
        lines = captured[0].split("\r\n")
        for name in ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "user-agent", "accept-language"):
            self.assertEqual(sum(line.lower().startswith(name + ":") for line in lines), 1, name)
        self.assertIn('sec-ch-ua: "Chromium";v="134", "Not:A-Brand";v="24", "Google Chrome";v="134"', lines)
        self.assertIn('sec-ch-ua-platform: "Windows"', lines)
        self.assertIn(f"User-Agent: {user_agent}", lines)
        self.assertFalse(any(line.lower().startswith("origin:") for line in lines))


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
        # The launcher and the network guard are tested elsewhere: here only how the runner calls them
        self.prepare = self._start_patch("modules.youtube_runner.prepare_chrome_profile")
        self.chrome_args = self._start_patch(
            "modules.youtube_runner.get_chrome_args", return_value=["--lang=de-DE", "--hide-crash-restore-bubble"]
        )
        self.network = self._start_patch("modules.youtube_runner.ensure_network_ready", new_callable=AsyncMock, return_value=None)
        self.geo_lock = self._start_patch("modules.youtube_runner.check_geo_lock")
        self.read_geo = self._start_patch("modules.youtube_runner.read_session_geo", return_value=None)
        self.write_geo = self._start_patch("modules.youtube_runner.write_session_geo")

    def _start_patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    @staticmethod
    def _geo(timezone="Europe/Berlin", locale="de-DE", country="DE", geolocation=None):
        return SimpleNamespace(timezone=timezone, locale=locale, country=country, geolocation=geolocation)

    @staticmethod
    def _page_fake():
        page = MagicMock()
        page.evaluate = AsyncMock(return_value=CHROME_UA)
        page.close = AsyncMock()
        page.set_default_timeout = MagicMock()
        page.set_default_navigation_timeout = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        return page

    @staticmethod
    def _identity_evaluate(user_agent=CHROME_UA, hints=None):
        """`page.evaluate` that answers the userAgent read and the userAgentData read apart."""
        async def evaluate(script):
            if "userAgentData" in script:
                if isinstance(hints, Exception):
                    raise hints
                return hints
            return user_agent
        return evaluate

    async def _started_runner(self, **kwargs):
        runner = self._runner(proxy=None, **kwargs)
        await runner.start()
        return runner

    def _fake_patchright(self, launch=None):
        self.page = MagicMock()
        self.page.evaluate = AsyncMock(return_value=CHROME_UA)
        self.page.close = AsyncMock()
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
            patch("modules.youtube_runner.resolve_chrome_geo", AsyncMock(return_value=self._geo())),
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
        self.assertNotIn("locale", kwargs)  # the language comes from --lang and Preferences, not from patchright
        self.assertEqual(kwargs["args"], self.chrome_args.return_value)
        self.assertEqual(kwargs["proxy"], {"server": "http://10.0.0.1:8080", "username": "u", "password": "p"})
        self.assertNotIn("geolocation", kwargs)  # the geo has no city point
        self.assertNotIn("permissions", kwargs)
        self.prepare.assert_called_once_with(self.session_dir, "de-DE")  # the profile is prepared for the launch locale
        self.chrome_args.assert_called_once_with(
            locale="de-DE", account_name="alice", proxy_url="http://u:p@10.0.0.1:8080"
        )
        self.assertEqual(runner._cached_user_agent, CHROME_UA)
        self.assertTrue(runner.is_running)
        self.assertEqual(runner.engine, "chrome")

    async def test_the_account_locale_and_country_reach_the_geo_lookup(self):
        ensure, _ = self._patches()
        geo = AsyncMock(return_value=self._geo(timezone=None, locale="pl-PL", country="PL"))
        with self._fake_patchright(), ensure, patch("modules.youtube_runner.resolve_chrome_geo", geo):
            await self._runner(proxy=None, locale="pl-PL", geo_country="PL").start()
        geo.assert_awaited_once_with(None, "PL", "pl-PL", "alice")

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

    # ---- tabs

    async def test_the_locale_is_resolved_before_the_profile_is_prepared(self):
        ensure, _ = self._patches()
        order = MagicMock()
        geo = AsyncMock(side_effect=lambda *a: order.geo(*a) or self._geo())
        self.prepare.side_effect = lambda *a: order.prepare(*a)
        self.network.side_effect = lambda *a, **k: order.network()
        with self._fake_patchright(), ensure, patch("modules.youtube_runner.resolve_chrome_geo", geo):
            await self._runner(proxy=None).start()
        self.assertEqual([c[0] for c in order.method_calls], ["network", "geo", "prepare"])

    async def test_the_first_tab_is_reused_and_no_new_tab_is_opened(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            page = await self._runner(proxy=None).start()
        self.assertIs(page, self.page)
        self.context.new_page.assert_not_awaited()

    async def test_a_new_tab_is_opened_only_when_the_profile_has_none(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            fresh = self._page_fake()
            self.context.pages = []
            self.context.new_page = AsyncMock(return_value=fresh)
            page = await self._runner(proxy=None).start()
        self.assertIs(page, fresh)
        self.context.new_page.assert_awaited_once()

    async def test_restored_extra_tabs_are_closed_and_the_working_tab_is_kept(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            blank, newtab = self._page_fake(), self._page_fake()
            blank.url, newtab.url = "about:blank", "chrome://newtab/"
            self.context.pages = [self.page, blank, newtab]
            runner = self._runner(proxy=None)
            page = await runner.start()
        self.assertIs(page, self.page)
        blank.close.assert_awaited_once()
        newtab.close.assert_awaited_once()
        self.page.close.assert_not_awaited()
        self.context.new_page.assert_not_awaited()
        self.assertTrue(runner.is_running)

    async def test_a_tab_that_refuses_to_close_does_not_fail_the_start(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            stuck, other = self._page_fake(), self._page_fake()
            stuck.close = AsyncMock(side_effect=RuntimeError("target closed"))
            self.context.pages = [self.page, stuck, other]
            page = await self._runner(proxy=None).start()
        self.assertIs(page, self.page)
        other.close.assert_awaited_once()  # the next tab is still handled

    # ---- client hints

    async def test_client_hints_are_cached_at_start_and_saved_at_close(self):
        ensure, geo = self._patches()
        hints = {"brands": [{"brand": "Chromium", "version": "131"}, {"brand": "Google Chrome", "version": "131"}],
                 "mobile": False, "platform": "Windows"}
        save = AsyncMock(return_value=True)
        with self._fake_patchright(), ensure, geo, patch("modules.youtube_runner.save_chrome_heartbeat_state", save):
            self.page.evaluate = AsyncMock(side_effect=self._identity_evaluate(hints=hints))
            runner = await self._started_runner()
            self.assertEqual(runner._cached_client_hints, hints)
            await runner.close()
        save.assert_awaited_once_with(
            profile_dir=self.session_dir, context=self.context, user_agent=CHROME_UA, client_hints=hints
        )

    async def test_the_client_hints_read_is_plain_evaluate_without_cdp(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            await self._runner(proxy=None).start()
        scripts = [call.args[0] for call in self.page.evaluate.await_args_list]
        self.assertTrue(any("navigator.userAgentData" in script for script in scripts))
        self.context.new_cdp_session.assert_not_called()

    async def test_missing_or_broken_client_hints_are_none_and_the_start_goes_on(self):
        ensure, geo = self._patches()
        for hints in (None, RuntimeError("navigating"), "nonsense", {"brands": "x"}):
            with self.subTest(hints=repr(hints)), self._fake_patchright(), ensure, geo:
                self.page.evaluate = AsyncMock(side_effect=self._identity_evaluate(hints=hints))
                runner = await self._started_runner()
                self.assertIsNone(runner._cached_client_hints)
                self.assertEqual(runner._cached_user_agent, CHROME_UA)
                await runner.close()

    async def test_a_failed_user_agent_read_does_not_lose_the_hints(self):
        ensure, geo = self._patches()
        hints = {"brands": [{"brand": "Chromium", "version": "131"}], "mobile": False, "platform": "Windows"}

        async def evaluate(script):
            if "userAgentData" in script:
                return hints
            raise RuntimeError("navigating")

        with self._fake_patchright(), ensure, geo:
            self.page.evaluate = AsyncMock(side_effect=evaluate)
            runner = await self._started_runner()
        self.assertIsNone(runner._cached_user_agent)
        self.assertEqual(runner._cached_client_hints, hints)

    # ---- the driver never outlives the session

    async def test_the_driver_is_stopped_even_when_the_context_close_fails(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            runner = await self._started_runner()
            self.context.close = AsyncMock(side_effect=RuntimeError("already closed"))
            await runner.close()
        self.context.close.assert_awaited_once()
        self.driver.stop.assert_awaited_once()
        self.assertIsNone(runner._patchright_driver)

    async def test_the_driver_is_stopped_even_when_the_state_save_raises_anything(self):
        ensure, geo = self._patches()
        save = AsyncMock(side_effect=asyncio.CancelledError())
        with self._fake_patchright(), ensure, geo, patch("modules.youtube_runner.save_chrome_heartbeat_state", save):
            runner = await self._started_runner()
            with self.assertRaises(asyncio.CancelledError):
                await runner.close()
        self.driver.stop.assert_awaited_once()

    async def test_a_driver_that_fails_to_stop_does_not_raise(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            runner = await self._started_runner()
            self.driver.stop = AsyncMock(side_effect=RuntimeError("node is gone"))
            await runner.close()
        self.context.close.assert_awaited_once()

    async def test_a_failure_after_the_launch_closes_the_context_and_stops_the_driver(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            self.context.pages = []
            self.context.new_page = AsyncMock(side_effect=RuntimeError("no target"))
            runner = self._runner(proxy=None)
            with self.assertRaises(RuntimeError):
                await runner.start()
        self.context.close.assert_awaited_once()
        self.driver.stop.assert_awaited_once()
        self.assertIsNone(runner._patchright_driver)
        self.assertIsNone(runner.context)
        self.assertFalse(runner.is_running)

    async def test_close_without_a_start_is_a_noop(self):
        await self._runner(proxy=None).close()

    # ---- network guard (fail closed)

    async def test_a_network_guard_error_stops_chrome_before_any_driver_or_profile_work(self):
        ensure, geo = self._patches()
        self.network.side_effect = NetworkGuardError("proxy is down")
        with self._fake_patchright() as _, ensure as ensure_mock, geo as geo_mock:
            runner = self._runner(proxy="http://10.0.0.1:8080")
            with self.assertRaises(NetworkGuardError):
                await runner.start()
            sys.modules["patchright.async_api"].async_playwright.assert_not_called()
        ensure_mock.assert_not_called()
        geo_mock.assert_not_called()
        self.prepare.assert_not_called()
        self.assertIsNone(runner._patchright_driver)
        self.assertIsNone(runner.context)

    async def test_a_network_guard_error_stops_camoufox_before_the_browser_is_created(self):
        runner = YouTubeRunner(self.cfg, self.cfg.targets[0], session_dir=self.root / "alice", proxy="http://10.0.0.1:8080")
        self.network.side_effect = NetworkGuardError("proxy is transparent")
        with patch("modules.youtube_runner.AsyncCamoufox") as camoufox:
            with self.assertRaises(NetworkGuardError):
                await runner.start()
        camoufox.assert_not_called()
        self.assertIsNone(runner.context)

    async def test_the_proxy_and_the_require_proxy_setting_reach_the_guard(self):
        ensure, geo = self._patches()
        self.cfg = _browser_settings(self.root, require_proxy=True)
        with self._fake_patchright(), ensure, geo:
            await self._runner(proxy="http://10.0.0.1:8080").start()
        self.network.assert_awaited_once_with("http://10.0.0.1:8080", require_proxy=True)

    async def test_without_require_proxy_a_proxyless_start_passes_none_and_is_not_blocked(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            await self._runner(proxy=None).start()
        self.network.assert_awaited_once_with(None, require_proxy=False)

    async def test_require_proxy_without_a_proxy_stops_start_with_the_guards_error(self):
        ensure, geo = self._patches()
        self.cfg = _browser_settings(self.root, require_proxy=True)
        self.network.side_effect = NetworkGuardError("proxy is required")
        with self._fake_patchright(), ensure as ensure_mock, geo:
            runner = self._runner(proxy=None)
            with self.assertRaises(NetworkGuardError):
                await runner.start()
        self.network.assert_awaited_once_with(None, require_proxy=True)
        ensure_mock.assert_not_called()

    # ---- geolocation of the proxy's city

    async def test_the_city_geolocation_reaches_the_launch_without_any_permission(self):
        ensure, _ = self._patches()
        point = {"latitude": 52.52, "longitude": 13.405}
        geo = AsyncMock(return_value=self._geo(geolocation=point))
        with self._fake_patchright(), ensure, patch("modules.youtube_runner.resolve_chrome_geo", geo):
            await self._runner(proxy=None).start()
        kwargs = self.driver.chromium.launch_persistent_context.call_args.kwargs
        self.assertEqual(kwargs["geolocation"], point)
        self.assertNotIn("permissions", kwargs)

    # ---- geo-consistency lock

    async def test_chrome_checks_the_geo_lock_with_the_mode_before_anything_is_prepared(self):
        ensure, geo = self._patches()
        self.cfg = _browser_settings(self.root, geo_lock="block")
        with self._fake_patchright(), ensure, geo:
            await self._runner(proxy=None).start()
        self.geo_lock.assert_called_once_with(self.session_dir, "DE", "block")

    async def test_the_default_geo_lock_mode_is_warn(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            await self._runner(proxy=None).start()
        self.geo_lock.assert_called_once_with(self.session_dir, "DE", "warn")

    async def test_a_blocked_geo_change_stops_chrome_before_the_driver_starts(self):
        ensure, geo = self._patches()
        self.geo_lock.side_effect = GeoLockError("country changed")
        with self._fake_patchright(), ensure as ensure_mock, geo:
            runner = self._runner(proxy=None)
            with self.assertRaises(GeoLockError):
                await runner.start()
            sys.modules["patchright.async_api"].async_playwright.assert_not_called()
        ensure_mock.assert_not_called()
        self.prepare.assert_not_called()
        self.assertIsNone(runner._patchright_driver)

    async def test_an_unknown_country_skips_the_geo_lock(self):
        ensure, _ = self._patches()
        geo = AsyncMock(return_value=self._geo(country=None))
        with self._fake_patchright(), ensure, patch("modules.youtube_runner.resolve_chrome_geo", geo):
            await self._runner(proxy=None).start()
        self.geo_lock.assert_not_called()

    async def test_camoufox_checks_the_geo_lock_with_the_exit_country(self):
        runner = YouTubeRunner(self.cfg, self.cfg.targets[0], session_dir=self.root / "alice", proxy="http://10.0.0.1:8080")
        self.network.return_value = SimpleNamespace(ip="1.2.3.4", country="PL", timezone="Europe/Warsaw")
        with self._camoufox_patches():
            await runner.start()
            await runner.close()
        self.geo_lock.assert_called_once_with(self.root / "alice", "PL", "warn")

    async def test_a_blocked_geo_change_stops_camoufox_before_the_browser_is_created(self):
        runner = YouTubeRunner(self.cfg, self.cfg.targets[0], session_dir=self.root / "alice", proxy="http://10.0.0.1:8080")
        self.network.return_value = SimpleNamespace(ip="1.2.3.4", country="PL", timezone="Europe/Warsaw")
        self.geo_lock.side_effect = GeoLockError("country changed")
        with patch("modules.youtube_runner.AsyncCamoufox") as camoufox:
            with self.assertRaises(GeoLockError):
                await runner.start()
        camoufox.assert_not_called()

    async def test_camoufox_without_a_proxy_has_no_country_and_no_geo_lock(self):
        runner = YouTubeRunner(self.cfg, self.cfg.targets[0], session_dir=self.root / "alice")
        with self._camoufox_patches():
            await runner.start()
            await runner.close()
        self.geo_lock.assert_not_called()
        self.write_geo.assert_not_called()

    # ---- the geo of a signed-in session is remembered

    async def test_chrome_records_the_geo_right_after_the_start_when_already_signed_in(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            await self._runner(proxy=None).start()
        self.write_geo.assert_called_once_with(self.session_dir, "DE", "Europe/Berlin")

    async def test_chrome_records_the_geo_at_close_after_a_sign_in_during_the_session(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            self.context.cookies = AsyncMock(return_value=[])
            runner = await self._started_runner()
            self.write_geo.assert_not_called()  # not signed in yet
            self.context.cookies = AsyncMock(return_value=[_cookie("LOGIN_INFO", "v", ".youtube.com")])
            await runner.close()
        self.write_geo.assert_called_once_with(self.session_dir, "DE", "Europe/Berlin")

    async def test_a_session_without_a_login_records_nothing(self):
        ensure, geo = self._patches()
        with self._fake_patchright(), ensure, geo:
            self.context.cookies = AsyncMock(return_value=[_cookie("SID", "v", ".google.com")])
            runner = await self._started_runner()
            await runner.close()
        self.write_geo.assert_not_called()

    async def test_a_changed_country_never_overwrites_the_recorded_geo(self):
        ensure, geo = self._patches()
        self.read_geo.return_value = {"country": "PL", "timezone": "Europe/Warsaw"}
        with self._fake_patchright(), ensure, geo:
            runner = await self._started_runner()
            await runner.close()
        self.write_geo.assert_not_called()  # the profile keeps the geo it signed in from

    async def test_the_same_country_refreshes_the_recorded_geo(self):
        ensure, geo = self._patches()
        self.read_geo.return_value = {"country": "de", "timezone": "Europe/Berlin"}
        with self._fake_patchright(), ensure, geo:
            await self._runner(proxy=None).start()
        self.write_geo.assert_called_once()

    async def test_a_failing_geo_record_never_breaks_the_session(self):
        ensure, geo = self._patches()
        self.write_geo.side_effect = OSError("disk full")
        self.read_geo.side_effect = OSError("locked")
        with self._fake_patchright(), ensure, geo:
            runner = await self._started_runner()
            self.assertTrue(runner.is_running)
            await runner.close()
        self.context.close.assert_awaited_once()
        self.driver.stop.assert_awaited_once()

    async def test_camoufox_records_the_exit_geo_at_close_while_the_context_is_alive(self):
        runner = YouTubeRunner(self.cfg, self.cfg.targets[0], session_dir=self.root / "alice", proxy="http://10.0.0.1:8080")
        self.network.return_value = SimpleNamespace(ip="1.2.3.4", country="PL", timezone="Europe/Warsaw")
        events = []
        with self._camoufox_patches() as (camoufox, context):
            context.cookies = AsyncMock(return_value=[_cookie("LOGIN_INFO", "v", ".youtube.com")])
            self.write_geo.side_effect = lambda *a: events.append("write")
            camoufox.return_value.__aexit__ = AsyncMock(side_effect=lambda *a: events.append("exit"))
            await runner.start()
            self.write_geo.assert_not_called()
            await runner.close()
        self.assertEqual(events, ["write", "exit"])
        self.write_geo.assert_called_once_with(self.root / "alice", "PL", "Europe/Warsaw")

    def _camoufox_patches(self):
        """Patch AsyncCamoufox and the fingerprint; the context manager yields `(camoufox mock, context)`."""
        from contextlib import contextmanager

        @contextmanager
        def manager():
            context = self._camoufox_context()
            with patch("modules.youtube_runner.AsyncCamoufox") as camoufox, \
                    patch("modules.youtube_runner.get_or_create_fingerprint", return_value={"pinned": True}):
                camoufox.return_value.__aenter__ = AsyncMock(return_value=context)
                camoufox.return_value.__aexit__ = AsyncMock(return_value=None)
                yield camoufox, context

        return manager()

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
        first = Database(old)
        await first.connect()
        await first.init_schema()
        await first.upsert_account("legacy", session_path="test_sessions/legacy")
        await first.close()
        conn = sqlite3.connect(old)  # the table as it was before the engine column existed
        conn.execute("ALTER TABLE accounts DROP COLUMN engine")
        conn.commit()
        self.assertNotIn("engine", {row[1] for row in conn.execute("PRAGMA table_info(accounts)")})
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


class _FakeCtx:
    """The part of AppContext the account actions use: `db(fn)` runs `fn(db)` against a stub."""

    def __init__(self, db):
        self._db = db

    def db(self, fn):
        return asyncio.run(fn(self._db))


class TestEngineInTheAccountsTab(unittest.TestCase):
    def setUp(self):
        from modules.ui import icons

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(icons.use_emoji, icons.EMOJI)

    def test_icons_have_an_emoji_and_a_plain_form(self):
        from modules.ui import icons

        icons.use_emoji(True)
        self.assertEqual((icons.engine_icon("camoufox"), icons.engine_icon("chrome")), ("🦊", "🌐"))
        icons.use_emoji(False)
        self.assertEqual((icons.engine_icon("camoufox"), icons.engine_icon("chrome")), ("F", "C"))
        self.assertEqual((icons.other_engine("chrome"), icons.other_engine("camoufox")), ("camoufox", "chrome"))
        self.assertEqual((icons.engine_title("chrome"), icons.engine_title("camoufox")), ("Google Chrome", "Camoufox"))

    def test_the_account_row_shows_the_engine(self):
        from modules.ui import icons
        from modules.ui.tab_accounts import account_row

        icons.use_emoji(False)
        for engine, mark in (("camoufox", "F"), ("chrome", "C")):
            acc = AccountRecord(id=1, name="alice", session_path="s", created_at="now", engine=engine)
            row = account_row(None, acc, False, None, [])
            self.assertIn(mark, [cell.text for cell in row.cells])

    def test_chrome_session_check_reads_the_saved_state(self):
        from modules.wizard import check_account_session

        profile = self.root / "chrome" / "alice"
        self.assertFalse(check_account_session(profile, "chrome")[0])
        asyncio.run(save_chrome_heartbeat_state(profile, _context([_cookie("SID", "v", ".google.com")]), None))
        self.assertEqual(check_account_session(profile, "chrome")[1], "Гостевой режим (вход в аккаунт НЕ выполнен)")
        asyncio.run(save_chrome_heartbeat_state(profile, _context([_cookie("LOGIN_INFO", "v", ".youtube.com", 4102444800)]), None))
        self.assertTrue(check_account_session(profile, "chrome")[0])
        self.assertFalse(check_account_session(profile)[0])  # as Camoufox the folder has no cookies.sqlite

    def test_switch_engine_moves_the_account_to_the_other_browser(self):
        from modules.ui.shell import Notice
        from modules.ui.tab_accounts import switch_engine

        db = MagicMock()
        db.set_account_engine = AsyncMock(return_value=MagicMock(engine="chrome"))
        acc = AccountRecord(id=7, name="alice", session_path="s", created_at="now")
        notice = switch_engine(_FakeCtx(db), acc)
        self.assertIsInstance(notice, Notice)
        self.assertEqual(notice.kind, "ok")
        db.set_account_engine.assert_awaited_once_with(7, "chrome")
        db.set_account_engine = AsyncMock(side_effect=ValueError("socks5 with a login"))
        self.assertEqual(switch_engine(_FakeCtx(db), acc).kind, "err")

    def test_a_signed_in_account_is_asked_before_the_switch(self):
        from modules.ui.shell import Confirm
        from modules.ui.tab_accounts import ENGINE_SWITCH_CONFIRM, engine_step

        acc = AccountRecord(id=7, name="alice", session_path=str(self.root), created_at="now")
        db = MagicMock()
        db.get_account = AsyncMock(return_value=acc)
        db.set_account_engine = AsyncMock(return_value=MagicMock(engine="chrome"))
        with patch("modules.wizard.check_account_session", return_value=(True, "")):
            step = engine_step(_FakeCtx(db), 7)
            self.assertIsInstance(step, Confirm)
            self.assertIn("отдельной авторизации в Google", ENGINE_SWITCH_CONFIRM)
            self.assertEqual(step.question[0].text, ENGINE_SWITCH_CONFIRM)
            self.assertEqual(step.choices, ["Нет", "Переключить"])
            self.assertIsNone(step.pick(0))
            db.set_account_engine.assert_not_awaited()
            self.assertEqual(step.pick(1).kind, "ok")
            db.set_account_engine.assert_awaited_once_with(7, "chrome")

    def test_an_account_without_a_session_switches_without_a_question(self):
        from modules.ui.shell import Notice
        from modules.ui.tab_accounts import engine_step

        acc = AccountRecord(id=7, name="alice", session_path=str(self.root), created_at="now", engine="chrome")
        db = MagicMock()
        db.get_account = AsyncMock(return_value=acc)
        db.set_account_engine = AsyncMock(return_value=MagicMock(engine="camoufox"))
        with patch("modules.wizard.check_account_session", return_value=(False, "")):
            step = engine_step(_FakeCtx(db), 7)
        self.assertIsInstance(step, Notice)
        db.set_account_engine.assert_awaited_once_with(7, "camoufox")


class TestChromeWindowSize(unittest.TestCase):
    def _size(self, name, work_area=None):
        with patch("modules.chrome_launcher.get_screen_work_area", return_value=work_area):
            return get_window_size(name)

    def test_size_is_stable_and_pinned_to_sha256_of_the_name(self):
        self.assertEqual(self._size("alice"), self._size("alice"))
        # pinned values: the built-in hash() would change them between processes
        self.assertEqual(self._size("alice"), (1447, 870))
        self.assertEqual(self._size("bob"), (1459, 859))

    def test_size_stays_in_range_and_differs_between_accounts(self):
        sizes = {self._size(f"account-{index}") for index in range(60)}
        for width, height in sizes:
            self.assertTrue(1280 <= width <= 1560, width)
            self.assertTrue(800 <= height <= 940, height)
        self.assertGreater(len(sizes), 30)

    def test_no_name_keeps_the_old_size(self):
        self.assertEqual(self._size(None), (1440, 900))
        self.assertEqual(self._size(""), (1440, 900))

    def test_a_small_screen_clamps_the_window_to_the_work_area(self):
        self.assertEqual(self._size("alice", (1366, 728)), (1366, 728))  # 1366x768 with a taskbar
        self.assertEqual(self._size(None, (1366, 728)), (1366, 728))
        self.assertEqual(self._size("alice", (1600, 800)), (1447, 800))  # only the too large side is cut
        self.assertEqual(self._size("alice", (3840, 2000)), (1447, 870))

    def test_the_arg_carries_the_clamped_size(self):
        with patch("modules.chrome_launcher.get_screen_work_area", return_value=(1366, 728)):
            self.assertIn("--window-size=1366,728", get_chrome_args("en-US", "alice"))
        with patch("modules.chrome_launcher.get_screen_work_area", return_value=None):
            self.assertIn("--window-size=1447,870", get_chrome_args("en-US", "alice"))
            self.assertIn("--window-size=1440,900", get_chrome_args())

    def test_work_area_failures_are_not_fatal(self):
        self.assertEqual(self._size("alice", None), (1447, 870))
        with patch("modules.chrome_launcher.sys.platform", "linux"):
            self.assertIsNone(chrome_launcher.get_screen_work_area())
        failing = MagicMock()
        failing.user32.SystemParametersInfoW.side_effect = OSError("no desktop")
        with patch("modules.chrome_launcher.sys.platform", "win32"), patch("ctypes.windll", failing, create=True):
            self.assertIsNone(chrome_launcher.get_screen_work_area())
        refusing = MagicMock()
        refusing.user32.SystemParametersInfoW.return_value = 0
        with patch("modules.chrome_launcher.sys.platform", "win32"), patch("ctypes.windll", refusing, create=True):
            self.assertIsNone(chrome_launcher.get_screen_work_area())

    @unittest.skipUnless(sys.platform == "win32", "Windows work area")
    def test_the_real_work_area_is_positive(self):
        area = chrome_launcher.get_screen_work_area()
        if area is not None:
            self.assertTrue(area[0] > 0 and area[1] > 0)


class TestChromeHostResolverRules(unittest.TestCase):
    def _rules(self, proxy):
        return [arg for arg in get_chrome_args("en-US", "alice", proxy) if arg.startswith("--host-resolver-rules=")]

    def test_socks5_forbids_local_dns_except_loopback_and_the_proxy(self):
        self.assertEqual(
            self._rules("socks5://10.0.0.1:1080"),
            ["--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1, EXCLUDE localhost, EXCLUDE 10.0.0.1"],
        )

    def test_a_domain_or_ipv6_proxy_host_is_excluded_without_brackets(self):
        self.assertEqual(socks5_host_resolver_rule("socks5://proxy.example.com:1080"),
                         "MAP * ~NOTFOUND , EXCLUDE 127.0.0.1, EXCLUDE localhost, EXCLUDE proxy.example.com")
        self.assertTrue(socks5_host_resolver_rule("socks5://[2001:db8::1]:1080").endswith("EXCLUDE 2001:db8::1"))
        self.assertEqual(socks5_host_resolver_rule("socks5://localhost:1080").count("localhost"), 1)

    def test_the_rule_is_one_unquoted_argument_without_credentials(self):
        args = get_chrome_args("en-US", "alice", "socks5://user:s3cret@proxy.example.com:1080")
        rules = [arg for arg in args if arg.startswith("--host-resolver-rules=")]
        self.assertEqual(len(rules), 1)
        self.assertNotIn('"', rules[0])  # with quotes Chrome ignores the whole rule
        for arg in args:
            self.assertNotIn("s3cret", arg)
            self.assertNotIn("user", arg)

    def test_other_proxies_and_broken_values_get_no_rule(self):
        for proxy in (None, "", "http://10.0.0.1:8080", "https://10.0.0.1:443", "10.0.0.1:8080:u:p", "socks5://nonsense"):
            with self.subTest(proxy=proxy):
                self.assertEqual(self._rules(proxy), [])
                self.assertIsNone(socks5_host_resolver_rule(proxy))


class TestChromeGeo(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _distance_km(lat1, lon1, lat2, lon2):
        import math

        return math.hypot((lat2 - lat1) * 111.0, (lon2 - lon1) * 111.0 * math.cos(math.radians(lat1)))

    async def test_the_exact_city_timezone_wins_over_the_country_table(self):
        with _exit_geo_patch(_exit_geo("US", "America/Chicago")):
            geo = await resolve_chrome_geo("http://p:1")
        self.assertEqual((geo.timezone, geo.locale, geo.country), ("America/Chicago", "en-US", "US"))
        self.assertIsNone(geo.geolocation)

    async def test_the_country_table_is_only_the_fallback_for_the_timezone(self):
        with _exit_geo_patch(_exit_geo("DE", None)):
            self.assertEqual((await resolve_chrome_geo(None)).timezone, "Europe/Berlin")
        with _exit_geo_patch(_exit_geo("ZW", None)):
            self.assertIsNone((await resolve_chrome_geo(None)).timezone)

    async def test_locale_and_country_priorities(self):
        with _exit_geo_patch(_exit_geo("DE", "Europe/Berlin")):
            self.assertEqual((await resolve_chrome_geo(None, "FR", "pl-PL")).locale, "pl-PL")  # explicit locale first
            geo = await resolve_chrome_geo(None, "FR")
            self.assertEqual((geo.locale, geo.country), ("de-DE", "DE"))  # the exit country beats the account's one
        with _exit_geo_patch(None):
            geo = await resolve_chrome_geo(None, "France")
            self.assertEqual((geo.country, geo.timezone, geo.locale, geo.geolocation), ("FR", "Europe/Paris", "fr-FR", None))
            geo = await resolve_chrome_geo(None)
            self.assertEqual((geo.country, geo.timezone, geo.locale), (None, None, "en-US"))

    async def test_the_lookup_gets_the_proxy(self):
        patcher = _exit_geo_patch(None)
        with patcher as lookup:
            await resolve_chrome_geo("http://u:p@10.0.0.1:8080")
        lookup.assert_awaited_once_with("http://u:p@10.0.0.1:8080")

    async def test_geolocation_is_the_city_point_without_a_name(self):
        with _exit_geo_patch(_exit_geo("DE", "Europe/Berlin", 52.52, 13.405)):
            geo = await resolve_chrome_geo(None)
        self.assertEqual(geo.geolocation, {"latitude": 52.52, "longitude": 13.405, "accuracy": 100})

    async def test_geolocation_noise_is_stable_per_account_and_within_1_to_2_km(self):
        points = set()
        for name in ("alice", "bob", "carol", "dave", "erin", "frank"):
            with _exit_geo_patch(_exit_geo("DE", "Europe/Berlin", 52.52, 13.405)):
                first = (await resolve_chrome_geo(None, account_name=name)).geolocation
                again = (await resolve_chrome_geo(None, account_name=name)).geolocation
            self.assertEqual(first, again)
            self.assertEqual(first["accuracy"], 100)
            distance = self._distance_km(52.52, 13.405, first["latitude"], first["longitude"])
            self.assertTrue(0.99 <= distance <= 2.01, (name, distance))
            for key in ("latitude", "longitude"):
                self.assertEqual(first[key], round(first[key], 6))
            points.add((first["latitude"], first["longitude"]))
        self.assertEqual(len(points), 6)

    def test_noise_survives_the_poles_and_the_date_line(self):
        for latitude, longitude in ((89.9999, 0.0), (-89.9999, 10.0), (0.0, 179.9999), (0.0, -179.9999)):
            point = city_geolocation(latitude, longitude, "alice")
            self.assertTrue(-90 <= point["latitude"] <= 90 and -180 <= point["longitude"] <= 180, point)

    async def test_the_old_wrapper_returns_the_pair_of_the_new_function(self):
        with _exit_geo_patch(_exit_geo("US", "America/Chicago", 41.88, -87.63)):
            self.assertEqual(await resolve_geo_timezone("http://p:1"), ("America/Chicago", "en-US"))

    def test_chrome_geo_is_immutable(self):
        geo = ChromeGeo("Europe/Berlin", "de-DE", "DE", None)
        with self.assertRaises(AttributeError):
            geo.locale = "en-US"


if __name__ == "__main__":
    unittest.main()
