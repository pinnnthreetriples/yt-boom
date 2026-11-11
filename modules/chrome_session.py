"""Session state of a Chrome profile for the offline heartbeat.

Chrome 127+ on Windows encrypts cookies with App-Bound Encryption (`v20`): the profile's own `Cookies` database cannot be
read by this process. The cookies the browser holds are therefore exported through the automation context
(`context.cookies()`) into `heartbeat_state.bin` next to the profile, encrypted with Windows DPAPI.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from secret_store import protect_secret, reveal_secret

HEARTBEAT_STATE_FILE = "heartbeat_state.bin"
_DOMAIN_MARKERS = ("youtube.", "google.")


def heartbeat_state_path(profile_dir: Path | str) -> Path:
    return Path(profile_dir) / HEARTBEAT_STATE_FILE


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


async def save_chrome_heartbeat_state(profile_dir: Path, context: Any, user_agent: str | None) -> bool:
    """Export the Google and YouTube cookies of `context` into `heartbeat_state.bin` (DPAPI, atomic). True when saved.

    Nothing is written (the previous state stays) when the context holds no such cookie. A missing `user_agent`
    keeps the one saved earlier.
    """
    cookies = await context.cookies()
    filtered = [c for c in cookies if _is_session_cookie(c)]
    if not filtered:
        return False
    profile_dir = Path(profile_dir)
    if not user_agent:
        user_agent = (load_chrome_heartbeat_payload(profile_dir) or {}).get("user_agent")
    payload = {
        "cookies": [
            {"name": c["name"], "value": c["value"], "domain": c.get("domain"), "expires": c.get("expires")}
            for c in filtered
        ],
        "user_agent": user_agent or "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    profile_dir.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_write_state, heartbeat_state_path(profile_dir), payload)
    return True


__all__ = [
    "HEARTBEAT_STATE_FILE",
    "chrome_login_info_expiry",
    "heartbeat_state_path",
    "load_chrome_heartbeat_payload",
    "load_chrome_heartbeat_state",
    "save_chrome_heartbeat_state",
]
