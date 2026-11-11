"""Browser-engine markers for account rows: emoji where the console can print them, `[Fx]` / `[Cr]` otherwise."""

from __future__ import annotations

import os
import sys

ENGINE_CAMOUFOX = "🦊"
ENGINE_CHROME = "🌐"
ENGINE_CAMOUFOX_PLAIN = "[Fx]"
ENGINE_CHROME_PLAIN = "[Cr]"

ENGINE_TITLES = {"camoufox": "Camoufox", "chrome": "Google Chrome"}
_EMOJI_CELLS = 2  # an emoji takes two console cells


def plain_mode(plain: bool | None = None) -> bool:
    """True when emoji must not be printed: forced by `YT_PLAIN_UI=1`, or the console encoding cannot hold them."""
    if plain is not None:
        return plain
    if os.environ.get("YT_PLAIN_UI", "").strip().lower() in {"1", "true", "yes"}:
        return True
    try:
        ENGINE_CAMOUFOX.encode(getattr(sys.stdout, "encoding", None) or "ascii")
    except (UnicodeEncodeError, LookupError):
        return True
    return False


def engine_icon(engine: str, plain: bool | None = None) -> str:
    chrome = engine == "chrome"
    if plain_mode(plain):
        return ENGINE_CHROME_PLAIN if chrome else ENGINE_CAMOUFOX_PLAIN
    return ENGINE_CHROME if chrome else ENGINE_CAMOUFOX


def icon_cells(plain: bool | None = None) -> int:
    """Console width of one engine icon."""
    return len(ENGINE_CAMOUFOX_PLAIN) if plain_mode(plain) else _EMOJI_CELLS


def engine_title(engine: str) -> str:
    return ENGINE_TITLES.get(engine, ENGINE_TITLES["camoufox"])


def other_engine(engine: str) -> str:
    return "camoufox" if engine == "chrome" else "chrome"


__all__ = [
    "ENGINE_CAMOUFOX",
    "ENGINE_CHROME",
    "engine_icon",
    "engine_title",
    "icon_cells",
    "other_engine",
    "plain_mode",
]
