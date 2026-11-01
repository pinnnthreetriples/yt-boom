from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any

from playwright.async_api import Error as PlaywrightError

# Structural handles only: the bot runs accounts in any UI language, and visible text, `aria-label` and `title` are
# localized (verified in en/ru/pl, see .mex/patterns/youtube-reply-flow.md). A text selector is never acceptable here.
SIGN_IN_LINK = 'ytd-masthead #buttons a[href*="ServiceLogin"]'
# consent.youtube.com: two forms that post to /save, told apart by the hidden `set_eom` flag
# (essential-only mode: true = "reject all", false = "accept all"). Taken from the page markup, not reproducible
# from a non-EU address; a miss only means the banner stays and the later steps report it.
CONSENT_REJECT_SELECTORS = ('form[action*="consent"]:has(input[name="set_eom"][value="true"]) button',)
CONSENT_ACCEPT_SELECTORS = ('form[action*="consent"]:has(input[name="set_eom"][value="false"]) button',)


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


__all__ = ["CONSENT_ACCEPT_SELECTORS", "CONSENT_REJECT_SELECTORS", "SIGN_IN_LINK", "first_visible"]
