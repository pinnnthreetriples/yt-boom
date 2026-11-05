"""Icons of the console interface: colour emoji in Windows Terminal, plain symbols elsewhere.

Only emoji drawn as emoji without a variation selector are used, so every icon is exactly two columns wide in
Windows Terminal and in prompt_toolkit's width math. The plain set keeps one-column symbols for the old console
(conhost), which draws no colour emoji. The choice saved with the «I» key, then `YTBOT_ICONS=emoji|plain`, override
the detection.
"""
from __future__ import annotations

import os
import sys

# name: (emoji, plain symbol)
_ICONS: dict[str, tuple[str, str]] = {
    # tabs
    "tab_launch": ("🚀", "▶"),
    "tab_accounts": ("👥", "◎"),
    "tab_proxies": ("🌐", "◇"),
    "tab_projects": ("📁", "▣"),
    "tab_target": ("🎯", "◉"),
    "tab_replies": ("💬", "✎"),
    # account state
    "working": ("🟢", "●"),
    "waiting": ("🕒", "◷"),
    "paused": ("💤", "‖"),
    "stopped": ("✋", "■"),
    "stale": ("🔑", "!"),
    "no_login": ("🔒", "✕"),
    "busy": ("⏳", "◌"),
    # lifecycle stage and mode
    "quarantine": ("🌱", "▁"),
    "aging": ("🌿", "▃"),
    "probation": ("🪴", "▅"),
    "mature": ("🌳", "▇"),
    "unlimited": ("⚡", "∞"),
    "warmup": ("🔥", "~"),
    "cookie": ("🍪", "o"),
    # proxies
    "own_ip": ("🏠", "⌂"),
    "online": ("🟢", "●"),
    "offline": ("🔴", "✕"),
    "unchecked": ("⚪", "○"),
    "search": ("🔍", "?"),
    "people": ("👤", "×"),
    # launch and journal
    "dry": ("🧪", "◇"),
    "live": ("🚀", "◆"),
    "threads": ("🧵", "≡"),
    "window_on": ("🪟", "▣"),
    "window_off": ("🙈", "▢"),
    "sent": ("✅", "✓"),
    "failed": ("❌", "✕"),
    "videos": ("🎬", "▤"),
    "clock": ("🕒", "◷"),
    "journal": ("📜", "≡"),
    # target and replies
    "topic": ("🔍", "?"),
    "links": ("🔗", "&"),
    "fresh": ("📅", "◷"),
    "views": ("👀", "◉"),
    "reply_to": ("💬", "↳"),
    "limit": ("🔢", "#"),
    "preview": ("📋", "≡"),
    "ai": ("✨", "*"),
    "key": ("🔑", "k"),
    "shuffle": ("🔀", "~"),
    "templates": ("📝", "≡"),
    # account actions and profile
    "profile": ("🪪", "@"),
    "avatar": ("🎨", "◐"),
    "nickname": ("📛", "@"),
    "description": ("📝", "¶"),
    "upload": ("📤", "↑"),
    "edited": ("🔸", "*"),
    "login": ("🔐", "→"),
    "rename": ("🔤", "a"),
    "add": ("➕", "+"),
    "refresh": ("🔄", "↻"),
    "check": ("🔎", "?"),
    "mark": ("📌", "x"),
    "trash": ("🧹", "×"),
    "warn": ("🟡", "!"),
    # projects
    "project": ("📁", "▣"),
    "similar": ("🧭", "≈"),
    "copy": ("🧬", "c"),
    "order": ("⇅", "⇅"),
}


def _console_is_pseudo() -> bool:
    """True when the console belongs to Windows Terminal (or another ConPTY host).

    A window opened by double-clicking start_bot.bat is handed to Windows Terminal without WT_SESSION, but its
    console window is then a hidden «PseudoConsoleWindow»; the old console (conhost) has «ConsoleWindowClass».
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return False
        name = ctypes.create_unicode_buffer(64)
        ctypes.windll.user32.GetClassNameW(hwnd, name, 64)
        return name.value == "PseudoConsoleWindow"
    except Exception:
        return False


def _pick_emoji() -> bool:
    forced = os.environ.get("YTBOT_ICONS", "").strip().lower()
    if forced in {"emoji", "plain"}:
        return forced == "emoji"
    if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
        return True
    if sys.platform != "win32":
        return True  # Linux / macOS terminals draw colour emoji
    return _console_is_pseudo()


EMOJI = _pick_emoji()


def icon(name: str) -> str:
    """The icon for `name` in the current set."""
    emoji, plain = _ICONS[name]
    return emoji if EMOJI else plain


def use_emoji(on: bool) -> None:
    """Switch the icon set (tests and the help screen)."""
    global EMOJI
    EMOJI = on


QUOTA_PLAIN = "○◔◑◕●"


def quota(done: int, limit: int) -> str:
    """A one-glyph pie of today's quota: empty → full."""
    if limit <= 0:
        return "·"
    step = min(4, max(0, round(done / limit * 4)))
    if done > 0 and step == 0:
        step = 1
    return QUOTA_PLAIN[step]
