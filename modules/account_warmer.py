from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from config import BrowserSettings, TargetSettings, resolve_account_proxy
from database import AccountRecord, Database
from modules.interaction_simulator import InteractionConfig, InteractionSimulator
from modules.youtube_runner import YouTubeRunner

FALLBACK_WARM_VIDEOS: tuple[str, ...] = (
    "https://www.youtube.com/watch?v=jfKfPfyJRdk",  # lofi hip hop radio
    "https://www.youtube.com/watch?v=7NOSDKb0HlU",  # 4k nature relaxation
    "https://www.youtube.com/watch?v=21X5lGlDOfg",  # NASA Earth from space
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",  # classic music
)


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
        self._simulator = simulator or InteractionSimulator(InteractionConfig())
        self._rng = rng or random.Random()

    async def warm_up(
        self,
        account: AccountRecord,
        duration_seconds: float = 45.0,
        target_video_url: str | None = None,
        db: Database | None = None,
    ) -> WarmUpResult:
        logger.info("Начинается прогрев аккаунта {!r} (длительность ~{:.0f}с)...", account.name, duration_seconds)
        proxy = resolve_account_proxy(account.proxy_url, self._cfg.proxy)
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
        )
        start_time = time.monotonic()
        chosen_video: str | None = target_video_url
        try:
            await runner.start()
            assert runner.context is not None
            pages = getattr(runner.context, "pages", [])
            page = pages[0] if pages else await runner.context.new_page()

            # 1. Open home page if no target video was explicitly passed
            if not chosen_video:
                await page.goto("https://www.youtube.com/?hl=en", wait_until="domcontentloaded")
                await self._simulator.human_pause(2.0, 4.0)
                await runner._dismiss_consent(page)

                # Try finding a video on the homepage
                try:
                    video_links = await page.eval_on_selector_all(
                        "ytd-rich-grid-media a#video-title-link, ytd-video-renderer a#video-title, a[href*='/watch?v=']",
                        "els => els.map(e => e.href).filter(h => h && h.includes('watch?v=')).slice(0, 5)",
                    )
                    if video_links:
                        chosen_video = self._rng.choice(video_links)
                except Exception as exc:
                    logger.debug("Не удалось извлечь видео с главной страницы: {}", exc)

            if not chosen_video:
                chosen_video = self._rng.choice(FALLBACK_WARM_VIDEOS)

            logger.info("Аккаунт {!r}: открытие видео для прогрева: {}", account.name, chosen_video)
            await page.goto(chosen_video, wait_until="domcontentloaded")
            await self._simulator.human_pause(2.0, 4.0)
            await runner._dismiss_consent(page)

            # 2. Watch video with human pauses and smooth mouse moves
            watch_start = time.monotonic()
            halfway = duration_seconds / 2.0

            # Move mouse toward player
            player = await page.query_selector("#movie_player")
            if player:
                try:
                    box = await player.bounding_box()
                    if box:
                        target_x = box["x"] + box["width"] * self._rng.uniform(0.3, 0.7)
                        target_y = box["y"] + box["height"] * self._rng.uniform(0.4, 0.6)
                        await self._simulator.move_mouse(page, target_x, target_y)
                except Exception:
                    pass

            elapsed = time.monotonic() - watch_start
            remaining_first_half = max(0.0, halfway - elapsed)
            if remaining_first_half > 0:
                await asyncio.sleep(remaining_first_half)

            # 3. Smoothly scroll towards comments section
            logger.debug("Аккаунт {!r}: плавный скролл к комментариям", account.name)
            await self._simulator.scroll(page, self._rng.randint(600, 1000))
            await self._simulator.human_pause(2.0, 4.0)

            # Second small scroll down
            await self._simulator.scroll(page, self._rng.randint(300, 500))

            elapsed = time.monotonic() - watch_start
            remaining = max(0.0, duration_seconds - elapsed)
            if remaining > 0:
                await asyncio.sleep(remaining)

            # Scroll back up a bit
            await self._simulator.scroll(page, -self._rng.randint(400, 700))
            await self._simulator.human_pause(1.0, 2.0)

            total_duration = time.monotonic() - start_time
            logger.success("Прогрев аккаунта {!r} успешно завершён ({:.1f}с)", account.name, total_duration)

            if db is not None:
                try:
                    await db.increment_account_stats(account.id, replies=0)
                except Exception as exc:
                    logger.debug("Не удалось обновить статистику аккаунта в БД: {}", exc)

            return WarmUpResult(
                account_name=account.name,
                success=True,
                video_url=chosen_video,
                duration_sec=total_duration,
                details=f"Watched {chosen_video} for {total_duration:.1f}s with natural scrolling",
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
