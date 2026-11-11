from __future__ import annotations

import datetime
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from curl_cffi.requests import AsyncSession
from loguru import logger

from config import parse_proxy, resolve_account_proxy
from database import AccountRecord
from modules.chrome_session import chrome_login_info_expiry, load_chrome_heartbeat_state
from modules.fingerprint_manager import load_fingerprint

# Camoufox sessions (Firefox): the request must look like Firefox, not like a Chrome replaying the cookies.
HEARTBEAT_IMPERSONATE = "firefox155"  # closest curl_cffi TLS profile to the installed Firefox 156
# Chrome sessions: a Chrome TLS profile (the newest curl_cffi knows, not above the cached UA's major version).
CHROME_IMPERSONATE_FALLBACK = "chrome124"


def curl_proxy_url(proxy: str | None) -> str | None:
    """Proxy URL for libcurl from any form `parse_proxy` accepts; `socks5://` becomes `socks5h://` so DNS is resolved on the proxy.

    libcurl resolves names locally for plain `socks5://` (a DNS leak next to the proxied request). Only for curl:
    Playwright does not know `socks5h`, so the browser path keeps using `parse_proxy` untouched.
    """
    parsed = parse_proxy(proxy)
    if parsed is None:
        return None
    scheme, _, address = parsed["server"].partition("://")
    credentials = ""
    if parsed.get("username"):
        credentials = quote(parsed["username"], safe="")
        if parsed.get("password"):
            credentials += f":{quote(parsed['password'], safe='')}"
        credentials += "@"
    return f"{'socks5h' if scheme == 'socks5' else scheme}://{credentials}{address}"


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
        cur.execute("SELECT name, value FROM moz_cookies WHERE host IN ('.youtube.com', 'youtube.com')")
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
        cur.execute(
            "SELECT expiry FROM moz_cookies WHERE name='LOGIN_INFO' AND host IN ('.youtube.com', 'youtube.com') LIMIT 1"
        )
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


def _no_login_info(session_dir: str | Path, account_name: str) -> HeartbeatStatus:
    """Dead status: the profile directory is gone (path only, no cookie data) or holds no LOGIN_INFO."""
    missing = not Path(session_dir).is_dir()
    return HeartbeatStatus(
        account_name=account_name,
        is_alive=False,
        login_info_present=False,
        details=f"profile directory not found: {session_dir}" if missing else "LOGIN_INFO cookie not found in profile",
    )


def read_chrome_login_info_metadata(session_dir: str | Path) -> tuple[bool, str | None, float | None]:
    """LOGIN_INFO presence and expiry of a Chrome profile, from its saved `heartbeat_state.bin` (the cookie DB is v20-encrypted)."""
    present, expiry = chrome_login_info_expiry(session_dir)
    if not present:
        return False, None, None
    if expiry is None:
        return True, None, None
    dt = datetime.datetime.fromtimestamp(expiry, tz=datetime.timezone.utc)
    return True, dt.strftime("%Y-%m-%d %H:%M:%S UTC"), round(max(0.0, (expiry - time.time()) / 86400.0), 1)


def _login_info_metadata(session_dir: str | Path, engine: str) -> tuple[bool, str | None, float | None]:
    if engine == "chrome":
        return read_chrome_login_info_metadata(session_dir)
    return read_login_info_metadata(session_dir)


def check_session_offline(session_dir: str | Path, account_name: str = "default", engine: str = "camoufox") -> HeartbeatStatus:
    """Routine status from the profile alone, no network: LOGIN_INFO present and not expired.

    A session revoked on Google's side is not seen here; `check_session_heartbeat` asks YouTube.
    """
    has_login_info, expires_at_str, days_left = _login_info_metadata(session_dir, engine)
    if not has_login_info:
        return _no_login_info(session_dir, account_name)
    expired = days_left is not None and days_left <= 0
    return HeartbeatStatus(
        account_name=account_name,
        is_alive=not expired,
        login_info_present=True,
        expires_at=expires_at_str,
        days_remaining=days_left,
        details="LOGIN_INFO expired" if expired else "LOGIN_INFO present (offline check, not verified with YouTube)",
    )


def _camoufox_accept_language(locale: str) -> str:
    """Accept-Language the way Camoufox sends it for `locale=`: en-US -> en-US,en;q=0.9 (read off a real request)."""
    language = locale.split("-")[0]
    return locale if language == locale else f"{locale},{language};q=0.9"


def _installed_firefox_major() -> str | None:
    """Major version of the installed Camoufox build: the browser rewrites the pinned UA to it (fingerprints.py)."""
    try:
        from camoufox.pkgman import installed_verstr

        return installed_verstr().split(".")[0] or None
    except Exception as exc:
        logger.debug("Could not read the installed Camoufox version: {}", exc)
        return None


def fingerprint_headers(session_dir: str | Path, locale: str = "en-US") -> dict[str, str]:
    """User-Agent and Accept-Language the profile's browser really sends, not what fingerprint.json says.

    Camoufox swaps the pinned UA's Firefox version for the installed build and takes the language from
    `locale=`, ignoring the fingerprint's own languages. No fingerprint: the impersonation default UA applies.
    """
    navigator = (load_fingerprint(Path(session_dir)) or {}).get("navigator") or {}
    headers: dict[str, str] = {"Accept-Language": _camoufox_accept_language(locale)}
    user_agent = str(navigator.get("userAgent") or "")
    if user_agent:
        major = _installed_firefox_major()
        if major:
            user_agent = re.sub(r"Firefox/\d+\.0", f"Firefox/{major}.0", user_agent)
            user_agent = re.sub(r"rv:\d+\.0", f"rv:{major}.0", user_agent)
        headers["User-Agent"] = user_agent
    return headers


def chrome_impersonate_profile(user_agent: str | None = None) -> str:
    """curl_cffi TLS profile for a Chrome session: the newest `chromeNNN` not above the UA's major, else the newest known."""
    versions: list[int] = []
    try:
        from curl_cffi.requests import BrowserType

        versions = sorted(int(m.group(1)) for member in BrowserType if (m := re.fullmatch(r"chrome(\d+)", str(member.value))))
    except Exception as exc:
        logger.debug("Could not list curl_cffi browser profiles: {}", exc)
    if not versions:
        return CHROME_IMPERSONATE_FALLBACK
    match = re.search(r"Chrome/(\d+)", user_agent or "")
    if match:
        not_above = [v for v in versions if v <= int(match.group(1))]
        if not_above:
            return f"chrome{not_above[-1]}"
    return f"chrome{versions[-1]}"


def chrome_client_hints(user_agent: str) -> dict[str, str]:
    """`sec-ch-ua*` headers matching a Chrome user agent; empty when the UA is not Chrome's."""
    match = re.search(r"Chrome/(\d+)", user_agent)
    if not match:
        return {}
    major = match.group(1)
    platform = "Windows" if "Windows" in user_agent else "macOS" if "Macintosh" in user_agent else "Linux"
    return {
        "sec-ch-ua": f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not.A/Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": f'"{platform}"',
    }


def chrome_headers(user_agent: str | None, locale: str) -> dict[str, str]:
    """Headers a Chrome page of this account sends to YouTube: the UA cached at its launch, client hints, Origin."""
    headers: dict[str, str] = {"Accept-Language": _camoufox_accept_language(locale), "Origin": "https://www.youtube.com"}
    if user_agent:
        user_agent = user_agent.replace("HeadlessChrome/", "Chrome/")  # a headless run caches the headless UA; HTTP never says so
        headers["User-Agent"] = user_agent
        headers.update(chrome_client_hints(user_agent))
    return headers


async def check_session_heartbeat(
    session_dir: str | Path,
    account_name: str = "default",
    proxy: str | None = None,
    timeout: float = 15.0,
    locale: str = "en-US",
    engine: str = "camoufox",
) -> HeartbeatStatus:
    """Send lightweight HTTP request to YouTube to verify session validity without launching a browser.

    Cookies are only read from the profile (Camoufox: `cookies.sqlite`; Chrome: the `heartbeat_state.bin` the runner
    saved); rotated Set-Cookie values are not written back (the browser owns the profile).
    """
    has_login_info, expires_at_str, days_left = _login_info_metadata(session_dir, engine)
    if not has_login_info:
        return _no_login_info(session_dir, account_name)

    if engine == "chrome":
        cookies, cached_user_agent = load_chrome_heartbeat_state(session_dir)
        impersonate = chrome_impersonate_profile(cached_user_agent)
        headers = chrome_headers(cached_user_agent, locale)
    else:
        cookies = read_youtube_cookies(session_dir)
        impersonate = HEARTBEAT_IMPERSONATE
        headers = fingerprint_headers(session_dir, locale)
    if "LOGIN_INFO" not in cookies:
        return HeartbeatStatus(
            account_name=account_name,
            is_alive=False,
            login_info_present=False,
            details="LOGIN_INFO not present in youtube cookies",
        )

    try:
        async with AsyncSession(
            impersonate=impersonate,
            proxy=curl_proxy_url(proxy),
            timeout=timeout,
            headers=headers,
        ) as session:
            resp = await session.get("https://www.youtube.com/", cookies=cookies)
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
    locale: str = "en-US",
) -> HeartbeatStatus:
    proxy = resolve_account_proxy(account.proxy_url, default_proxy)
    return await check_session_heartbeat(
        session_dir=account.session_path,
        account_name=account.name,
        proxy=proxy,
        timeout=timeout,
        locale=account.locale or locale,
        engine=account.engine,
    )
