from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

from playwright.async_api import Error as PlaywrightError


async def first_visible(
    page: Any,
    selectors: str | Sequence[str],
    timeout_seconds: float = 0.0,
    limit: int = 8,
    poll_seconds: float = 0.4,
) -> Any | None:
    """First visible match of the selector(s), waiting up to `timeout_seconds`.

    YouTube and Google keep hidden duplicates ahead of the real element, so several matches are checked. Elements
    that vanish while the page navigates (any PlaywrightError) count as "not visible", never as a failure.
    """
    chosen = (selectors,) if isinstance(selectors, str) else tuple(selectors)
    deadline = time.monotonic() + timeout_seconds
    while True:
        for selector in chosen:
            try:
                matches = page.locator(selector)
                for index in range(min(await matches.count(), limit)):
                    candidate = matches.nth(index)
                    if await candidate.is_visible():
                        return candidate
            except PlaywrightError:
                continue
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(poll_seconds)


__all__ = ["first_visible"]
