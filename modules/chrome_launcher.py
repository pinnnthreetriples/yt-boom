"""Google Chrome (Blink) as the browser engine of an account, driven through `patchright` next to Camoufox.

Everything here is blocking and cheap to import: the runner calls `ensure_chrome_executable` and
`prepare_chrome_profile` in a worker thread. `patchright` itself is imported only by the runner at launch time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from loguru import logger

CHROME_RELATIVE_PATH = Path("Google") / "Chrome" / "Application" / "chrome.exe"
_LOCK_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile")
_PROFILE_BUSY = "Профиль {} заблокирован другим работающим процессом Chrome (WinError 32). Закройте процесс перед стартом."
_CHROME_MISSING = "Google Chrome не найден и не смог установиться автоматически. Пожалуйста, установите Google Chrome вручную."

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


def prepare_chrome_profile(profile_dir: Path) -> None:
    """Make the profile folder ready for a start: remove stale lock files, quiet the first-run and crash UI.

    A lock file that cannot be deleted belongs to a Chrome that is still running on this profile (WinError 32):
    RuntimeError, the profile must not be shared. `Default/Preferences` is merged, not replaced: other keys stay.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)

    for lock_name in _LOCK_FILES:
        lock_file = profile_dir / lock_name
        try:
            if lock_file.exists() or lock_file.is_symlink():
                lock_file.unlink(missing_ok=True)
        except PermissionError:
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

    # No crash-restore dialog, no password manager, no Google sign-in interception (the Chrome profile sign-in)
    profile_prefs = prefs.get("profile")
    if not isinstance(profile_prefs, dict):
        profile_prefs = prefs["profile"] = {}
    profile_prefs["exit_type"] = "Normal"
    profile_prefs["exited_cleanly"] = True
    profile_prefs["password_manager_enabled"] = False
    profile_prefs["password_manager_leak_detection"] = False

    prefs["credentials_enable_service"] = False

    signin_prefs = prefs.get("signin")
    if not isinstance(signin_prefs, dict):
        signin_prefs = prefs["signin"] = {}
    signin_prefs["allowed"] = False
    signin_prefs["allowed_on_next_startup"] = False

    tmp_file = prefs_file.with_suffix(".tmp")
    try:
        tmp_file.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_file, prefs_file)
    except PermissionError:
        raise RuntimeError(_PROFILE_BUSY.format(profile_dir)) from None


def get_chrome_args(locale: str = "en-US") -> list[str]:
    return [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-quic",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--hide-crash-restore-bubble",
        "--disable-features=IdentityConsistency,SigninInterceptBubbleV2,SyncPromo,OptimizationHints,WebAuthentication,PasskeyUnlockManager",
        "--window-size=1440,900",
        f"--lang={locale}",
    ]


async def resolve_geo_timezone(
    proxy_url: str | None,
    fallback_country: str | None = None,
    fallback_locale: str | None = None,
) -> tuple[str | None, str]:
    """`(time zone, locale)` for a Chrome that exits through `proxy_url`.

    The country comes from the exit IP (`modules.geo_guard.resolve_exit_country`), else `fallback_country` (the
    account's own). The time zone is None for an unknown country: the system time zone applies. An explicit
    `fallback_locale` (the account's locale) wins over the country's default locale.
    """
    from modules.geo_guard import normalize_country, resolve_exit_country

    country = await resolve_exit_country(proxy_url)
    if not country:
        country = normalize_country(fallback_country)
    timezone_id = COUNTRY_TO_TIMEZONE.get(country or "")
    locale = fallback_locale or COUNTRY_TO_LOCALE.get(country or "") or DEFAULT_LOCALE
    return timezone_id, locale


__all__ = [
    "COUNTRY_TO_LOCALE",
    "COUNTRY_TO_TIMEZONE",
    "ensure_chrome_executable",
    "find_chrome_executable",
    "get_chrome_args",
    "prepare_chrome_profile",
    "resolve_geo_timezone",
]
