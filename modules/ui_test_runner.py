from __future__ import annotations

import asyncio
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from camoufox.async_api import AsyncCamoufox
from loguru import logger
from playwright.async_api import Error as PlaywrightError
from pydantic import BaseModel, ConfigDict

from config import BrowserSettings, TargetSettings
from modules.interaction_simulator import InteractionError, InteractionSimulator


class SubmissionError(RuntimeError):
    """Raised when the fixture text cannot be submitted through the target form."""


class NavigationError(RuntimeError):
    """Raised when the target page or its result container cannot be reached."""


class SubmissionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    success: bool
    remote_id: str | None = None
    final_url: str | None = None
    duration_seconds: float
    error: str | None = None


class UITestRunner:
    """Drives one persistent Camoufox session against a configured staging target."""

    def __init__(
        self,
        browser_settings: BrowserSettings,
        target: TargetSettings,
        *,
        simulator: InteractionSimulator | None = None,
        artifacts_dir: Path | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._cfg = browser_settings
        self._target = target
        self._rng = rng or random.Random()
        self._simulator = simulator or InteractionSimulator(rng=self._rng)
        self._session_dir = browser_settings.session_root / target.name
        self._artifacts_dir = artifacts_dir or (browser_settings.session_root / "_artifacts")

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    async def run(
        self,
        text: str,
        *,
        external_id: str | None = None,
        payload_id: int | None = None,
        query: str | None = None,
    ) -> SubmissionResult:
        if not text.strip():
            raise ValueError("submission text must not be empty")

        if self._target.name == "youtube" or "youtube.com" in self._target.base_url:
            from modules.youtube_runner import YouTubeRunner
            yt_runner = YouTubeRunner(
                self._cfg,
                self._target,
                simulator=self._simulator,
                artifacts_dir=self._artifacts_dir,
                rng=self._rng,
            )
            return await yt_runner.run(
                text,
                external_id=external_id,
                payload_id=payload_id,
                query=query,
            )

        self._prepare_dirs()

        started = time.monotonic()
        page: Any = None
        try:
            async with AsyncCamoufox(**self._launch_options()) as context:
                pages = getattr(context, "pages", [])
                page = pages[0] if pages else await context.new_page()
                page.set_default_timeout(self._target.wait_timeout_ms)
                page.set_default_navigation_timeout(self._cfg.navigation_timeout_ms)

                await self._navigate(page, external_id=external_id, payload_id=payload_id, query=query)
                await self._render(page)
                remote_id = await self._submit(page, text)
                result = SubmissionResult(
                    success=True,
                    remote_id=remote_id,
                    final_url=page.url,
                    duration_seconds=time.monotonic() - started,
                )
                logger.info(
                    "Target {} submitted payload in {:.1f}s (remote_id={})",
                    self._target.name,
                    result.duration_seconds,
                    remote_id,
                )
                return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            duration = time.monotonic() - started
            logger.exception("Target {} submission failed after {:.1f}s: {}", self._target.name, duration, exc)
            if page is not None:
                await self._capture_failure(page)
            return SubmissionResult(
                success=False,
                final_url=page.url if page is not None else None,
                duration_seconds=duration,
                error=f"{type(exc).__name__}: {exc}",
            )

    def _prepare_dirs(self) -> None:
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)

    def _launch_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "headless": self._cfg.headless,
            "os": self._cfg.os,
            "geoip": self._cfg.geoip,
            "humanize": self._cfg.humanize,
            "locale": self._cfg.locale,
            "persistent_context": True,
            "user_data_dir": str(self._session_dir),
            "firefox_user_prefs": dict(self._cfg.firefox_user_prefs),
        }
        proxy = self._cfg.playwright_proxy()
        if proxy is not None:
            options["proxy"] = proxy
        return options

    async def _navigate(
        self,
        page: Any,
        *,
        external_id: str | None,
        payload_id: int | None,
        query: str | None,
    ) -> None:
        url = self._target.base_url
        direct = bool(self._target.item_url_template and external_id)
        if direct and self._target.item_url_template:
            url = self._target.item_url_template.format(
                external_id=quote(str(external_id), safe=""),
                payload_id="" if payload_id is None else quote(str(payload_id), safe=""),
            )

        logger.info("Target {}: navigating to {}", self._target.name, url)
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=self._cfg.navigation_timeout_ms)
        except PlaywrightError as exc:
            raise NavigationError(f"navigation to {url} failed: {exc}") from exc
        if response is not None and response.status >= 400:
            raise NavigationError(f"{url} returned HTTP {response.status}")
        await self._simulator.human_pause(1.0, 2.5)

        if not direct and query and self._target.search_input_selector:
            await self._search(page, query)

        if not direct and self._target.result_item_selector:
            try:
                await page.wait_for_selector(self._target.result_item_selector, state="visible")
            except PlaywrightError as exc:
                raise NavigationError(
                    f"result container {self._target.result_item_selector!r} did not appear"
                ) from exc

    async def _search(self, page: Any, query: str) -> None:
        assert self._target.search_input_selector is not None
        assert self._target.search_button_selector is not None
        logger.debug("Target {}: searching for {!r}", self._target.name, query[:60])
        search_input = page.locator(self._target.search_input_selector)
        try:
            await search_input.wait_for(state="visible")
            await self._simulator.click_locator(page, search_input)
            await search_input.fill("")
            await self._simulator.type_text(page, query[:80])
            await self._simulator.human_pause(0.3, 0.9)
            await self._simulator.click_locator(page, page.locator(self._target.search_button_selector))
        except (PlaywrightError, InteractionError) as exc:
            raise NavigationError(f"search flow failed: {exc}") from exc
        await self._simulator.human_pause(1.0, 2.5)

    async def _render(self, page: Any) -> None:
        duration = self._rng.uniform(self._cfg.render_min_seconds, self._cfg.render_max_seconds)
        deadline = time.monotonic() + duration
        logger.info("Target {}: rendering page content for {:.1f}s", self._target.name, duration)
        await self._simulator.idle(page, 2.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            pixels = self._rng.randint(250, 800)
            if self._rng.random() < 0.2:
                pixels = -pixels
            await self._simulator.scroll(page, pixels)
            await self._simulator.human_pause(0.6, 2.2)
            if self._rng.random() < 0.25:
                await self._simulator.idle(page, 1.5)
        await self._simulator.human_pause(0.5, 1.2)

    async def _submit(self, page: Any, text: str) -> str | None:
        if self._target.submit_form_selector:
            form = page.locator(self._target.submit_form_selector)
            try:
                await form.wait_for(state="visible")
            except PlaywrightError as exc:
                raise SubmissionError(f"submit form {self._target.submit_form_selector!r} not found") from exc

        submit_input = page.locator(self._target.submit_input_selector)
        try:
            await submit_input.wait_for(state="visible")
        except PlaywrightError as exc:
            raise SubmissionError(f"submit input {self._target.submit_input_selector!r} not found") from exc

        try:
            await self._simulator.click_locator(page, submit_input)
            await self._simulator.thinking_pause()
            await submit_input.fill("")
            await self._simulator.type_text(page, text)
            typed = (await submit_input.input_value()).strip()
        except (PlaywrightError, InteractionError) as exc:
            raise SubmissionError(f"could not fill the submit input: {exc}") from exc
        if typed != text.strip():
            logger.warning("Target {}: typed text differs from the payload", self._target.name)

        await self._simulator.human_pause(0.4, 1.2)
        try:
            await self._simulator.click_locator(page, page.locator(self._target.submit_button_selector))
        except (PlaywrightError, InteractionError) as exc:
            raise SubmissionError(f"could not click the submit button: {exc}") from exc

        if self._target.confirmation_selector:
            try:
                await page.wait_for_selector(self._target.confirmation_selector, state="visible")
            except PlaywrightError as exc:
                raise SubmissionError(
                    f"confirmation {self._target.confirmation_selector!r} did not appear"
                ) from exc
        elif self._target.remote_id_selector:
            try:
                await page.wait_for_selector(self._target.remote_id_selector, state="visible")
            except PlaywrightError as exc:
                raise SubmissionError(f"remote id {self._target.remote_id_selector!r} did not appear") from exc
        else:
            await self._simulator.human_pause(1.5, 3.0)
            try:
                await page.wait_for_load_state("networkidle")
            except PlaywrightError:
                logger.debug("Target {}: networkidle wait timed out after submit", self._target.name)

        return await self._extract_remote_id(page)

    async def _extract_remote_id(self, page: Any) -> str | None:
        if self._target.remote_id_selector:
            locator = page.locator(self._target.remote_id_selector).first
            try:
                if await locator.count() > 0:
                    value = ((await locator.text_content()) or "").strip()
                    if value:
                        return value
            except PlaywrightError as exc:
                logger.debug("Target {}: remote id selector failed: {}", self._target.name, exc)

        if self._target.remote_id_url_regex:
            match = re.search(self._target.remote_id_url_regex, page.url)
            if match:
                return match.group(1)
        return None

    async def _capture_failure(self, page: Any) -> None:
        try:
            path = self._artifacts_dir / f"{self._target.name}-{int(time.time())}.png"
            await page.screenshot(path=str(path), timeout=5000)
            logger.info("Saved failure screenshot to {}", path)
        except Exception as exc:
            logger.debug("Could not capture failure screenshot: {}", exc)


__all__ = ["NavigationError", "SubmissionError", "SubmissionResult", "UITestRunner"]
