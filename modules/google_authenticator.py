from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from loguru import logger
from playwright.async_api import Error as PlaywrightError

from modules.interaction_simulator import InteractionError, InteractionSimulator
from modules.page_helpers import first_visible
from modules.totp_helper import generate_fresh_totp, get_remaining_seconds

YOUTUBE_HOME_URL = "https://www.youtube.com/?hl=en&gl=US"
CHANNEL_CREATION_URL = "https://www.youtube.com/channel_creation"

# Hosts that carry the sign-in flow and its speedbumps; accounts.youtube.com (SetSID redirects) is only transit.
GOOGLE_HOSTS = frozenset({"accounts.google.com", "gds.google.com", "myaccount.google.com"})
YOUTUBE_HOSTS = frozenset({"www.youtube.com", "youtube.com", "m.youtube.com"})


def _buttons(*labels: str) -> tuple[str, ...]:
    """`button` and `[role=button]` selectors for each label (Google renders both)."""
    return tuple(
        selector
        for label in labels
        for selector in (f'button:has-text("{label}")', f'[role="button"]:has-text("{label}")')
    )


SIGN_IN_LINK = 'ytd-masthead #buttons a[href*="ServiceLogin"]'
NEXT_BUTTONS = (
    'button#passwordNext',
    'button#identifierNext',
    'button#totpNext',
    '#passwordNext button',
    '#identifierNext button',
    '#totpNext button',
    'button:has-text("Next")',
    'button:has-text("Далее")',
    '[role="button"]:has-text("Next")',
    '[role="button"]:has-text("Далее")',
)
CONSENT_REJECT = (
    'button[aria-label*="Reject all"]',
    'button:has-text("Reject all")',
    'button:has-text("Отклонить все")',
    'button[aria-label*="Accept all"]',
    'button:has-text("Accept all")',
    'button:has-text("Принять все")',
)
EMAIL_INPUT = ('input#identifierId', 'input[name="identifier"]', 'input[type="email"]:not([name*="recovery"]):not([id*="recovery"])')
PASSWORD_INPUT = ('input[type="password"]', 'input[name="Passwd"]')
TOTP_INPUT = ('input#totpPin',)
TOTP_TRUST_CHECKBOX = ('input#trustDevice', 'input[name="TrustDevice"]', 'input[type="checkbox"]')
KPE_INPUT = ('input#knowledge-preregistered-email-response',)
CAPTCHA_MARKERS = ('img#captchaimg', 'iframe[src*="recaptcha"]')
ERROR_BLOCKS = ('[role="alert"]', 'div[jsname="B34EJ"]', '[aria-live="assertive"] [jsname="B34EJ"]')
TRY_ANOTHER_WAY = _buttons("Try another way", "Другой способ")
AUTHENTICATOR_OPTION = (
    'div[data-challengetype="6"]',
    '[role="link"]:has-text("Google Authenticator")',
    'li:has-text("Google Authenticator")',
)
USE_ANOTHER_ACCOUNT = (
    '[role="link"]:has-text("Use another account")',
    'li:has-text("Use another account")',
    '[role="link"]:has-text("Использовать другой аккаунт")',
    'li:has-text("Использовать другой аккаунт")',
)
# Only refusals: "Continue" on the passkey screen would open the native Windows Hello prompt.
DECLINE_BUTTONS = _buttons("Not now", "Не сейчас", "Skip", "Пропустить", "No thanks", "Нет, спасибо", "Cancel", "Отмена")
WAS_IT_YOU_BUTTONS = _buttons("Yes, it was me", "Да, это я")
ACKNOWLEDGE_BUTTONS = _buttons("I understand", "Я понимаю") + ('input[type="submit"][value="I understand"]',)
CREATE_CHANNEL_BUTTON = '#create-channel-button button'
YTCFG_JS = (
    "() => ({ready: !!window.ytcfg,"
    " loggedIn: !!(window.ytcfg && window.ytcfg.get('LOGGED_IN')),"
    " channelId: (window.ytcfg && window.ytcfg.get('CHANNEL_ID')) || null})"
)

SESSION_OWNER_FILE = "account_owner.txt"

AUTH_TIMEOUT_SECONDS = 240.0
NAVIGATION_TIMEOUT_MS = 45000
POLL_SECONDS = 1.0
FIELD_POLL_SECONDS = 0.4
SUBMIT_WAIT_SECONDS = 18.0  # how long Google may take to answer a submitted field before it counts as rejected
UNKNOWN_SCREEN_SECONDS = 90.0  # no known element on the same page for this long: give up with the page name
STUCK_REPEATS = 3  # the same screen handled this many times in a row
TOTP_MAX_ATTEMPTS = 2
TOTP_WINDOW_PAD_SECONDS = 0.5
LOGIN_COOKIE_WAIT_SECONDS = 10.0
YTCFG_ATTEMPTS = 5
YTCFG_POLL_SECONDS = 1.0
CHANNEL_CHECK_ATTEMPTS = 3

_CHANGED, _ERROR, _TIMEOUT = "changed", "error", "timeout"


class GoogleAuthError(RuntimeError):
    """Sign-in was refused or cannot be completed (wrong credentials, captcha, an unknown screen...)."""


@dataclass
class _FormState:
    email: str
    password: str = field(repr=False)
    totp_secret: str | None = field(repr=False)
    recovery_email: str | None
    totp_attempts: int = 0
    another_way_clicks: int = 0
    last_key: str = ""
    repeats: int = 0
    idle_key: str = ""
    idle_since: float = 0.0


def _screen_path(url: str) -> str:
    """URL without query and fragment: it names the screen in messages and never carries tokens."""
    parsed = urlparse(url)
    return f"{parsed.hostname or ''}{parsed.path}"


def read_session_owner(session_dir: Path | str) -> str | None:
    """Email the profile was signed in with (written after a successful sign-in), None for older sessions."""
    try:
        text = (Path(session_dir) / SESSION_OWNER_FILE).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    return text or None


def write_session_owner(session_dir: Path | str, email: str) -> None:
    """Remember which Google account owns the profile, so a changed email never reuses a foreign session."""
    path = Path(session_dir) / SESSION_OWNER_FILE
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(email.strip().lower(), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Не удалось сохранить владельца сессии {}: {}", path, exc)


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
            domain = str(cookie.get("domain", "")).lstrip(".")
            if domain != "youtube.com" and not domain.endswith(".youtube.com"):
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
        force_relogin: bool = False,
    ) -> bool:
        """Sign in and make sure the account has a channel.

        Returns True on success (also when the profile was already signed in). `force_relogin` drops the profile's
        cookies first: use it when the stored session belongs to another account.

        Raises GoogleAuthError when Google refuses or the flow cannot be completed (wrong password or code, captcha,
        an unknown or looping screen, a timeout, no channel). Browser and network failures (PlaywrightError) are not
        wrapped: they say nothing about the credentials.
        """
        if force_relogin:
            await context.clear_cookies()
            logger.info("Сессия профиля принадлежит другому аккаунту, куки сброшены, выполняется вход заново")
        elif await self.is_session_authenticated(context):
            logger.info("Сессия уже авторизована (LOGIN_INFO найден), вход пропущен")
            return True

        page = await context.new_page()
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        page.set_default_timeout(NAVIGATION_TIMEOUT_MS)
        try:
            await self._open_sign_in(page)
            await self._run_form(page, email, password, totp_secret, recovery_email, timeout_seconds)
            if not await self._wait_login_cookie(context):
                raise GoogleAuthError("после входа кука LOGIN_INFO не появилась в контексте")
            await self._ensure_channel(page)
            if not await self.is_session_authenticated(context):
                raise GoogleAuthError("после создания канала кука LOGIN_INFO пропала")
            logger.success("Авторизация в YouTube завершена")
            return True
        except Exception as exc:
            try:
                screen_path = "auth_failure.png"
                await page.screenshot(path=screen_path)
                logger.info("Скриншот экрана ошибки сохранён в {}", screen_path)
            except Exception:
                pass
            raise
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
        state = _FormState(email, password, totp_secret, recovery_email)
        deadline = time.monotonic() + timeout_seconds
        seen_google = False
        while time.monotonic() < deadline:
            url = page.url
            host = urlparse(url).hostname or ""
            if host in GOOGLE_HOSTS:
                seen_google = True
            elif seen_google and host in YOUTUBE_HOSTS:
                await self._settle(page)
                return
            if host == "consent.youtube.com":
                await self._dismiss_consent(page)
                continue
            if host not in GOOGLE_HOSTS:  # youtube.com before the redirect, accounts.youtube.com in transit
                await asyncio.sleep(POLL_SECONDS)
                continue

            path = _screen_path(url)
            try:
                if await self._any_visible(page, CAPTCHA_MARKERS) or "recaptcha" in url:
                    raise GoogleAuthError("Google запросил капчу: пройдите её вручную (--login)")
                screen = await self._handle_screen(page, state)
            except (InteractionError, PlaywrightError) as exc:  # the page navigated under the cursor
                logger.debug("Экран {} изменился во время действия: {}", path, exc)
                await asyncio.sleep(FIELD_POLL_SECONDS)
                continue

            now = time.monotonic()
            if screen is None:
                if state.idle_key != path:
                    state.idle_key, state.idle_since = path, now
                elif now - state.idle_since > UNKNOWN_SCREEN_SECONDS:
                    raise GoogleAuthError(f"неизвестный экран, подходящих действий нет: {path}")
                await asyncio.sleep(POLL_SECONDS)
                continue
            state.idle_key = ""
            key = f"{screen}@{path}"
            if key == state.last_key:
                state.repeats += 1
            else:
                state.last_key, state.repeats = key, 1
            if state.repeats >= STUCK_REPEATS:
                raise GoogleAuthError(f"застряли на экране «{screen}» ({path})")
        raise GoogleAuthError(f"таймаут авторизации, последний экран: {_screen_path(page.url)}")

    async def _handle_screen(self, page: Any, state: _FormState) -> str | None:
        """Act on the first recognised element of the current screen; returns the screen name, None when nothing matches."""
        # Speedbumps and security confirmation interstitials: decline or acknowledge them first
        if await self._click_first(page, DECLINE_BUTTONS):
            return "decline"
        if await self._click_first(page, WAS_IT_YOU_BUTTONS):
            return "was_it_you"
        if await self._click_first(page, ACKNOWLEDGE_BUTTONS):
            return "acknowledge"

        if await self._submit_field(page, PASSWORD_INPUT, state.password, "password", "неверный пароль"):
            return "password"
        if await self._submit_totp(page, state):
            return "totp"
        # KPE goes before the email screen: its input can be type="email" too
        if await self._any_visible(page, KPE_INPUT):
            if not state.recovery_email:
                raise GoogleAuthError("Google запросил резервный email, а recovery_email не передан")
            await self._submit_field(page, KPE_INPUT, state.recovery_email, "recovery", "резервный email не принят")
            return "recovery"
        if await self._submit_field(page, EMAIL_INPUT, state.email, "email", "email не принят"):
            return "email"
        if await self._choose_account(page, state.email):
            return "account_chooser"
        if await self._pick_authenticator(page, state):
            return "challenge"
        return None

    async def _settle(self, page: Any) -> None:
        """The flow ended on youtube.com: let its redirect chain finish before anything navigates away."""
        try:
            await page.wait_for_load_state("load", timeout=15000)
        except PlaywrightError as exc:
            logger.debug("YouTube не дождался события load: {}", exc)

    _NON_ERROR_STRINGS = frozenset({
        "welcome", "добро пожаловать", "sign in", "вход",
        "choose an account", "выберите аккаунт", "next", "далее",
        "enter your password", "введите пароль", "to continue to youtube",
        "show password", "показать пароль", "forgot password?", "забыли пароль?",
        "2-step verification", "2-этапная аутентификация",
        "to help keep your account safe", "чтобы защитить аккаунт",
        "make sure you can always sign in", "добавьте резервный номер телефона",
        "add a recovery", "recovery email", "enter recovery email",
    })

    async def _enter_value(self, page: Any, field_locator: Any, value: str, selectors: tuple[str, ...]) -> str:
        """Type `value` into the field, click Next or press Enter and wait for Google's answer; returns changed / error / timeout."""
        path_before = _screen_path(page.url)
        await self._simulator.human_pause(0.6, 1.4)
        await self._simulator.click_locator(page, field_locator)
        await self._clear_field(field_locator)
        await self._simulator.type_text(page, value, allow_typos=False)
        await self._simulator.human_pause(0.4, 0.9)
        next_button = await self._first_visible(page, NEXT_BUTTONS, timeout_seconds=1.5)
        if next_button is not None:
            await self._simulator.click_locator(page, next_button)
        else:
            await page.keyboard.press("Enter")
        return await self._wait_after_submit(page, selectors, path_before)

    @staticmethod
    async def _clear_field(field_locator: Any) -> None:
        """A rejected value stays in the input: never append to it."""
        try:
            if await field_locator.input_value():
                await field_locator.fill("")
        except PlaywrightError as exc:
            logger.debug("Не удалось очистить поле: {}", exc)

    async def _wait_after_submit(self, page: Any, selectors: tuple[str, ...], path_before: str) -> str:
        deadline = time.monotonic() + SUBMIT_WAIT_SECONDS
        while True:
            if _screen_path(page.url) != path_before:
                return _CHANGED
            if await self._first_visible(page, selectors) is None:
                return _CHANGED
            error = await self._visible_error_text(page)
            if error:
                logger.warning("Google ответил ошибкой на экране {}: {}", path_before, error[:200])
                return f"{_ERROR}: {error}"
            if time.monotonic() >= deadline:
                return _TIMEOUT
            await asyncio.sleep(FIELD_POLL_SECONDS)

    async def _visible_error_text(self, page: Any) -> str | None:
        for selector in ERROR_BLOCKS:
            try:
                matches = page.locator(selector)
                for index in range(min(await matches.count(), 5)):
                    candidate = matches.nth(index)
                    if await candidate.is_visible():
                        tag = ""
                        try:
                            tag = ((await candidate.evaluate("e => e.tagName")) or "").lower()
                        except Exception:
                            pass
                        if tag in {"h1", "h2", "h3"}:
                            continue
                        text = (await candidate.inner_text()).strip()
                        if not text:
                            continue
                        clean = text.lower()
                        if any(s in clean for s in self._NON_ERROR_STRINGS) or "@" in text:
                            continue
                        return text
            except PlaywrightError:
                continue
        return None

    async def _submit_field(
        self, page: Any, selectors: tuple[str, ...], value: str, key: str, failure: str
    ) -> bool:
        field_locator = await self._first_visible(page, selectors)
        if field_locator is None:
            return False
        outcome = await self._enter_value(page, field_locator, value, selectors)
        logger.info("Экран {} заполнен (результат: {})", key, outcome)
        if outcome != _CHANGED:
            detail = outcome.split(": ", 1)[1] if ": " in outcome else None
            msg = f"{failure}: {detail}" if detail else failure
            raise GoogleAuthError(msg)
        return True

    async def _submit_totp(self, page: Any, state: _FormState) -> bool:
        field_locator = await self._first_visible(page, TOTP_INPUT)
        if field_locator is None:
            return False
        if not state.totp_secret:
            raise GoogleAuthError("Google запросил код 2FA, а totp_secret не задан")
        if state.totp_attempts >= TOTP_MAX_ATTEMPTS:
            raise GoogleAuthError("код 2FA не принят")
        if state.totp_attempts:  # the previous code was rejected: inside the same window the code would be the same
            await asyncio.sleep(get_remaining_seconds() + TOTP_WINDOW_PAD_SECONDS)
        await self._ensure_trust_device(page)
        try:
            code = await generate_fresh_totp(state.totp_secret)  # waits for a new window when the current one is about to end
        except ValueError as exc:
            raise GoogleAuthError(f"некорректный TOTP-секрет: {exc}") from None
        state.totp_attempts += 1
        outcome = await self._enter_value(page, field_locator, code, TOTP_INPUT)
        logger.debug("Код 2FA введён (попытка {}, результат: {})", state.totp_attempts, outcome)
        if outcome != _CHANGED and state.totp_attempts >= TOTP_MAX_ATTEMPTS:
            raise GoogleAuthError("код 2FA не принят")
        return True

    async def _ensure_trust_device(self, page: Any) -> None:
        """"Don't ask again on this device" must be checked."""
        checkbox = await self._first_visible(page, TOTP_TRUST_CHECKBOX)
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

    async def _choose_account(self, page: Any, email: str) -> bool:
        """"Choose an account" screen: our tile when it is there, otherwise "Use another account"."""
        if "/challenge/" in page.url:  # identity chips of challenge pages are not account tiles
            return False
        selectors: list[str] = []
        if email and '"' not in email and "\\" not in email:
            selectors.append(f'[data-identifier="{email}" i]')
        tile = await self._first_visible(page, tuple(selectors)) if selectors else None
        if tile is None:
            tile = await self._first_visible(page, USE_ANOTHER_ACCOUNT)
        if tile is None:
            return False
        await self._simulator.human_pause(0.8, 1.8)
        await self._simulator.click_locator(page, tile)
        logger.debug("Экран выбора аккаунта обработан")
        await self._simulator.human_pause(2.0, 3.5)
        return True

    async def _pick_authenticator(self, page: Any, state: _FormState) -> bool:
        """Challenge screen other than a code prompt ("Check your phone"): choose Google Authenticator."""
        if not state.totp_secret or "/challenge/" not in page.url:
            return False
        option = await self._first_visible(page, AUTHENTICATOR_OPTION)
        if option is not None:
            await self._simulator.click_locator(page, option)
            logger.debug("Выбран способ подтверждения Google Authenticator")
            await self._simulator.human_pause(2.0, 3.5)
            return True
        if state.another_way_clicks >= 2:
            return False
        button = await self._first_visible(page, TRY_ANOTHER_WAY)
        if button is None:
            return False
        state.another_way_clicks += 1
        await self._simulator.click_locator(page, button)
        await self._simulator.human_pause(2.0, 3.5)
        return True

    # ------------------------------------------------------------------ finalization

    async def _wait_login_cookie(self, context: Any) -> bool:
        """LOGIN_INFO is set by the last redirects of the flow: give them a moment."""
        deadline = time.monotonic() + LOGIN_COOKIE_WAIT_SECONDS
        while True:
            if await self.is_session_authenticated(context):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(FIELD_POLL_SECONDS)

    async def _read_ytcfg(self, page: Any) -> dict[str, Any]:
        """LOGGED_IN / CHANNEL_ID of the loaded YouTube page; retried while the page is still navigating."""
        for attempt in range(YTCFG_ATTEMPTS):
            try:
                data = await page.evaluate(YTCFG_JS)
            except PlaywrightError as exc:
                logger.debug("ytcfg недоступен ({}/{}): {}", attempt + 1, YTCFG_ATTEMPTS, exc)
                data = None
            if isinstance(data, dict) and data.get("ready"):
                return data
            await asyncio.sleep(YTCFG_POLL_SECONDS)
        raise GoogleAuthError("YouTube не отдал ytcfg: не удалось проверить вход и канал")

    async def _ensure_channel(self, page: Any) -> None:
        await page.goto(YOUTUBE_HOME_URL, wait_until="domcontentloaded")
        await self._simulator.human_pause(2.0, 4.0)
        config = await self._read_ytcfg(page)
        if not config.get("loggedIn"):
            raise GoogleAuthError("после входа YouTube открылся в гостевом режиме (LOGGED_IN=false)")
        if config.get("channelId"):
            return
        logger.info("У аккаунта нет канала, создаю")
        await page.goto(CHANNEL_CREATION_URL, wait_until="domcontentloaded")
        button = await self._first_visible(page, (CREATE_CHANNEL_BUTTON,), timeout_seconds=15.0)
        if button is None:
            raise GoogleAuthError("не найдена кнопка создания канала")
        await self._simulator.click_locator(page, button)
        await self._simulator.human_pause(3.0, 5.0)
        for _ in range(CHANNEL_CHECK_ATTEMPTS):
            await page.goto(YOUTUBE_HOME_URL, wait_until="domcontentloaded")
            await self._simulator.human_pause(2.0, 3.0)
            if (await self._read_ytcfg(page)).get("channelId"):
                logger.info("Канал создан")
                return
        raise GoogleAuthError("канал не создан: CHANNEL_ID не появился после подтверждения")

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _first_visible(page: Any, selectors: tuple[str, ...], timeout_seconds: float = 0.0) -> Any | None:
        return await first_visible(page, selectors, timeout_seconds, limit=5, poll_seconds=FIELD_POLL_SECONDS)

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


__all__ = [
    "GoogleAuthError",
    "GoogleAuthenticator",
    "SESSION_OWNER_FILE",
    "read_session_owner",
    "write_session_owner",
]
