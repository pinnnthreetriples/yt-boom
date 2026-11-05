"""The structural selectors resolve to the same elements whatever the UI language (real Camoufox, static DOM).

Fixtures copy the structure captured live on 2026-11-01 (YouTube watch page and reply composer in ru-RU / en-US,
accounts.google.com first screen in en / ru / pl) with only the localized strings changing per language. The
consent.youtube.com pages (/m and /d, captured live 2026-11-01 in en / pl / de / fr) keep the real form structure and hidden
inputs; classes, jsactions and the `escs` / `bl` tokens are dropped.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import modules.google_authenticator as ga
from modules.page_helpers import CONSENT_ACCEPT_SELECTORS, CONSENT_REJECT_SELECTORS, SIGN_IN_LINK, first_visible
from modules.youtube_runner import (
    EXPAND_REPLIES_SELECTORS,
    REPLY_INPUT_SELECTORS,
    SUBMIT_BUTTON_PAGE_SELECTORS,
    SUBMIT_BUTTON_SELECTOR,
    YouTubeRunner,
)

try:
    from camoufox.async_api import AsyncCamoufox
except Exception:  # pragma: no cover - the browser is optional for the rest of the suite
    AsyncCamoufox = None

# language -> (Sign in, Reply, "N replies", Hide replies, add a reply, Cancel, Next, email field label)
STRINGS = {
    "en": ("Sign in", "Reply", "663 replies", "Hide replies", "Add a reply...", "Cancel", "Next", "Email or phone"),
    "ru": ("Войти", "Ответить", "663 ответа", "Скрыть ответы", "Введите ответ", "Отмена", "Далее", "Телефон или электронный адрес"),
    "pl": ("Zaloguj się", "Odpowiedz", "663 odpowiedzi", "Ukryj odpowiedzi", "Dodaj odpowiedź...", "Anuluj", "Dalej", "Adres e-mail lub telefon"),
    "ky": ("Кирүү", "Жооп", "663 жооп", "Жоопторду жашыруу", "Жооп кошуу...", "Жокко чыгаруу", "Кийинки", "Электрондук почта же телефон"),
}

YOUTUBE = """
<ytd-masthead><div id="buttons"><a href="https://accounts.google.com/ServiceLogin?service=youtube&hl={hl}">{signin}</a></div></ytd-masthead>
<ytd-comment-thread-renderer>
  <ytd-comment-view-model>
    <a href="/watch?v=V1&lc=OTHER">1 day</a>
    <div id="reply-button-end"><ytd-button-renderer><button aria-label="{reply}">{reply}</button></ytd-button-renderer></div>
  </ytd-comment-view-model>
  <div id="replies"><ytd-comment-replies-renderer>
    <div id="expander" hidden>
      <ytd-button-renderer id="more-replies"><button aria-label="{more}">{more}</button></ytd-button-renderer>
      <ytd-button-renderer id="less-replies"><button aria-label="{less}">{less}</button></ytd-button-renderer>
    </div>
    <ytd-button-renderer id="more-replies-sub-thread"><button aria-label="{more}">{more}</button></ytd-button-renderer>
  </ytd-comment-replies-renderer></div>
</ytd-comment-thread-renderer>
<ytd-comment-thread-renderer>
  <ytd-comment-view-model>
    <a href="/watch?v=V1&lc=TARGET.sub">2 days</a>
    <div id="reply-button-end"><ytd-button-renderer><button aria-label="{reply}">{reply}</button></ytd-button-renderer></div>
  </ytd-comment-view-model>
</ytd-comment-thread-renderer>
<ytd-comment-reply-dialog-renderer><div id="reply-dialog"><ytd-commentbox>
  <div id="contenteditable-root" contenteditable="true" aria-label="{add}"></div>
  <div id="cancel-button"><button aria-label="{cancel}">{cancel}</button></div>
  <ytd-button-renderer id="submit-button"><button aria-label="{reply}">{reply}</button></ytd-button-renderer>
</ytd-commentbox></div></ytd-comment-reply-dialog-renderer>
"""

# Captured live 2026-11-05 (ru-RU and ky-KG accounts see the same): for some comments YouTube renders the whole action row
# but sets `hidden` on `ytd-button-renderer#reply-button-end`, so the Reply button exists and is never visible.
ACTION_ROWS = """
<ytd-comment-thread-renderer><ytd-comment-view-model>
  <a href="/watch?v=V1&lc=CAN.reply">1 day</a>
  <div id="toolbar"><span id="vote-count-middle">5</span>
    <ytd-button-renderer id="reply-button-end"><yt-button-shape><button aria-label="{reply}">{reply}</button></yt-button-shape></ytd-button-renderer></div>
</ytd-comment-view-model></ytd-comment-thread-renderer>
<ytd-comment-thread-renderer><ytd-comment-view-model>
  <a href="/watch?v=V1&lc=NO.reply">2 days</a>
  <div id="toolbar"><span id="vote-count-middle">9</span>
    <ytd-button-renderer id="reply-button-end" hidden><yt-button-shape><button aria-label="{reply}">{reply}</button></yt-button-shape></ytd-button-renderer></div>
</ytd-comment-view-model></ytd-comment-thread-renderer>
<ytd-comment-thread-renderer><ytd-comment-view-model>
  <a href="/watch?v=V1&lc=RENDERING.reply">3 days</a>
  <div id="toolbar" hidden><ytd-button-renderer id="reply-button-end" hidden><button>{reply}</button></ytd-button-renderer></div>
</ytd-comment-view-model></ytd-comment-thread-renderer>
<ytd-comment-thread-renderer><ytd-comment-view-model>
  <a href="/watch?v=V1&lc=BARE.reply">4 days</a>
  <div id="toolbar"><span id="vote-count-middle">1</span></div>
</ytd-comment-view-model></ytd-comment-thread-renderer>
"""

GOOGLE = """
<form><input id="identifierId" name="identifier" type="text" aria-label="{email}">
<button type="button" jsname="Cuz2Ue">?</button>
<div id="identifierNext"><div><button type="button" jsname="LgbsSe">{next}</button></div></div></form>
"""

_HIDDEN = ('<input type="hidden" name="gl" value="DE"><input type="hidden" name="m" value="0"><input type="hidden" name="app" value="0">'
           '<input type="hidden" name="pc" value="yt"><input type="hidden" name="continue" value="https://www.youtube.com/watch?v=dQw4w9WgXcQ">'
           '<input type="hidden" name="x" value="{x}"><input type="hidden" name="hl" value="{hl}"><input type="hidden" name="src" value="1">')
_ACCEPT_FLAGS = '<input type="hidden" name="set_ytc" value="true" required=""><input type="hidden" name="set_apyt" value="true" required="">'


def _consent_page(variant: str, hl: str, reject: str, accept: str) -> str:
    """consent.youtube.com: the buttons are laid out twice (one copy is display:none), reject and accept are separate forms."""
    m = variant == "m"
    hidden = _HIDDEN.format(x=6 if m else 7, hl=hl) + ('<input type="hidden" name="uxe" value="eomtm"><input type="hidden" name="cm" value="2">' if m else "")

    def form(label: str, flags: str) -> str:
        aria = f' aria-label="{label}"' if m else ""
        return (f'<form action="https://consent.youtube.com/save" method="POST"><div data-is-touch-wrapper="true">'
                f'<button jscontroller="soHxf"{aria}><div></div><span jsname="V67aGc">{label}</span></button></div>{hidden}{flags}</form>')

    reject_form = form(reject, '<input type="hidden" name="set_eom" value="true" required="">')
    accept_flags = _ACCEPT_FLAGS + '<input type="hidden" name="set_eom" value="false" required="">' if m else (
        '<input type="hidden" name="set_eom" value="false" required="">' + _ACCEPT_FLAGS)
    accept_form = form(accept, accept_flags)
    return f'<div class="shown">{reject_form}{accept_form}</div><div class="other-layout" style="display:none">{accept_form}{reject_form}</div>'


# (variant, hl) -> (reject label, accept label)
CONSENT_PAGES = {
    ("m", "en"): ("Reject all", "Accept all"),
    ("m", "pl"): ("Odrzuć wszystko", "Zaakceptuj wszystko"),
    ("d", "en"): ("Reject all", "Confirm your settings"),
    ("d", "de"): ("Alle ablehnen", "Einstellungen bestätigen"),
    ("d", "fr"): ("Tout refuser", "Confirmer vos paramètres"),
    ("d", "pl"): ("Odrzuć wszystko", "Potwierdź ustawienia"),
}

YTCFG_PAGES = {
    "signed in": ('window.ytcfg = {get: () => false}; ytcfg.set({"SESSION_INDEX":"0","LOGGED_IN":true,"CHANNEL_ID":"UC1234567890abcdefghij_-"});', True, True, "UC1234567890abcdefghij_-"),
    "signed in, no channel id on this page": ('ytcfg.set({"DATASYNC_ID":"1||","LOGGED_IN": true});', True, True, None),
    "guest": ('ytcfg.set({"LOGGED_IN":false,"INNERTUBE_CONTEXT_CLIENT_NAME":1});', True, False, None),
    "config not parsed yet": ("var ytInitialData = {};", False, False, None),
}


class _BrowserCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        if AsyncCamoufox is None:
            self.skipTest("camoufox is not installed")
        self._camoufox = AsyncCamoufox(headless=True, os="windows", geoip=False)
        try:
            self.browser = await self._camoufox.__aenter__()
        except Exception as exc:  # no browser binary / sandbox: the DOM checks cannot run here
            self.skipTest(f"camoufox did not start: {exc}")
        self.page = await self.browser.new_page()
        self.addAsyncCleanup(self._camoufox.__aexit__, None, None, None)

    async def count(self, selector: str) -> int:
        return await self.page.locator(selector).count()

    async def host_id(self, locator) -> str:
        return await locator.evaluate("e => (e.closest('ytd-button-renderer, [id]') || e).id")


class TestYouTubeSelectors(_BrowserCase):
    async def _check_thread_expander_and_composer(self) -> None:
        for lang, (signin, reply, more, less, add, cancel, _next, _email) in STRINGS.items():
            with self.subTest(lang=lang):
                await self.page.set_content(YOUTUBE.format(hl=lang, signin=signin, reply=reply, more=more, less=less, add=add, cancel=cancel))
                self.assertEqual(await self.count(SIGN_IN_LINK), 1)
                self.assertEqual(await self.count("#reply-button-end :is(button, a)"), 2)

                # the expander: the visible #more-replies-sub-thread button wins, #less-replies is never selected
                first = await first_visible(self.page, EXPAND_REPLIES_SELECTORS)
                self.assertIsNotNone(first)
                self.assertEqual(await self.host_id(first), "more-replies-sub-thread")
                for selector in EXPAND_REPLIES_SELECTORS:
                    self.assertEqual(await self.count(f"#less-replies :is({selector})"), 0)
                    self.assertEqual(
                        await self.page.locator(selector).evaluate_all("els => els.filter(e => e.closest('#less-replies')).length"), 0
                    )

                # the composer: the input, the submit button (not the cancel button)
                inputs = await first_visible(self.page, REPLY_INPUT_SELECTORS)
                self.assertIsNotNone(inputs)
                self.assertEqual(await inputs.get_attribute("id"), "contenteditable-root")
                submit = await first_visible(self.page, (SUBMIT_BUTTON_SELECTOR,))
                self.assertEqual(await self.host_id(submit), "submit-button")
                for selector in SUBMIT_BUTTON_PAGE_SELECTORS:
                    self.assertEqual(await self.count(selector), 1, selector)

    async def _check_reply_button_of_the_linked_comment(self) -> None:
        for lang, (signin, reply, more, less, add, cancel, _next, _email) in STRINGS.items():
            with self.subTest(lang=lang):
                await self.page.set_content(YOUTUBE.format(hl=lang, signin=signin, reply=reply, more=more, less=less, add=add, cancel=cancel))
                runner = SimpleNamespace(_first_visible=YouTubeRunner._first_visible)
                button = await YouTubeRunner._find_sub_comment_reply_button(runner, self.page, comment_id="TARGET.sub", timeout_seconds=2.0)
                self.assertIsNotNone(button)
                owner = await button.evaluate("e => e.closest('ytd-comment-view-model').querySelector('a[href*=\"lc=\"]').href")
                self.assertTrue(owner.endswith("lc=TARGET.sub"), owner)

    async def _check_reply_button_hidden_by_youtube(self) -> None:
        """A comment YouTube gives no Reply button is told apart by structure, in every UI language."""
        for lang, (signin, reply, *_rest) in STRINGS.items():
            with self.subTest(lang=lang):
                await self.page.set_content(ACTION_ROWS.format(reply=reply))
                runner = SimpleNamespace(_first_visible=YouTubeRunner._first_visible)
                disabled = {
                    cid: await YouTubeRunner._reply_disabled_on_target(runner, self.page, cid)
                    for cid in ("CAN.reply", "NO.reply", "RENDERING.reply", "BARE.reply", "MISSING.reply")
                }
                self.assertEqual(
                    disabled,
                    {"CAN.reply": False, "NO.reply": True, "RENDERING.reply": False, "BARE.reply": False, "MISSING.reply": False},
                )
                # the finder agrees: no visible button for the hidden one, one for the normal one
                found = await YouTubeRunner._find_sub_comment_reply_button(runner, self.page, comment_id="CAN.reply", timeout_seconds=1.0)
                self.assertIsNotNone(found)
                missing = await YouTubeRunner._find_sub_comment_reply_button(runner, self.page, comment_id="NO.reply", timeout_seconds=0.6)
                self.assertIsNone(missing)

    async def _check_consent_forms_told_apart_by_the_hidden_flag(self) -> None:
        for (variant, hl), (reject_label, accept_label) in CONSENT_PAGES.items():
            with self.subTest(variant=variant, hl=hl):
                await self.page.set_content(_consent_page(variant, hl, reject_label, accept_label))
                self.assertEqual(await self.count(CONSENT_REJECT_SELECTORS[0]), 2)  # both layouts
                self.assertEqual(await self.count(CONSENT_ACCEPT_SELECTORS[0]), 2)
                reject = await first_visible(self.page, CONSENT_REJECT_SELECTORS)
                accept = await first_visible(self.page, CONSENT_ACCEPT_SELECTORS)
                self.assertEqual((await reject.inner_text()).strip(), reject_label)
                self.assertEqual((await accept.inner_text()).strip(), accept_label)
                flag = "e => e.closest('form').querySelector('input[name=set_eom]').value"
                self.assertEqual((await reject.evaluate(flag), await accept.evaluate(flag)), ("true", "false"))

    async def test_youtube_selectors_in_every_ui_language(self) -> None:
        # one browser start for all three checks (a Camoufox launch costs seconds)
        await self._check_thread_expander_and_composer()
        await self._check_reply_button_of_the_linked_comment()
        await self._check_reply_button_hidden_by_youtube()
        await self._check_consent_forms_told_apart_by_the_hidden_flag()


class TestYtcfgFromInlineScript(_BrowserCase):
    async def test_login_state_is_read_from_the_inline_config_in_any_world(self) -> None:
        for name, (script, ready, logged_in, channel) in YTCFG_PAGES.items():
            with self.subTest(page=name):
                await self.page.set_content(f"<html><body><script nonce='x'>{script}</script><script src='/x.js'></script></body></html>")
                self.assertEqual(await self.page.evaluate(ga.YTCFG_JS), {"ready": ready, "loggedIn": logged_in, "channelId": channel})


class TestGoogleSignInSelectors(_BrowserCase):
    async def test_identifier_screen_in_every_ui_language(self) -> None:
        for lang, strings in STRINGS.items():
            with self.subTest(lang=lang):
                await self.page.set_content(GOOGLE.format(email=strings[7], next=strings[6]))
                email = await first_visible(self.page, ga.EMAIL_INPUT)
                self.assertEqual(await email.get_attribute("id"), "identifierId")
                nxt = await first_visible(self.page, ga.NEXT_BUTTONS)
                self.assertIsNotNone(nxt)
                self.assertEqual(await nxt.evaluate("e => e.closest('#identifierNext').id"), "identifierNext")
                self.assertEqual(await nxt.get_attribute("jsname"), "LgbsSe")  # not the "Forgot email?" button


if __name__ == "__main__":
    unittest.main()
