"""Google Chrome (Blink) as the browser engine of an account, driven through `patchright` next to Camoufox.

Everything here is blocking and cheap to import: the runner calls `ensure_chrome_executable` and
`prepare_chrome_profile` in a worker thread. `patchright` itself is imported only by the runner at launch time.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from config import parse_proxy

CHROME_RELATIVE_PATH = Path("Google") / "Chrome" / "Application" / "chrome.exe"
_LOCK_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile")
_PROFILE_BUSY = "Профиль {} заблокирован другим работающим процессом Chrome (WinError 32). Закройте процесс перед стартом."
_PROFILE_BUSY_LOG = "Профиль {} занят живым процессом Chrome: не удалось изменить {} ({}). Закройте Chrome этого аккаунта."
_CHROME_MISSING ="Google Chrome не найден и не смог установиться автоматически. Пожалуйста, установите Google Chrome вручную."

# Time zone of the exit IP's country; a country that is not listed keeps the system time zone of the machine.
COUNTRY_TO_TIMEZONE: dict[str, str] = {
    "US": "America/New_York", "CA": "America/Toronto", "MX": "America/Mexico_City", "BR": "America/Sao_Paulo",
    "AR": "America/Argentina/Buenos_Aires", "CL": "America/Santiago", "CO": "America/Bogota", "PE": "America/Lima",
    "GB": "Europe/London", "IE": "Europe/Dublin", "DE": "Europe/Berlin", "FR": "Europe/Paris",
    "ES": "Europe/Madrid", "PT": "Europe/Lisbon", "IT": "Europe/Rome", "NL": "Europe/Amsterdam",
    "BE": "Europe/Brussels", "CH": "Europe/Zurich", "AT": "Europe/Vienna", "PL": "Europe/Warsaw",
    "CZ": "Europe/Prague", "SK": "Europe/Bratislava", "HU": "Europe/Budapest", "RO": "Europe/Bucharest",
    "BG": "Europe/Sofia", "GR": "Europe/Athens", "SE": "Europe/Stockholm", "NO": "Europe/Oslo",
    "DK": "Europe/Copenhagen", "FI": "Europe/Helsinki", "EE": "Europe/Tallinn", "LV": "Europe/Riga",
    "LT": "Europe/Vilnius", "UA": "Europe/Kyiv", "BY": "Europe/Minsk", "RU": "Europe/Moscow",
    "TR": "Europe/Istanbul", "GE": "Asia/Tbilisi", "AM": "Asia/Yerevan", "AZ": "Asia/Baku",
    "KZ": "Asia/Almaty", "UZ": "Asia/Tashkent", "IL": "Asia/Jerusalem", "AE": "Asia/Dubai",
    "SA": "Asia/Riyadh", "IN": "Asia/Kolkata", "ID": "Asia/Jakarta", "TH": "Asia/Bangkok",
    "VN": "Asia/Ho_Chi_Minh", "PH": "Asia/Manila", "MY": "Asia/Kuala_Lumpur", "SG": "Asia/Singapore",
    "HK": "Asia/Hong_Kong", "TW": "Asia/Taipei", "CN": "Asia/Shanghai", "KR": "Asia/Seoul",
    "JP": "Asia/Tokyo", "AU": "Australia/Sydney", "NZ": "Pacific/Auckland", "ZA": "Africa/Johannesburg",
    "EG": "Africa/Cairo", "NG": "Africa/Lagos",
}
# Default browser locale of that country (the account's own locale, when set, wins).
COUNTRY_TO_LOCALE: dict[str, str] = {
    "US": "en-US", "CA": "en-CA", "MX": "es-MX", "BR": "pt-BR", "AR": "es-AR", "CL": "es-CL", "CO": "es-CO",
    "PE": "es-PE", "GB": "en-GB", "IE": "en-IE", "DE": "de-DE", "FR": "fr-FR", "ES": "es-ES", "PT": "pt-PT",
    "IT": "it-IT", "NL": "nl-NL", "BE": "nl-BE", "CH": "de-CH", "AT": "de-AT", "PL": "pl-PL", "CZ": "cs-CZ",
    "SK": "sk-SK", "HU": "hu-HU", "RO": "ro-RO", "BG": "bg-BG", "GR": "el-GR", "SE": "sv-SE", "NO": "nb-NO",
    "DK": "da-DK", "FI": "fi-FI", "EE": "et-EE", "LV": "lv-LV", "LT": "lt-LT", "UA": "uk-UA", "BY": "ru-BY",
    "RU": "ru-RU", "TR": "tr-TR", "GE": "ka-GE", "AM": "hy-AM", "AZ": "az-AZ", "KZ": "ru-KZ", "UZ": "uz-UZ",
    "IL": "he-IL", "AE": "ar-AE", "SA": "ar-SA", "IN": "en-IN", "ID": "id-ID", "TH": "th-TH", "VN": "vi-VN",
    "PH": "en-PH", "MY": "ms-MY", "SG": "en-SG", "HK": "zh-HK", "TW": "zh-TW", "CN": "zh-CN", "KR": "ko-KR",
    "JP": "ja-JP", "AU": "en-AU", "NZ": "en-NZ", "ZA": "en-ZA", "EG": "ar-EG", "NG": "en-NG",
}
DEFAULT_LOCALE = "en-US"

# Window geometry: a stable per-account size inside these ranges, the old fixed size when the account is unknown.
DEFAULT_WINDOW_SIZE = (1440, 900)
WINDOW_WIDTH_RANGE = (1280, 1560)
WINDOW_HEIGHT_RANGE = (800, 940)
_SPI_GETWORKAREA = 0x0030

# Noise of the reported position around the exit IP's city, so that all accounts of one city do not share a point.
GEO_NOISE_KM = (1.0, 2.0)
GEO_ACCURACY_M = 100
_KM_PER_DEGREE = 111.0

# ONE `--disable-features=` only: Chrome keeps the LAST such switch and drops the earlier ones (checked on Chrome 157).
# patchright puts its own `--disable-features=` list before the `args` of the caller, so that list is repeated here
# (minus MediaRouter, which patchright keeps enabled, and its Edge-only entries); otherwise the launch loses it.
_DISABLED_FEATURES = (
    "IdentityConsistency", "SigninInterceptBubbleV2", "SyncPromo", "OptimizationHints", "PasskeyUnlockManager",
    "CalculateNativeWinOcclusion",  # Windows native occlusion tracking: a covered window must not become `hidden`
    # Mirror of the baseline of patchright (chromiumSwitches), re-check it after a patchright upgrade
    "AvoidUnnecessaryBeforeUnloadCheckSync", "DestroyProfileOnBrowserClose", "DialMediaRouteProvider",
    "GlobalMediaControls", "HttpsUpgrades", "LensOverlay", "PaintHolding", "ThirdPartyStoragePartitioning",
    "BlockOriginHeaderModificationOnRedirect", "AvoidCorsURLLoaderRestartOnRedirect", "Translate", "AutoDeElevate",
    "NetworkTimeServiceQuerying", "AimEnabled",
)


def chrome_candidate_paths() -> list[Path]:
    """Standard Windows install locations of Google Chrome, per-machine first."""
    candidates: list[Path] = []
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / CHROME_RELATIVE_PATH)
    return candidates


def find_chrome_executable() -> Path | None:
    return next((path for path in chrome_candidate_paths() if path.is_file()), None)


def ensure_chrome_executable() -> Path:
    """Path of the installed Google Chrome; installs it through `patchright install chrome` when it is missing.

    Blocking (a subprocess): call it with `asyncio.to_thread`. Raises RuntimeError when Chrome is still missing.
    """
    found = find_chrome_executable()
    if found is not None:
        return found
    logger.info("Системный Google Chrome не найден. Выполняется автоматическая установка через patchright...")
    result = subprocess.run([sys.executable, "-m", "patchright", "install", "chrome"], capture_output=True, text=True)
    if result.returncode != 0:
        logger.debug("patchright install chrome: {}", (result.stderr or result.stdout or "").strip()[-500:])
        raise RuntimeError(_CHROME_MISSING)
    found = find_chrome_executable()
    if found is None:  # the installer succeeded but Chrome is not in a standard place: patchright finds it by channel
        logger.warning("patchright установил Chrome, но chrome.exe не найден в стандартных папках")
        return Path("chrome")
    return found


def accept_languages_for(locale: str) -> str:
    """`intl.accept_languages` of a profile with this locale: `de-DE` -> `de-DE,de,en-US,en`, `en-GB` -> `en-GB,en`."""
    locale = (locale or DEFAULT_LOCALE).replace("_", "-")
    base = locale.split("-")[0]
    languages = [locale]
    if base != locale:
        languages.append(base)
    if base != "en":
        languages += ["en-US", "en"]
    return ",".join(dict.fromkeys(languages))


def prepare_chrome_profile(profile_dir: Path, locale: str = "en-US") -> None:
    """Make the profile folder ready for a start: remove stale lock files, quiet the first-run and crash UI.

    A lock file that cannot be deleted belongs to a Chrome that is still running on this profile (WinError 32):
    a warning is logged and RuntimeError raised, the profile must not be shared. `Default/Preferences` is merged,
    not replaced: other keys stay, except the ones this function owns. Languages (`intl.accept_languages`,
    `spellcheck.dictionaries`) always follow `locale`, so that the language of the host system never reaches
    `navigator.languages`; WebRTC and DNS prediction are switched off in the profile as well as by the command line.
    """
    locale = (locale or DEFAULT_LOCALE).replace("_", "-")
    profile_dir.mkdir(parents=True, exist_ok=True)

    for lock_name in _LOCK_FILES:
        lock_file = profile_dir / lock_name
        try:
            if lock_file.exists() or lock_file.is_symlink():
                lock_file.unlink(missing_ok=True)
        except PermissionError as exc:
            logger.warning(_PROFILE_BUSY_LOG, profile_dir, lock_name, exc)
            raise RuntimeError(_PROFILE_BUSY.format(profile_dir)) from None

    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs_file = default_dir / "Preferences"
    prefs: dict[str, Any] = {}
    if prefs_file.is_file():
        try:
            loaded = json.loads(prefs_file.read_text(encoding="utf-8"))
            prefs = loaded if isinstance(loaded, dict) else {}
        except Exception:
            prefs = {}

    def section(name: str) -> dict[str, Any]:
        value = prefs.get(name)
        if not isinstance(value, dict):
            value = prefs[name] = {}
        return value

    # No crash-restore dialog, no password manager, no Google sign-in interception (the Chrome profile sign-in)
    profile_prefs = section("profile")
    profile_prefs["exit_type"] = "Normal"
    profile_prefs["exited_cleanly"] = True
    profile_prefs["password_manager_enabled"] = False
    profile_prefs["password_manager_leak_detection"] = False

    prefs["credentials_enable_service"] = False

    signin_prefs = section("signin")
    signin_prefs["allowed"] = False
    signin_prefs["allowed_on_next_startup"] = False

    # Languages of the profile follow the locale of the account, not the one of the host system
    section("intl")["accept_languages"] = accept_languages_for(locale)
    section("spellcheck")["dictionaries"] = [locale]

    # WebRTC: no UDP outside the proxy (the command line says the same: two independent locks)
    webrtc_prefs = section("webrtc")
    webrtc_prefs["ip_handling_policy"] = "disable_non_proxied_udp"
    webrtc_prefs["multiple_routes_enabled"] = False
    webrtc_prefs["nonproxied_udp_enabled"] = False

    # No DNS prefetch / preconnect: 2 = "never predict network actions"
    section("net")["network_prediction_options"] = 2
    section("dns_prefetching")["enabled"] = False

    tmp_file = prefs_file.with_suffix(".tmp")
    try:
        tmp_file.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_file, prefs_file)
    except PermissionError as exc:
        logger.warning(_PROFILE_BUSY_LOG, profile_dir, prefs_file.name, exc)
        try:
            tmp_file.unlink(missing_ok=True)
        except OSError:
            pass
        raise RuntimeError(_PROFILE_BUSY.format(profile_dir)) from None


def get_screen_work_area() -> tuple[int, int] | None:
    """`(width, height)` of the work area of the primary monitor (the screen without the taskbar); None if unknown.

    What a page sees as `screen.availWidth` / `screen.availHeight`. Windows only: elsewhere, and on any failure of
    the system call, None (the caller then does not clamp).
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if not ctypes.windll.user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return None
        width, height = rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        return None
    return (width, height) if width > 0 and height > 0 else None


def get_window_size(account_name: str | None = None) -> tuple[int, int]:
    """Window size of an account: stable (sha256 of the name) inside the ranges, never larger than the work area.

    Without a name the old fixed size. A work area smaller than the size (a 1366x768 screen) clamps the window to it.
    """
    if account_name:
        digest = hashlib.sha256(account_name.encode("utf-8")).digest()
        min_w, max_w = WINDOW_WIDTH_RANGE
        min_h, max_h = WINDOW_HEIGHT_RANGE
        width = min_w + int.from_bytes(digest[:4], "big") % (max_w - min_w + 1)
        height = min_h + int.from_bytes(digest[4:8], "big") % (max_h - min_h + 1)
    else:
        width, height = DEFAULT_WINDOW_SIZE
    work_area = get_screen_work_area()
    if work_area is not None:
        width, height = min(width, work_area[0]), min(height, work_area[1])
    return width, height


def socks5_host_resolver_rule(proxy_url: str | None) -> str | None:
    """`--host-resolver-rules` that forbids every local DNS lookup while the proxy is SOCKS5; None for other proxies.

    Only the host of the proxy and loopback are excluded: Chrome has to resolve the proxy itself locally (an IP
    literal needs the exclusion too). No quotes around the value: it is one argument of a list (with quotes Chrome 157
    ignores the whole rule). The proxy credentials never get here, only the host.
    """
    try:
        proxy = parse_proxy(proxy_url)
    except ValueError:
        return None
    if not proxy:
        return None
    scheme, _, host_port = proxy["server"].partition("://")
    if not scheme.lower().startswith("socks5"):
        return None
    host = host_port.rsplit(":", 1)[0].strip("[]")
    excluded = dict.fromkeys(["127.0.0.1", "localhost", host])
    return "MAP * ~NOTFOUND , " + ", ".join(f"EXCLUDE {name}" for name in excluded)


def get_chrome_args(
    locale: str = "en-US", account_name: str | None = None, proxy_url: str | None = None, http2: bool = True,
) -> list[str]:
    """Command line on top of patchright's own. `http2=False` (`--disable-http2`, Chrome speaks only HTTP/1.1) is a
    fallback for a proxy that stalls Google over HTTP/2: it is not a usual Chrome fingerprint, so it is never the default."""
    width, height = get_window_size(account_name)
    args = [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-quic",
        # No UDP outside the proxy: WebRTC exposes neither the local nor the real public address (IPv6 included).
        # `--enforce-webrtc-ip-permission-check` is not in the Chrome 157 binary (inert there), kept for older builds
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--enforce-webrtc-ip-permission-check",
        "--dns-prefetch-disable",  # not in the Chrome 157 binary either; the profile prefs do the work
        "--hide-crash-restore-bubble",
        # A covered or minimized window keeps its timers and rendering; patchright already passes the first three
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--disable-features=" + ",".join(_DISABLED_FEATURES),
        f"--window-size={width},{height}",
        f"--lang={locale}",
    ]
    rule = socks5_host_resolver_rule(proxy_url)
    if rule:
        args.append(f"--host-resolver-rules={rule}")
    if not http2:
        args.append("--disable-http2")
    return args


@dataclass(frozen=True)
class ChromeGeo:
    """What the exit IP says about the browser: time zone (IANA), locale, country, position of the city."""

    timezone: str | None
    locale: str
    country: str | None
    geolocation: dict[str, float] | None


def city_geolocation(latitude: float, longitude: float, account_name: str | None = None) -> dict[str, float]:
    """Playwright `geolocation` near the city of the exit IP; a stable offset of 1-2 km per account name.

    Without a name no offset. dlat = km / 111, dlon = km / (111 * cos(lat)); radius and angle come from sha256(name).
    """
    d_lat = d_lon = 0.0
    if account_name:
        digest = hashlib.sha256(account_name.encode("utf-8")).digest()
        low, high = GEO_NOISE_KM
        radius_km = low + (high - low) * int.from_bytes(digest[8:12], "big") / 0xFFFFFFFF
        angle = 2 * math.pi * int.from_bytes(digest[12:16], "big") / 0xFFFFFFFF
        d_lat = radius_km * math.sin(angle) / _KM_PER_DEGREE
        d_lon = radius_km * math.cos(angle) / (_KM_PER_DEGREE * max(math.cos(math.radians(latitude)), 0.01))
    return {
        "latitude": round(max(-90.0, min(90.0, latitude + d_lat)), 6),
        "longitude": round(((longitude + d_lon + 180.0) % 360.0) - 180.0, 6),
        "accuracy": float(GEO_ACCURACY_M),
    }


async def resolve_chrome_geo(
    proxy_url: str | None,
    fallback_country: str | None = None,
    fallback_locale: str | None = None,
    account_name: str | None = None,
) -> ChromeGeo:
    """Geo of a Chrome that exits through `proxy_url`.

    The exit IP (`modules.geo_guard.resolve_exit_geo`) gives the country, the exact time zone of the city and the
    position. Without them: the account's own `fallback_country` and the zone of that country (`COUNTRY_TO_TIMEZONE`),
    else None, the system time zone then applies. An explicit `fallback_locale` wins over the locale of the country.
    """
    from modules.geo_guard import normalize_country, resolve_exit_geo

    geo = await resolve_exit_geo(proxy_url)
    country = (geo.country if geo else None) or normalize_country(fallback_country)
    timezone_id = (geo.timezone if geo else None) or COUNTRY_TO_TIMEZONE.get(country or "")
    locale = fallback_locale or COUNTRY_TO_LOCALE.get(country or "") or DEFAULT_LOCALE
    geolocation = None
    if geo is not None and geo.latitude is not None and geo.longitude is not None:
        geolocation = city_geolocation(geo.latitude, geo.longitude, account_name)
    return ChromeGeo(timezone_id, locale, country, geolocation)


async def resolve_geo_timezone(
    proxy_url: str | None,
    fallback_country: str | None = None,
    fallback_locale: str | None = None,
) -> tuple[str | None, str]:
    """`(time zone, locale)` of `resolve_chrome_geo`, for the callers that need nothing else."""
    geo = await resolve_chrome_geo(proxy_url, fallback_country, fallback_locale)
    return geo.timezone, geo.locale


__all__ = [
    "COUNTRY_TO_LOCALE",
    "COUNTRY_TO_TIMEZONE",
    "ChromeGeo",
    "accept_languages_for",
    "city_geolocation",
    "ensure_chrome_executable",
    "find_chrome_executable",
    "get_chrome_args",
    "get_screen_work_area",
    "get_window_size",
    "prepare_chrome_profile",
    "resolve_chrome_geo",
    "resolve_geo_timezone",
    "socks5_host_resolver_rule",
]
