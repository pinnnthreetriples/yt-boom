"""The observer account's own point of view on YouTube: its proxy, its cookies, signed requests.

The observer is a separate Google account that never posts. To see what a signed-in viewer sees under a video, a
curl_cffi session goes through the observer's own proxy with the cookies of its browser profile and the
`SAPISIDHASH` header (without it InnerTube answers a cookie request as an anonymous one).

Cookies come from the profile of either browser engine, read-only and never written back:
- Camoufox: `cookies.sqlite` (copied first, the browser may hold it locked);
- Chrome: the DPAPI `heartbeat_state.bin` the runner exports after every session (`modules/chrome_session.py`),
  because Chrome's own `Cookies` database is app-bound encrypted and cannot be read from this process.
"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

from curl_cffi.requests import AsyncSession
from loguru import logger

from config import CollectorSettings, ProxyUnavailableError, resolve_account_proxy
from modules.chrome_session import load_chrome_client_hints, load_chrome_heartbeat_payload
from modules.geo_guard import NetworkGuardError, ensure_network_ready
from modules.session_heartbeat import (
    HEARTBEAT_IMPERSONATE,
    chrome_headers,
    chrome_impersonate_profile,
    curl_proxy_url,
    fingerprint_headers,
)

# Only these sites' cookies are ever read from the observer's profile.
COOKIE_DOMAINS = ("youtube.com", "google.com")
_COOKIE_FILES = ("cookies.sqlite", "cookies.sqlite-wal", "cookies.sqlite-shm")

YOUTUBE_ORIGIN = "https://www.youtube.com"
# SAPISID first; the __Secure-3PAPISID copy carries the same value when only it is present
_SAPISID_NAMES = ("SAPISID", "__Secure-3PAPISID")

CookieRow = tuple[str, str, str, str, bool]  # name, value, host, path, secure


class ObserverUnavailable(RuntimeError):
    """The observer cannot look at YouTube right now (not signed in, no profile); the check is skipped."""


def read_observer_cookies(session_dir: str | Path) -> list[CookieRow]:
    """youtube.com / google.com cookies from a Camoufox (Firefox) profile.

    The live `cookies.sqlite` (with its WAL) is copied to a temp folder first: the browser may hold it locked.
    Expired cookies are skipped. Blocking file I/O: call it through `asyncio.to_thread`.
    """
    source = Path(session_dir)
    if not (source / "cookies.sqlite").exists():
        return []
    where = " OR ".join(["host = ? OR host = ? OR host LIKE ?"] * len(COOKIE_DOMAINS))
    params: list[str] = []
    for domain in COOKIE_DOMAINS:
        params += [domain, f".{domain}", f"%.{domain}"]
    now = time.time()
    rows: list[CookieRow] = []
    with tempfile.TemporaryDirectory(prefix="observer-cookies-") as tmp:
        for name in _COOKIE_FILES:
            if (source / name).exists():
                try:
                    shutil.copyfile(source / name, Path(tmp) / name)
                except OSError as exc:
                    logger.debug("Не удалось скопировать {}: {}", name, exc)
        try:
            conn = sqlite3.connect(Path(tmp, "cookies.sqlite").as_posix(), timeout=2.0)
        except sqlite3.Error as exc:
            logger.debug("cookies.sqlite наблюдателя не открыт: {}", exc)
            return []
        try:
            for name, value, host, path, secure, expiry in conn.execute(
                f"SELECT name, value, host, path, isSecure, expiry FROM moz_cookies WHERE {where}", params
            ):
                if expiry:
                    expiry_sec = expiry / 1000.0 if expiry > 1e11 else float(expiry)
                    if expiry_sec < now:
                        continue
                rows.append((str(name), str(value), str(host), str(path or "/"), bool(secure)))
        except sqlite3.Error as exc:
            logger.debug("Не удалось прочитать cookies наблюдателя: {}", exc)
        finally:
            conn.close()
    return rows


def read_chrome_observer_cookies(session_dir: str | Path) -> list[CookieRow]:
    """youtube.com / google.com cookies of a Chrome profile from the DPAPI state the runner exported.

    The export keeps no path or `secure` flag: every Google cookie is served on `/` over https. Expired cookies are
    skipped. Empty when the profile was never signed in through the bot. Blocking: call it through `asyncio.to_thread`.
    """
    payload = load_chrome_heartbeat_payload(session_dir)
    if payload is None:
        return []
    now = time.time()
    rows: list[CookieRow] = []
    for cookie in payload.get("cookies") or []:
        if not isinstance(cookie, dict) or not cookie.get("name"):
            continue
        host = str(cookie.get("domain") or "").lower()
        if not any(host.lstrip(".") == d or host.endswith(f".{d}") for d in COOKIE_DOMAINS):
            continue
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and 0 < expires < now:
            continue
        rows.append((str(cookie["name"]), str(cookie.get("value", "")), host, "/", True))
    return rows


def read_account_cookies(session_dir: str | Path, engine: str = "camoufox") -> list[CookieRow]:
    """The cookies of one account's profile, whichever browser engine it uses."""
    return read_chrome_observer_cookies(session_dir) if engine == "chrome" else read_observer_cookies(session_dir)


def sapisid_hash(sapisid: str, origin: str = YOUTUBE_ORIGIN, now: float | None = None) -> str:
    """`SAPISIDHASH <ts>_<sha1(ts sapisid origin)>`: without it InnerTube answers a cookie request as anonymous."""
    ts = int(time.time() if now is None else now)
    digest = hashlib.sha1(f"{ts} {sapisid} {origin}".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


def has_login(cookies: list[CookieRow]) -> bool:
    """Only a LOGIN_INFO cookie of youtube.com proves the profile is signed in there."""
    return any(name == "LOGIN_INFO" and host.lstrip(".") == "youtube.com" for name, _, host, _, _ in cookies)


async def observer_signed_in(account: Any) -> bool:
    """Does the observer's profile hold a YouTube login (cookies only, nothing is sent)?"""
    cookies = await asyncio.to_thread(
        read_account_cookies, account.session_path, getattr(account, "engine", "camoufox")
    )
    return has_login(cookies)


class ObserverSession:
    """A curl_cffi session that looks at YouTube as the observer: its own proxy, its own cookies.

    Opened lazily by the first check; `get_session` is what `CommentVisibilityChecker` takes as `session_provider`.
    The observer never posts: this session only issues GET /watch and POST /next. TLS profile and headers follow the
    observer's browser engine like the account heartbeat does. A proxy goes through the network guard first (fail
    closed: a proxy that leaks or does not answer means no request); an assigned but unreadable proxy never falls back
    to the host's own address.
    """

    def __init__(self, account: Any, settings: CollectorSettings) -> None:
        self._account = account
        self._settings = settings
        self._session: AsyncSession | None = None
        self._sapisid: str | None = None

    def auth_headers(self) -> dict[str, str]:
        """Signed-in InnerTube headers for POST /next; empty until the session is open or without SAPISID."""
        if not self._sapisid:
            return {}
        return {
            "Authorization": sapisid_hash(self._sapisid),
            "X-Origin": YOUTUBE_ORIGIN,
            "Origin": YOUTUBE_ORIGIN,
            "X-Goog-AuthUser": "0",
        }

    def _browser_identity(self) -> tuple[str, dict[str, str]]:
        """(curl_cffi TLS profile, headers) that match the observer's browser."""
        account = self._account
        locale = getattr(account, "locale", None) or "en-US"
        if getattr(account, "engine", "camoufox") == "chrome":
            payload = load_chrome_heartbeat_payload(account.session_path) or {}
            user_agent = payload.get("user_agent") or None
            return chrome_impersonate_profile(user_agent), chrome_headers(
                user_agent, locale, load_chrome_client_hints(account.session_path)
            )
        return HEARTBEAT_IMPERSONATE, fingerprint_headers(account.session_path, locale)

    async def get_session(self) -> AsyncSession:
        if self._session is not None:
            return self._session
        account = self._account
        try:
            proxy = resolve_account_proxy(account)
        except ProxyUnavailableError as exc:
            raise ObserverUnavailable(str(exc)) from exc
        cookies = await asyncio.to_thread(
            read_account_cookies, account.session_path, getattr(account, "engine", "camoufox")
        )
        if not has_login(cookies):
            raise ObserverUnavailable(f"наблюдатель {account.name!r} не вошёл в YouTube")
        try:
            proxy_url = curl_proxy_url(proxy)
            if proxy_url is not None:
                await ensure_network_ready(proxy)
        except (ValueError, NetworkGuardError) as exc:
            raise ObserverUnavailable(f"прокси наблюдателя {account.name!r} не годится: {exc}") from exc
        impersonate, headers = self._browser_identity()
        session = AsyncSession(
            impersonate=impersonate,
            proxy=proxy_url,
            timeout=self._settings.timeout_seconds,
            headers=headers,
            trust_env=False,  # the host's HTTP(S)_PROXY must neither add a proxy nor bypass the observer's
        )
        if proxy_url is None:
            logger.warning("У наблюдателя {!r} нет прокси: проверки идут с IP этого компьютера", account.name)
        for name, value, host, path, secure in cookies:
            session.cookies.set(name, value, domain=host, path=path, secure=secure)
        by_name = {name: value for name, value, host, _, _ in cookies if host.lstrip(".") in COOKIE_DOMAINS}
        self._sapisid = next((by_name[n] for n in _SAPISID_NAMES if by_name.get(n)), None)
        if self._sapisid is None:
            logger.warning("У наблюдателя {!r} нет cookie SAPISID: YouTube может ответить как анонимному", account.name)
        self._session = session
        logger.debug("Сессия наблюдателя {!r} открыта ({} cookies)", account.name, len(cookies))
        return session

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()


__all__ = [
    "COOKIE_DOMAINS",
    "ObserverSession",
    "ObserverUnavailable",
    "has_login",
    "observer_signed_in",
    "read_account_cookies",
    "read_chrome_observer_cookies",
    "read_observer_cookies",
    "sapisid_hash",
]
