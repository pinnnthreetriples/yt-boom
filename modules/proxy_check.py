"""One proxy check for the wizard and the bot: the same libcurl path the runtime uses (`curl_proxy_url`).

`check_proxy(proxy)` asks an IP echo service through the proxy and returns `ProxyCheckResult`: ok, the type that
worked (an unknown type is tried as HTTP, then SOCKS5), exit IP, exit country (Camoufox's GeoIP database, as
`modules.geo_guard.resolve_exit_country`), latency and, on failure, the kind of error with a Russian message.
A proxy passes only when the answer is a real IP address, that IP is not the machine's own and a proxied echo
request does not carry the machine's IP in a forwarding header. Every request ignores the environment and the
Windows system proxy (`trust_env=False`): NO_PROXY can never send a check around a dead proxy.
`check_proxies` checks many at once (bounded), `check_direct` reports the machine's own IP ("с вашего IP"),
`check_and_record` stores the result in the library. A password never reaches a message or a log line: only our own
texts and the libcurl error number are shown.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Literal, Sequence

from loguru import logger

from config import ProxyParts, compose_proxy_url, split_proxy
from modules import geo_guard
from modules.geo_guard import country_for_ip, parse_echo_ip, remember_exit_country

ProxyErrorKind = Literal[
    "auth", "forbidden", "unreachable", "timeout", "protocol", "upstream", "bad_response", "host_ip_leak",
    "header_leak", "other",
]

CHECK_TIMEOUT = 10.0
CHECK_CONCURRENCY = 8
IP_ECHO_URLS = ("https://api.ipify.org?format=json", "https://icanhazip.com")

# libcurl error numbers (CURLcode) that tell the kind of failure apart. 6 (could not resolve HOST) is the echo
# service's name, not the proxy's: another echo service may still answer.
_UNREACHABLE_CODES = {5}  # could not resolve the proxy
_UPSTREAM_CODES = {6}
_CONNECT_RESPONSE_RE = re.compile(r"connect tunnel failed, response (\d{3})")
_TIMEOUT_CODES = {28}
_AUTH_MARKERS = ("407", "proxy authentication required", "user was rejected", "authentication failed",
                 "no authentication method was acceptable")
_UNREACHABLE_MARKERS = ("connection refused", "failed to connect", "could not resolve proxy", "couldn't connect",
                        "no route to host", "network is unreachable")
# Kinds after which another echo service cannot help: the proxy itself refused or nothing listens there.
_PROXY_REFUSED = ("auth", "forbidden", "unreachable", "protocol")
_TIMEOUT_MARKERS = ("timed out", "timeout")

_TYPE_NAMES = {"http": "HTTP", "https": "HTTPS", "socks5": "SOCKS5", "socks4": "SOCKS4"}


@dataclass(frozen=True)
class ProxyCheckResult:
    """`scheme`: the type that worked (or that answered with a login error); None when nothing answered.
    `error_kind`: None on success, else auth (wrong login/password) / forbidden (CONNECT 401/403: IP not
    whitelisted) / unreachable / timeout / protocol (the proxy did not speak this type) / upstream (the echo service
    failed behind the proxy) / bad_response (an answer that is not an IP) / host_ip_leak (exits from the machine's
    own IP) / header_leak (forwards the machine's IP in a header) / other.
    `message`: Russian, ready to show. `browser_warning`: set when the browser cannot use this proxy (see
    `browser_proxy_warning`) even though the check passed. `leak_warning`: the check passed but something about
    hiding the machine's IP is worth knowing (proxy headers seen, or the comparison could not be made)."""

    ok: bool
    scheme: str | None
    exit_ip: str | None
    exit_country: str | None
    latency_ms: int | None
    error_kind: ProxyErrorKind | None
    message: str
    browser_warning: str | None = None
    leak_warning: str | None = None


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
    connect = _CONNECT_RESPONSE_RE.search(text)
    if connect:  # the proxy answered the HTTP CONNECT itself: it is reachable, its status says why it refused
        status = int(connect.group(1))
        if status == 407:
            return "auth", code
        if status in (401, 403):
            return "forbidden", code
        if status in (400, 405, 501):
            return "protocol", code  # e.g. a SOCKS server that got an HTTP CONNECT: SOCKS5 is worth a try
        if status in (502, 503, 504):
            return "upstream", code
        return "other", code
    if any(marker in text for marker in _AUTH_MARKERS) or (code == 97 and "auth" in text):
        return "auth", code
    if code in _UPSTREAM_CODES or "could not resolve host" in text:
        return "upstream", code
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
    # trust_env=False: HTTP(S)_PROXY / ALL_PROXY / NO_PROXY and the system proxy must neither replace the proxy under
    # test nor let a request go around it (a dead proxy would then "work" with the machine's own IP).
    async with AsyncSession(proxy=curl_proxy_url(proxy_url), timeout=timeout, trust_env=False) as session:
        for url in IP_ECHO_URLS:
            started = time.monotonic()
            try:
                resp = await session.get(url, allow_redirects=False)  # a redirect is a captive portal, not an IP
            except Exception as exc:
                kind, code = classify_curl_error(exc)
                logger.debug("Проверка прокси: {} (curl {}) на {}", kind, code, url.split("/")[2])
                if kind in _PROXY_REFUSED:
                    raise _FetchError(kind, code) from None  # the proxy itself refused: another service won't help
                last = _FetchError(kind, code)
                continue
            if resp.status_code == 407:
                raise _FetchError("auth", None)
            if resp.status_code != 200:
                last = _FetchError("bad_response" if 300 <= resp.status_code < 400 else "upstream", None)
                continue
            ip = parse_echo_ip(resp.text)
            if ip:
                return ip, int((time.monotonic() - started) * 1000)
            last = _FetchError("bad_response", None)  # an error page or a login form, not an IP address
    raise last or _FetchError("other", None)


HOST_IP_LEAK_MESSAGE = "Прокси не скрывает ваш IP: сайты видят ваш настоящий адрес, а не адрес прокси"
HEADER_LEAK_MESSAGE = (
    "Прокси передаёт ваш настоящий IP в заголовках (X-Forwarded-For / Via / Forwarded): сайты его видят"
)


def _failure_message(kind: ProxyErrorKind, code: int | None, timeout: float, scheme_known: bool,
                     scheme: str | None = None) -> str:
    if kind == "auth":
        status = " (ошибка 407)" if (scheme or "http") in ("http", "https") else ""
        return f"Прокси отклонил логин или пароль{status}: проверьте их у продавца"
    if kind == "forbidden":
        return ("Прокси отказал в доступе (ошибка 401/403): обычно ваш IP не добавлен в белый список у продавца "
                "или доступ закончился")
    if kind == "unreachable":
        return "Прокси-сервер недоступен: неверный адрес или порт, либо сервер выключен"
    if kind == "timeout":
        return f"Прокси не ответил за {timeout:.0f} с: сервер перегружен или недоступен"
    if kind == "bad_response":
        return ("Прокси вернул вместо IP-адреса другой ответ (страницу ошибки или перенаправление): "
                "через него не выйти в интернет")
    if kind == "upstream":
        return "Прокси принял подключение, но сайт за ним не ответил: прокси не выпускает в интернет или перегружен"
    if kind == "host_ip_leak":
        return HOST_IP_LEAK_MESSAGE
    if kind == "header_leak":
        return HEADER_LEAK_MESSAGE
    suffix = f" (код curl {code})" if code else ""
    if not scheme_known:
        return f"Прокси не ответил ни как HTTP, ни как SOCKS5{suffix}: проверьте адрес, порт и тип у продавца"
    return f"Не удалось выйти в интернет через прокси{suffix}: возможно, указан не тот тип (HTTP / SOCKS5)"


async def _try(parts: ProxyParts, scheme: str, timeout: float) -> ProxyCheckResult:
    url = compose_proxy_url(scheme, parts.host, parts.port, parts.login, parts.password)
    try:
        ip, latency = await asyncio.wait_for(_fetch_ip(url, timeout), timeout * len(IP_ECHO_URLS) + 5)
    except _FetchError as exc:
        return ProxyCheckResult(False, scheme if exc.kind in ("auth", "forbidden") else None, None, None, None,
                                exc.kind, _failure_message(exc.kind, exc.code, timeout, True, scheme))
    except asyncio.TimeoutError:
        return ProxyCheckResult(False, None, None, None, None, "timeout", _failure_message("timeout", None, timeout, True))
    country = await country_for_ip(ip)
    remember_exit_country(url, country)
    country_text = country or "не определена"
    message = f"Прокси работает ({_TYPE_NAMES.get(scheme, scheme)}): внешний IP {ip}, страна {country_text}, {latency} мс"
    return ProxyCheckResult(True, scheme, ip, country, latency, None, message, browser_proxy_warning(scheme, parts.login))


async def _verify_route(proxy_url: str, exit_ip: str, host_ip: str | None) -> tuple[ProxyErrorKind | None, str | None]:
    """(error kind or None, warning or None): does the proxy hide the machine's IP? `host_ip` None = unknown."""
    if host_ip is None:
        return None, "Ваш IP определить не удалось: проверка «скрывает ли прокси ваш IP» пропущена"
    if geo_guard._same_ip(exit_ip, host_ip):
        return "host_ip_leak", None
    try:
        headers = await asyncio.wait_for(
            asyncio.to_thread(geo_guard._fetch_echo_headers, proxy_url), geo_guard.HEADER_ECHO_TIMEOUT + 5
        )
    except Exception as exc:  # the message may carry the proxy URL: only the type is logged
        logger.debug("Проверка заголовков прокси не удалась: {}", type(exc).__name__)
        return None, "Проверить заголовки прокси не удалось (эхо-сервис не ответил): утечка IP в заголовках не исключена"
    names = geo_guard._LEAK_HEADERS
    if any(geo_guard._mentions_ip(headers.get(name, ""), host_ip) for name in names):
        return "header_leak", None
    seen = [name for name in names if headers.get(name, "").strip()]
    if seen:
        return None, f"Прокси добавляет заголовки {', '.join(seen)}: сайты могут понять, что это прокси"
    return None, None


async def _with_route_check(result: ProxyCheckResult, parts: ProxyParts, host_ip: str | None) -> ProxyCheckResult:
    if not result.ok or result.exit_ip is None:
        return result
    url = compose_proxy_url(result.scheme, parts.host, parts.port, parts.login, parts.password)
    kind, warning = await _verify_route(url, result.exit_ip, host_ip)
    if kind is not None:
        return ProxyCheckResult(False, result.scheme, result.exit_ip, result.exit_country, result.latency_ms, kind,
                                _failure_message(kind, None, CHECK_TIMEOUT, True, result.scheme), result.browser_warning)
    return replace(result, leak_warning=warning) if warning else result


async def check_proxy(proxy: Any, timeout: float = CHECK_TIMEOUT, *, host_ip: str | None = None,
                      resolve_host: bool = True) -> ProxyCheckResult:
    """Check one proxy (ProxyParts / ProxyDraft / ProxyRecord / address string). With an unknown type: HTTP first,
    then SOCKS5; a login error on HTTP settles the type (the server spoke HTTP). A proxy that answered is then
    compared with the machine's own IP (`host_ip`, looked up when None and `resolve_host`) and its forwarding
    headers. Never raises for network failures; ValueError for an address string that is not a proxy."""
    parts = _as_parts(proxy)
    if host_ip is None and resolve_host:
        host_ip = await geo_guard.resolve_host_ip()
    if parts.scheme:
        return await _with_route_check(await _try(parts, parts.scheme, timeout), parts, host_ip)
    first = await _try(parts, "http", timeout)
    if first.ok:
        return await _with_route_check(first, parts, host_ip)
    if first.error_kind in ("auth", "forbidden", "unreachable"):
        return first  # unreachable: nothing listens there, SOCKS5 would only wait for the same timeout
    second = await _try(parts, "socks5", timeout)
    if second.ok:
        return await _with_route_check(second, parts, host_ip)
    if second.error_kind in ("auth", "forbidden"):
        return second
    kind = first.error_kind or "other"
    return ProxyCheckResult(False, None, None, None, None, kind, _failure_message(kind, None, timeout, False))


async def check_proxies(
    proxies: Sequence[Any],
    concurrency: int = CHECK_CONCURRENCY,
    timeout: float = CHECK_TIMEOUT,
    on_done: Callable[[int, int], None] | None = None,
    host_ip: str | None = None,
) -> list[ProxyCheckResult]:
    """`check_proxy` for each, at most `concurrency` at a time; results in the input order. The machine's own IP is
    looked up once for the whole batch when `host_ip` is not given. A broken item becomes a failed result, it never
    stops the batch. `on_done(finished, total)` is called after every finished check (a progress line)."""
    gate = asyncio.Semaphore(max(1, concurrency))
    finished = 0
    if host_ip is None and proxies:
        host_ip = await geo_guard.resolve_host_ip()

    async def one(item: Any) -> ProxyCheckResult:
        nonlocal finished
        async with gate:
            try:
                result = await check_proxy(item, timeout, host_ip=host_ip, resolve_host=False)
            except Exception as exc:  # a malformed address or record: report it, keep checking the others
                logger.debug("Проверка прокси не выполнена: {}", type(exc).__name__)
                result = ProxyCheckResult(False, None, None, None, None, "other",
                                          "Прокси не проверен: адрес записан с ошибкой")
        finished += 1
        if on_done is not None:
            on_done(finished, len(proxies))
        return result

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


async def check_and_record(db: Any, proxy_id: int, timeout: float = CHECK_TIMEOUT, *,
                           host_ip: str | None = None) -> tuple[Any, ProxyCheckResult]:
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
        result = await check_proxy(record, timeout, host_ip=host_ip)
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
    "HEADER_LEAK_MESSAGE",
    "HOST_IP_LEAK_MESSAGE",
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
