from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from camoufox.async_api import AsyncCamoufox
from loguru import logger
from modules.browser_errors import PlaywrightError
from pydantic import BaseModel, ConfigDict

from config import BrowserSettings, EngineType, TargetSettings, parse_proxy, validate_proxy_for_engine
from modules.chrome_launcher import ensure_chrome_executable, get_chrome_args, prepare_chrome_profile, resolve_chrome_geo
from modules.chrome_session import save_chrome_heartbeat_state
from modules.fingerprint_manager import get_or_create_fingerprint, host_webgl_config, is_compatible
from modules.geo_guard import check_geo_lock, ensure_network_ready, read_session_geo, write_session_geo
from modules.google_authenticator import has_live_login_cookie, read_chrome_identity
from modules.interaction_simulator import InteractionError, InteractionSimulator
from modules.page_helpers import CONSENT_ACCEPT_SELECTORS, SIGN_IN_LINK, first_visible
from modules.ui_test_runner import NavigationError, ReplyUnavailableError, SubmissionError, SubmissionResult


class YouTubeCommentElement(BaseModel):
    """Metadata of a sub-comment located on the live YouTube DOM."""
    model_config = ConfigDict(frozen=True)

    comment_id: str | None
    author: str
    text: str
    is_reply: bool


_UNSET = object()

# Structural selectors only, verified live in ru-RU and en-US (see .mex/patterns/youtube-reply-flow.md): visible text and
# aria-label are localized and must never be matched.
EXPAND_REPLIES_SELECTORS = (
    # the visible expander of the current layout; the two ids below sit in the collapsed #expander and are hidden
    "#replies #more-replies-sub-thread button",
    "#replies ytd-button-renderer#more-replies button",
    "ytd-comment-replies-renderer #more-replies",
    "#more-replies button",
)
REPLY_INPUT_SELECTORS = (
    "#reply-dialog #contenteditable-root",
    "ytd-comment-reply-dialog-renderer #contenteditable-root",
    "div#contenteditable-root[contenteditable='true']",
    "#contenteditable-textarea #contenteditable-root",
    "ytd-comment-reply-dialog-renderer textarea",
)
SUBMIT_BUTTON_SELECTOR = "#submit-button button"  # inside the composer that holds the typed text
SUBMIT_BUTTON_PAGE_SELECTORS = (
    "#reply-dialog #submit-button button",
    "ytd-comment-reply-dialog-renderer #submit-button button",
    "ytd-button-renderer#submit-button button",
)
_PLAYER_STATE_JS = """() => {
    const player = document.getElementById('movie_player');
    const video = player && player.querySelector('video');
    if (!video) return {video: false};
    const big = player.querySelector('.ytp-large-play-button');
    const box = video.getBoundingClientRect();
    return {
        video: true, paused: video.paused, ended: video.ended, muted: video.muted,
        ad: player.classList.contains('ad-showing') || player.classList.contains('ad-interrupting'),
        // a video that never started sits off-screen under the cued thumbnail: its big play button is the click target
        bigPlay: !!(big && big.offsetWidth > 0 && big.offsetHeight > 0),
        shown: box.height > 0 && box.bottom > 0 && box.top < innerHeight,
    };
}"""
# The player's own mute (what its mute button does), called before the click so playback never starts audibly.
_MUTE_PLAYER_JS = """() => {
    const player = document.getElementById('movie_player');
    if (player && typeof player.mute === 'function') { player.mute(); return true; }
    return false;
}"""
_COMMENT_ID_RE = re.compile(r'"commentId"\s*:\s*"(Ug[\w-][\w.\-]*)"')  # comment ids start with Ugw, Ugx, Ugy or Ugz


class YouTubeRunner:
    """Specialized Camoufox browser runner for YouTube comment and sub-comment interactions."""

    def __init__(
        self,
        browser_settings: BrowserSettings,
        target: TargetSettings,
        session_dir: Path | str | None = None,
        proxy: str | None | Any = _UNSET,
        account_name: str = "default",
        *,
        simulator: InteractionSimulator | None = None,
        artifacts_dir: Path | None = None,
        rng: random.Random | None = None,
        locale: str | None = None,
        engine: EngineType = "camoufox",
        geo_country: str | None = None,
    ) -> None:
        self._cfg = browser_settings
        self._target = target
        self._account_name = account_name
        self._engine: EngineType = engine
        self._geo_country = geo_country  # Chrome: time zone fallback when the exit country cannot be resolved
        self._explicit_locale = locale  # the account's own locale: wins over the country default of Chrome
        self._locale = locale or browser_settings.locale  # the account's own locale, else browser.locale
        if proxy is not _UNSET:
            self._proxy = proxy
        else:
            self._proxy = browser_settings.proxy
        if session_dir is not None:
            self._session_dir = Path(session_dir)
        else:
            self._session_dir = browser_settings.session_root / target.name
        self._artifacts_dir = artifacts_dir or (browser_settings.session_root / "_artifacts")
        self._rng = rng or random.Random()
        self._simulator = simulator or InteractionSimulator(rng=self._rng)
        self._camoufox: Any | None = None
        self._patchright_driver: Any | None = None
        self._cached_user_agent: str | None = None
        self._cached_client_hints: dict[str, Any] | None = None  # Chrome: navigator.userAgentData, replayed as Sec-CH-UA
        self._context: Any | None = None
        self._page: Any | None = None
        self._managed_session = False
        self._session_country: str | None = None  # exit country of this session: recorded as the profile's geo after a sign-in
        self._session_timezone: str | None = None

    @property
    def engine(self) -> EngineType:
        return self._engine

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    @property
    def account_name(self) -> str:
        return self._account_name

    @property
    def proxy(self) -> str | None:
        return self._proxy

    @property
    def context(self) -> Any | None:
        return self._context

    @property
    def is_running(self) -> bool:
        return self._page is not None and not getattr(self._page, "is_closed", lambda: True)()

    def _prepare_dirs(self) -> None:
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)

    def launch_options(self) -> dict[str, Any]:
        """Camoufox options of this account (proxy, prefs, pinned fingerprint): for browser sessions opened outside `start()`."""
        return self._launch_options()

    def _launch_options(self) -> dict[str, Any]:
        prefs = dict(self._cfg.firefox_user_prefs)
        # Autonomous / background execution optimizations:
        # Prevent background tabs from sleeping, throttling DOM timers, or occlusion freezing
        prefs.setdefault("dom.min_background_timeout_value", 4)
        prefs.setdefault("dom.timeout.enable_budget_timer_throttling", False)
        prefs.setdefault("dom.timeout.background_throttling_max_budget", -1)
        prefs.setdefault("dom.timeout.background_budget_regeneration_rate", 100)
        prefs.setdefault("widget.windows.window_occlusion_tracking.enabled", False)
        prefs.setdefault("browser.tabs.unloadOnLowMemory", False)
        prefs.setdefault("network.proxy.socks_remote_dns", True)
        prefs.setdefault("network.http.http3.enable", False)
        prefs.setdefault("media.volume_scale", "0.0")

        options: dict[str, Any] = {
            "headless": self._cfg.headless,
            "os": self._cfg.os,
            "geoip": self._cfg.geoip,
            "humanize": self._cfg.humanize,
            "locale": self._locale,
            "persistent_context": True,
            "user_data_dir": str(self._session_dir),
            "firefox_user_prefs": prefs,
        }
        proxy_dict = parse_proxy(self._proxy)
        if proxy_dict is not None:
            options["proxy"] = proxy_dict
        try:
            options["fingerprint"] = get_or_create_fingerprint(self._session_dir, self._cfg.os)
            options["i_know_what_im_doing"] = True
        except Exception as exc:
            logger.warning("Не удалось получить сохранённый отпечаток, Camoufox сгенерирует случайный: {}", exc)
        gpu = host_webgl_config(self._session_dir, self._cfg.os)
        if gpu is not None:
            options["webgl_config"] = gpu  # the fingerprint's own gpu is not applied to WebGL: without this Camoufox redraws it per launch
        return options

    async def start(self) -> Any:
        """Launch persistent browser session once and keep it open across comments."""
        if self.is_running:
            return self._page
        if self._camoufox is not None or self._patchright_driver is not None:
            await self.close()  # the page was closed under us: release the old browser and its profile lock first
        self._prepare_dirs()
        if self._engine == "chrome":
            validate_proxy_for_engine(self._proxy, "chrome")  # the account proxy or the global one: SOCKS5 with a login fails
        # Fail closed before any browser or driver exists: a dead, transparent or host-IP proxy (NetworkGuardError) must
        # never let the account out through the machine's own address. Without a proxy and without require_proxy: a no-op.
        exit_geo = await ensure_network_ready(self._proxy, require_proxy=bool(getattr(self._cfg, "require_proxy", False)))
        if self._engine == "chrome":
            return await self._start_chrome()
        self._session_country = getattr(exit_geo, "country", None)
        self._session_timezone = getattr(exit_geo, "timezone", None)
        await self._check_geo_lock(self._session_country)
        options = await asyncio.to_thread(self._launch_options)  # fingerprint file and GPU table reads must not stall the loop
        self._camoufox = AsyncCamoufox(**options)
        try:
            self._context = await self._camoufox.__aenter__()
        except Exception as exc:
            pinned = options.get("fingerprint")
            if pinned is None or is_compatible(pinned):
                raise
            logger.warning("Сохранённый отпечаток несовместим с установленным Camoufox ({}), создаю новый", exc)
            try:
                await self._camoufox.__aexit__(None, None, None)
            except Exception as close_exc:
                logger.debug("Ошибка при освобождении неудавшегося запуска: {}", close_exc)
            options["fingerprint"] = get_or_create_fingerprint(self._session_dir, self._cfg.os, force_new=True)
            options["i_know_what_im_doing"] = True
            self._camoufox = AsyncCamoufox(**options)
            self._context = await self._camoufox.__aenter__()

        pages = getattr(self._context, "pages", [])
        self._page = pages[0] if pages else await self._context.new_page()
        self._page.set_default_timeout(self._target.wait_timeout_ms)
        self._page.set_default_navigation_timeout(self._cfg.navigation_timeout_ms)
        self._managed_session = True
        return self._page

    async def _start_chrome(self) -> Any:
        """Google Chrome through patchright: own profile folder, locale and time zone of the exit country.

        Only the launch options below are passed to the browser: no injected scripts, no request interception, no
        user agent / header / viewport overrides (see tests/test_chrome_webauthn_tabs.py). No `locale=` either: patchright
        would then replace navigator.languages and Accept-Language with a single tag; the language comes from `--lang`
        and the profile Preferences (`prepare_chrome_profile`). `no_viewport` keeps the real window.
        """
        geo = await resolve_chrome_geo(self._proxy, self._geo_country, self._explicit_locale, self._account_name)
        self._session_country, self._session_timezone = geo.country, geo.timezone
        await self._check_geo_lock(geo.country)
        await asyncio.to_thread(ensure_chrome_executable)
        await asyncio.to_thread(prepare_chrome_profile, self._session_dir, geo.locale)  # the profile language follows the launch locale
        chrome_args = get_chrome_args(locale=geo.locale, account_name=self._account_name, proxy_url=self._proxy)
        launch_options: dict[str, Any] = {
            "user_data_dir": str(self._session_dir),
            "channel": "chrome",
            "headless": self._cfg.headless,
            "args": chrome_args,
            "proxy": parse_proxy(self._proxy),
            "timezone_id": geo.timezone,
            "no_viewport": True,
        }
        if geo.geolocation:
            # The point of the proxy's city. No `permissions=`: nothing is granted on purpose, a site that asks gets the prompt.
            launch_options["geolocation"] = geo.geolocation

        from patchright.async_api import async_playwright as async_patchright

        self._patchright_driver = await async_patchright().start()
        try:
            self._context = await self._patchright_driver.chromium.launch_persistent_context(**launch_options)
        except BaseException:
            driver, self._patchright_driver, self._context = self._patchright_driver, None, None
            try:
                await driver.stop()
            except Exception as exc:
                logger.debug("Ошибка при остановке драйвера patchright после неудачного запуска: {}", exc)
            raise

        try:
            pages = list(getattr(self._context, "pages", None) or [])
            # The first tab is the working one: a new tab is opened only when the profile came up with none
            self._page = pages[0] if pages else await self._context.new_page()
            await self._close_extra_tabs(pages)
            self._page.set_default_timeout(self._target.wait_timeout_ms)
            self._page.set_default_navigation_timeout(self._cfg.navigation_timeout_ms)
            # The UA and the client hints are read once right after the start: the heartbeat replays them later without a browser
            self._cached_user_agent, self._cached_client_hints = await read_chrome_identity(self._page)
            await self._remember_session_geo(self._context)  # a profile that is already signed in
        except BaseException:  # a half-started Chrome must not keep the profile locked or leave node.exe behind
            await self._close_chrome()
            raise
        self._managed_session = True
        return self._page

    async def _check_geo_lock(self, country: str | None) -> None:
        """Compare the exit country with the one this profile signed in from: GeoLockError (mode block) stops the start.

        Nothing is launched yet. An unknown country (no proxy, lookup failed) has nothing to compare and is skipped.
        """
        if not country:
            return
        await asyncio.to_thread(check_geo_lock, self._session_dir, country, getattr(self._cfg, "geo_lock", "warn"))

    async def _remember_session_geo(self, context: Any) -> None:
        """Record the exit geo of a session that holds a live YouTube login; best effort, never raises.

        The record is created when there is none and refreshed when the country is the same. A different country never
        overwrites it silently: `check_geo_lock` already reported the change (critical in mode warn, an error in
        block), and the profile keeps the geo it signed in from.
        """
        country = self._session_country
        if not country or context is None:
            return
        try:
            if not has_live_login_cookie(await context.cookies()):
                return
            saved = await asyncio.to_thread(read_session_geo, self._session_dir)
            saved_country = str(saved.get("country") or "").upper() if isinstance(saved, dict) else ""
            if saved_country and saved_country != country.upper():
                return
            await asyncio.to_thread(write_session_geo, self._session_dir, country, self._session_timezone)
        except Exception as exc:
            logger.debug("Не удалось записать гео сессии профиля: {}", exc)

    async def _close_extra_tabs(self, pages: list[Any]) -> None:
        """Close the tabs Chrome restored next to the working one (blank, new tab page or leftovers); never the working tab."""
        for extra in pages:
            if extra is self._page:
                continue
            try:
                await extra.close()
            except Exception as exc:
                logger.debug("Не удалось закрыть лишнюю вкладку Chrome: {}", exc)

    async def close(self) -> None:
        """Close browser session at the end of the batch and save cookies."""
        if self._engine == "chrome":
            await self._close_chrome()
            return
        if self._camoufox is not None:
            await self._remember_session_geo(self._context)  # the context is still alive here
            try:
                await self._camoufox.__aexit__(None, None, None)
            except Exception as exc:
                logger.debug("Ошибка при закрытии сессии браузера: {}", exc)
            finally:
                self._camoufox = None
                self._context = None
                self._page = None
                self._managed_session = False
                self._session_country = self._session_timezone = None

    async def _close_chrome(self) -> None:
        """Save the heartbeat state (cookies are v20-encrypted in the profile), close the context, stop the driver.

        Each step is on its own: a failed save or close never leaves the patchright driver (node.exe) running.
        """
        context, driver = self._context, self._patchright_driver
        self._context = self._page = None
        self._patchright_driver = None
        self._managed_session = False
        try:
            if context is not None:
                await self._remember_session_geo(context)
                try:
                    await save_chrome_heartbeat_state(
                        profile_dir=self._session_dir,
                        context=context,
                        user_agent=self._cached_user_agent,
                        client_hints=self._cached_client_hints,
                    )
                except Exception as exc:
                    logger.warning("Не удалось сохранить heartbeat_state.bin: {}", exc)
                try:
                    await context.close()
                except Exception as exc:
                    logger.debug("Ошибка при закрытии сессии Chrome: {}", exc)
        finally:
            if driver is not None:
                try:
                    await driver.stop()
                except Exception as exc:
                    logger.debug("Ошибка при остановке драйвера patchright: {}", exc)
            self._session_country = self._session_timezone = None

    async def __aenter__(self) -> YouTubeRunner:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def run(
        self,
        text: str,
        *,
        external_id: str | None = None,
        payload_id: int | None = None,
        query: str | None = None,
        dry_run: bool = False,
    ) -> SubmissionResult:
        """Execute YouTube workflow in the running browser session without restarting."""
        if not text.strip():
            raise ValueError("submission text must not be empty")
        self._prepare_dirs()

        started = time.monotonic()
        if not self.is_running:
            await self.start()

        page = self._page
        try:
            # 1. Navigate to YouTube video (with &lc= if sub-comment ID present)
            await self._navigate_youtube(page, external_id=external_id, query=query)

            # 2. Dismiss cookie / consent popup if present
            await self._dismiss_consent(page)

            # 3. Simulate human watching/rendering video
            await self._render(page)

            # Extract comment_id from composite external_id (video_id#parent_id#comment_id)
            comment_id = None
            if external_id and "#" in external_id:
                comment_id = external_id.split("#")[-1]

            # 4. Scroll to comments section and ensure comments load
            await self._scroll_to_comments(page, comment_id=comment_id)

            # 5. Expand replies under comments if needed
            await self._expand_replies(page, comment_id=comment_id)

            # 6. Submit reply to the comment under comment
            remote_id = await self._submit_reply(page, text, comment_id=comment_id, dry_run=dry_run)

            result = SubmissionResult(
                success=True,
                remote_id=remote_id,
                final_url=page.url if page is not None else None,
                duration_seconds=time.monotonic() - started,
            )
            return result
        except asyncio.CancelledError:
            raise
        except ReplyUnavailableError as exc:
            duration = time.monotonic() - started
            logger.warning("Цель пропущена: у комментария нет кнопки «Ответить» (YouTube скрыл её, {:.1f}с)", duration)
            return SubmissionResult(
                success=False,
                final_url=page.url if page is not None else None,
                duration_seconds=duration,
                error=f"{type(exc).__name__}: {exc}",
                reply_unavailable=True,
            )
        except Exception as exc:
            duration = time.monotonic() - started
            logger.exception("Ошибка при выполнении действия на YouTube ({:.1f}с): {}", duration, exc)
            if page is not None and not getattr(page, "is_closed", lambda: True)():
                await self._capture_failure(page)
            return SubmissionResult(
                success=False,
                final_url=page.url if page is not None else None,
                duration_seconds=duration,
                error=f"{type(exc).__name__}: {exc}",
            )

    async def _navigate_youtube(
        self,
        page: Any,
        *,
        external_id: str | None,
        query: str | None,
    ) -> None:
        url = self._target.base_url
        if external_id:
            # Handle composite ID format: video_id#parent_id#comment_id or plain video_id
            if "#" in external_id:
                parts = external_id.split("#")
                video_id = parts[0]
                comment_id = parts[-1]
                url = f"https://www.youtube.com/watch?v={quote(video_id)}&lc={quote(comment_id)}"
            else:
                url = f"https://www.youtube.com/watch?v={quote(external_id)}"
        elif query:
            url = f"https://www.youtube.com/results?search_query={quote(query)}"

        logger.debug("Переход к видео в браузере: {}", url)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self._cfg.navigation_timeout_ms)
            try:
                await page.bring_to_front()
            except Exception:
                pass
        except PlaywrightError as exc:
            raise NavigationError(f"Ошибка перехода по адресу {url}: {exc}") from exc
        if response is not None and response.status >= 400:
            raise NavigationError(f"{url} вернул HTTP {response.status}")
        await self._simulator.human_pause(1.5, 3.0)

    async def _dismiss_consent(self, page: Any) -> None:
        """Dismiss EU/US cookie consent banner if it appears on YouTube."""
        for sel in CONSENT_ACCEPT_SELECTORS:
            try:
                locator = page.locator(sel).first
                if await locator.count() > 0 and await locator.is_visible():
                    logger.debug("Закрытие всплывающего баннера согласия YouTube ({})", sel)
                    await self._simulator.click_locator(page, locator)  # the simulated pointer path, never locator.click()
                    await self._simulator.human_pause(1.0, 2.0)
                    break
            except InteractionError as exc:  # no clickable box: the banner is not really there, try the next selector
                logger.debug("Баннер согласия не кликабелен ({}): {}", sel, exc)
                continue
            except Exception:
                continue

    async def _start_playback(self, page: Any) -> None:
        """Start a paused video with a real, muted click: Firefox blocks autoplay without a user gesture.

        Never during an ad (nothing else on the page is clicked), never on an ended or missing player; the click is
        verified to land on the big play button (a video that never started) or on the video. Muted through the player's own API before the click (no audible start); the
        `m` key, a trusted key event on the just-clicked player, is only the fallback when it is still unmuted.
        Best effort: any failure is logged and the render goes on.
        """
        if not self._cfg.render_play_video:
            return
        try:
            state = None
            for _ in range(6):  # the player appears a moment after domcontentloaded and its layout settles later
                state = await page.evaluate(_PLAYER_STATE_JS)
                if state and state.get("video") and (state.get("bigPlay") or state.get("shown")):
                    break
                await self._simulator.human_pause(0.4, 0.7)
            if not state or not state.get("video") or not state.get("paused") or state.get("ended") or state.get("ad"):
                return
            if not (state.get("bigPlay") or state.get("shown")):
                logger.debug("Плеер не прорисован, клик для запуска видео пропущен")
                return
            await page.evaluate(_MUTE_PLAYER_JS)
            target = "#movie_player .ytp-large-play-button" if state.get("bigPlay") else "#movie_player video"
            await self._simulator.click_locator(page, page.locator(target).first, timeout_ms=3000, verify_hit=True)
            await self._simulator.human_pause(0.6, 1.0)
            after = await page.evaluate(_PLAYER_STATE_JS)
            if after and after.get("video") and not after.get("muted") and not after.get("ad"):
                await page.keyboard.press("m", delay=self._key_hold_ms())
            logger.debug("Видео запущено кликом без звука (было на паузе)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Не удалось запустить видео кликом, продолжаю без него: {}", exc)

    async def _render(self, page: Any) -> None:
        """Simulate realistic human video viewing and initial page interaction."""
        min_s = max(0.0, float(self._cfg.render_min_seconds))
        max_s = max(min_s, float(self._cfg.render_max_seconds))
        duration = self._rng.uniform(min_s, max_s)
        deadline = time.monotonic() + duration
        logger.debug("Просмотр страницы видео ({:.1f}с)...", duration)
        await self._start_playback(page)
        await self._simulator.idle(page, min(1.5, max(0.1, duration)))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await self._simulator.human_pause(min(0.5, remaining), min(1.5, max(0.5, remaining)))
            if self._rng.random() < 0.3:
                await self._simulator.scroll(page, self._rng.randint(80, 200))

    async def _scroll_to_comments(self, page: Any, comment_id: str | None = None) -> None:
        """Scroll down to trigger lazy-loaded YouTube comments section and target sub-comment."""
        logger.debug("Прокрутка страницы для загрузки комментариев YouTube...")
        # YouTube requires scrolling ~400-800px below video player to load comments
        for _ in range(4):
            await self._simulator.scroll(page, self._rng.randint(350, 600))
            await self._simulator.human_pause(0.5, 1.2)
            try:
                comments = page.locator("ytd-comment-thread-renderer, ytd-comment-view-model")
                if await comments.count() > 0:
                    break
            except Exception:
                pass

        try:
            await page.wait_for_selector(
                "ytd-comment-thread-renderer, ytd-comment-view-model, ytd-comments#comments",
                state="attached",
                timeout=20000,
            )
            logger.debug("Секция комментариев YouTube загружена в DOM")
        except PlaywrightError as exc:
            logger.warning("Секция комментариев не успела прикрепиться за отведенный таймаут: {}", exc)

        # If targeting a specific linked sub-comment, scroll it into view
        if comment_id:
            try:
                target_loc = page.locator(f"a[href*='lc={comment_id}']").first
                if await target_loc.count() > 0 and await target_loc.is_visible():
                    await self._scroll_target_into_view(page, target_loc)
                    logger.debug("Целевой комментарий {} прокручен в область видимости", comment_id)
            except Exception:
                pass

    def _key_hold_ms(self) -> float:
        """How long a key stays down (ms): a keydown followed by a keyup in the same millisecond is not a human press."""
        return self._rng.uniform(40, 110)

    async def _scroll_target_into_view(self, page: Any, locator: Any) -> None:
        """Bring a linked comment into view with wheel scrolling; the instant scroll stays as a correction only.

        `scroll_into_view_if_needed` is a no-op once the element is visible, so after the wheel scroll it only fixes what
        the lazy-loaded layout moved meanwhile.
        """
        try:
            box = await locator.bounding_box()
            height = await page.evaluate("() => window.innerHeight")
            if box and isinstance(height, (int, float)) and height > 0 and not (0 <= box["y"] and box["y"] + box["height"] <= height):
                await self._simulator.scroll(page, int(box["y"] - height * 0.4))
        except Exception as exc:
            logger.debug("Плавная прокрутка к комментарию не удалась: {}", exc)
        await locator.scroll_into_view_if_needed()

    async def _expand_replies(self, page: Any, comment_id: str | None = None) -> None:
        """Find and expand reply buttons ('View replies', 'X ответов') under comments."""
        if comment_id:
            try:
                # If target comment is already highlighted and visible, it is already expanded.
                # Replies load lazily on &lc= pages, so give the target a moment before expanding other threads.
                target_loc = page.locator(f"a[href*='lc={comment_id}']").first
                try:
                    await target_loc.wait_for(state="visible", timeout=8000)
                except Exception:
                    pass
                if await target_loc.count() > 0 and await target_loc.is_visible():
                    logger.debug("Целевой комментарий {} уже открыт и виден в DOM", comment_id)
                    return
            except Exception:
                pass

        expanded_count = 0
        for sel in EXPAND_REPLIES_SELECTORS:
            try:
                buttons = page.locator(sel)
                count = await buttons.count()
                for i in range(min(count, 3)):
                    btn = buttons.nth(i)
                    if await btn.is_visible():
                        # Do not click if the button is already expanded (the collapse control is #less-replies)
                        try:
                            aria_attr = btn.get_attribute("aria-expanded")
                            if asyncio.iscoroutine(aria_attr):
                                aria_attr = await aria_attr
                            if aria_attr == "true":
                                continue
                        except Exception:
                            pass

                        logger.debug("Раскрытие ветки ответов под комментарием ({})", sel)
                        await self._simulator.click_locator(page, btn)
                        expanded_count += 1
                        await self._simulator.human_pause(1.0, 2.5)
                        break
            except Exception as exc:
                logger.debug("Не удалось раскрыть ветку ответов селектором {}: {}", sel, exc)
            if expanded_count > 0:
                break

    async def _submit_reply(
        self,
        page: Any,
        text: str,
        *,
        comment_id: str | None = None,
        dry_run: bool = False,
    ) -> str | None:
        """Click Reply button on sub-comment, human-type text into reply dialog, and submit."""
        # 1. Locate Reply button on the sub-comment with retry loop
        reply_button = await self._find_sub_comment_reply_button(page, comment_id=comment_id, timeout_seconds=15.0)
        if reply_button is None:
            # Check if YouTube is asking user to sign in
            sign_in = page.locator(SIGN_IN_LINK)
            if await sign_in.count() > 0:
                msg = (
                    f"Сессия YouTube в {self._session_dir} не авторизована. Выполните вход через "
                    "`python youtube_bot.py --auth-account <аккаунт>` или `--login`, чтобы отправлять комментарии."
                )
                logger.warning(msg)
                if dry_run:
                    logger.info("[Тестовый режим] Навигация успешна. Текст ответа готов: {!r}", text[:60])
                    return f"simulated_sub_reply_{int(time.time())}"
                raise SubmissionError(msg)
            if await self._reply_disabled_on_target(page, comment_id):
                raise ReplyUnavailableError(
                    "YouTube hides the Reply button on the target comment (#reply-button-end is hidden)"
                )
            raise SubmissionError("could not locate Reply button on sub-comment")

        logger.debug("Нажатие кнопки «Ответить» под комментарием")
        await self._simulator.click_locator(page, reply_button)
        await self._simulator.human_pause(1.0, 2.0)

        # 2. Wait for YouTube reply dialog to open with retry
        reply_input = await self._wait_reply_input(page)
        if reply_input is None and await self._confirm_channel_creation(page):
            # The first click only raised the profile setup dialog: click Reply again to open the form
            logger.debug("Повторное нажатие кнопки «Ответить» после создания канала")
            await self._simulator.click_locator(page, reply_button)
            await self._simulator.human_pause(1.0, 2.0)
            reply_input = await self._wait_reply_input(page)

        if reply_input is None:
            if dry_run:
                logger.info("[Тестовый режим] Кнопка «Ответить» нажата. Симуляция успешна")
                return f"simulated_sub_reply_{int(time.time())}"
            raise SubmissionError("YouTube reply input dialog (#contenteditable-root) did not appear")

        # 3. Focus and type into contenteditable with human cadence & typo simulation
        logger.debug("Активация поля ввода ответа на YouTube")
        await self._simulator.click_locator(page, reply_input)  # the click focuses the field: no programmatic focus()
        logger.debug("Осмысленная пауза перед вводом текста...")
        await self._simulator.thinking_pause()

        logger.debug("Печать текста ответа в поле ввода ({} симв.)...", len(text))
        await self._simulator.type_text(page, text)
        await self._simulator.human_pause(0.5, 1.5)

        if dry_run:
            logger.info("[Тестовый режим] Текст успешно напечатан в поле ввода (без клика отправки).")
            return f"dry_run_sub_reply_{int(time.time())}"

        # 4. Locate and click Submit / Reply button with retry
        # Only the dialog that holds the typed text: a page-wide 'Reply' match hits other comments' buttons.
        dialog = reply_input.locator(
            "xpath=ancestor::*[self::ytd-commentbox or self::ytd-comment-reply-dialog-renderer][1]"
        )
        submit_btn = None
        submit_deadline = time.monotonic() + 6.0
        while time.monotonic() < submit_deadline and submit_btn is None:
            searches = [(dialog, SUBMIT_BUTTON_SELECTOR)] + [(page, sel) for sel in SUBMIT_BUTTON_PAGE_SELECTORS]
            for scope, sel in searches:
                try:
                    locator = await self._first_visible(scope, sel)
                    if locator is not None:
                        submit_btn = locator
                        break
                except Exception:
                    continue
            if submit_btn is None:
                await asyncio.sleep(0.4)

        if submit_btn is None:
            raise SubmissionError("could not locate YouTube reply submit button in active dialog")

        logger.debug("Нажатие кнопки отправки ответа")
        # 5. The click must produce the create-comment request; a click on 'Cancel' closes the dialog without one.
        try:
            async with page.expect_response(lambda r: "/comment/create_comment" in r.url, timeout=15000) as sent:
                await self._simulator.click_locator(page, submit_btn, verify_hit=True)
            response = await sent.value
        except (InteractionError, *PlaywrightError) as exc:
            raise SubmissionError(f"reply was not submitted: {exc}") from exc
        if not response.ok:
            raise SubmissionError(f"YouTube rejected the reply: HTTP {response.status}")
        await self._simulator.human_pause(1.5, 3.0)

        remote_id = await self._extract_remote_id(response, exclude=(comment_id or "",))
        if remote_id is None:
            logger.warning(
                "В ответе create_comment не найден id нового ответа: проверка видимости для него невозможна"
            )
        return remote_id

    @staticmethod
    async def _extract_remote_id(response: Any, *, exclude: Iterable[str] = ()) -> str | None:
        """Server-side id of the NEW reply (`Parent.Reply`, Ugw/Ugx/Ugy/Ugz...) from the create_comment body.

        The body may also name the thread's parent, the comment we answered or its siblings; an id of any of them is
        public whatever happened to our reply, so taking one would give the visibility check a false "visible".
        Only a reply-shaped id (it holds a dot, a parent's never does) that is not in `exclude` and sits in the
        answered comment's thread qualifies, and only when exactly one such id is left; otherwise None ("unknown").
        """
        try:
            body = await response.text()
        except Exception as exc:
            logger.debug("Не удалось прочитать тело ответа create_comment: {}", exc)
            return None
        skipped = {item for item in exclude if item}
        threads = {item.split(".", 1)[0] for item in skipped}
        found = dict.fromkeys(match.group(1) for match in _COMMENT_ID_RE.finditer(body or ""))
        candidates = [
            item
            for item in found
            if "." in item and item not in skipped and (not threads or item.split(".", 1)[0] in threads)
        ]
        if len(candidates) > 1:
            logger.debug("В ответе create_comment несколько кандидатов на id нового ответа: {}", candidates)
            return None
        return candidates[0] if candidates else None

    async def _wait_reply_input(self, page: Any, timeout_seconds: float = 6.0) -> Any | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for sel in REPLY_INPUT_SELECTORS:
                try:
                    locator = await self._first_visible(page, sel)
                    if locator is not None:
                        return locator
                except Exception:
                    continue
            await asyncio.sleep(0.4)
        return None

    async def _confirm_channel_creation(self, page: Any) -> bool:
        """Confirm the first-time profile setup dialog if it blocks the reply form; True when it was closed."""
        try:
            dialog = await self._first_visible(page, "ytd-channel-creation-dialog-renderer")
            if dialog is None:
                return False
            logger.debug("Диалог первичной настройки профиля YouTube, подтверждение")
            button = await self._first_visible(page, "ytd-channel-creation-dialog-renderer #create-channel-button button")
            if button is None:
                return False
            await self._simulator.click_locator(page, button)
            await dialog.wait_for(state="hidden", timeout=15000)
            await self._simulator.human_pause(1.0, 2.0)
            return True
        except Exception as exc:
            logger.debug("Не удалось закрыть диалог создания канала: {}", exc)
            return False

    @staticmethod
    async def _first_visible(page: Any, selector: str, limit: int = 8) -> Any | None:
        """First visible match of `selector`; YouTube keeps hidden duplicates of comments ahead of the real one."""
        return await first_visible(page, selector, limit=limit)

    async def _reply_disabled_on_target(self, page: Any, comment_id: str | None) -> bool:
        """True when the target comment is fully rendered but YouTube hides its Reply button.

        Structure only (no text, no locale): the comment is visible, its action row `#toolbar` is rendered, and
        `#reply-button-end` exists yet none of its buttons is visible (YouTube sets `hidden` on it for comments whose
        replies are off, e.g. some pinned or channel comments). Verified live 2026-11-05: identical for ru-RU and ky-KG
        accounts, so it is a property of the comment, never of the account language. Unknown state (comment not
        rendered, no toolbar, no `#reply-button-end` at all) is False: that stays the generic "could not locate" error.
        """
        if not comment_id:
            return False
        try:
            for tag in ("ytd-comment-view-model", "ytd-comment-renderer"):
                comment = await self._first_visible(page, f"{tag}:has(a[href*='lc={comment_id}'])")
                if comment is None:
                    continue
                if await comment.locator("#toolbar").count() == 0 or not await comment.locator("#toolbar").first.is_visible():
                    return False  # the action row is still rendering: not a verdict
                slots = comment.locator("#reply-button-end")
                if await slots.count() == 0:
                    return False
                buttons = comment.locator("#reply-button-end :is(button, a)")
                for index in range(await buttons.count()):
                    if await buttons.nth(index).is_visible():
                        return False
                return True
        except Exception as exc:
            logger.debug("Не удалось определить, скрыта ли кнопка «Ответить»: {}", exc)
        return False

    async def _find_sub_comment_reply_button(
        self,
        page: Any,
        comment_id: str | None = None,
        timeout_seconds: float = 8.0,
    ) -> Any | None:
        """Locate the specific Reply button corresponding to a comment under comment with retry loop."""
        selectors: list[str] = []
        if comment_id:
            # 1. Exact match by sub-comment's permalink timestamp anchor. Matches both <button> (when channel
            # exists) and <a> (when profile setup dialog is required).
            selectors.extend([
                f"ytd-comment-view-model:has(a[href*='lc={comment_id}']) #reply-button-end :is(button, a)",
                f"ytd-comment-view-model:has(a[href*='lc={comment_id}']) #reply-button-end button",
                f"ytd-comment-view-model:has(a[href*='lc={comment_id}']) #reply-button-end a",
                f"ytd-comment-renderer:has(a[href*='lc={comment_id}']) #reply-button-end :is(button, a)",
                f"a[href*='lc={comment_id}'] >> xpath=ancestor::ytd-comment-view-model >> #reply-button-end :is(button, a)",
                f"a[href*='lc={comment_id}'] >> xpath=ancestor::ytd-comment-renderer >> #reply-button-end :is(button, a)",
            ])

        generic = [
            # 2. Reply button inside highlighted sub-comment (&lc= permalink)
            "ytd-comment-view-model[linked-comment-badge] #reply-button-end :is(button, a)",
            "ytd-comment-view-model:has(#linked-comment-badge) #reply-button-end :is(button, a)",
            "ytd-comment-renderer[linked-comment-badge] #reply-button-end :is(button, a)",
            ".badge-shape-wiz--highlighted >> xpath=ancestor::ytd-comment-view-model >> #reply-button-end :is(button, a)",
            # 3. Reply button inside expanded replies container (#replies #expander)
            "#replies #expander ytd-comment-view-model #reply-button-end :is(button, a)",
            "#replies #expander ytd-comment-renderer #reply-button-end :is(button, a)",
            "ytd-comment-replies-renderer #expander #reply-button-end :is(button, a)",
            "ytd-comment-replies-renderer ytd-comment-view-model #reply-button-end :is(button, a)",
            # 4. Fallback: reply button in replies section
            "#replies #reply-button-end :is(button, a)",
            # 5. Last resort fallback
            "#reply-button-end :is(button, a)",
        ]
        if not comment_id:
            selectors.extend(generic)

        deadline = time.monotonic() + max(0.5, timeout_seconds)
        while time.monotonic() < deadline:
            for sel in selectors:
                try:
                    locator = await self._first_visible(page, sel)
                    if locator is not None:
                        logger.debug("Найдена кнопка «Ответить» по селектору: {}", sel)
                        try:
                            await locator.scroll_into_view_if_needed(timeout=1000)
                        except Exception:
                            pass
                        return locator
                except Exception:
                    continue
            await asyncio.sleep(0.4)

        return None

    async def _capture_failure(self, page: Any) -> None:
        try:
            path = self._artifacts_dir / f"youtube_failure_{int(time.time())}.png"
            await page.screenshot(path=str(path), timeout=5000)
            logger.info("Снимок экрана при ошибке сохранен в {}", path)
        except Exception as exc:
            logger.debug("Не удалось сохранить снимок экрана при ошибке: {}", exc)


__all__ = ["YouTubeRunner", "YouTubeCommentElement"]
