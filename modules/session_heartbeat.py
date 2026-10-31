from __future__ import annotations

import datetime
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from curl_cffi.requests import AsyncSession
from loguru import logger

from config import resolve_account_proxy
from database import AccountRecord


@dataclass(frozen=True)
class HeartbeatStatus:
    account_name: str
    is_alive: bool
    login_info_present: bool
    expires_at: str | None = None
    days_remaining: float | None = None
    avatar_url: str | None = None
    details: str = ""


def get_cookies_db_path(session_dir: str | Path) -> Path | None:
    p = Path(session_dir) / "cookies.sqlite"
    return p if p.exists() else None


def read_youtube_cookies(session_dir: str | Path) -> dict[str, str]:
    """Read `.youtube.com` scoped cookies from Firefox/Camoufox cookies.sqlite."""
    db_path = get_cookies_db_path(session_dir)
    if db_path is None:
        return {}
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    cookies: dict[str, str] = {}
    try:
        conn = sqlite3.connect(uri, uri=True)
        cur = conn.cursor()
        cur.execute("SELECT name, value FROM moz_cookies WHERE host LIKE '%youtube.com'")
        for name, value in cur.fetchall():
            cookies[name] = value
        conn.close()
    except Exception as exc:
        logger.debug("Failed to read cookies from {}: {}", db_path, exc)
    return cookies


def read_login_info_metadata(session_dir: str | Path) -> tuple[bool, str | None, float | None]:
    """Inspect LOGIN_INFO cookie presence and expiry without HTTP requests."""
    db_path = get_cookies_db_path(session_dir)
    if db_path is None:
        return False, None, None
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        cur = conn.cursor()
        cur.execute("SELECT expiry FROM moz_cookies WHERE name='LOGIN_INFO' AND host LIKE '%youtube.com' LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if not row:
            return False, None, None
        raw_expiry = row[0]
        if not raw_expiry:
            return True, None, None
        expiry_sec = raw_expiry / 1000.0 if raw_expiry > 1e11 else float(raw_expiry)
        dt = datetime.datetime.fromtimestamp(expiry_sec, tz=datetime.timezone.utc)
        now = time.time()
        days_left = max(0.0, (expiry_sec - now) / 86400.0)
        return True, dt.strftime("%Y-%m-%d %H:%M:%S UTC"), round(days_left, 1)
    except Exception as exc:
        logger.debug("Failed to read LOGIN_INFO expiry: {}", exc)
        return False, None, None


async def check_session_heartbeat(
    session_dir: str | Path,
    account_name: str = "default",
    proxy: str | None = None,
    timeout: float = 15.0,
) -> HeartbeatStatus:
    """Send lightweight HTTP request to YouTube to verify session validity without launching a browser."""
    has_login_info, expires_at_str, days_left = read_login_info_metadata(session_dir)
    if not has_login_info:
        return HeartbeatStatus(
            account_name=account_name,
            is_alive=False,
            login_info_present=False,
            details="LOGIN_INFO cookie not found in profile",
        )

    cookies = read_youtube_cookies(session_dir)
    if "LOGIN_INFO" not in cookies:
        return HeartbeatStatus(
            account_name=account_name,
            is_alive=False,
            login_info_present=False,
            details="LOGIN_INFO not present in youtube cookies",
        )

    try:
        async with AsyncSession(
            impersonate="chrome124",
            proxy=proxy,
            timeout=timeout,
            headers={
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            },
        ) as session:
            resp = await session.get("https://www.youtube.com/?hl=en", cookies=cookies)
            if resp.status_code != 200:
                return HeartbeatStatus(
                    account_name=account_name,
                    is_alive=False,
                    login_info_present=True,
                    expires_at=expires_at_str,
                    days_remaining=days_left,
                    details=f"HTTP status {resp.status_code}",
                )

            is_logged_in = bool(re.search(r'"LOGGED_IN"\s*:\s*true', resp.text))
            avatar_match = re.search(r'"avatar":\{"thumbnails":\[\{"url":"([^"]+)"', resp.text)
            avatar_url = avatar_match.group(1) if avatar_match else None

            if is_logged_in:
                return HeartbeatStatus(
                    account_name=account_name,
                    is_alive=True,
                    login_info_present=True,
                    expires_at=expires_at_str,
                    days_remaining=days_left,
                    avatar_url=avatar_url,
                    details="Session active (LOGGED_IN: true)",
                )
            else:
                return HeartbeatStatus(
                    account_name=account_name,
                    is_alive=False,
                    login_info_present=True,
                    expires_at=expires_at_str,
                    days_remaining=days_left,
                    details="YouTube returned LOGGED_IN: false (session expired or revoked)",
                )
    except Exception as exc:
        logger.warning("Heartbeat check failed for {!r}: {}", account_name, exc)
        return HeartbeatStatus(
            account_name=account_name,
            is_alive=False,
            login_info_present=True,
            expires_at=expires_at_str,
            days_remaining=days_left,
            details=f"Network/Connection error: {exc}",
        )


async def check_account_heartbeat(
    account: AccountRecord,
    default_proxy: str | None = None,
    timeout: float = 15.0,
) -> HeartbeatStatus:
    proxy = resolve_account_proxy(account.proxy_url, default_proxy)
    return await check_session_heartbeat(
        session_dir=account.session_path,
        account_name=account.name,
        proxy=proxy,
        timeout=timeout,
    )
