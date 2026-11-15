"""Pure and DB helpers of the «Наблюдатель» screen (tab «Запуск»): the account that only checks reply visibility.

The observer never posts: it is excluded from every posting / warm-up / project selection in `database.py`
(`list_active_accounts`, `list_accounts`, `list_project_accounts`, `add_project_accounts`),
`youtube_bot.warm_candidates` and the orchestrator's `_is_usable`. Passwords and TOTP secrets never appear in
anything returned here. Its proxy is one of the proxy library (`Database.assign_proxy`), picked on the screen.
"""
from __future__ import annotations

from typing import Any

from config import AppConfig, parse_proxy
from database import Database

MAX_DELAY_SECONDS = 600.0


def proxy_host(proxy_url: str | None) -> str:
    """`host:port` of a stored proxy (no scheme, no credentials); '' without a proxy."""
    if not proxy_url:
        return ""
    try:
        parsed = parse_proxy(proxy_url)
    except ValueError:
        return "***"
    return parsed["server"].partition("://")[2] if parsed else ""


def observer_status(name: str | None, proxy_url: str | None, enabled: bool = True) -> tuple[str, str]:
    """Text and style for the «Наблюдатель» row: `● name · прокси host` / `○ не задан`."""
    return observer_status_text(name, proxy_host(proxy_url), enabled)


def observer_status_text(name: str | None, host: str, enabled: bool = True) -> tuple[str, str]:
    """Same row from the proxy's plain `host:port` ('' = no proxy)."""
    if not name:
        return "○ не задан", "warn"
    if not enabled:
        return f"○ {name} · проверки выключены", "warn"
    return (f"● {name} · прокси {host}", "") if host else (f"● {name} · без прокси", "warn")


def parse_delay(text: str) -> float:
    """Seconds typed by the user (`45`, `1,5`); ValueError with a user-facing message otherwise. 0 turns checks off."""
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError:
        raise ValueError("Нужно число секунд, например 45") from None
    if not 0 <= value <= MAX_DELAY_SECONDS:
        raise ValueError(f"Задержка от 0 до {MAX_DELAY_SECONDS:.0f} секунд")
    return value


async def assign_observer(db: Database, name: str) -> Any:
    """Make `name` the observer. A previous observer is not turned into a poster: it becomes a disabled worker."""
    previous = await db.get_observer_account()
    observer = await db.set_observer(name)
    if previous is not None and previous.name != observer.name:
        await db.upsert_account(previous.name, is_active=False)
    return observer


async def register_observer(db: Database, cfg: AppConfig, line: str) -> Any:
    """Register a `login:password:TOTP` line as a new account (own profile under `session_root`) and make it the observer.

    A line of an already known account is refused: promoting a posting account is the separate «существующий» action.
    """
    from modules.account_parser import parse_account_line
    from youtube_bot import _register_credentials_account

    parsed = parse_account_line(line)
    known = {a.email.lower() for a in await db.list_accounts(include_observer=True) if a.email}
    if await db.get_account(parsed.default_name) is not None or parsed.email.lower() in known:
        raise ValueError("такой аккаунт уже есть — выберите «существующий аккаунт»")
    await _register_credentials_account(db, cfg, line)
    return await assign_observer(db, parsed.default_name)


async def set_observer_enabled(db: Database, name: str, enabled: bool) -> Any:
    return await db.upsert_account(name, is_active=enabled)


async def remove_observer(db: Database) -> str | None:
    """Delete the observer's account row (its browser profile folder is left on disk); returns its name."""
    observer = await db.get_observer_account()
    if observer is None:
        return None
    await db.delete_account(observer.name)
    return observer.name


def check_counts_text(counts: dict[str, int]) -> str:
    """«виден 5 · не найден 1 · не проверен 2»; 'пока нет' when nothing was checked."""
    parts = [
        f"{word} {counts[key]}"
        for key, word in (("seen", "виден"), ("missing", "не найден"), ("unknown", "не проверен"))
        if counts.get(key)
    ]
    return " · ".join(parts) or "пока нет"


__all__ = [
    "assign_observer",
    "check_counts_text",
    "observer_status",
    "observer_status_text",
    "parse_delay",
    "proxy_host",
    "register_observer",
    "remove_observer",
    "set_observer_enabled",
]
