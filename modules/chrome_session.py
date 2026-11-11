"""Session state of a Chrome profile for the offline heartbeat.

Chrome 127+ on Windows encrypts cookies with App-Bound Encryption (`v20`): the profile's own `Cookies` database cannot be
read by this process. The cookies the browser holds are therefore exported through the automation context
(`context.cookies()`) into `heartbeat_state.bin` next to the profile, encrypted with Windows DPAPI.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from secret_store import protect_secret, reveal_secret

HEARTBEAT_STATE_FILE = "heartbeat_state.bin"
_DOMAIN_MARKERS = ("youtube.", "google.")


def heartbeat_state_path(profile_dir: Path | str) -> Path:
    return Path(profile_dir) / HEARTBEAT_STATE_FILE


def chrome_major_from_user_agent(user_agent: str | None) -> int | None:
    """Major Chrome version of a user agent (`Chrome/134.0.0.0`, also `HeadlessChrome/...`); None when it is not Chrome's."""
    match = re.search(r"Chrome/(\d+)", user_agent or "")
    return int(match.group(1)) if match else None


def chrome_platform_from_user_agent(user_agent: str | None) -> str:
    """`sec-ch-ua-platform` value (without quotes) a Chrome user agent implies."""
    text = user_agent or ""
    return "Windows" if "Windows" in text else "macOS" if "Macintosh" in text else "Linux"


def _normalize_client_hints(raw: Any) -> dict[str, Any] | None:
    """Validated copy of low-entropy `navigator.userAgentData` values; None when `raw` is not usable."""
    if not isinstance(raw, dict):
        return None
    brands: list[dict[str, str]] = []
    for item in raw.get("brands") or []:
        if isinstance(item, dict) and item.get("brand") and item.get("version") not in (None, ""):
            brands.append({"brand": str(item["brand"]), "version": str(item["version"])})
    if not brands:
        return None
    return {"brands": brands, "mobile": bool(raw.get("mobile", False)), "platform": str(raw.get("platform") or "")}


def _hints_major(hints: dict[str, Any], user_agent: str | None) -> int | None:
    """Major version a stored hints record stands for: its Chrome/Chromium brand, else the user agent's."""
    for item in hints.get("brands") or []:
        if str(item.get("brand")) in ("Google Chrome", "Chromium", "HeadlessChrome") and str(item.get("version", "")).isdigit():
            return int(item["version"])
    stored = hints.get("major")
    return stored if isinstance(stored, int) else chrome_major_from_user_agent(user_agent)


def _build_client_hints(
    user_agent: str | None, client_hints: Any, previous: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Hints record to store: the browser's own values; else the earlier ones while still matching the UA; else the UA's major.

    A new user agent without hints (the browser updated) must not keep hints of the old version: the two would disagree.
    """
    real = _normalize_client_hints(client_hints)
    major = chrome_major_from_user_agent(user_agent)
    if real is not None:
        real["major"] = _hints_major(real, user_agent)
        real["source"] = "browser"
        return real
    if isinstance(previous, dict) and previous.get("brands") and previous.get("source") == "browser":
        kept = _normalize_client_hints(previous)
        if kept is not None:
            kept_major = _hints_major(kept, None)
            if major is None or kept_major == major:
                kept.update(major=kept_major, source="browser")
                return kept
    if major is None:
        return None
    return {"brands": [], "mobile": False, "platform": chrome_platform_from_user_agent(user_agent), "major": major, "source": "user_agent"}


def _is_session_cookie(cookie: dict[str, Any]) -> bool:
    domain = str(cookie.get("domain") or "").lower()
    return any(marker in domain for marker in _DOMAIN_MARKERS)


def load_chrome_heartbeat_payload(profile_dir: Path | str) -> dict[str, Any] | None:
    """Decrypted content of `heartbeat_state.bin`; None when there is no file or it cannot be read."""
    path = heartbeat_state_path(profile_dir)
    try:
        if not path.is_file():
            return None
        payload = json.loads(reveal_secret(path.read_bytes().decode("utf-8")) or "")
    except Exception as exc:  # another Windows user, a damaged file: the session simply counts as not saved
        logger.debug("Не удалось прочитать {}: {}", path, type(exc).__name__)
        return None
    return payload if isinstance(payload, dict) else None


def load_chrome_heartbeat_state(profile_dir: Path | str) -> tuple[dict[str, str], str | None]:
    """`({cookie name: value}, user agent)` saved by the last Chrome session; `({}, None)` when nothing is saved.

    When a name exists on both Google and YouTube domains, the YouTube cookie wins (the heartbeat asks YouTube).
    """
    payload = load_chrome_heartbeat_payload(profile_dir)
    if payload is None:
        return {}, None
    cookies = [c for c in payload.get("cookies") or [] if isinstance(c, dict) and c.get("name")]
    cookies.sort(key=lambda c: "youtube." in str(c.get("domain") or "").lower())  # stable: youtube.* last, so it overrides
    values = {str(c["name"]): str(c.get("value", "")) for c in cookies}
    user_agent = payload.get("user_agent")
    return values, (str(user_agent) if user_agent else None)


def load_chrome_client_hints(profile_dir: Path | str) -> dict[str, Any] | None:
    """Client hints saved with the last Chrome session, None when nothing is saved (or an older file has none).

    `{"brands": [{"brand", "version"}, ...], "mobile": bool, "platform": str, "major": int | None, "source": str}`;
    `source` is `browser` for the values read off `navigator.userAgentData`, `user_agent` when only the UA's major
    version was known (`brands` is then empty).
    """
    payload = load_chrome_heartbeat_payload(profile_dir)
    hints = payload.get("client_hints") if payload else None
    if not isinstance(hints, dict):
        return None
    brands = _normalize_client_hints(hints)
    major = hints.get("major")
    return {
        "brands": brands["brands"] if brands else [],
        "mobile": bool(hints.get("mobile", False)),
        "platform": str(hints.get("platform") or ""),
        "major": major if isinstance(major, int) else None,
        "source": "browser" if brands else "user_agent",
    }


def chrome_login_info_expiry(profile_dir: Path | str) -> tuple[bool, float | None]:
    """`(LOGIN_INFO present, its expiry as a Unix time or None for a session cookie)` from the saved state."""
    payload = load_chrome_heartbeat_payload(profile_dir)
    if payload is None:
        return False, None
    for cookie in payload.get("cookies") or []:
        if isinstance(cookie, dict) and cookie.get("name") == "LOGIN_INFO" and "youtube." in str(cookie.get("domain") or "").lower():
            if not cookie.get("value"):
                continue
            expires = cookie.get("expires")
            return True, float(expires) if isinstance(expires, (int, float)) and expires > 0 else None
    return False, None


def _write_state(path: Path, payload: dict[str, Any]) -> None:
    encrypted = protect_secret(json.dumps(payload, ensure_ascii=False))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes((encrypted or "").encode("utf-8"))
    os.replace(tmp, path)


async def save_chrome_heartbeat_state(
    profile_dir: Path, context: Any, user_agent: str | None, client_hints: dict[str, Any] | None = None
) -> bool:
    """Export the Google and YouTube cookies of `context` into `heartbeat_state.bin` (DPAPI, atomic). True when saved.

    `client_hints` are the browser's real low-entropy `navigator.userAgentData` values
    (`{"brands": [{"brand", "version"}, ...], "mobile": bool, "platform": str}`); without them the UA's major version is
    stored. Nothing is written (the previous state stays) when the context holds no such cookie. A missing `user_agent`
    keeps the one saved earlier, and missing hints keep the earlier real ones while they still match the user agent.
    """
    cookies = await context.cookies()
    filtered = [c for c in cookies if _is_session_cookie(c)]
    if not filtered:
        return False
    profile_dir = Path(profile_dir)
    previous = load_chrome_heartbeat_payload(profile_dir) or {}
    if not user_agent:
        user_agent = previous.get("user_agent")
    payload = {
        "cookies": [
            {"name": c["name"], "value": c["value"], "domain": c.get("domain"), "expires": c.get("expires")}
            for c in filtered
        ],
        "user_agent": user_agent or "",
        "client_hints": _build_client_hints(user_agent, client_hints, previous.get("client_hints")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    profile_dir.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_write_state, heartbeat_state_path(profile_dir), payload)
    return True


__all__ = [
    "HEARTBEAT_STATE_FILE",
    "chrome_login_info_expiry",
    "chrome_major_from_user_agent",
    "chrome_platform_from_user_agent",
    "heartbeat_state_path",
    "load_chrome_client_hints",
    "load_chrome_heartbeat_payload",
    "load_chrome_heartbeat_state",
    "save_chrome_heartbeat_state",
]
