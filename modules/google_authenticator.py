from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from modules.interaction_simulator import InteractionSimulator
from modules.totp_helper import generate_fresh_totp

YOUTUBE_HOME_URL = "https://www.youtube.com/?hl=en&gl=US"
CHANNEL_CREATION_URL = "https://www.youtube.com/channel_creation"

SIGN_IN_LINK = 'ytd-masthead #buttons a[href*="ServiceLogin"]'
CONSENT_REJECT = (
    'button[aria-label*="Reject all"]',
    'button:has-text("Reject all")',
    'button:has-text("Отклонить все")',
    'button[aria-label*="Accept all"]',
    'button:has-text("Accept all")',
    'button:has-text("Принять все")',
)
EMAIL_INPUT = ('input#identifierId', 'input[type="email"]')
PASSWORD_INPUT = ('input[type="password"]', 'input[name="Passwd"]')
TOTP_INPUT = ('input#totpPin',)
TOTP_TRUST_CHECKBOX = ('input#trustDevice', 'input[name="TrustDevice"]', 'input[type="checkbox"]')
KPE_INPUT = ('input#knowledge-preregistered-email-response',)
CAPTCHA_MARKERS = ('img#captchaimg', 'iframe[src*="recaptcha"]')
TRY_ANOTHER_WAY = (
    'button:has-text("Try another way")',
    'button:has-text("Другой способ")',
    '[role="button"]:has-text("Try another way")',
    '[role="button"]:has-text("Другой способ")',
)
AUTHENTICATOR_OPTION = (
    'div[data-challengetype="6"]',
    '[role="link"]:has-text("Google Authenticator")',
    'li:has-text("Google Authenticator")',
)
# Only refusals: "Continue" on the passkey screen would open the native Windows Hello prompt.
DECLINE_BUTTONS = (
    'button:has-text("Not now")',
    'button:has-text("Не сейчас")',
    'button:has-text("Skip")',
    'button:has-text("Пропустить")',
)
WAS_IT_YOU_BUTTONS = (
    'button:has-text("Yes, it was me")',
    'button:has-text("Да, это я")',
)
CREATE_CHANNEL_BUTTON = '#create-channel-button button'

AUTH_TIMEOUT_SECONDS = 240.0
POLL_SECONDS = 1.0
TOTP_MAX_ATTEMPTS = 2


class GoogleAuthError(RuntimeError):
    """Sign-in could not be completed (wrong credentials, captcha, an unknown screen...)."""


class GoogleAuthenticator:
    """Signs a Camoufox persistent context into YouTube through the regular Google sign-in flow."""

    def __init__(self, simulator: InteractionSimulator) -> None:
        self._simulator = simulator

    async def is_session_authenticated(self, context: Any) -> bool:
        """Zero-risk fast path: a live LOGIN_INFO cookie on .youtube.com means the profile is already signed in."""
        try:
            cookies = await context.cookies()
        except Exception as exc:
            logger.debug("Не удалось прочитать cookies контекста: {}", exc)
            return False
        now = time.time()
        for cookie in cookies:
            if cookie.get("name") != "LOGIN_INFO":
                continue
            if not str(cookie.get("domain", "")).lstrip(".").endswith("youtube.com"):
                continue
            expires = cookie.get("expires", -1)
            if cookie.get("value") and (expires in (None, -1) or expires > now):
                return True
        return False

    async def authenticate(
        self,
        context: Any,
        email: str,
        password: str,
        totp_secret: str | None = None,
        recovery_email: str | None = None,
        timeout_seconds: float = AUTH_TIMEOUT_SECONDS,
    ) -> bool:
        """Sign in and make sure the account has a channel. Returns True on success, raises GoogleAuthError otherwise."""
        if await self.is_session_authenticated(context):
            logger.info("Сессия уже авторизована (LOGIN_INFO найден), вход пропущен")
            return True

        page = await context.new_page()
        try:
            await self._open_sign_in(page)
            await self._run_form(page, email, password, totp_secret, recovery_email, timeout_seconds)
            await self._ensure_channel(page)
            if not await self.is_session_authenticated(context):
                raise GoogleAuthError("после входа кука LOGIN_INFO не появилась в контексте")
            logger.success("Авторизация в YouTube завершена")
            return True
        finally:
            try:
                await page.close()
            except Exception as exc:
                logger.debug("Не удалось закрыть страницу авторизации: {}", exc)

    # ------------------------------------------------------------------ entry point

    async def _open_sign_in(self, page: Any) -> None:
        await page.goto(YOUTUBE_HOME_URL, wait_until="domcontentloaded")
        await self._simulator.human_pause(1.5, 3.0)
        await self._dismiss_consent(page)
        link = await self._first_visible(page, (SIGN_IN_LINK,), timeout_seconds=15.0)
        if link is None:
            raise GoogleAuthError("на youtube.com не найдена ссылка Sign in")
        await self._simulator.click_locator(page, link)
        await self._simulator.human_pause(1.5, 3.0)

    async def _dismiss_consent(self, page: Any) -> None:
        """GDPR banner: full-screen consent.youtube.com page or a lightbox on youtube.com. Prefer rejecting."""
        button = await self._first_visible(page, CONSENT_REJECT, timeout_seconds=4.0)
        if button is None:
            return
        await self._simulator.click_locator(page, button)
        logger.debug("Баннер согласия закрыт")
        await self._simulator.human_pause(1.5, 3.0)
        if "consent.youtube.com" in page.url:
            try:
                await page.wait_for_url(lambda url: "consent.youtube.com" not in url, timeout=15000)
            except Exception:
                await page.goto(YOUTUBE_HOME_URL, wait_until="domcontentloaded")

    # ------------------------------------------------------------------ form state machine

    async def _run_form(
        self,
        page: Any,
        email: str,
        password: str,
        totp_secret: str | None,
        recovery_email: str | None,
        timeout_seconds: float,
    ) -> None:
        deadline = time.monotonic() + timeout_seconds
        seen_google = False
        done: set[str] = set()
        totp_attempts = 0
        while time.monotonic() < deadline:
            url = page.url
            host = urlparse(url).hostname or ""
            if host == "accounts.google.com":
                seen_google = True
            elif seen_google and host.endswith("youtube.com") and "consent." not in host:
                return
            if "consent.youtube.com" in host:
                await self._dismiss_consent(page)
                continue
            if host != "accounts.google.com":
                await asyncio.sleep(POLL_SECONDS)
                continue

            if await self._any_visible(page, CAPTCHA_MARKERS) or "recaptcha" in url:
                raise GoogleAuthError("Google запросил капчу: пройдите её вручную (--login)")

            if await self._submit_field(page, PASSWORD_INPUT, password, done, "password", "неверный пароль"):
                continue
            if await self._submit_totp(page, totp_secret, totp_attempts):
                totp_attempts += 1
                continue
            # KPE goes before the email screen: its input can be type="email" too
            if await self._any_visible(page, KPE_INPUT):
                if not recovery_email:
                    raise GoogleAuthError("Google запросил резервный email, а recovery_email не передан")
                await self._submit_field(page, KPE_INPUT, recovery_email, done, "recovery", "резервный email не принят")
                continue
            if await self._submit_field(page, EMAIL_INPUT, email, done, "email", "email не принят"):
                continue
            if await self._pick_authenticator(page, totp_secret, done):
                continue
            if await self._click_first(page, DECLINE_BUTTONS) or await self._click_first(page, WAS_IT_YOU_BUTTONS):
                continue
            await asyncio.sleep(POLL_SECONDS)
        raise GoogleAuthError(f"таймаут авторизации, последний экран: {page.url.split('?')[0]}")

    async def _submit_field(
        self, page: Any, selectors: tuple[str, ...], value: str, done: set[str], key: str, failure: str
    ) -> bool:
        field = await self._first_visible(page, selectors, timeout_seconds=0.0)
        if field is None:
            return False
        if key in done:
            # The same screen again after we submitted it: Google rejected the value
            raise GoogleAuthError(failure)
        await self._simulator.human_pause(0.6, 1.4)
        await self._simulator.click_locator(page, field)
        await self._simulator.type_text(page, value, allow_typos=False)
        await self._simulator.human_pause(0.3, 0.8)
        await page.keyboard.press("Enter")
        done.add(key)
        logger.debug("Экран {} заполнен", key)
        await self._simulator.human_pause(2.0, 4.0)
        return True

    async def _submit_totp(self, page: Any, totp_secret: str | None, attempts: int) -> bool:
        field = await self._first_visible(page, TOTP_INPUT, timeout_seconds=0.0)
        if field is None:
            return False
        if not totp_secret:
            raise GoogleAuthError("Google запросил код 2FA, а totp_secret не задан")
        if attempts >= TOTP_MAX_ATTEMPTS:
            raise GoogleAuthError("код 2FA не принят")
        await self._ensure_trust_device(page)
        code = await generate_fresh_totp(totp_secret)  # waits for a new window when the current one is about to end
        await self._simulator.click_locator(page, field)
        await self._simulator.type_text(page, code, allow_typos=False)
        await self._simulator.human_pause(0.3, 0.8)
        await page.keyboard.press("Enter")
        logger.debug("Код 2FA введён (попытка {})", attempts + 1)
        await self._simulator.human_pause(2.5, 4.5)
        return True

    async def _ensure_trust_device(self, page: Any) -> None:
        """"Don't ask again on this device" must be checked."""
        checkbox = await self._first_visible(page, TOTP_TRUST_CHECKBOX, timeout_seconds=0.0)
        if checkbox is None:
            return
        try:
            if await checkbox.is_checked():
                return
            await self._simulator.click_locator(page, checkbox)
            if not await checkbox.is_checked():
                await checkbox.check(force=True)
        except Exception as exc:
            logger.debug("Не удалось отметить чекбокс «не спрашивать снова»: {}", exc)

    async def _pick_authenticator(self, page: Any, totp_secret: str | None, done: set[str]) -> bool:
        """Challenge screen other than a code prompt ("Check your phone"): choose Google Authenticator."""
        if not totp_secret or "/challenge/" not in page.url:
            return False
        option = await self._first_visible(page, AUTHENTICATOR_OPTION, timeout_seconds=0.0)
        if option is not None:
            await self._simulator.click_locator(page, option)
            logger.debug("Выбран способ подтверждения Google Authenticator")
            await self._simulator.human_pause(2.0, 3.5)
            return True
        if "another_way" in done:
            return False
        button = await self._first_visible(page, TRY_ANOTHER_WAY, timeout_seconds=0.0)
        if button is None:
            return False
        done.add("another_way")
        await self._simulator.click_locator(page, button)
        await self._simulator.human_pause(2.0, 3.5)
        return True

    # ------------------------------------------------------------------ finalization

    async def _ensure_channel(self, page: Any) -> None:
        await page.goto(YOUTUBE_HOME_URL, wait_until="domcontentloaded")
        await self._simulator.human_pause(2.0, 4.0)
        channel_id = await page.evaluate("() => window.ytcfg && window.ytcfg.get('CHANNEL_ID')")
        if channel_id:
            return
        logger.info("У аккаунта нет канала, создаю")
        await page.goto(CHANNEL_CREATION_URL, wait_until="domcontentloaded")
        button = await self._first_visible(page, (CREATE_CHANNEL_BUTTON,), timeout_seconds=15.0)
        if button is None:
            raise GoogleAuthError("не найдена кнопка создания канала")
        await self._simulator.click_locator(page, button)
        await self._simulator.human_pause(3.0, 5.0)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _first_visible(page: Any, selectors: tuple[str, ...], timeout_seconds: float = 0.0) -> Any | None:
        deadline = time.monotonic() + timeout_seconds
        while True:
            for selector in selectors:
                matches = page.locator(selector)
                for index in range(min(await matches.count(), 5)):
                    candidate = matches.nth(index)
                    if await candidate.is_visible():
                        return candidate
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.4)

    async def _any_visible(self, page: Any, selectors: tuple[str, ...]) -> bool:
        return await self._first_visible(page, selectors) is not None

    async def _click_first(self, page: Any, selectors: tuple[str, ...]) -> bool:
        target = await self._first_visible(page, selectors)
        if target is None:
            return False
        await self._simulator.human_pause(0.8, 1.8)
        await self._simulator.click_locator(page, target)
        await self._simulator.human_pause(2.0, 3.5)
        return True


__all__ = ["GoogleAuthError", "GoogleAuthenticator"]
