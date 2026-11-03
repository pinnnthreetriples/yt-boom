"""One proxy check for the wizard and the bot: the same libcurl path the runtime uses (`curl_proxy_url`).

`check_proxy(proxy)` asks an IP echo service through the proxy and returns `ProxyCheckResult`: ok, the type that
worked (an unknown type is tried as HTTP, then SOCKS5), exit IP, exit country (Camoufox's GeoIP database, as
`modules.geo_guard.resolve_exit_country`), latency and, on failure, the kind of error with a Russian message.
`check_proxies` checks many at once (bounded), `check_direct` reports the machine's own IP ("с вашего IP"),
`check_and_record` stores the result in the library. A password never reaches a message or a log line: only our own
texts and the libcurl error number are shown.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from loguru import logger

from config import ProxyParts, compose_proxy_url, split_proxy
from modules.geo_guard import country_for_ip, remember_exit_country

ProxyErrorKind = Literal["auth", "unreachable", "timeout", "other"]

CHECK_TIMEOUT = 10.0
CHECK_CONCURRENCY = 8
IP_ECHO_URLS = ("https://api.ipify.org?format=json", "https://icanhazip.com")

# libcurl error numbers (CURLcode) that tell the kind of failure apart
_UNREACHABLE_CODES = {5, 6, 7}  # could not resolve proxy / host, could not connect
_TIMEOUT_CODES = {28}
_AUTH_MARKERS = ("407", "proxy authentication required", "user was rejected", "authentication failed",
                 "no authentication method was acceptable")
_UNREACHABLE_MARKERS = ("connection refused", "failed to connect", "could not resolve proxy", "couldn't connect")
_TIMEOUT_MARKERS = ("timed out", "timeout")

_TYPE_NAMES = {"http": "HTTP", "https": "HTTPS", "socks5": "SOCKS5", "socks4": "SOCKS4"}


@dataclass(frozen=True)
class ProxyCheckResult:
    """`scheme`: the type that worked (or that answered with a login error); None when nothing answered.
    `error_kind`: None on success, else auth (wrong login/password, 407) / unreachable / timeout / other.
    `message`: Russian, ready to show. `browser_warning`: set when the browser cannot use this proxy (see
    `browser_proxy_warning`) even though the check passed."""

    ok: bool
    scheme: str | None
    exit_ip: str | None
    exit_country: str | None
    latency_ms: int | None
    error_kind: ProxyErrorKind | None
    message: str
    browser_warning: str | None = None


class _FetchError(Exception):
    def __init__(self, kind: ProxyErrorKind, code: int | None) -> None:
        super().__init__(kind)
        self.kind, self.code = kind, code


def browser_proxy_warning(scheme: str | None, login: str | None) -> str | None:
    """Russian warning when Camoufox cannot browse through this proxy, else None.

    Playwright refuses SOCKS5 with a login before Firefox starts ("Browser does not support socks5 proxy
    authentication", checked with Camoufox 0.5.8 / Playwright 1.62): such a proxy works for the search (libcurl)
    but an account on it never opens its browser.
    """
    if (scheme or "").lower() in ("socks5", "socks5h", "socks4") and login:
        return (
            "SOCKS5 с логином и паролем подходит только для поиска: браузер аккаунта (Camoufox) через него не "
            "запустится. Для аккаунтов возьмите HTTP-вариант этого прокси (у продавцов он обычно есть) "
            "или доступ по IP без пароля"
        )
    return None


def classify_curl_error(exc: BaseException) -> tuple[ProxyErrorKind, int | None]:
    """(kind, libcurl code) of a curl_cffi exception, from its code and text; the text itself is never kept."""
    raw_code = getattr(exc, "code", None)
    try:
        code = int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        code = None
    text = str(exc).lower()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or code in _TIMEOUT_CODES:
        return "timeout", code
    if any(marker in text for marker in _AUTH_MARKERS) or (code == 97 and "auth" in text):
        return "auth", code
    if code in _UNREACHABLE_CODES or any(marker in text for marker in _UNREACHABLE_MARKERS):
        return "unreachable", code
    if any(marker in text for marker in _TIMEOUT_MARKERS):
        return "timeout", code
    return "other", code


def _as_parts(proxy: Any) -> ProxyParts:
    """ProxyParts from a ProxyParts, a `proxy_paste.ProxyDraft`, a `database.ProxyRecord` or an address string."""
    if isinstance(proxy, ProxyParts):
        return proxy
    if isinstance(proxy, str):
        return split_proxy(proxy)
    parts = getattr(proxy, "parts", None)
    if isinstance(parts, ProxyParts):
        return parts
    return ProxyParts(proxy.scheme, proxy.host, proxy.port, getattr(proxy, "login", None), getattr(proxy, "password", None))


async def _fetch_ip(proxy_url: str | None, timeout: float) -> tuple[str, int]:
    """(exit IP, latency ms) through `proxy_url` (None = direct); _FetchError with the kind of failure."""
    from curl_cffi.requests import AsyncSession

    from modules.session_heartbeat import curl_proxy_url

    last: _FetchError | None = None
    async with AsyncSession(proxy=curl_proxy_url(proxy_url), timeout=timeout) as session:
        for url in IP_ECHO_URLS:
            started = time.monotonic()
            try:
                resp = await session.get(url)
            except Exception as exc:
                kind, code = classify_curl_error(exc)
                logger.debug("Проверка прокси: {} (curl {}) на {}", kind, code, url.split("/")[2])
                if kind in ("auth", "unreachable"):
                    raise _FetchError(kind, code) from None  # the proxy itself refused: another service won't help
                last = _FetchError(kind, code)
                continue
            if resp.status_code == 407:
                raise _FetchError("auth", None)
            if resp.status_code != 200:
                last = _FetchError("other", None)
                continue
            text = resp.text.strip()
            ip = text
            if text.startswith("{"):
                try:
                    ip = str(resp.json().get("ip") or "")
                except ValueError:
                    ip = ""
            if ip and len(ip) <= 45:
                return ip, int((time.monotonic() - started) * 1000)
            last = _FetchError("other", None)
    raise last or _FetchError("other", None)


def _failure_message(kind: ProxyErrorKind, code: int | None, timeout: float, scheme_known: bool) -> str:
    if kind == "auth":
        return "Прокси отклонил логин или пароль (ошибка 407): проверьте их у продавца"
    if kind == "unreachable":
        return "Прокси-сервер недоступен: неверный адрес или порт, либо сервер выключен"
    if kind == "timeout":
        return f"Прокси не ответил за {timeout:.0f} с: сервер перегружен или недоступен"
    suffix = f" (код curl {code})" if code else ""
    if not scheme_known:
        return f"Прокси не ответил ни как HTTP, ни как SOCKS5{suffix}: проверьте адрес, порт и тип у продавца"
    return f"Не удалось выйти в интернет через прокси{suffix}: возможно, указан не тот тип (HTTP / SOCKS5)"


async def _try(parts: ProxyParts, scheme: str, timeout: float) -> ProxyCheckResult:
    url = compose_proxy_url(scheme, parts.host, parts.port, parts.login, parts.password)
    try:
        ip, latency = await asyncio.wait_for(_fetch_ip(url, timeout), timeout * len(IP_ECHO_URLS) + 5)
    except _FetchError as exc:
        return ProxyCheckResult(False, scheme if exc.kind == "auth" else None, None, None, None, exc.kind,
                                _failure_message(exc.kind, exc.code, timeout, True))
    except asyncio.TimeoutError:
        return ProxyCheckResult(False, None, None, None, None, "timeout", _failure_message("timeout", None, timeout, True))
    country = await country_for_ip(ip)
    remember_exit_country(url, country)
    country_text = country or "не определена"
    message = f"Прокси работает ({_TYPE_NAMES.get(scheme, scheme)}): внешний IP {ip}, страна {country_text}, {latency} мс"
    return ProxyCheckResult(True, scheme, ip, country, latency, None, message, browser_proxy_warning(scheme, parts.login))


async def check_proxy(proxy: Any, timeout: float = CHECK_TIMEOUT) -> ProxyCheckResult:
    """Check one proxy (ProxyParts / ProxyDraft / ProxyRecord / address string). With an unknown type: HTTP first,
    then SOCKS5; a login error on HTTP settles the type (the server spoke HTTP). Never raises for network failures;
    ValueError for an address string that is not a proxy."""
    parts = _as_parts(proxy)
    if parts.scheme:
        return await _try(parts, parts.scheme, timeout)
    first = await _try(parts, "http", timeout)
    if first.ok or first.error_kind in ("auth", "unreachable"):
        return first  # unreachable: nothing listens there, SOCKS5 would only wait for the same timeout
    second = await _try(parts, "socks5", timeout)
    if second.ok or second.error_kind == "auth":
        return second
    kind = first.error_kind or "other"
    return ProxyCheckResult(False, None, None, None, None, kind, _failure_message(kind, None, timeout, False))


async def check_proxies(
    proxies: Sequence[Any], concurrency: int = CHECK_CONCURRENCY, timeout: float = CHECK_TIMEOUT
) -> list[ProxyCheckResult]:
    """`check_proxy` for each, at most `concurrency` at a time; results in the input order."""
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(item: Any) -> ProxyCheckResult:
        async with gate:
            return await check_proxy(item, timeout)

    return list(await asyncio.gather(*(one(item) for item in proxies)))


async def check_direct(timeout: float = CHECK_TIMEOUT) -> ProxyCheckResult:
    """The machine's own exit IP and country, for "напрямую, с вашего IP" (scheme None)."""
    try:
        ip, latency = await asyncio.wait_for(_fetch_ip(None, timeout), timeout * len(IP_ECHO_URLS) + 5)
    except (_FetchError, asyncio.TimeoutError) as exc:
        kind: ProxyErrorKind = exc.kind if isinstance(exc, _FetchError) else "timeout"
        return ProxyCheckResult(False, None, None, None, None, kind, "Нет выхода в интернет напрямую: проверьте сеть")
    country = await country_for_ip(ip)
    remember_exit_country(None, country)
    return ProxyCheckResult(
        True, None, ip, country, latency, None, f"Прямое соединение: ваш IP {ip}, страна {country or 'не определена'}"
    )


async def check_and_record(db: Any, proxy_id: int, timeout: float = CHECK_TIMEOUT) -> tuple[Any, ProxyCheckResult]:
    """Check a library proxy and store the result (`Database.record_proxy_check`); returns (updated record, result).
    RecordNotFoundError for an unknown id; a password that cannot be decrypted is reported, not checked."""
    from database import RecordNotFoundError

    record = await db.get_proxy(proxy_id)
    if record is None:
        raise RecordNotFoundError(f"proxy id={proxy_id} not found")
    if record.password_unavailable:
        result = ProxyCheckResult(
            False, None, None, None, None, "other",
            "Пароль прокси не расшифровывается на этом компьютере: введите прокси заново",
        )
    else:
        result = await check_proxy(record, timeout)
    updated = await db.record_proxy_check(
        proxy_id, result.ok, scheme=result.scheme, exit_ip=result.exit_ip, exit_country=result.exit_country,
        latency_ms=result.latency_ms, error=None if result.ok else result.message,
    )
    return updated, result


def check_proxy_url_sync(proxy_url: str | None, timeout: float = CHECK_TIMEOUT) -> ProxyCheckResult:
    """Blocking wrapper for synchronous menus: a proxy address, or None for the direct connection."""
    if not proxy_url:
        return asyncio.run(check_direct(timeout))
    return asyncio.run(check_proxy(proxy_url, timeout))


__all__ = [
    "CHECK_CONCURRENCY",
    "CHECK_TIMEOUT",
    "ProxyCheckResult",
    "ProxyErrorKind",
    "browser_proxy_warning",
    "check_and_record",
    "check_direct",
    "check_proxies",
    "check_proxy",
    "check_proxy_url_sync",
    "classify_curl_error",
]
