from __future__ import annotations

import asyncio
import re
import time
from functools import lru_cache
from typing import Any, Literal

from loguru import logger

CountryCheck = Literal["ok", "mismatch", "unknown", "skipped"]

EXIT_COUNTRY_TTL = 600.0  # seconds a resolved exit country of a proxy is reused
EXIT_LOOKUP_TIMEOUT = 20.0

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
_warned: set[str] = set()


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


def _lookup_exit_country(proxy_url: str | None) -> str | None:
    """Blocking: exit IP through the proxy (Camoufox's own `public_ip`, socks5h so DNS stays on the proxy), then the
    country from the GeoIP database Camoufox uses for `geoip=True`. The database is not downloaded here."""
    from camoufox.ip import public_ip

    from modules.session_heartbeat import curl_proxy_url

    lookup = getattr(public_ip, "__wrapped__", public_ip)  # Camoufox caches an IP per proxy forever; the TTL here is ours
    return _country_for_ip(lookup(curl_proxy_url(proxy_url)))


def _country_for_ip(ip: str) -> str | None:
    """Blocking: ISO country of an IP from Camoufox's GeoIP database; None when the database is missing or silent."""
    from camoufox.geolocation import get_mmdb_path, load_geoip_config

    config = load_geoip_config()
    path = get_mmdb_path("ipv6" if ":" in ip else "ipv4", config)
    if not path.exists():
        return None
    import maxminddb

    with maxminddb.open_database(str(path)) as reader:
        record: Any = reader.get(ip)
    for part in config["paths"]["iso_code"].split("."):
        record = record.get(part) if isinstance(record, dict) else None
    return normalize_country(str(record)) if record else None


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
    "check_account_exit",
    "check_expected_country",
    "country_for_ip",
    "geoip_database_status",
    "normalize_country",
    "normalize_locale",
    "remember_exit_country",
    "resolve_exit_country",
    "warn_if_exit_mismatch",
]
