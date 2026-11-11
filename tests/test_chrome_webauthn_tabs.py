"""Chrome sign-in hygiene: the silent WebAuthn authenticator, no tab churn, no scripts or interception in the browser.

Covers the Chrome engine only: Camoufox keeps its own behaviour (no CDP session, a fresh tab for the sign-in).
"""
from __future__ import annotations

import ast
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from config import BrowserSettings, TargetSettings
from modules import google_authenticator as ga
from modules.google_authenticator import GoogleAuthenticator, GoogleAuthError
from modules.youtube_runner import YouTubeRunner

ROOT = Path(__file__).resolve().parent.parent
CHROME_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
HINTS = {"brands": [{"brand": "Chromium", "version": "131"}, {"brand": "Google Chrome", "version": "131"}],
         "mobile": False, "platform": "Windows"}

# The only keyword arguments the Chrome launch may carry. `geolocation` is the city of the proxy; `permissions` is NOT
# allowed (nothing is granted on purpose). Everything that rewrites what the page sees is forbidden below.
ALLOWED_LAUNCH_KWARGS = frozenset(
    {"user_data_dir", "channel", "headless", "args", "proxy", "timezone_id", "no_viewport", "geolocation"}
)
FORBIDDEN_LAUNCH_KWARGS = frozenset(
    {"locale", "user_agent", "extra_http_headers", "viewport", "screen", "device_scale_factor", "is_mobile", "has_touch",
     "permissions", "color_scheme", "reduced_motion", "forced_colors", "java_script_enabled", "bypass_csp",
     "http_credentials", "ignore_default_args", "executable_path", "service_workers", "offline"}
)
LAUNCH_CALLS = frozenset({"launch_persistent_context", "new_context"})
# Calls that inject a script into pages or intercept / rewrite traffic: never made on a Chrome context or page.
FORBIDDEN_CALLS = frozenset(
    {"add_init_script", "route", "route_from_har", "route_web_socket", "set_extra_http_headers", "expose_function",
     "expose_binding", "add_script_tag", "add_style_tag", "set_viewport_size", "set_geolocation", "grant_permissions",
     "emulate_media"}
)
SCANNED_FILES = ("youtube_bot.py", "main.py", "config.py")


def find_violations(source: str, name: str = "<source>") -> list[str]:
    """Forbidden script injection / interception calls and launch keywords in `source`."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr in FORBIDDEN_CALLS:
            found.append(f"{name}:{node.lineno}: .{attr}()")
        elif attr in LAUNCH_CALLS:
            for keyword in node.keywords:
                if keyword.arg is None:
                    continue  # `**options` is checked at runtime by the launch test below
                if keyword.arg in FORBIDDEN_LAUNCH_KWARGS or keyword.arg not in ALLOWED_LAUNCH_KWARGS:
                    found.append(f"{name}:{node.lineno}: {attr}({keyword.arg}=...)")
    return found


def _sources() -> list[Path]:
    return sorted((ROOT / "modules").glob("*.py")) + [ROOT / name for name in SCANNED_FILES if (ROOT / name).is_file()]


class TestNoInjectionOrInterception(unittest.TestCase):
    def test_no_module_injects_scripts_or_intercepts_traffic(self):
        violations: list[str] = []
        for path in _sources():
            violations += find_violations(path.read_text(encoding="utf-8"), path.name)
        self.assertEqual(violations, [], "Chrome stays untouched: no init scripts, routes, header or viewport overrides")

    def test_the_scan_catches_a_reintroduced_violation(self):
        bad = (
            "await context.add_init_script('x')",
            "await page.route('**/*', handler)",
            "await page.set_extra_http_headers({})",
            "await ctx.launch_persistent_context(user_data_dir='x', user_agent='ua')",
            "await ctx.launch_persistent_context(user_data_dir='x', viewport={})",
            "await ctx.launch_persistent_context(extra_http_headers={})",
            "await ctx.launch_persistent_context(device_scale_factor=1)",
            "await ctx.launch_persistent_context(permissions=['geolocation'])",
            "await ctx.launch_persistent_context(locale='de-DE')",
        )
        for source in bad:
            self.assertTrue(find_violations(source), source)
        good = (
            "await ctx.launch_persistent_context(user_data_dir='x', channel='chrome', headless=True, args=[], proxy=None,"
            " timezone_id=None, no_viewport=True, geolocation=None)"
        )
        self.assertEqual(find_violations(good), [])

    def test_cdp_is_used_for_webauthn_only(self):
        owners = []
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "new_cdp_session":
                    owners.append(path.name)
        self.assertEqual(owners, ["google_authenticator.py"])
        source = (ROOT / "modules" / "google_authenticator.py").read_text(encoding="utf-8")
        methods = [
            node.args[0].value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "send"
            and node.args and isinstance(node.args[0], ast.Constant)
        ]
        self.assertTrue(methods)
        self.assertTrue(all(method.startswith("WebAuthn.") for method in methods), methods)


def _browser_settings(root: Path) -> BrowserSettings:
    target = TargetSettings(
        name="youtube", base_url="https://www.youtube.com",
        item_url_template="https://www.youtube.com/watch?v={external_id}",
        submit_input_selector="#reply-dialog #contenteditable-root",
        submit_button_selector="#reply-dialog #submit-button button",
        confirmation_selector="ytd-comment-view-model",
    )
    return BrowserSettings(headless=True, session_root=root, targets=[target])


class TestRuntimeKwargsAndCalls(unittest.IsolatedAsyncioTestCase):
    """A fake patchright: whatever the Chrome start, sign-in and close do reaches these mocks."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, kwargs in (
            ("prepare_chrome_profile", {}),
            ("get_chrome_args", {"return_value": ["--lang=de-DE"]}),
            ("ensure_chrome_executable", {"return_value": Path("chrome.exe")}),
            ("check_geo_lock", {}),
            ("read_session_geo", {"return_value": None}),
            ("write_session_geo", {}),
        ):
            patcher = patch(f"modules.youtube_runner.{target}", **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in (
            ("ensure_network_ready", None),
            ("resolve_chrome_geo", SimpleNamespace(timezone="Europe/Berlin", locale="de-DE", country="DE",
                                                   geolocation={"latitude": 52.5, "longitude": 13.4})),
        ):
            patcher = patch(f"modules.youtube_runner.{target}", AsyncMock(return_value=value))
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fake_patchright(self):
        self.page = _page()
        self.context = _context([self.page])
        self.driver = MagicMock()
        self.driver.stop = AsyncMock()
        self.driver.chromium.launch_persistent_context = AsyncMock(return_value=self.context)
        starter = MagicMock()
        starter.start = AsyncMock(return_value=self.driver)
        module = MagicMock()
        module.async_playwright = MagicMock(return_value=starter)
        return patch.dict(sys.modules, {"patchright": MagicMock(), "patchright.async_api": module})

    def _assert_untouched(self, *objects):
        for obj in objects:
            for name in FORBIDDEN_CALLS:
                self.assertFalse(getattr(obj, name).called, f"{name} must never be called on a Chrome context or page")

    async def test_the_launch_carries_only_the_allowed_keywords(self):
        cfg = _browser_settings(self.root)
        with self._fake_patchright():
            runner = YouTubeRunner(cfg, cfg.targets[0], session_dir=self.root / "alice", account_name="alice", engine="chrome")
            await runner.start()
        kwargs = self.driver.chromium.launch_persistent_context.call_args.kwargs
        self.assertLessEqual(set(kwargs), ALLOWED_LAUNCH_KWARGS)
        self.assertFalse(set(kwargs) & FORBIDDEN_LAUNCH_KWARGS)
        for required in ("user_data_dir", "channel", "headless", "args", "proxy", "timezone_id", "no_viewport"):
            self.assertIn(required, kwargs)
        self.assertEqual(kwargs["geolocation"], {"latitude": 52.5, "longitude": 13.4})
        self.assertNotIn("permissions", kwargs)
        self.assertNotIn("locale", kwargs)  # would collapse navigator.languages / Accept-Language to one tag

    async def test_start_sign_in_and_close_never_inject_or_intercept(self):
        cfg = _browser_settings(self.root)
        auth_page, cdp = _page(), _cdp()
        with self._fake_patchright():
            self.context.new_page = AsyncMock(return_value=auth_page)
            self.context.new_cdp_session = AsyncMock(return_value=cdp)
            runner = YouTubeRunner(cfg, cfg.targets[0], session_dir=self.root / "alice", account_name="alice", engine="chrome")
            await runner.start()
            self.page.url = "https://www.youtube.com/"  # not blank: the sign-in opens its own tab
            authenticator = GoogleAuthenticator(MagicMock(), chrome_profile_dir=self.root / "alice")
            await _authenticate(authenticator, self.context)
            await runner.close()
        self._assert_untouched(self.context, self.page, auth_page)
        sent = [call.args[0] for call in cdp.send.await_args_list]
        self.assertTrue(sent)
        self.assertTrue(all(method.startswith("WebAuthn.") for method in sent), sent)


# ---------------------------------------------------------------- fakes for the sign-in


def _page(url: str = "about:blank", events: list | None = None):
    page = MagicMock()
    page.url = url
    page.is_closed = MagicMock(return_value=False)
    page.set_default_timeout = MagicMock()
    page.set_default_navigation_timeout = MagicMock()
    page.screenshot = AsyncMock()
    page.goto = AsyncMock()

    async def evaluate(script):
        if "userAgentData" in script:
            return HINTS
        return CHROME_UA

    page.evaluate = AsyncMock(side_effect=evaluate)

    async def close():
        if events is not None:
            events.append("page.close")

    page.close = AsyncMock(side_effect=close)
    return page


def _context(pages: list):
    context = MagicMock()
    context.pages = pages
    context.cookies = AsyncMock(return_value=[])
    context.clear_cookies = AsyncMock()
    context.close = AsyncMock()
    context.new_page = AsyncMock(side_effect=lambda: _page())
    context.new_cdp_session = AsyncMock()
    return context


def _cdp(events: list | None = None, fail: frozenset[str] = frozenset()):
    """A CDP session that records every command; `fail` names the commands (or "detach") that raise."""
    cdp = MagicMock()

    async def send(method, params=None):
        if events is not None:
            events.append(method)
        if method in fail:
            raise RuntimeError(f"{method} failed")
        return {"authenticatorId": "auth-1"} if method == "WebAuthn.addVirtualAuthenticator" else {}

    async def detach():
        if events is not None:
            events.append("detach")
        if "detach" in fail:
            raise RuntimeError("detach failed")

    cdp.send = AsyncMock(side_effect=send)
    cdp.detach = AsyncMock(side_effect=detach)
    return cdp


async def _authenticate(authenticator: GoogleAuthenticator, context, *, fail_form: Exception | None = None) -> bool:
    """Run `authenticate()` with the Google screens replaced: only the page / CDP handling is under test."""
    with patch.object(authenticator, "_open_sign_in", AsyncMock()), \
            patch.object(authenticator, "_run_form", AsyncMock(side_effect=fail_form)), \
            patch.object(authenticator, "_wait_login_cookie", AsyncMock(return_value=True)), \
            patch.object(authenticator, "_ensure_channel", AsyncMock()), \
            patch.object(authenticator, "is_session_authenticated", AsyncMock(side_effect=[False, True])), \
            patch.object(authenticator, "detect_account_country", AsyncMock(return_value=None)), \
            patch.object(authenticator, "_save_chrome_state", AsyncMock()):
        return await authenticator.authenticate(context, "a@b.c", "pw")


class _AuthCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.events: list[str] = []
        self.chrome = GoogleAuthenticator(MagicMock(), chrome_profile_dir=Path(self.temp.name))
        self.camoufox = GoogleAuthenticator(MagicMock())

    def context(self, pages=None, fail: frozenset[str] = frozenset(), cdp_error: Exception | None = None):
        context = _context(pages if pages is not None else [])
        self.cdp = _cdp(self.events, fail)
        context.new_cdp_session = AsyncMock(return_value=self.cdp, side_effect=cdp_error)
        new_pages = []

        async def new_page():
            page = _page(events=self.events)
            new_pages.append(page)
            return page

        context.new_page = AsyncMock(side_effect=new_page)
        self.new_pages = new_pages
        return context


class TestVirtualAuthenticator(_AuthCase):
    async def test_chrome_adds_a_silent_authenticator_and_removes_it_before_the_page_closes(self):
        context = self.context()
        self.assertTrue(await _authenticate(self.chrome, context))
        self.assertEqual(
            self.events,
            ["WebAuthn.enable", "WebAuthn.addVirtualAuthenticator", "WebAuthn.removeVirtualAuthenticator",
             "WebAuthn.disable", "detach", "page.close"],
        )
        context.new_cdp_session.assert_awaited_once_with(self.new_pages[0])
        sends = {call.args[0]: call.args[1:] for call in self.cdp.send.await_args_list}
        self.assertEqual(sends["WebAuthn.enable"], ())
        self.assertEqual(
            sends["WebAuthn.addVirtualAuthenticator"],
            ({"options": {"protocol": "ctap2", "transport": "internal", "hasResidentKey": True, "hasUserVerification": True,
                          "isUserVerified": False, "automaticPresenceSimulation": False}},),
        )
        self.assertEqual(sends["WebAuthn.removeVirtualAuthenticator"], ({"authenticatorId": "auth-1"},))

    async def test_a_failed_sign_in_still_removes_the_authenticator(self):
        context = self.context()
        with self.assertRaises(GoogleAuthError):
            await _authenticate(self.chrome, context, fail_form=GoogleAuthError("wrong password"))
        self.assertEqual(
            self.events[-4:], ["WebAuthn.removeVirtualAuthenticator", "WebAuthn.disable", "detach", "page.close"]
        )

    async def test_every_cdp_failure_is_survived_and_the_cleanup_goes_on(self):
        for failing in ("WebAuthn.enable", "WebAuthn.addVirtualAuthenticator", "WebAuthn.removeVirtualAuthenticator",
                        "WebAuthn.disable", "detach"):
            with self.subTest(failing=failing):
                self.events.clear()
                context = self.context(fail=frozenset({failing}))
                self.assertTrue(await _authenticate(self.chrome, context))
                self.assertIn("page.close", self.events)
                if failing in ("WebAuthn.enable", "WebAuthn.addVirtualAuthenticator"):  # nothing was added: only the cleanup
                    self.assertNotIn("WebAuthn.removeVirtualAuthenticator", self.events)
                self.assertIn("WebAuthn.disable", self.events)
                self.assertIn("detach", self.events)

    async def test_a_session_that_cannot_be_opened_does_not_stop_the_login(self):
        context = self.context(cdp_error=RuntimeError("no cdp"))
        self.assertTrue(await _authenticate(self.chrome, context))
        self.assertEqual(self.events, ["page.close"])

    async def test_only_the_webauthn_domain_is_used(self):
        context = self.context()
        await _authenticate(self.chrome, context)
        methods = [call.args[0] for call in self.cdp.send.await_args_list]
        self.assertTrue(methods)
        self.assertTrue(all(method.startswith("WebAuthn.") for method in methods), methods)
        self.assertFalse(any(method.startswith("Runtime") for method in methods))

    async def test_camoufox_gets_no_cdp_session_and_a_fresh_tab_that_is_closed(self):
        blank = _page("about:blank", self.events)
        context = self.context(pages=[blank])
        self.assertTrue(await _authenticate(self.camoufox, context))
        context.new_cdp_session.assert_not_called()
        context.new_page.assert_awaited_once()
        blank.close.assert_not_awaited()  # the existing tab is never touched
        self.new_pages[0].close.assert_awaited_once()


class TestTabReuse(_AuthCase):
    async def test_chrome_reuses_the_open_blank_tab_and_leaves_it_open(self):
        for url in ("about:blank", "", "chrome://newtab/", "chrome://new-tab-page/"):
            with self.subTest(url=url):
                blank = _page(url, self.events)
                context = self.context(pages=[blank])
                self.assertTrue(await _authenticate(self.chrome, context))
                context.new_page.assert_not_awaited()
                blank.close.assert_not_awaited()
                blank.set_default_timeout.assert_not_called()  # the runner's own timeouts stay
                context.new_cdp_session.assert_awaited_once_with(blank)

    async def test_chrome_opens_a_tab_when_no_open_tab_is_blank(self):
        busy = _page("https://www.youtube.com/", self.events)
        context = self.context(pages=[busy])
        await _authenticate(self.chrome, context)
        context.new_page.assert_awaited_once()
        busy.close.assert_not_awaited()
        self.new_pages[0].close.assert_awaited_once()

    async def test_chrome_opens_a_tab_when_there_is_none(self):
        context = self.context(pages=[])
        await _authenticate(self.chrome, context)
        context.new_page.assert_awaited_once()
        self.new_pages[0].close.assert_awaited_once()

    async def test_a_closed_blank_tab_is_not_reused(self):
        closed = _page("about:blank", self.events)
        closed.is_closed = MagicMock(return_value=True)
        context = self.context(pages=[closed])
        await _authenticate(self.chrome, context)
        context.new_page.assert_awaited_once()

    async def test_a_reused_tab_stays_open_after_a_failed_sign_in(self):
        blank = _page("about:blank", self.events)
        context = self.context(pages=[blank])
        with self.assertRaises(GoogleAuthError):
            await _authenticate(self.chrome, context, fail_form=GoogleAuthError("wrong password"))
        blank.close.assert_not_awaited()
        self.assertIn("detach", self.events)

    def test_blank_urls(self):
        for url in ("about:blank", "ABOUT:BLANK", "", "chrome://newtab/", "chrome://new-tab-page/"):
            self.assertTrue(ga.is_blank_page_url(url), url)
        for url in ("https://www.youtube.com/", "chrome://settings/", None, 5):
            self.assertFalse(ga.is_blank_page_url(url), url)


class TestClientHintsAfterSignIn(_AuthCase):
    async def test_the_hints_are_read_on_the_same_page_and_saved_with_the_user_agent(self):
        page = _page()
        save = AsyncMock(return_value=True)
        context = MagicMock()
        with patch.object(ga, "save_chrome_heartbeat_state", save):
            await self.chrome._save_chrome_state(context, page)
        save.assert_awaited_once_with(Path(self.temp.name), context, CHROME_UA, client_hints=HINTS)

    async def test_the_first_tab_is_used_when_no_page_is_given(self):
        page = _page()
        save = AsyncMock(return_value=True)
        context = MagicMock(pages=[page])
        with patch.object(ga, "save_chrome_heartbeat_state", save):
            await self.chrome._save_chrome_state(context)
        save.assert_awaited_once_with(Path(self.temp.name), context, CHROME_UA, client_hints=HINTS)

    async def test_a_failed_read_still_saves_the_cookies(self):
        page = _page()
        page.evaluate = AsyncMock(side_effect=RuntimeError("navigating"))
        save = AsyncMock(return_value=True)
        with patch.object(ga, "save_chrome_heartbeat_state", save):
            await self.chrome._save_chrome_state(MagicMock(), page)
        save.assert_awaited_once()
        self.assertIsNone(save.await_args.args[2])
        self.assertIsNone(save.await_args.kwargs["client_hints"])

    async def test_a_failed_save_never_raises(self):
        with patch.object(ga, "save_chrome_heartbeat_state", AsyncMock(side_effect=RuntimeError("disk"))):
            await self.chrome._save_chrome_state(MagicMock(), _page())

    async def test_camoufox_saves_nothing(self):
        save = AsyncMock()
        with patch.object(ga, "save_chrome_heartbeat_state", save):
            await self.camoufox._save_chrome_state(MagicMock(), _page())
        save.assert_not_awaited()

    def test_client_hints_shape_is_validated(self):
        self.assertEqual(ga.normalize_client_hints(HINTS), HINTS)
        brands = [{"brand": "HeadlessChrome", "version": "131"}]
        self.assertEqual(
            ga.normalize_client_hints({"brands": brands, "mobile": 0, "platform": None}),
            {"brands": brands, "mobile": False, "platform": ""},  # kept as the browser says: C normalizes the headless brand
        )
        for raw in (None, "x", {}, {"brands": []}, {"brands": "x"}, {"brands": [{"brand": 1, "version": "2"}]}):
            self.assertIsNone(ga.normalize_client_hints(raw), raw)


if __name__ == "__main__":
    unittest.main()
