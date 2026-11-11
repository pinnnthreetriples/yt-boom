from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone as _timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger

from config import parse_proxy

CountryCheck = Literal["ok", "mismatch", "unknown", "skipped"]
GeoLockVerdict = Literal["ok", "new", "changed", "unknown"]

EXIT_COUNTRY_TTL = 600.0  # seconds a resolved exit country of a proxy is reused
EXIT_LOOKUP_TIMEOUT = 20.0
HOST_IP_TTL = 1800.0  # the machine's own public IP changes rarely: cached longer than a proxy's exit
HOST_IP_TIMEOUT = 20.0
# Plain HTTP on purpose: a proxy can add forwarding headers only to a request it can read. `show_env=1` is required:
# httpbin hides X-Forwarded-For / Via / X-Real-Ip from its answer by default (checked against the live service).
HEADER_ECHO_URL = "http://httpbin.org/headers?show_env=1"
HEADER_ECHO_TIMEOUT = 15.0
SESSION_GEO_FILE = "session_geo.json"
# Headers through which a proxy may hand the client's real address to the site.
_LEAK_HEADERS = (
    "x-forwarded-for", "forwarded", "via", "x-real-ip", "client-ip",
    "x-client-ip", "true-client-ip", "x-originating-ip", "x-cluster-client-ip",
)

# Names Google, sellers and users write that differ from the IANA registry's wording (lower case).
_ALIASES = {
    "russia": "RU", "south korea": "KR", "north korea": "KP", "vietnam": "VN", "czech republic": "CZ",
    "uk": "GB", "usa": "US", "united states of america": "US", "ivory coast": "CI", "turkey": "TR",
    "laos": "LA", "moldova": "MD", "taiwan": "TW", "hong kong": "HK", "macau": "MO", "bolivia": "BO",
    "iran": "IR", "syria": "SY", "tanzania": "TZ", "venezuela": "VE", "brunei": "BN", "palestine": "PS",
    "россия": "RU", "рф": "RU", "сша": "US", "соединенные штаты": "US", "великобритания": "GB",
    "германия": "DE", "франция": "FR", "польша": "PL", "украина": "UA", "беларусь": "BY",
    "белоруссия": "BY", "казахстан": "KZ", "турция": "TR", "италия": "IT", "испания": "ES",
    "нидерланды": "NL", "канада": "CA", "австралия": "AU", "индия": "IN", "бразилия": "BR",
    "япония": "JP", "китай": "CN", "чехия": "CZ", "литва": "LT", "латвия": "LV", "эстония": "EE",
    "грузия": "GE", "армения": "AM", "азербайджан": "AZ", "израиль": "IL", "оаэ": "AE",
    "португалия": "PT", "швеция": "SE", "финляндия": "FI", "молдова": "MD", "индонезия": "ID",
    "мексика": "MX", "кыргызстан": "KG", "узбекистан": "UZ",
}
_NOT_COUNTRIES = {"ZZ", "EU", "UN", "AA", "QM", "QN", "QO", "QP", "QQ", "QR", "QS", "QT", "QU", "QV", "QW", "QX", "QY", "QZ"}
_LOCALE_RE = re.compile(r"^([A-Za-z]{2,3})[-_]([A-Za-z]{2})$")
_EXIT_CACHE: dict[str, tuple[float, str]] = {}
_GEO_CACHE: dict[str, tuple[float, ExitGeo]] = {}
_READY_CACHE: dict[str, tuple[float, ExitGeo]] = {}
_HOST_IP_CACHE: list[tuple[float, str]] = []  # at most one entry: (when, ip)
_warned: set[str] = set()
_TZ_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)+")  # Area/City, Area/Region/City, Etc/GMT+5


class NetworkGuardError(RuntimeError):
    """The network path is not safe to open a session on (no/dead/leaky proxy). The message never carries proxy data."""


class GeoLockError(RuntimeError):
    """The profile was last used from another country and the geo lock is in `block` mode."""


@dataclass(frozen=True)
class ExitGeo:
    """Where a request leaves from: the exit IP and what the GeoIP database knows about it (None = unknown/invalid)."""

    ip: str
    country: str | None  # ISO alpha-2 via normalize_country
    timezone: str | None  # exact IANA zone of the exit IP's city/region from the GeoIP record, e.g. "America/Chicago"
    latitude: float | None
    longitude: float | None


def _now() -> float:
    return time.monotonic()


@lru_cache(maxsize=1)
def _country_tables() -> tuple[frozenset[str], dict[str, str]]:
    """ISO alpha-2 codes and English names -> code from the IANA registry shipped with `language_tags` (a Camoufox dependency)."""
    codes: set[str] = set()
    names: dict[str, str] = {}
    try:
        from language_tags import data

        for record in data.get("registry") or []:
            code = str(record.get("Subtag", "")).upper()
            if record.get("Type") != "region" or not re.fullmatch(r"[A-Z]{2}", code) or record.get("Deprecated"):
                continue
            if code in _NOT_COUNTRIES:
                continue
            codes.add(code)
            for description in record.get("Description", []):
                names[str(description).casefold()] = code
    except Exception as exc:  # no registry: only the aliases and bare two-letter codes of the aliases remain
        logger.debug("Реестр стран недоступен: {}", type(exc).__name__)
    codes.update(_ALIASES.values())
    names.update(_ALIASES)
    return frozenset(codes), names


def normalize_country(text: str | None) -> str | None:
    """`PL`, `pl`, `Poland`, `Польша` -> `PL`. Anything unknown is None: a country is never guessed."""
    value = " ".join((text or "").split()).casefold().replace("ё", "е")
    if not value:
        return None
    codes, names = _country_tables()
    if len(value) == 2 and value.isalpha():
        return value.upper() if value.upper() in codes else None
    return names.get(value) or names.get(value.removeprefix("the "))


def normalize_locale(text: str | None) -> str | None:
    """`pl-pl`, `pl_PL` -> `pl-PL`; None when it is not a language-REGION pair of a known language and country."""
    match = _LOCALE_RE.match((text or "").strip())
    if not match:
        return None
    language, region = match.group(1).lower(), match.group(2).upper()
    try:
        from language_tags import tags

        if not tags.language(language):
            return None
    except Exception:
        return None
    return f"{language}-{region}" if region in _country_tables()[0] else None


def check_expected_country(expected: str | None, exit_country: str | None) -> CountryCheck:
    if not expected:
        return "skipped"
    if not exit_country:
        return "unknown"
    return "ok" if expected.upper() == exit_country.upper() else "mismatch"


def _find_path(record: Any, dotted: str | None) -> Any:
    """Value at a dotted path (`location.time_zone`) of a nested GeoIP record; None when any step is missing."""
    for part in (dotted or "").split("."):
        record = record.get(part) if isinstance(record, dict) else None
        if record is None:
            return None
    return record


@lru_cache(maxsize=1)
def _tz_database_available() -> bool:
    """False on a machine without any IANA database (Windows without the `tzdata` package): zones cannot be looked up."""
    try:
        ZoneInfo("Etc/UTC")
    except Exception:
        return False
    return True


def _valid_timezone(value: Any) -> str | None:
    """An `Area/City` IANA name that the tz database knows; without a database only the shape is checked. Else None."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not _TZ_RE.fullmatch(name):
        return None
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None if _tz_database_available() else name
    except Exception:  # bad key shape, a directory instead of a zone file, unreadable file
        return None
    return name


def _valid_coordinate(value: Any, limit: float) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and -limit <= number <= limit else None


def _geo_from_record(ip: str, record: Any, paths: dict[str, Any]) -> ExitGeo:
    """ExitGeo from a GeoIP record by the dotted paths of Camoufox's GeoIP config. Invalid fields become None.

    The coordinates are a pair: one of them out of range drops both (half a point locates nothing).
    """
    country = _find_path(record, paths.get("iso_code"))
    latitude = _valid_coordinate(_find_path(record, paths.get("latitude")), 90.0)
    longitude = _valid_coordinate(_find_path(record, paths.get("longitude")), 180.0)
    if latitude is None or longitude is None:
        latitude = longitude = None
    return ExitGeo(
        ip=ip,
        country=normalize_country(str(country)) if country else None,
        timezone=_valid_timezone(_find_path(record, paths.get("timezone"))),
        latitude=latitude,
        longitude=longitude,
    )


def _lookup_exit_geo(proxy_url: str | None) -> ExitGeo | None:
    """Blocking: exit IP through the proxy (Camoufox's own `public_ip`, socks5h so DNS stays on the proxy), then country,
    time zone and coordinates from the GeoIP database Camoufox uses for `geoip=True` (its configured `paths`).

    The database is not downloaded here. When it is missing or unreadable the IP is still returned (geo fields None):
    the proxy provably answers. A failing IP lookup raises.
    """
    from camoufox.ip import public_ip

    from modules.session_heartbeat import curl_proxy_url

    lookup = getattr(public_ip, "__wrapped__", public_ip)  # Camoufox caches an IP per proxy forever; the TTL here is ours
    return _geo_for_ip(lookup(curl_proxy_url(proxy_url)))


def _geo_for_ip(ip: str) -> ExitGeo:
    """Blocking: country, time zone and coordinates of an IP from Camoufox's GeoIP database (no network).

    A missing or unreadable database gives an ExitGeo with only the IP.
    """
    from camoufox.geolocation import get_mmdb_path, load_geoip_config

    config = load_geoip_config()
    paths = config.get("paths") or {}
    path = get_mmdb_path("ipv6" if ":" in ip else "ipv4", config)
    if not path.exists():
        return ExitGeo(ip, None, None, None, None)
    try:
        import maxminddb

        with maxminddb.open_database(str(path)) as reader:
            record: Any = reader.get(ip)
    except Exception as exc:
        logger.debug("GeoIP-базу прочитать не удалось: {}", type(exc).__name__)
        return ExitGeo(ip, None, None, None, None)
    return _geo_from_record(ip, record, paths)


def _country_for_ip(ip: str) -> str | None:
    """Blocking: ISO country of an IP from Camoufox's GeoIP database; None when the database is missing or silent."""
    return _geo_for_ip(ip).country


def _lookup_exit_country(proxy_url: str | None) -> str | None:
    """Blocking: the country part of `_lookup_exit_geo` (None when the database does not know it)."""
    geo = _lookup_exit_geo(proxy_url)
    return geo.country if geo else None


async def resolve_exit_geo(proxy_url: str | None, timeout: float = EXIT_LOOKUP_TIMEOUT) -> ExitGeo | None:
    """IP, country, city time zone and coordinates of the point a request leaves from (through `proxy_url`, or directly).

    None on any failure (proxy down, no answer, timeout). Only a result with a known country is cached, for
    `EXIT_COUNTRY_TTL`; it also feeds the `resolve_exit_country` cache. Runs in a worker thread; a lookup that outlives
    the timeout finishes in the background and is dropped.
    """
    key = proxy_url or ""
    hit = _GEO_CACHE.get(key)
    if hit and _now() - hit[0] < EXIT_COUNTRY_TTL:
        return hit[1]
    try:
        geo = await asyncio.wait_for(asyncio.to_thread(_lookup_exit_geo, proxy_url), timeout)
    except Exception as exc:  # the message may carry the proxy URL with its password: only the type is logged
        logger.debug("Точку выхода определить не удалось: {}", type(exc).__name__)
        return None
    if geo and geo.country:
        stamp = _now()
        _GEO_CACHE[key] = (stamp, geo)
        _EXIT_CACHE[key] = (stamp, geo.country)
    return geo


async def resolve_exit_country(proxy_url: str | None, timeout: float = EXIT_LOOKUP_TIMEOUT) -> str | None:
    """ISO country of the IP a request leaves from (through `proxy_url`, or directly). Any failure is None (unknown).

    Runs in a worker thread; a lookup that outlives the timeout finishes in the background and is dropped.
    """
    key = proxy_url or ""
    hit = _EXIT_CACHE.get(key)
    if hit and _now() - hit[0] < EXIT_COUNTRY_TTL:
        return hit[1]
    try:
        country = await asyncio.wait_for(asyncio.to_thread(_lookup_exit_country, proxy_url), timeout)
    except Exception as exc:  # the message may carry the proxy URL with its password: only the type is logged
        logger.debug("Страну выхода определить не удалось: {}", type(exc).__name__)
        return None
    if country:
        _EXIT_CACHE[key] = (_now(), country)
    return country


async def country_for_ip(ip: str | None, timeout: float = EXIT_LOOKUP_TIMEOUT) -> str | None:
    """ISO country of `ip` from the same GeoIP database as `resolve_exit_country` (no network). Failure: None."""
    if not ip:
        return None
    try:
        return await asyncio.wait_for(asyncio.to_thread(_country_for_ip, ip), timeout)
    except Exception as exc:
        logger.debug("Страну IP определить не удалось: {}", type(exc).__name__)
        return None


def remember_exit_country(proxy_url: str | None, country: str | None) -> None:
    """Seed the exit-country cache with a result found elsewhere (a proxy check), so a sign-in right after reuses it."""
    if country:
        _EXIT_CACHE[proxy_url or ""] = (_now(), country)


def geoip_database_status() -> tuple[bool, str]:
    """(ready, Russian text): is Camoufox's GeoIP database downloaded? Without it no country is ever known, and an
    account with an expected country cannot sign in. The hint names the command that downloads it."""
    hint = "скачайте её командой: .venv\\Scripts\\python.exe -m camoufox fetch (около 120 МБ)"
    try:
        from camoufox.geolocation import get_mmdb_path, load_geoip_config

        path = get_mmdb_path("ipv4", load_geoip_config())
    except Exception as exc:
        logger.debug("Гео-база Camoufox недоступна: {}", type(exc).__name__)
        return False, f"Гео-база Camoufox недоступна (страна прокси не определится): {hint}"
    if not path.exists():
        return False, f"Гео-база Camoufox не скачана (страна прокси не определится): {hint}"
    return True, "Гео-база Camoufox на месте"


def _lookup_host_ip() -> str:
    """Blocking: the machine's real public IP, a DIRECT request (no proxy) by Camoufox's own `public_ip`."""
    from camoufox.ip import public_ip

    return getattr(public_ip, "__wrapped__", public_ip)(None)


async def resolve_host_ip(timeout: float = HOST_IP_TIMEOUT) -> str | None:
    """The machine's real public IP via a direct lookup (never through a proxy); None on any failure.

    Cached for `HOST_IP_TTL`, failures are not. Used only to prove a proxy does not expose it.
    """
    if _HOST_IP_CACHE and _now() - _HOST_IP_CACHE[0][0] < HOST_IP_TTL:
        return _HOST_IP_CACHE[0][1]
    try:
        ip = await asyncio.wait_for(asyncio.to_thread(_lookup_host_ip), timeout)
        ipaddress.ip_address(ip)
    except Exception as exc:
        logger.debug("Реальный IP хоста определить не удалось: {}", type(exc).__name__)
        return None
    _HOST_IP_CACHE[:] = [(_now(), ip)]
    return ip


def _same_ip(first: str, second: str) -> bool:
    try:
        return ipaddress.ip_address(first.strip()) == ipaddress.ip_address(second.strip())
    except ValueError:
        return False


def _fetch_echo_headers(proxy_url: str) -> dict[str, str]:
    """Blocking: request headers the echo endpoint received through the proxy (lower-cased names). Raises on any failure."""
    from curl_cffi import requests as curl_requests

    from modules.session_heartbeat import curl_proxy_url

    proxy = curl_proxy_url(proxy_url)
    response = curl_requests.get(HEADER_ECHO_URL, proxies={"http": proxy, "https": proxy}, timeout=HEADER_ECHO_TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(f"status {response.status_code}")
    headers = response.json()["headers"]
    return {str(name).lower(): str(value) for name, value in headers.items()}


def _mentions_ip(header_value: str, ip: str) -> bool:
    """Whether the header value (`1.2.3.4, 5.6.7.8`, `for="[2001:db8::1]:80";proto=http`, `1.1 gw (1.2.3.4)`) names `ip`."""
    for token in re.findall(r"[0-9A-Fa-f:.]{2,}", header_value):
        token = token.strip(":.")
        if _same_ip(token, ip):
            return True
        if token.count(":") == 1 and _same_ip(token.split(":")[0], ip):  # IPv4 with a port
            return True
    return False


async def _proxy_leaks_host_ip(proxy_url: str, host_ip: str) -> bool:
    """True when a proxied plain-HTTP request carries the host's IP in a forwarding header. A failed probe is only a
    warning (best effort): the proxy has already been proven to answer."""
    try:
        headers = await asyncio.wait_for(asyncio.to_thread(_fetch_echo_headers, proxy_url), HEADER_ECHO_TIMEOUT + 5)
    except Exception as exc:
        logger.warning("Проверка прозрачности прокси пропущена: эхо-запрос не удался ({})", type(exc).__name__)
        return False
    return any(_mentions_ip(headers.get(name, ""), host_ip) for name in _LEAK_HEADERS)


async def ensure_network_ready(proxy_url: str | None, *, require_proxy: bool = False) -> ExitGeo | None:
    """Fail-closed gate before a session opens: refuse to run from the machine's own IP or through a leaky proxy.

    No proxy: None (nothing is checked) unless `require_proxy`, then NetworkGuardError before any network call.
    With a proxy it must (a) parse, (b) answer and resolve to an exit IP, (c) not exit from the host's real IP,
    (d) not forward the host's IP in X-Forwarded-For / Forwarded / Via / X-Real-IP / Client-IP on a plain-HTTP echo
    request. An unknown host IP or a failed echo request only log a warning. A success is cached per proxy for
    `EXIT_COUNTRY_TTL`; failures never. Messages and logs never carry the proxy string or any header.
    """
    proxy = (proxy_url or "").strip()
    if not proxy:
        if require_proxy:
            raise NetworkGuardError("Прокси обязателен (browser.require_proxy), но для аккаунта он не задан: сессия не запущена.")
        return None
    try:
        parse_proxy(proxy)
    except ValueError:
        raise NetworkGuardError("Прокси задан в неверном формате: сессия не запущена.") from None
    hit = _READY_CACHE.get(proxy)
    if hit and _now() - hit[0] < EXIT_COUNTRY_TTL:
        return hit[1]
    geo = await resolve_exit_geo(proxy)
    if geo is None:
        raise NetworkGuardError("Прокси недоступен или не вернул внешний IP: сессия не запущена, чтобы не выйти с реального IP.")
    host_ip = await resolve_host_ip()
    if host_ip is None:
        logger.warning("Реальный IP хоста определить не удалось: проверка «прокси светит реальный IP» пропущена")
    elif _same_ip(host_ip, geo.ip):
        raise NetworkGuardError("Прокси не скрывает реальный IP хоста (внешний IP совпадает с IP машины): сессия не запущена.")
    elif await _proxy_leaks_host_ip(proxy, host_ip):
        raise NetworkGuardError("Прокси передаёт реальный IP хоста в заголовках (X-Forwarded-For/Via/...): сессия не запущена.")
    _READY_CACHE[proxy] = (_now(), geo)
    return geo


def session_geo_path(profile_dir: str | Path) -> Path:
    """The geo lock file of a browser profile: `<profile_dir>/session_geo.json`."""
    return Path(profile_dir) / SESSION_GEO_FILE


def read_session_geo(profile_dir: str | Path) -> dict[str, Any] | None:
    """The saved `{"country", "timezone", "updated_at"}` of a profile; None when missing, unreadable or not an object."""
    try:
        data = json.loads(session_geo_path(profile_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("session_geo.json не прочитан: {}", type(exc).__name__)
        return None
    return data if isinstance(data, dict) else None


def write_session_geo(profile_dir: str | Path, country: str | None, timezone: str | None) -> None:
    """Remember where the profile is used from: atomic write (tmp file + os.replace) of plain JSON, no secrets.

    Nothing is written when both values are unknown (an empty record would only hide a real one). A write error is
    logged, never raised: a session must not die because the lock file could not be saved.
    """
    country = normalize_country(country)
    if not country and not timezone:
        return
    path = session_geo_path(profile_dir)
    payload = {"country": country, "timezone": timezone, "updated_at": datetime.now(_timezone.utc).isoformat(timespec="seconds")}
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("Не удалось сохранить session_geo.json: {}", type(exc).__name__)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def check_geo_lock(profile_dir: str | Path, current_country: str | None, mode: str = "warn") -> GeoLockVerdict:
    """Compare the country the profile is opened from with the one it was last used from.

    "new" no record yet (nothing is written here); "unknown" the current country or the saved one is unknown, or the
    file is unreadable; "ok" same country; "changed" another country: a CRITICAL log, and with mode="block" also
    GeoLockError (same text).
    """
    if not session_geo_path(profile_dir).exists():
        return "new"
    saved = read_session_geo(profile_dir)
    saved_country = normalize_country(str(saved.get("country") or "")) if saved else None
    current = normalize_country(current_country)
    if not current or not saved_country:
        return "unknown"
    if current == saved_country:
        return "ok"
    message = (
        f"Гео-блокировка профиля {Path(profile_dir).name!r}: прошлая сессия была из страны {saved_country}, "
        f"сейчас выход из страны {current}. Вход в аккаунт с другой страны или континента выглядит как кража сессии "
        "и может привести к блокировке аккаунта."
    )
    logger.critical(message)
    if mode == "block":
        raise GeoLockError(message)
    return "changed"


async def check_account_exit(expected: str | None, proxy_url: str | None) -> tuple[CountryCheck, str | None]:
    """Verdict on the account's expected country against the exit country; no lookup when nothing is expected."""
    if not expected:
        return "skipped", None
    exit_country = await resolve_exit_country(proxy_url)
    return check_expected_country(expected, exit_country), exit_country


async def warn_if_exit_mismatch(account_key: str, expected: str | None, proxy_url: str | None) -> None:
    """Once per process and account: log a warning when the exit country differs from the expected one (never blocks)."""
    if not expected or account_key in _warned:
        return
    _warned.add(account_key)
    verdict, exit_country = await check_account_exit(expected, proxy_url)
    if verdict == "mismatch":
        logger.warning("Аккаунт {!r}: страна выхода прокси {} не совпадает с ожидаемой {}", account_key, exit_country, expected)
    elif verdict == "unknown":
        logger.warning("Аккаунт {!r}: страну выхода прокси определить не удалось (ожидается {})", account_key, expected)


__all__ = [
    "CountryCheck",
    "ExitGeo",
    "GeoLockError",
    "GeoLockVerdict",
    "NetworkGuardError",
    "check_account_exit",
    "check_expected_country",
    "check_geo_lock",
    "country_for_ip",
    "ensure_network_ready",
    "geoip_database_status",
    "normalize_country",
    "normalize_locale",
    "read_session_geo",
    "remember_exit_country",
    "resolve_exit_country",
    "resolve_exit_geo",
    "resolve_host_ip",
    "session_geo_path",
    "warn_if_exit_mismatch",
    "write_session_geo",
]
