from __future__ import annotations

import asyncio
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from loguru import logger

from modules.browser_errors import PlaywrightError
from modules.chrome_session import save_chrome_heartbeat_state
from modules.dom_snapshot import redact, sanitize_html
from modules.interaction_simulator import InteractionError, InteractionSimulator
from modules.page_helpers import CONSENT_REJECT_SELECTORS, SIGN_IN_LINK, first_visible
from modules.totp_helper import generate_fresh_totp, get_remaining_seconds

YOUTUBE_HOME_URL = "https://www.youtube.com/?hl=en&gl=US"
CHANNEL_CREATION_URL = "https://www.youtube.com/channel_creation"
GOOGLE_TERMS_URL = "https://policies.google.com/terms?hl=en"

# Hosts that carry the sign-in flow and its speedbumps; accounts.youtube.com (SetSID redirects) is only transit.
GOOGLE_HOSTS = frozenset({"accounts.google.com", "gds.google.com", "myaccount.google.com"})
YOUTUBE_HOSTS = frozenset({"www.youtube.com", "youtube.com", "m.youtube.com"})


def _buttons(*labels: str) -> tuple[str, ...]:
    """`button` and `[role=button]` selectors for each label (Google renders both).

    Text matching is allowed ONLY for the five constants below (see the note above them): never use this helper for
    YouTube UI. `tests/test_selector_guard.py` allowlists exactly this helper and those constants.
    """
    return tuple(
        selector
        for label in labels
        for selector in (f'button:has-text("{label}")', f'[role="button"]:has-text("{label}")')
    )


# Selectors are structural only (ids, names, types, data-*): the accounts run in any UI language and Google localizes
# visible text and aria-label. Without Next the flow presses Enter (see _enter_value).
NEXT_BUTTONS = (
    'button#passwordNext',
    'button#identifierNext',
    'button#totpNext',
    '#passwordNext button',
    '#identifierNext button',
    '#totpNext button',
)
CONSENT_REJECT = CONSENT_REJECT_SELECTORS
EMAIL_INPUT = ('input#identifierId', 'input[name="identifier"]')  # no generic type="email": it matches recovery-contact fields
PASSWORD_INPUT = ('input[type="password"]', 'input[name="Passwd"]')
TOTP_INPUT = ('input#totpPin',)
TOTP_TRUST_CHECKBOX = ('input#trustDevice', 'input[name="TrustDevice"]', 'input[type="checkbox"]')
KPE_INPUT = ('input#knowledge-preregistered-email-response',)
CAPTCHA_MARKERS = ('img#captchaimg', 'iframe[src*="recaptcha"]')
ERROR_BLOCKS = ('[role="alert"]', 'div[jsname="B34EJ"]', '[aria-live="assertive"] [jsname="B34EJ"]')
AUTHENTICATOR_OPTION = ('div[data-challengetype="6"]',)
# "Verify your info to continue" (accounts.google.com/uplevelingstep/selection, captured live): Google asks for a phone-based
# check, tiles with `data-step-type` 5 (QR code) and 1 (SMS), that cannot be automated. Recognised by the path and by the
# tiles, never by text, so it is the same in every UI language and fails at once instead of idling for UNKNOWN_SCREEN_SECONDS.
UPLEVELING_PATH = "/uplevelingstep/"
UPLEVELING_TILES = ('div[role="link"][data-step-type]',)
# DELIBERATE, DO NOT "FIX" BLINDLY: these five are the only text matchers allowed in modules/*.py. They target
# accounts.google.com sign-in / speedbump screens (not YouTube UI), which have no structural handle, and the login must
# stay fully automatic. Labels cover en and ru only; any other language falls through to the "неизвестный экран" error.
# Never match "Continue" on the passkey screen: it opens the native Windows Hello prompt.
TRY_ANOTHER_WAY = _buttons("Try another way", "Другой способ")
USE_ANOTHER_ACCOUNT = (
    '[role="link"]:has-text("Use another account")',
    'li:has-text("Use another account")',
    '[role="link"]:has-text("Использовать другой аккаунт")',
    'li:has-text("Использовать другой аккаунт")',
)
DECLINE_BUTTONS = _buttons("Not now", "Не сейчас", "Skip", "Пропустить", "No thanks", "Нет, спасибо", "Cancel", "Отмена")
WAS_IT_YOU_BUTTONS = _buttons("Yes, it was me", "Да, это я")
ACKNOWLEDGE_BUTTONS = _buttons("I understand", "Я понимаю") + ('input[type="submit"][value="I understand"]',)
CREATE_CHANNEL_BUTTON = '#create-channel-button button'
# Camoufox evaluates page.evaluate() in an isolated world: `window.ytcfg` is invisible there (so LOGGED_IN always read false)
# and the main world (`mw:`) stays off on purpose. The inline `ytcfg.set({...})` script is DOM, readable from any world.
YTCFG_JS = r"""() => {
  const find = (re) => { for (const s of document.scripts) { const m = !s.src && re.exec(s.textContent); if (m) return m; } return null; };
  const flag = find(/"LOGGED_IN":\s*(true|false)/);
  const channel = find(/"CHANNEL_ID":\s*"([\w-]+)"/);
  return {ready: flag !== null, loggedIn: flag !== null && flag[1] === 'true', channelId: channel ? channel[1] : null};
}"""

# Chrome only: the real navigator.userAgent / navigator.userAgentData of the browser, cached for the offline heartbeat so its
# Sec-CH-UA headers match the User-Agent. Plain property reads, no CDP and no injected script. In headless Chrome the brands
# may contain "HeadlessChrome": they are stored as they are, `session_heartbeat` normalizes them.
CHROME_USER_AGENT_JS = "() => navigator.userAgent"
CHROME_CLIENT_HINTS_JS = (
    "() => navigator.userAgentData ? {brands: navigator.userAgentData.brands.map(b => ({brand: b.brand, version: b.version})), "
    "mobile: navigator.userAgentData.mobile, platform: navigator.userAgentData.platform} : null"
)
BLANK_PAGE_URLS = ("", "about:blank")
BLANK_PAGE_PREFIXES = ("chrome://newtab", "chrome://new-tab-page")

# Chrome only: a silent virtual WebAuthn authenticator for the time of the sign-in. Without it Google's passkey screens call
# navigator.credentials.get/create and Windows opens its native Windows Hello / security key dialog, which automation cannot
# close. Only the WebAuthn CDP domain is used (never Runtime or anything else). With automaticPresenceSimulation=False a
# request never resolves (nobody simulates the touch) and with isUserVerified=False a user verification fails: the page
# buttons stay clickable, the flow declines or goes "Try another way" like it does without an authenticator.
VIRTUAL_AUTHENTICATOR_OPTIONS = {
    "protocol": "ctap2",
    "transport": "internal",
    "hasResidentKey": True,
    "hasUserVerification": True,
    "isUserVerified": False,
    "automaticPresenceSimulation": False,
}

SESSION_OWNER_FILE = "account_owner.txt"

AUTH_TIMEOUT_SECONDS = 240.0
NAVIGATION_TIMEOUT_MS = 45000
POLL_SECONDS = 1.0
FIELD_POLL_SECONDS = 0.4
SUBMIT_WAIT_SECONDS = 18.0  # how long Google may take to answer a submitted field before it counts as rejected
STALE_ERROR_GRACE_SECONDS = 3.0  # an error already on screen before the submit is not Google's answer until this long passed
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
    password_sent: set[str] = field(default_factory=set)  # screen paths that already received the password


def _screen_path(url: str) -> str:
    """URL without query and fragment: it names the screen in messages and never carries tokens."""
    parsed = urlparse(url)
    return f"{parsed.hostname or ''}{parsed.path}"


def _key_hold_ms() -> float:
    """How long a key stays down (ms): a keydown followed by a keyup in the same millisecond is not a human press."""
    return random.uniform(40, 110)


def with_hl_en(url: str) -> str:
    """The same URL with `hl=en`: every other parameter (service, continue, flowName...) is kept as it is."""
    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "hl"]
    return urlunparse(parsed._replace(query=urlencode(query + [("hl", "en")])))


def _is_english(url: str) -> bool:
    return dict(parse_qsl(urlparse(url).query)).get("hl", "").lower() == "en"


def _is_google_sign_in(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == "accounts.google.com"


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


def has_live_login_cookie(cookies: list[dict[str, Any]]) -> bool:
    """A non-empty, unexpired LOGIN_INFO cookie on .youtube.com: the profile is signed in."""
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


def is_blank_page_url(url: Any) -> bool:
    """True for a tab that shows nothing: an empty URL, about:blank or the new tab page."""
    if not isinstance(url, str):
        return False
    url = url.strip().lower()
    return url in BLANK_PAGE_URLS or url.startswith(BLANK_PAGE_PREFIXES)


def normalize_client_hints(raw: Any) -> dict[str, Any] | None:
    """`{"brands": [{"brand", "version"}], "mobile": bool, "platform": str}` from a page read; None when it is not that shape."""
    if not isinstance(raw, dict) or not isinstance(raw.get("brands"), list):
        return None
    brands = [
        {"brand": item["brand"], "version": item["version"]}
        for item in raw["brands"]
        if isinstance(item, dict) and isinstance(item.get("brand"), str) and isinstance(item.get("version"), str)
    ]
    if not brands:
        return None
    return {"brands": brands, "mobile": bool(raw.get("mobile")), "platform": str(raw.get("platform") or "")}


async def read_chrome_identity(page: Any) -> tuple[str | None, dict[str, Any] | None]:
    """`(navigator.userAgent, client hints)` of a Chrome page; each half is None when its read failed (no CDP, plain evaluate)."""
    user_agent: str | None = None
    hints: dict[str, Any] | None = None
    try:
        value = await page.evaluate(CHROME_USER_AGENT_JS)
        user_agent = value if isinstance(value, str) and value else None
    except Exception as exc:
        logger.debug("Не удалось прочитать navigator.userAgent: {}", exc)
    try:
        hints = normalize_client_hints(await page.evaluate(CHROME_CLIENT_HINTS_JS))
    except Exception as exc:
        logger.debug("Не удалось прочитать navigator.userAgentData: {}", exc)
    return user_agent, hints


class GoogleAuthenticator:
    """Signs a Camoufox persistent context into YouTube through the regular Google sign-in flow."""

    def __init__(
        self,
        simulator: InteractionSimulator,
        artifacts_dir: Path | str | None = None,
        chrome_profile_dir: Path | str | None = None,
    ) -> None:
        self._simulator = simulator
        self._artifacts_dir = Path(artifacts_dir) if artifacts_dir is not None else None  # None: no unknown-screen dump
        # Chrome profile folder: after a successful sign-in the cookies are exported to its heartbeat_state.bin right away
        self._chrome_profile_dir = Path(chrome_profile_dir) if chrome_profile_dir is not None else None
        self.detected_country: str | None = None

    async def _save_chrome_state(self, context: Any, page: Any | None = None) -> None:
        """Chrome only, best effort: persist the signed-in cookies without waiting for the browser to close."""
        if self._chrome_profile_dir is None:
            return
        try:
            if page is None:
                pages = getattr(context, "pages", None) or []
                page = pages[0] if pages else None
            user_agent, client_hints = await read_chrome_identity(page) if page is not None else (None, None)
            await save_chrome_heartbeat_state(self._chrome_profile_dir, context, user_agent, client_hints=client_hints)
        except Exception as exc:
            logger.warning("Не удалось сохранить heartbeat_state.bin после входа: {}", exc)

    @staticmethod
    async def _acquire_page(context: Any, reuse_blank: bool) -> tuple[Any, bool]:
        """`(page, reused)`. Chrome reuses an empty tab it already has instead of opening a second one; Camoufox never does."""
        if reuse_blank:
            try:
                for candidate in list(getattr(context, "pages", None) or []):
                    if not candidate.is_closed() and is_blank_page_url(candidate.url):
                        return candidate, True
            except Exception as exc:
                logger.debug("Не удалось найти пустую вкладку для авторизации: {}", exc)
        return await context.new_page(), False

    @staticmethod
    async def _attach_virtual_authenticator(context: Any, page: Any) -> tuple[Any, str | None] | None:
        """Chrome only: add the silent virtual authenticator to `page`; None (the login goes on) when any CDP step fails."""
        cdp: Any | None = None
        try:
            cdp = await context.new_cdp_session(page)
            await cdp.send("WebAuthn.enable")
            added = await cdp.send("WebAuthn.addVirtualAuthenticator", {"options": dict(VIRTUAL_AUTHENTICATOR_OPTIONS)})
            authenticator_id = added.get("authenticatorId") if isinstance(added, dict) else None
        except Exception as exc:
            logger.debug("Виртуальный аутентификатор WebAuthn не подключён: {}", exc)
            if cdp is not None:
                await GoogleAuthenticator._detach_virtual_authenticator((cdp, None))
            return None
        logger.debug("Подключён виртуальный аутентификатор WebAuthn на время входа")
        return cdp, authenticator_id

    @staticmethod
    async def _detach_virtual_authenticator(virtual: tuple[Any, str | None] | None) -> None:
        """Remove the authenticator, disable the WebAuthn domain, detach the session: every step on its own, never raises."""
        if virtual is None:
            return
        cdp, authenticator_id = virtual
        if authenticator_id is not None:
            try:
                await cdp.send("WebAuthn.removeVirtualAuthenticator", {"authenticatorId": authenticator_id})
            except Exception as exc:
                logger.debug("Не удалось удалить виртуальный аутентификатор: {}", exc)
        try:
            await cdp.send("WebAuthn.disable")
        except Exception as exc:
            logger.debug("Не удалось отключить домен WebAuthn: {}", exc)
        try:
            await cdp.detach()
        except Exception as exc:
            logger.debug("Не удалось отсоединить CDP-сессию WebAuthn: {}", exc)

    async def is_session_authenticated(self, context: Any) -> bool:
        """Zero-risk fast path: a live LOGIN_INFO cookie on .youtube.com means the profile is already signed in."""
        try:
            cookies = await context.cookies()
        except Exception as exc:
            logger.debug("Не удалось прочитать cookies контекста: {}", exc)
            return False
        return has_live_login_cookie(cookies)

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
            await self._save_chrome_state(context)
            return True

        is_chrome = self._chrome_profile_dir is not None
        page, reused = await self._acquire_page(context, reuse_blank=is_chrome)
        if not reused:  # a reused tab belongs to the runner: its own timeouts stay
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
            page.set_default_timeout(NAVIGATION_TIMEOUT_MS)
        virtual: tuple[Any, str | None] | None = None
        try:
            if is_chrome:
                virtual = await self._attach_virtual_authenticator(context, page)
            await self._open_sign_in(page)
            await self._run_form(page, email, password, totp_secret, recovery_email, timeout_seconds)
            if not await self._wait_login_cookie(context):
                raise GoogleAuthError("после входа кука LOGIN_INFO не появилась в контексте")
            await self._ensure_channel(page)
            if not await self.is_session_authenticated(context):
                raise GoogleAuthError("после создания канала кука LOGIN_INFO пропала")
            try:
                self.detected_country = await self.detect_account_country(page)
                if self.detected_country:
                    logger.info("Определён гео-регион аккаунта: {}", self.detected_country)
            except Exception as exc:
                logger.debug("Не удалось определить страну аккаунта: {}", exc)
            await self._save_chrome_state(context, page)
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
            await self._detach_virtual_authenticator(virtual)  # before the page goes away: the CDP session belongs to it
            if not reused:  # the page after the sign-in stays open when it was the runner's own tab
                try:
                    await page.close()
                except Exception as exc:
                    logger.debug("Не удалось закрыть страницу авторизации: {}", exc)

    async def detect_account_country(self, page: Any) -> str | None:
        """Fetch the account's registered country version from Google Terms (hl=en)."""
        try:
            await page.goto(GOOGLE_TERMS_URL, wait_until="domcontentloaded")
            await self._simulator.human_pause(0.5, 1.5)
            content = await page.content()
            import re
            m = re.search(r"Country version:\s*([A-Za-z\s\-]+?)(?:<|\n|\r|$)", content, re.IGNORECASE)
            if m:
                country = m.group(1).strip().split("<")[0].strip()
                if country:
                    return country
            for el in await page.query_selector_all("span, div, p, h1, h2, h3"):
                txt = (await el.inner_text()).strip()
                if "Country version:" in txt:
                    parts = txt.split("Country version:")
                    if len(parts) > 1 and parts[1].strip():
                        candidate = parts[1].strip().split("\n")[0].strip()
                        if candidate:
                            return candidate
        except Exception as exc:
            logger.debug("Не удалось определить страну аккаунта: {}", exc)
        return None

    # ------------------------------------------------------------------ entry point

    async def _open_sign_in(self, page: Any) -> None:
        await page.goto(YOUTUBE_HOME_URL, wait_until="domcontentloaded")
        await self._simulator.human_pause(1.5, 3.0)
        await self._dismiss_consent(page)
        link = await self._first_visible(page, (SIGN_IN_LINK,), timeout_seconds=15.0)
        if link is None:
            raise GoogleAuthError("на youtube.com не найдена ссылка Sign in")
        # The handlers below know en/ru labels only, and the link carries the browser / account language (hl=ru, hl=pl...):
        # open the same sign-in URL with hl=en so every Google screen is rendered in English.
        href = await link.get_attribute("href")
        if href and _is_google_sign_in(href) and not _is_english(href):
            await page.goto(with_hl_en(href), referer=page.url, wait_until="domcontentloaded")
        else:
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
            except (InteractionError, *PlaywrightError) as exc:  # the page navigated under the cursor
                logger.debug("Экран {} изменился во время действия: {}", path, exc)
                await asyncio.sleep(FIELD_POLL_SECONDS)
                continue

            now = time.monotonic()
            if screen is None:
                if state.idle_key != path:
                    state.idle_key, state.idle_since = path, now
                elif now - state.idle_since > UNKNOWN_SCREEN_SECONDS:
                    await self._dump_unknown_screen(page, path)
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

    async def _dump_unknown_screen(self, page: Any, path: str) -> None:
        """Save the sanitized DOM of a screen no handler knows (no query string, values, emails or tokens) for a new handler."""
        if self._artifacts_dir is None:
            return
        try:
            snapshot = sanitize_html(await page.content())
            target = self._artifacts_dir / f"google_unknown_screen_{int(time.time())}.html"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"<!-- {redact(path)} -->\n{snapshot}\n", encoding="utf-8")
            logger.info("Снимок неизвестного экрана Google сохранён в {}", target)
        except Exception as exc:
            logger.debug("Не удалось сохранить снимок неизвестного экрана: {}", exc)

    async def _handle_screen(self, page: Any, state: _FormState) -> str | None:
        """Act on the first recognised element of the current screen; returns the screen name, None when nothing matches."""
        # Before any click: the footer button of this screen is not a safe "decline" (its meaning is not known)
        if UPLEVELING_PATH in page.url or await self._any_visible(page, UPLEVELING_TILES):
            raise GoogleAuthError(
                f"Google требует подтверждение аккаунта по телефону (QR/SMS), автоматически не пройти: {_screen_path(page.url)}"
            )
        # Speedbumps and security confirmation interstitials: decline or acknowledge them first
        if await self._click_first(page, DECLINE_BUTTONS):
            return "decline"
        if await self._click_first(page, WAS_IT_YOU_BUTTONS):
            return "was_it_you"
        if await self._click_first(page, ACKNOWLEDGE_BUTTONS):
            return "acknowledge"

        # The same password is never sent twice to one screen: if it came back, the first try was most likely rejected
        password_path = _screen_path(page.url)
        if password_path in state.password_sent and await self._any_visible(page, PASSWORD_INPUT):
            raise GoogleAuthError(f"экран пароля вернулся после отправки, повторно пароль не вводится: {password_path}")
        if await self._submit_field(page, PASSWORD_INPUT, state.password, "password", "неверный пароль"):
            state.password_sent.add(password_path)
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
        await self._clear_field(page, field_locator)
        await self._simulator.type_text(page, value, allow_typos=False)
        await self._simulator.human_pause(0.4, 0.9)
        next_button = await self._first_visible(page, NEXT_BUTTONS, timeout_seconds=1.5)
        stale_error = await self._visible_error_text(page)  # left over from the previous try: not the answer to this submit
        if next_button is not None:
            await self._simulator.click_locator(page, next_button)
        else:
            if selectors != KPE_INPUT:  # only the recovery-email screen has no Next; anywhere else Enter is a fallback worth a trace
                logger.warning("На экране {} нет кнопки Next, отправляю Enter", path_before)
            await page.keyboard.press("Enter", delay=_key_hold_ms())
        return await self._wait_after_submit(page, selectors, path_before, stale_error)

    async def _clear_field(self, page: Any, field_locator: Any) -> None:
        """A rejected value stays in the input: never append to it.

        Select-all + Backspace instead of `fill("")`: a direct DOM write has no keydown/keyup and a 0 ms delta. The focusing
        click goes through the simulator (a pointer path), keys are held for a human-like time.
        """
        try:
            if await field_locator.input_value():
                await self._simulator.click_locator(page, field_locator)
                await page.keyboard.press("Control+A", delay=_key_hold_ms())
                await asyncio.sleep(random.uniform(0.1, 0.25))
                await page.keyboard.press("Backspace", delay=_key_hold_ms())
                await asyncio.sleep(random.uniform(0.15, 0.3))
        except (InteractionError, *PlaywrightError) as exc:
            logger.debug("Не удалось очистить поле: {}", exc)

    async def _wait_after_submit(
        self, page: Any, selectors: tuple[str, ...], path_before: str, stale_error: str | None = None
    ) -> str:
        started = time.monotonic()
        deadline = started + SUBMIT_WAIT_SECONDS
        stale_gone = False  # the error that was on screen at the submit has disappeared (so any error now is a new one)
        while True:
            if _screen_path(page.url) != path_before:
                return _CHANGED
            if await self._first_visible(page, selectors) is None:
                return _CHANGED
            error = await self._visible_error_text(page)
            if error is None:
                stale_gone = True
            elif (
                stale_error is None or error != stale_error or stale_gone
                or time.monotonic() - started >= STALE_ERROR_GRACE_SECONDS
            ):
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
        if outcome == _CHANGED:
            state.totp_attempts = 0  # accepted: the next 2FA screen (re-auth) gets a fresh budget and no wait for a new window
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

        # Modern YouTube: check avatar menu for in-situ create_channel dialog
        avatar = await self._first_visible(page, ("button#avatar-btn", "#avatar-btn"), timeout_seconds=3.0)
        if avatar is not None:
            await self._simulator.click_locator(page, avatar)
            await self._simulator.human_pause(1.0, 2.0)
            create_link = await self._first_visible(page, ("a[href*='create_channel']",), timeout_seconds=3.0)
            if create_link is not None:
                logger.info("У аккаунта нет канала, создаю через меню профиля")
                await self._simulator.click_locator(page, create_link)
                await self._simulator.human_pause(2.0, 3.0)
                button = await self._first_visible(page, (CREATE_CHANNEL_BUTTON,), timeout_seconds=15.0)
                if button is not None:
                    await self._simulator.click_locator(page, button)
                    await self._simulator.human_pause(3.0, 5.0)
                    logger.info("Канал создан")
                    return
            else:
                try:
                    await page.keyboard.press("Escape", delay=_key_hold_ms())
                except Exception:
                    pass
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
