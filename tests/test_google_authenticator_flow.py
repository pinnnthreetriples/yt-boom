"""State machine of GoogleAuthenticator on a fake Playwright page with controllable latency (no browser, no network)."""
import asyncio
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import Error as PlaywrightError

import modules.google_authenticator as ga
from modules.page_helpers import first_visible

PWD_URL = "https://accounts.google.com/v3/signin/challenge/pwd?token=SECRET_TOKEN"
HOME_URL = "https://www.youtube.com/"
TRANSIT_URL = "https://accounts.youtube.com/accounts/SetSID?ssdc=1"


@dataclass
class Screen:
    url: str
    exact: frozenset = frozenset()  # selectors that are visible as written
    contains: tuple = ()  # every selector holding one of these substrings is visible
    error: str | None = None
    on_enter: list | None = None  # [(delay_seconds, screen_name)] timeline started by Enter
    on_click: dict = field(default_factory=dict)  # selector substring -> screen_name


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    async def count(self):
        return 1 if self.page.visible(self.selector) else 0

    def nth(self, index):
        return self

    async def is_visible(self):
        return self.page.visible(self.selector)

    async def input_value(self):
        return self.page.field_text

    async def click(self, *args, **kwargs):
        pass

    async def inner_text(self):
        return self.page.screen.error or ""

    async def is_checked(self):
        return True


class FakePage:
    def __init__(self, screens, start):
        self.screens, self.name = screens, start
        self.timeline = []
        self.typed, self.typed_into, self.clicks, self.gotos = [], [], [], []
        self.field_text, self.fills, self.loaded = "", 0, 0
        self.ytcfg = []
        self.keyboard = SimpleNamespace(press=self._press)

    def _enter(self, name):
        if self.screens[name].url != self.screens[self.name].url:  # a rejected value stays in the input
            self.field_text = ""
        self.name = name

    @property
    def screen(self):
        now = time.monotonic()
        while self.timeline and self.timeline[0][0] <= now:
            self._enter(self.timeline.pop(0)[1])
        return self.screens[self.name]

    @property
    def url(self):
        return self.screen.url

    async def _press(self, key):
        if key == "Backspace":  # after Control+A: the whole value is removed by a keystroke, never by fill("")
            self.fills += 1
            self.field_text = ""
        if key == "Enter":
            base = time.monotonic()
            self.timeline = [(base + delay, name) for delay, name in (self.screen.on_enter or [])]

    def visible(self, selector):
        screen = self.screen
        return selector in screen.exact or any(part in selector for part in screen.contains)

    def locator(self, selector):
        return FakeLocator(self, selector)

    def click(self, selector):
        for key, target in self.screen.on_click.items():
            if key in selector:
                self._enter(target)
                return

    async def wait_for_load_state(self, *args, **kwargs):
        self.loaded += 1

    async def goto(self, url, **kwargs):
        self.gotos.append(url)

    async def evaluate(self, script):
        result = self.ytcfg.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeSimulator:
    async def human_pause(self, minimum, maximum):
        await asyncio.sleep(0)

    async def click_locator(self, page, locator, *args, **kwargs):
        page.clicks.append(locator.selector)
        page.click(locator.selector)

    async def type_text(self, page, text, allow_typos=True):
        page.typed_into.append(page.field_text)
        page.typed.append(text)
        page.field_text += text


HOME = Screen(HOME_URL)
TRANSIT = Screen(TRANSIT_URL)
PWD = Screen(PWD_URL, exact=frozenset({ga.PASSWORD_INPUT[0]}))


class _FlowCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, value in {
            "SUBMIT_WAIT_SECONDS": 2.0,
            "FIELD_POLL_SECONDS": 0.01,
            "POLL_SECONDS": 0.01,
            "UNKNOWN_SCREEN_SECONDS": 0.3,
            "TOTP_WINDOW_PAD_SECONDS": 0.0,
        }.items():
            patcher = patch.object(ga, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        fresh = patch.object(ga, "generate_fresh_totp", AsyncMock(return_value="123456"))
        fresh.start()
        self.addCleanup(fresh.stop)
        remaining = patch.object(ga, "get_remaining_seconds", return_value=0)
        remaining.start()
        self.addCleanup(remaining.stop)
        self.auth = ga.GoogleAuthenticator(FakeSimulator())

    async def run_form(self, page, totp="JBSWY3DP", recovery=None, timeout=10.0):
        await self.auth._run_form(page, "a@b.c", "pw", totp, recovery, timeout)


class TestSlowAnswers(_FlowCase):
    async def test_slow_password_is_typed_once_and_accepted(self):
        pwd = Screen(PWD_URL, exact=PWD.exact, on_enter=[(0.4, "transit"), (0.6, "home")])
        page = FakePage({"pwd": pwd, "transit": TRANSIT, "home": HOME}, "pwd")
        await self.run_form(page)
        self.assertEqual(page.typed, ["pw"])
        self.assertEqual(page.name, "home")  # returned on www.youtube.com, not on accounts.youtube.com in transit
        self.assertEqual(page.loaded, 1)

    async def test_slow_totp_is_entered_once(self):
        totp = Screen(
            "https://accounts.google.com/v3/signin/challenge/totp", exact=frozenset({ga.TOTP_INPUT[0]}),
            on_enter=[(0.5, "transit"), (0.7, "home")],
        )
        page = FakePage({"totp": totp, "transit": TRANSIT, "home": HOME}, "totp")
        await self.run_form(page)
        self.assertEqual(page.typed, ["123456"])

    async def test_wrong_totp_is_retried_on_a_cleared_field_then_rejected(self):
        url = "https://accounts.google.com/v3/signin/challenge/totp"
        shown = frozenset({ga.TOTP_INPUT[0], ga.ERROR_BLOCKS[0]})
        screens = {
            "totp": Screen(url, exact=frozenset({ga.TOTP_INPUT[0]}), on_enter=[(0.05, "wrong")]),
            "wrong": Screen(url, exact=shown, error="Wrong code. Try again.", on_enter=[(0.05, "wrong2")]),
            "wrong2": Screen(url, exact=shown, error="Wrong code. Try again."),
        }
        page = FakePage(screens, "totp")
        with self.assertRaisesRegex(ga.GoogleAuthError, "2FA"):
            await self.run_form(page)
        self.assertEqual(page.typed, ["123456", "123456"])
        self.assertEqual(page.typed_into, ["", ""])  # the rejected code was cleared before the second try
        self.assertEqual(page.fills, 1)

    async def test_explicit_error_block_is_wrong_password(self):
        wrong = Screen(PWD_URL, exact=frozenset({ga.PASSWORD_INPUT[0], ga.ERROR_BLOCKS[0]}), error="Wrong password")
        pwd = Screen(PWD_URL, exact=PWD.exact, on_enter=[(0.05, "wrong")])
        page = FakePage({"pwd": pwd, "wrong": wrong}, "pwd")
        with self.assertRaisesRegex(ga.GoogleAuthError, "неверный пароль"):
            await self.run_form(page)

    async def test_field_still_there_after_the_timeout_is_a_rejection(self):
        page = FakePage({"pwd": Screen(PWD_URL, exact=PWD.exact)}, "pwd")
        with patch.object(ga, "SUBMIT_WAIT_SECONDS", 0.2), self.assertRaisesRegex(ga.GoogleAuthError, "неверный пароль"):
            await self.run_form(page)
        self.assertEqual(page.typed, ["pw"])

    async def test_legit_reauth_password_screen_is_not_a_false_rejection(self):
        screens = {
            "pwd": Screen(PWD_URL, exact=PWD.exact, on_enter=[(0.05, "bump")]),
            "bump": Screen(
                "https://accounts.google.com/v3/signin/speedbump/x", contains=('has-text("Not now")',),
                on_click={'has-text("Not now")': "again"},
            ),
            "again": Screen(
                "https://accounts.google.com/v3/signin/reauth/pwd", exact=PWD.exact, on_enter=[(0.05, "home")]
            ),
            "home": HOME,
        }
        page = FakePage(screens, "pwd")
        await self.run_form(page)
        self.assertEqual(page.typed, ["pw", "pw"])


class TestScreens(_FlowCase):
    async def test_speedbump_not_now_is_clicked_and_continue_never(self):
        bump = Screen(
            "https://accounts.google.com/v3/signin/speedbump/passkeyenrollment",
            contains=('has-text("Not now")', 'has-text("Continue")'),
            on_click={'has-text("Not now")': "home"},
        )
        page = FakePage({"bump": bump, "home": HOME}, "bump")
        await self.run_form(page)
        self.assertEqual(page.clicks, [ga.DECLINE_BUTTONS[0]])
        self.assertFalse([c for c in page.clicks if "Continue" in c])

    async def test_passkey_prompt_uses_try_another_way_then_authenticator_not_continue(self):
        screens = {
            "passkey": Screen(
                "https://accounts.google.com/v3/signin/challenge/pk/presend",
                contains=('has-text("Continue")', 'has-text("Try another way")'),
                on_click={'has-text("Try another way")': "selection"},
            ),
            "selection": Screen(
                "https://accounts.google.com/v3/signin/challenge/selection", exact=frozenset({ga.AUTHENTICATOR_OPTION[0]}),
                on_click={'data-challengetype="6"': "totp"},
            ),
            "totp": Screen(
                "https://accounts.google.com/v3/signin/challenge/totp", exact=frozenset({ga.TOTP_INPUT[0]}),
                on_enter=[(0.05, "home")],
            ),
            "home": HOME,
        }
        page = FakePage(screens, "passkey")
        await self.run_form(page)
        self.assertEqual(page.clicks[:2], [ga.TRY_ANOTHER_WAY[0], ga.AUTHENTICATOR_OPTION[0]])
        self.assertFalse([c for c in page.clicks if "Continue" in c])
        self.assertEqual(page.typed, ["123456"])

    async def test_captcha_stops_the_flow(self):
        page = FakePage({"captcha": Screen(PWD_URL, exact=frozenset({ga.CAPTCHA_MARKERS[0]}))}, "captcha")
        with self.assertRaisesRegex(ga.GoogleAuthError, "капч"):
            await self.run_form(page)
        self.assertEqual(page.typed, [])

    async def test_unknown_screen_names_the_page_without_query(self):
        page = FakePage({"x": Screen("https://accounts.google.com/v3/signin/weird?token=SECRET_TOKEN")}, "x")
        with self.assertRaises(ga.GoogleAuthError) as caught:
            await self.run_form(page)
        message = str(caught.exception)
        self.assertIn("неизвестный экран", message)
        self.assertIn("/v3/signin/weird", message)
        self.assertNotIn("SECRET_TOKEN", message)

    async def test_screen_that_never_advances_is_reported_as_stuck(self):
        stuck = Screen(
            "https://accounts.google.com/v3/signin/speedbump/sticky", contains=('has-text("Skip")',),
        )
        page = FakePage({"stuck": stuck}, "stuck")
        with self.assertRaisesRegex(ga.GoogleAuthError, "застряли на экране «decline»"):
            await self.run_form(page)

    async def test_gds_host_speedbump_and_acknowledge_button(self):
        screens = {
            "gds": Screen(
                "https://gds.google.com/web/recoveryoptions", contains=('has-text("Skip")',),
                on_click={'has-text("Skip")': "tos"},
            ),
            "tos": Screen(
                "https://accounts.google.com/speedbump/gaplustos", contains=('has-text("I understand")',),
                on_click={'has-text("I understand")': "home"},
            ),
            "home": HOME,
        }
        page = FakePage(screens, "gds")
        await self.run_form(page)
        self.assertEqual(page.clicks, [ga.DECLINE_BUTTONS[4], ga.ACKNOWLEDGE_BUTTONS[0]])

    async def test_role_button_variant_is_recognised(self):
        bump = Screen(
            "https://accounts.google.com/v3/signin/speedbump/x", exact=frozenset({ga.DECLINE_BUTTONS[1]}),
            on_click={"role": "home"},
        )
        page = FakePage({"bump": bump, "home": HOME}, "bump")
        await self.run_form(page)
        self.assertEqual(page.clicks, [ga.DECLINE_BUTTONS[1]])

    async def test_account_chooser_picks_our_tile_then_signs_in(self):
        screens = {
            "chooser": Screen(
                "https://accounts.google.com/v3/signin/accountchooser", exact=frozenset({'[data-identifier="a@b.c" i]'}),
                on_click={"data-identifier": "pwd"},
            ),
            "pwd": Screen(PWD_URL, exact=PWD.exact, on_enter=[(0.05, "home")]),
            "home": HOME,
        }
        page = FakePage(screens, "chooser")
        await self.run_form(page)
        self.assertEqual(page.clicks[0], '[data-identifier="a@b.c" i]')
        self.assertEqual(page.typed, ["pw"])

    async def test_use_another_account_leads_to_the_email_screen(self):
        screens = {
            "chooser": Screen(
                "https://accounts.google.com/v3/signin/accountchooser", contains=('has-text("Use another account")',),
                on_click={"Use another account": "email"},
            ),
            "email": Screen(
                "https://accounts.google.com/v3/signin/identifier", exact=frozenset({ga.EMAIL_INPUT[0]}),
                on_enter=[(0.05, "home")],
            ),
            "home": HOME,
        }
        page = FakePage(screens, "chooser")
        await self.run_form(page)
        self.assertEqual(page.typed, ["a@b.c"])

    async def test_missing_recovery_email_is_an_error(self):
        kpe = Screen(PWD_URL, exact=frozenset({ga.KPE_INPUT[0], ga.EMAIL_INPUT[1]}))
        page = FakePage({"kpe": kpe}, "kpe")
        with self.assertRaisesRegex(ga.GoogleAuthError, "recovery_email"):
            await self.run_form(page)

    async def test_playwright_errors_while_navigating_do_not_abort_the_flow(self):
        page = FakePage({"pwd": Screen(PWD_URL, exact=PWD.exact, on_enter=[(0.05, "home")]), "home": HOME}, "pwd")
        real_visible, calls = page.visible, {"n": 0}

        def flaky(selector):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise PlaywrightError("Execution context was destroyed, most likely because of a navigation")
            return real_visible(selector)

        page.visible = flaky
        await self.run_form(page)
        self.assertEqual(page.typed, ["pw"])


class TestFirstVisible(unittest.IsolatedAsyncioTestCase):
    async def test_playwright_error_means_not_visible(self):
        locator = MagicMock(count=AsyncMock(side_effect=PlaywrightError("detached")))
        page = MagicMock(locator=MagicMock(return_value=locator))
        self.assertIsNone(await first_visible(page, "button"))

    async def test_skips_hidden_duplicates(self):
        hidden, shown = MagicMock(is_visible=AsyncMock(return_value=False)), MagicMock(is_visible=AsyncMock(return_value=True))
        locator = MagicMock(count=AsyncMock(return_value=2), nth=MagicMock(side_effect=[hidden, shown]))
        page = MagicMock(locator=MagicMock(return_value=locator))
        self.assertIs(await first_visible(page, ("button",)), shown)


class TestEnsureChannel(_FlowCase):
    def _page(self, ytcfg, screen=None):
        page = FakePage({"s": screen or Screen(HOME_URL)}, "s")
        page.ytcfg = list(ytcfg)
        return page

    async def test_guest_mode_after_sign_in_is_an_error(self):
        page = self._page([{"ready": True, "loggedIn": False, "channelId": None}])
        with self.assertRaisesRegex(ga.GoogleAuthError, "LOGGED_IN"):
            await self.auth._ensure_channel(page)

    async def test_existing_channel_needs_no_creation(self):
        page = self._page([{"ready": True, "loggedIn": True, "channelId": "UC1"}])
        await self.auth._ensure_channel(page)
        self.assertEqual(len(page.gotos), 1)

    async def test_channel_is_created_and_rechecked_after_a_navigation_error(self):
        page = self._page(
            [
                PlaywrightError("Execution context was destroyed"),
                {"ready": True, "loggedIn": True, "channelId": None},
                {"ready": True, "loggedIn": True, "channelId": "UC1"},
            ],
            Screen(HOME_URL, exact=frozenset({ga.CREATE_CHANNEL_BUTTON})),
        )
        with patch.object(ga, "YTCFG_POLL_SECONDS", 0.0):
            await self.auth._ensure_channel(page)
        self.assertEqual(page.clicks, [ga.CREATE_CHANNEL_BUTTON])
        self.assertIn(ga.CHANNEL_CREATION_URL, page.gotos)

    async def test_channel_that_does_not_appear_is_an_error(self):
        none = {"ready": True, "loggedIn": True, "channelId": None}
        page = self._page([none] * 6, Screen(HOME_URL, exact=frozenset({ga.CREATE_CHANNEL_BUTTON})))
        with self.assertRaisesRegex(ga.GoogleAuthError, "канал не создан"):
            await self.auth._ensure_channel(page)


class TestAuthenticateEntry(_FlowCase):
    @staticmethod
    def _context(login_info: bool):
        cookies = [{"name": "LOGIN_INFO", "value": "x", "domain": ".youtube.com", "expires": -1}] if login_info else []
        page = MagicMock(close=AsyncMock(), goto=AsyncMock(side_effect=RuntimeError("stop here")))
        return MagicMock(cookies=AsyncMock(return_value=cookies), clear_cookies=AsyncMock(), new_page=AsyncMock(return_value=page))

    async def test_force_relogin_clears_cookies_and_skips_the_fast_path(self):
        context = self._context(login_info=True)
        with self.assertRaisesRegex(RuntimeError, "stop here"):
            await self.auth.authenticate(context, "a@b.c", "pw", force_relogin=True)
        context.clear_cookies.assert_awaited_once()
        context.new_page.assert_awaited_once()

    async def test_without_force_a_live_cookie_is_enough(self):
        context = self._context(login_info=True)
        self.assertTrue(await self.auth.authenticate(context, "a@b.c", "pw"))
        context.clear_cookies.assert_not_awaited()
        context.new_page.assert_not_awaited()

    async def test_login_cookie_is_waited_for(self):
        answers = [False, False, True]
        with patch.object(self.auth, "is_session_authenticated", AsyncMock(side_effect=answers)):
            self.assertTrue(await self.auth._wait_login_cookie(MagicMock()))

    async def test_login_cookie_wait_gives_up(self):
        with patch.object(ga, "LOGIN_COOKIE_WAIT_SECONDS", 0.05), \
             patch.object(self.auth, "is_session_authenticated", AsyncMock(return_value=False)):
            self.assertFalse(await self.auth._wait_login_cookie(MagicMock()))

    async def test_youtube_lookalike_domain_cookie_is_not_a_session(self):
        context = MagicMock(cookies=AsyncMock(return_value=[
            {"name": "LOGIN_INFO", "value": "x", "domain": ".notyoutube.com", "expires": -1}
        ]))
        self.assertFalse(await self.auth.is_session_authenticated(context))


class TestSessionOwner(unittest.TestCase):
    def test_owner_round_trip_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(ga.read_session_owner(temp))
            ga.write_session_owner(Path(temp) / "profile", "Bob@Gmail.com")
            self.assertEqual(ga.read_session_owner(Path(temp) / "profile"), "bob@gmail.com")


if __name__ == "__main__":
    unittest.main()
