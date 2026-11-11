"""The browser error classes every `except PlaywrightError` must catch.

`patchright` ships its own copy of the Playwright client: its `Error` is a different class from `playwright`'s, so a
Chrome (patchright) failure would slip past an `except playwright.async_api.Error`. `PlaywrightError` is a tuple of the
classes (both when patchright is installed): use it as `except PlaywrightError` or, next to other classes, unpacked as
`except (InteractionError, *PlaywrightError)`. Python does not accept a nested tuple in `except`.
"""

from __future__ import annotations

from playwright.async_api import Error as _PlaywrightError

try:
    from patchright.async_api import Error as _PatchrightError
except ImportError:  # Chrome engine not installed: Camoufox only
    PlaywrightError: tuple[type[BaseException], ...] = (_PlaywrightError,)
else:
    PlaywrightError = (_PlaywrightError, _PatchrightError)

__all__ = ["PlaywrightError"]
