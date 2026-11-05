"""Cookie robot: walks a fresh account's own browser profile through a few trusted sites BEFORE the Google sign-in.

A brand-new profile has no cookies and no history, which is what makes Google ask for a captcha at sign-in. The robot
opens 4-5 randomly chosen sites (google.com with an everyday search always among the first two), stays a few seconds on
each, scrolls like a person so the analytics / ad scripts run, and accepts a cookie banner when one shows up. The same
Camoufox profile is used as for the sign-in: the saved fingerprint and the account's own proxy.
"""
from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

from loguru import logger

from config import BrowserSettings, ProxyUnavailableError, TargetSettings, resolve_account_proxy
from database import AccountRecord
from modules.interaction_simulator import InteractionConfig, InteractionSimulator
from modules.page_helpers import CMP_ACCEPT_SELECTORS, CONSENT_ACCEPT_SELECTORS, first_visible
from modules.youtube_runner import YouTubeRunner

ProgressCallback = Callable[[int, int, str], None]  # (current step, total steps, site name)


@dataclass(frozen=True)
class Site:
    name: str
    url: str
    query: str | None = None  # an everyday search typed on the page (google.com only)


GOOGLE_URL = "https://www.google.com/?hl=en"
GOOGLE_SEARCH_URL = "https://www.google.com/search?hl=en&q={query}"
GOOGLE_QUERIES = ("weather today", "world news", "news today", "what time is it")
GOOGLE_SEARCH_BOX = ('textarea[name="q"]', 'input[name="q"]')  # structural: the page is localized

SITE_POOL: tuple[Site, ...] = (
    Site("wikipedia.org", "https://www.wikipedia.org/"),
    Site("reddit.com", "https://www.reddit.com/"),
    Site("weather.com", "https://weather.com/"),
    Site("cnn.com", "https://edition.cnn.com/"),
    Site("yahoo.com", "https://www.yahoo.com/"),
    Site("bbc.com", "https://www.bbc.com/"),
    Site("amazon.com", "https://www.amazon.com/"),
    Site("nytimes.com", "https://www.nytimes.com/"),
    Site("theguardian.com", "https://www.theguardian.com/international"),
    Site("imdb.com", "https://www.imdb.com/"),
    Site("espn.com", "https://www.espn.com/"),
    Site("nationalgeographic.com", "https://www.nationalgeographic.com/"),
    Site("apple.com", "https://www.apple.com/"),
    Site("microsoft.com", "https://www.microsoft.com/en-us/"),
)

MIN_SITES, MAX_SITES = 4, 5  # google.com included
DWELL_MIN, DWELL_MAX = 5.0, 8.0  # seconds on a page after it loaded
SCROLLS_MIN, SCROLLS_MAX = 1, 2
SCROLL_MIN_PX, SCROLL_MAX_PX = 300, 700
NAV_TIMEOUT_MIN, NAV_TIMEOUT_MAX = 10.0, 12.0  # seconds to load one page
SITE_MARGIN = 20.0  # on top of load + dwell: banner, typing the search, scrolling
CONSENT_WAIT = 1.5  # seconds to wait for a cookie banner to show up


@dataclass(frozen=True)
class CookieRobotResult:
    account_name: str
    success: bool  # at least one site was visited
    visited: tuple[str, ...]
    failed: tuple[str, ...]
    duration_sec: float
    details: str


def plan_sites(rng: random.Random, count: int | None = None) -> list[Site]:
    """The sites of one run: 4-5 random ones in random order, google.com (with a search) always first or second."""
    total = count if count is not None else rng.randint(MIN_SITES, MAX_SITES)
    total = max(2, min(total, len(SITE_POOL) + 1))
    sites = rng.sample(SITE_POOL, total - 1)
    rng.shuffle(sites)
    sites.insert(rng.randint(0, 1), Site("google.com", GOOGLE_URL, query=rng.choice(GOOGLE_QUERIES)))
    return sites


def format_progress_bar(current: int, total: int, label: str, width: int = 10) -> str:
    """`[=====>    ] 3/5 wikipedia.org`: the bar is `width` cells, its head `>` stands on the current step."""
    total = max(total, 1)
    current = max(0, min(current, total))
    filled = round(width * current / total)
    bar = ("=" * (filled - 1) + ">" if filled else "") + " " * (width - filled)
    return f"[{bar}] {current}/{total} {label}".rstrip()


class CookieRobot:
    """Visits a few trusted sites in an account's persistent profile to give it cookies and a browsing trace."""

    def __init__(
        self,
        browser_settings: BrowserSettings,
        simulator: InteractionSimulator | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._cfg = browser_settings
        self._rng = rng or random.Random()
        self._simulator = simulator or InteractionSimulator(InteractionConfig(), rng=self._rng)
        self._sleep = sleep

    async def run(self, account: AccountRecord, on_progress: ProgressCallback | None = None) -> CookieRobotResult:
        started = time.monotonic()
        sites = plan_sites(self._rng)
        visited: list[str] = []
        failed: list[str] = []
        logger.info("Куки-робот: аккаунт {!r}, сайтов {}: {}", account.name, len(sites), ", ".join(s.name for s in sites))
        # the account's own proxy and its saved fingerprint (YouTubeRunner.start pins it through get_or_create_fingerprint):
        # the profile must look the same as at the sign-in
        try:
            proxy = resolve_account_proxy(account)
        except ProxyUnavailableError as exc:  # fail closed: the robot never goes out from the own IP
            logger.error("{}", exc)
            return CookieRobotResult(account.name, False, (), (), 0.0, str(exc))
        runner = YouTubeRunner(
            self._cfg,
            self._target(),
            session_dir=account.session_path,
            proxy=proxy,
            account_name=account.name,
            simulator=self._simulator,
            locale=account.locale,
        )
        error: str | None = None
        try:
            await runner.start()
            assert runner.context is not None
            pages = getattr(runner.context, "pages", [])
            page = pages[0] if pages else await runner.context.new_page()
            for step, site in enumerate(sites, 1):
                self._report(on_progress, step, len(sites), site.name)
                try:
                    await self._visit(page, site)
                    visited.append(site.name)
                except Exception as exc:  # one blocked / slow site never stops the walk
                    failed.append(site.name)
                    logger.warning("Куки-робот: {} не открылся у {!r}: {}", site.name, account.name, exc or exc.__class__.__name__)
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            logger.error("Куки-робот: аккаунт {!r}: {}", account.name, error)
        finally:
            # closing the browser flushes the cookies to the profile and frees its parent.lock
            try:
                await runner.close()
            except Exception as exc:
                logger.debug("Куки-робот: ошибка при закрытии браузера: {}", exc)

        duration = time.monotonic() - started
        if error is not None and not visited:
            details = error
        else:
            details = f"{len(visited)} из {len(sites)} сайтов" + (f" (не открылись: {', '.join(failed)})" if failed else "")
        success = bool(visited)
        (logger.success if success else logger.error)("Куки-робот: аккаунт {!r}: {} ({:.0f}с)", account.name, details, duration)
        return CookieRobotResult(account.name, success, tuple(visited), tuple(failed), duration, details)

    def _target(self) -> TargetSettings:
        if self._cfg.targets:
            return self._cfg.targets[0]
        return TargetSettings(
            name="youtube",
            base_url="https://www.youtube.com",
            item_url_template="https://www.youtube.com/watch?v={external_id}",
            submit_input_selector="#contenteditable-root",
            submit_button_selector="#submit-button button",
            confirmation_selector="ytd-comment-view-model",
        )

    @staticmethod
    def _report(callback: ProgressCallback | None, step: int, total: int, name: str) -> None:
        if callback is None:
            return
        try:
            callback(step, total, name)
        except Exception as exc:  # a broken progress bar must not break the run
            logger.debug("Куки-робот: ошибка в отчёте о ходе работы: {}", exc)

    async def _visit(self, page: Any, site: Site) -> None:
        nav_timeout = self._rng.uniform(NAV_TIMEOUT_MIN, NAV_TIMEOUT_MAX)
        dwell = self._rng.uniform(DWELL_MIN, DWELL_MAX)
        await asyncio.wait_for(self._browse(page, site, nav_timeout, dwell), timeout=nav_timeout + dwell + SITE_MARGIN)

    async def _browse(self, page: Any, site: Site, nav_timeout: float, dwell: float) -> None:
        await page.goto(site.url, wait_until="domcontentloaded", timeout=nav_timeout * 1000)
        await self._dismiss_consent(page)
        if site.query:
            await self._search(page, site.query, nav_timeout)
        # a few seconds on the page with 1-2 smooth scrolls: gtag.js / AdSense run on real scroll input
        scrolls = self._rng.randint(SCROLLS_MIN, SCROLLS_MAX)
        share = dwell / (scrolls + 1)
        for _ in range(scrolls):
            await self._sleep(share)
            await self._simulator.scroll(page, self._rng.randint(SCROLL_MIN_PX, SCROLL_MAX_PX))
        await self._sleep(share)

    async def _dismiss_consent(self, page: Any) -> None:
        """Accept the cookie banner when one is shown (a banner is a cookie source, not an obstacle)."""
        try:
            button = await first_visible(page, CONSENT_ACCEPT_SELECTORS + CMP_ACCEPT_SELECTORS, timeout_seconds=CONSENT_WAIT)
            if button is None:
                return
            await self._simulator.click_locator(page, button)
            await self._simulator.human_pause(0.8, 1.6)
        except Exception as exc:
            logger.debug("Куки-робот: баннер согласия не закрылся: {}", exc)

    async def _search(self, page: Any, query: str, nav_timeout: float) -> None:
        """Type an everyday query into the google.com search box (native NID / AEC cookies); the URL is the fallback."""
        typed = False
        box = await first_visible(page, GOOGLE_SEARCH_BOX, timeout_seconds=3.0)
        if box is not None:
            try:
                await self._simulator.click_locator(page, box)
                await self._simulator.type_text(page, query, allow_typos=False)
                await page.keyboard.press("Enter")
                typed = True
            except Exception as exc:
                logger.debug("Куки-робот: поисковую строку не удалось заполнить, открываю поиск по ссылке: {}", exc)
        if typed:
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=nav_timeout * 1000)
            except Exception as exc:
                logger.debug("Куки-робот: результаты поиска не загрузились вовремя: {}", exc)
        else:
            await page.goto(GOOGLE_SEARCH_URL.format(query=quote_plus(query)), wait_until="domcontentloaded", timeout=nav_timeout * 1000)
        await self._dismiss_consent(page)


__all__ = [
    "CookieRobot",
    "CookieRobotResult",
    "ProgressCallback",
    "SITE_POOL",
    "Site",
    "format_progress_bar",
    "plan_sites",
]
