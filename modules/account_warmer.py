from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus

from loguru import logger

from config import BrowserSettings, ProxyUnavailableError, TargetSettings, resolve_account_proxy
from database import AccountRecord, Database
from modules.interaction_simulator import InteractionConfig, InteractionSimulator
from modules.youtube_runner import YouTubeRunner

HOME_URL = "https://www.youtube.com/?hl=en"
HOME_FEED_SELECTOR = "ytd-browse[page-subtype='home'], ytd-browse[browse-id='FEwhat_to_watch']"  # FEwhat_to_watch
VIDEO_CARD_SELECTOR = "ytd-rich-grid-media a#video-title-link"
# YouTube retired /feed/trending in July 2025: a fresh account with an empty home feed falls back to a search
SEARCH_URL = "https://www.youtube.com/results?hl=en&search_query={query}"
SEARCH_RESULTS_SELECTOR = "ytd-search"
SEARCH_CARD_SELECTOR = "ytd-video-renderer a#video-title"
FALLBACK_SEARCH_QUERIES = ("relaxing music", "cooking recipes", "travel vlog", "science explained", "funny animals")
PLAYER_SELECTOR = "#movie_player"
COMMENTS_SELECTOR = "#comments"
MORE_REPLIES_SELECTOR = "#replies #more-replies-sub-thread button, #replies #more-replies button"  # the first is the visible one
LIKE_BUTTON_SELECTOR = "#segmented-like-button button"

THEATER_CHANCE = 0.35
PAUSE_CHANCE = 0.20
SKIP_CHANCE = 0.15
REPLIES_CHANCE = 0.40
LIKE_CHANCE = 0.15


@dataclass(frozen=True)
class WarmUpResult:
    account_name: str
    success: bool
    video_url: str | None
    duration_sec: float
    details: str


class AccountWarmer:
    """Simulates realistic human viewing behavior on YouTube to build account activity history."""

    def __init__(
        self,
        browser_settings: BrowserSettings,
        simulator: InteractionSimulator | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._cfg = browser_settings
        self._rng = rng or random.Random()
        self._simulator = simulator or InteractionSimulator(InteractionConfig(), rng=self._rng)

    async def warm_up(
        self,
        account: AccountRecord,
        duration_seconds: float = 45.0,
        target_video_url: str | None = None,
        db: Database | None = None,
    ) -> WarmUpResult:
        logger.info("Начинается прогрев аккаунта {!r} (длительность ~{:.0f}с)...", account.name, duration_seconds)
        try:
            proxy = resolve_account_proxy(account)
        except ProxyUnavailableError as exc:  # fail closed: no warm-up from the own IP
            logger.error("{}", exc)
            return WarmUpResult(account.name, False, None, 0.0, str(exc))
        target = (
            self._cfg.targets[0]
            if self._cfg.targets
            else TargetSettings(
                name="youtube",
                base_url="https://www.youtube.com",
                item_url_template="https://www.youtube.com/watch?v={external_id}",
                submit_input_selector="#contenteditable-root",
                submit_button_selector="#submit-button button",
                confirmation_selector="ytd-comment-view-model",
            )
        )
        runner = YouTubeRunner(
            self._cfg,
            target,
            session_dir=account.session_path,
            proxy=proxy,
            account_name=account.name,
            simulator=self._simulator,
            locale=account.locale,
            engine=account.engine,
            geo_country=account.geo_country or account.expected_country,
        )
        start_time = time.monotonic()
        chosen_video: str | None = target_video_url
        try:
            await runner.start()
            assert runner.context is not None
            pages = getattr(runner.context, "pages", [])
            page = pages[0] if pages else await runner.context.new_page()

            if chosen_video:
                await page.goto(chosen_video, wait_until="domcontentloaded")
            else:
                chosen_video = await self._open_video_from_feed(page, runner, account)
            await self._simulator.human_pause(2.0, 4.0)
            await runner._dismiss_consent(page)
            logger.info("Аккаунт {!r}: видео для прогрева: {}", account.name, chosen_video)

            watch_seconds = duration_seconds * 0.6
            await self._watch(page, watch_seconds)
            await self._engage_comments(page, account)
            remaining = duration_seconds - (time.monotonic() - start_time)
            if remaining > 0:
                await self._hover_controls(page)
                await asyncio.sleep(remaining)
            await self._maybe_like(page, account)

            total_duration = time.monotonic() - start_time
            logger.success("Прогрев аккаунта {!r} успешно завершён ({:.1f}с)", account.name, total_duration)

            if db is not None:
                try:
                    await db.record_account_warmup_success(account.id)
                except Exception as exc:
                    logger.debug("Не удалось записать прогрев аккаунта в БД: {}", exc)

            return WarmUpResult(
                account_name=account.name,
                success=True,
                video_url=chosen_video,
                duration_sec=total_duration,
                details=f"Watched {chosen_video} for {total_duration:.1f}s with natural interaction",
            )
        except Exception as exc:
            total_duration = time.monotonic() - start_time
            logger.error("Ошибка при прогреве аккаунта {!r}: {}", account.name, exc)
            return WarmUpResult(
                account_name=account.name,
                success=False,
                video_url=chosen_video,
                duration_sec=total_duration,
                details=str(exc),
            )
        finally:
            try:
                await runner.close()
            except Exception as exc:
                logger.debug("Ошибка при закрытии браузера: {}", exc)

    # ------------------------------------------------------------ discovery

    async def _open_video_from_feed(self, page: Any, runner: Any, account: AccountRecord) -> str:
        """Home feed (a neutral search when it is empty) -> scroll -> click one visible video card; returns the watch URL."""
        cards = await self._cards_on(page, runner, account, HOME_URL, HOME_FEED_SELECTOR, VIDEO_CARD_SELECTOR)
        if not cards:
            query = self._rng.choice(FALLBACK_SEARCH_QUERIES)
            logger.info("Аккаунт {!r}: лента главной пуста, ищу видео по запросу {!r}", account.name, query)
            cards = await self._cards_on(
                page, runner, account, SEARCH_URL.format(query=quote_plus(query)), SEARCH_RESULTS_SELECTOR, SEARCH_CARD_SELECTOR
            )
        if not cards:
            raise RuntimeError("no video cards found on the home feed or in the fallback search")

        card = self._rng.choice(cards[:8])
        href = await card.get_attribute("href")
        await self._simulator.click_locator(page, card)
        try:
            await page.wait_for_url("**/watch?v=*", timeout=20000)
        except Exception as exc:
            if not href:
                raise RuntimeError(f"video card click did not open a watch page: {exc}") from exc
            logger.debug("Аккаунт {!r}: клик по карточке не открыл видео ({}), перехожу по ссылке", account.name, exc)
            await page.goto(href if href.startswith("http") else f"https://www.youtube.com{href}", wait_until="domcontentloaded")
        url = page.url
        return url if isinstance(url, str) and "watch?v=" in url else (href or "")

    async def _cards_on(
        self, page: Any, runner: Any, account: AccountRecord, url: str, ready_selector: str, card_selector: str
    ) -> list[Any]:
        """Open `url`, scroll a little and return the visible video cards (empty when the page has none)."""
        await page.goto(url, wait_until="domcontentloaded")
        await self._simulator.human_pause(2.0, 4.0)
        await runner._dismiss_consent(page)
        try:
            await page.wait_for_selector(ready_selector, timeout=15000)
        except Exception as exc:
            logger.debug("Аккаунт {!r}: страница {} не появилась вовремя: {}", account.name, url, exc)

        cards: list[Any] = []
        for _ in range(4):
            await self._simulator.scroll(page, self._rng.randint(300, 700))
            await self._simulator.human_pause(1.0, 2.5)
            cards = await self._visible_cards(page, card_selector)
            if len(cards) >= 3:
                break
        return cards

    @staticmethod
    async def _visible_cards(page: Any, selector: str = VIDEO_CARD_SELECTOR) -> list[Any]:
        cards: list[Any] = []
        for element in await page.query_selector_all(selector):
            try:
                href = await element.get_attribute("href")
                if href and "/watch?v=" in href and await element.is_visible():
                    cards.append(element)
            except Exception:
                continue
        return cards

    # ------------------------------------------------------------ player

    async def _hover_controls(self, page: Any) -> None:
        """Mouse over the player's control bar: real input events keep `lact` fresh in the watchtime beacons."""
        try:
            player = await page.query_selector(PLAYER_SELECTOR)
            box = await player.bounding_box() if player else None
            if box:
                x = box["x"] + box["width"] * self._rng.uniform(0.08, 0.92)
                y = box["y"] + box["height"] - self._rng.uniform(16.0, 44.0)
                await self._simulator.move_mouse(page, x, y)
        except Exception as exc:
            logger.debug("Не удалось навести курсор на плеер: {}", exc)

    async def _press(self, page: Any, key: str) -> None:
        try:
            await page.keyboard.press(key, delay=random.uniform(40, 110))  # a held key, not a 0 ms keydown/keyup
        except Exception as exc:
            logger.debug("Клавиша {} не нажата: {}", key, exc)

    def _plan_player_events(self, seconds: float) -> list[tuple[float, str]]:
        events: list[tuple[float, str]] = []
        if self._rng.random() < THEATER_CHANCE:
            events.append((self._rng.uniform(0.0, min(3.0, seconds * 0.2)), "theater"))
        if self._rng.random() < PAUSE_CHANCE:
            events.append((min(self._rng.uniform(10.0, 25.0), seconds * 0.6), "pause"))
        if self._rng.random() < SKIP_CHANCE:
            events.append((self._rng.uniform(seconds * 0.2, seconds * 0.8), "skip"))
        return sorted(events)

    async def _watch(self, page: Any, seconds: float) -> None:
        """Watch the video for `seconds`: hover the controls bar and fire the planned keyboard events on time."""
        events = self._plan_player_events(seconds)
        started = time.monotonic()
        await self._hover_controls(page)
        while True:
            elapsed = time.monotonic() - started
            remaining = seconds - elapsed
            if remaining <= 0 and not events:
                return
            while events and events[0][0] <= elapsed:
                await self._run_player_event(page, events.pop(0)[1], max(0.0, remaining))
                elapsed = time.monotonic() - started
                remaining = seconds - elapsed
            if remaining <= 0:
                return
            next_event = events[0][0] - elapsed if events else remaining
            await asyncio.sleep(max(0.0, min(remaining, next_event, self._rng.uniform(2.0, 5.0))))
            await self._hover_controls(page)

    async def _run_player_event(self, page: Any, name: str, remaining: float) -> None:
        if name == "theater":
            await self._press(page, "t")
        elif name == "pause":
            await self._press(page, "k")
            await asyncio.sleep(min(self._rng.uniform(2.0, 5.0), remaining))
            await self._press(page, "k")
        elif name == "skip":
            await self._press(page, "ArrowRight")

    # ------------------------------------------------------------ comments and reaction

    async def _engage_comments(self, page: Any, account: AccountRecord) -> None:
        """Wheel-scroll down to the comments; sometimes expand a reply thread and 'read' it."""
        logger.debug("Аккаунт {!r}: плавный скролл к комментариям", account.name)
        for _ in range(12):
            comments = await page.query_selector(COMMENTS_SELECTOR)
            box = await comments.bounding_box() if comments else None
            viewport = getattr(page, "viewport_size", None)
            height = viewport["height"] if isinstance(viewport, dict) and viewport.get("height") else 720
            if box and box["y"] < height * 0.5:
                break
            await self._simulator.scroll(page, self._rng.randint(300, 600))
            await self._simulator.human_pause(0.6, 1.4)
        await self._simulator.human_pause(2.0, 4.0)

        if self._rng.random() < REPLIES_CHANCE:
            try:
                for button in await page.query_selector_all(MORE_REPLIES_SELECTOR):
                    if await button.is_visible():
                        await self._simulator.click_locator(page, button)
                        await self._simulator.human_pause(1.5, 3.0)  # replies load
                        await self._simulator.human_pause(4.0, 7.0)  # reading them
                        break
            except Exception as exc:
                logger.debug("Аккаунт {!r}: не удалось раскрыть ответы: {}", account.name, exc)

    async def _maybe_like(self, page: Any, account: AccountRecord) -> None:
        if self._rng.random() >= LIKE_CHANCE:
            return
        try:
            await self._simulator.scroll(page, -3000)
            await self._simulator.human_pause(1.0, 2.0)
            like = await page.query_selector(LIKE_BUTTON_SELECTOR)
            if like is None or await like.get_attribute("aria-pressed") == "true":
                return
            await self._simulator.click_locator(page, like)
            logger.debug("Аккаунт {!r}: поставлен лайк", account.name)
            await self._simulator.human_pause(1.0, 2.0)
        except Exception as exc:
            logger.debug("Аккаунт {!r}: не удалось поставить лайк: {}", account.name, exc)
