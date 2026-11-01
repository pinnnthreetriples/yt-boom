"""The structural selectors resolve to the same elements whatever the UI language (real Camoufox, static DOM).

Fixtures copy the structure captured live on 2026-11-01 (YouTube watch page and reply composer in ru-RU / en-US,
accounts.google.com first screen in en / ru / pl) with only the localized strings changing per language. The
consent.youtube.com forms are the one synthetic fixture: no consent page is served to a non-EU address.
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

GOOGLE = """
<form><input id="identifierId" name="identifier" type="text" aria-label="{email}">
<button type="button" jsname="Cuz2Ue">?</button>
<div id="identifierNext"><div><button type="button" jsname="LgbsSe">{next}</button></div></div></form>
"""

CONSENT = """
<form action="https://consent.youtube.com/save" method="POST"><input type="hidden" name="set_eom" value="true"><button>{reject}</button></form>
<form action="https://consent.youtube.com/save" method="POST"><input type="hidden" name="set_eom" value="false"><button>{accept}</button></form>
"""


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

    async def _check_consent_forms_told_apart_by_the_hidden_flag(self) -> None:
        for lang, labels in {"en": ("Reject all", "Accept all"), "ru": ("Отклонить все", "Принять все"), "pl": ("Odrzuć wszystko", "Zaakceptuj wszystko")}.items():
            with self.subTest(lang=lang):
                await self.page.set_content(CONSENT.format(reject=labels[0], accept=labels[1]))
                reject = await first_visible(self.page, CONSENT_REJECT_SELECTORS)
                accept = await first_visible(self.page, CONSENT_ACCEPT_SELECTORS)
                self.assertEqual(await reject.inner_text(), labels[0])
                self.assertEqual(await accept.inner_text(), labels[1])


    async def test_youtube_selectors_in_every_ui_language(self) -> None:
        # one browser start for all three checks (a Camoufox launch costs seconds)
        await self._check_thread_expander_and_composer()
        await self._check_reply_button_of_the_linked_comment()
        await self._check_consent_forms_told_apart_by_the_hidden_flag()


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
