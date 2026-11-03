"""Proxy library logic behind the «Прокси» tab (`modules.ui.tab_proxies`): state, checks, assignment, labels.

Data: the library in `database.py`; check: `modules.proxy_check`. A password is never shown. Wizard helpers are
looked up in `modules.wizard` at call time, so tests patch them in one place.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules import tui
from modules import wizard as w
from modules.proxy_paste import ProxyDraft

Notice = tuple[str, str] | None

TYPE_NAMES = {"http": "HTTP", "https": "HTTPS", "socks5": "SOCKS5", "socks4": "SOCKS4"}

# Last «напрямую» check of this process (`check_direct`): shows the user's own IP next to «с вашего IP».
_direct: Any = None


@dataclass
class ProxyState:
    """One snapshot of the library for a screen: proxies (oldest first), accounts with their login state, the search
    proxy (None = direct) and whether a search proxy is set but its record is gone."""

    proxies: list[Any]
    accounts: list[tuple[Any, bool]]
    search: Any = None
    search_lost: bool = False

    def proxy(self, proxy_id: int | None) -> Any:
        return next((p for p in self.proxies if p.id == proxy_id), None)

    def on_proxy(self, proxy_id: int) -> list[Any]:
        return [acc for acc, _ in self.accounts if acc.proxy_id == proxy_id]


def load_state(config_path: Path) -> ProxyState:
    from database import SEARCH_PROXY_KEY

    async def read(db: Any) -> tuple[list[Any], str | None]:
        return await db.list_proxies(), await db.get_setting(SEARCH_PROXY_KEY)

    accounts = w._setup_accounts(config_path)  # seeds «default» from config.yaml like stage 1
    proxies, stored = w._pools_db(config_path, read)
    search = next((p for p in proxies if p.is_search), None)
    return ProxyState(proxies, accounts, search, bool(stored) and search is None)


# --------------------------------------------------------------------------- texts


def country_of(record: Any) -> str | None:
    return record.country or record.last_exit_country


def proxy_name(record: Any) -> str:
    """«[PL] 45.130.253.31:8000» without colors, for values and messages."""
    return f"[{country_of(record) or '??'}] {record.address}"


def type_name(scheme: str | None) -> str:
    return TYPE_NAMES.get(scheme or "", "тип ?")


def short_error(text: str | None) -> str:
    """A stored check error (our own Russian texts, `modules.proxy_check`) in a word or two."""
    low = (text or "").lower()
    if "407" in low or "логин" in low:
        return "логин/пароль"
    if "не ответил за" in low:
        return "таймаут"
    if "недоступен" in low:
        return "нет связи"
    if "расшифр" in low:
        return "не расшифрован"
    return "ошибка"


def ago(stamp: str | None) -> str:
    if not stamp:
        return ""
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    minutes = int((datetime.now(timezone.utc) - then).total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes < 60:
        return f"{minutes} мин назад"
    if minutes < 48 * 60:
        return f"{minutes // 60} ч назад"
    return f"{minutes // (24 * 60)} дн назад"


def browser_warning(record: Any) -> str | None:
    from modules.proxy_check import browser_proxy_warning

    return browser_proxy_warning(record.scheme, record.login)


def direct_text() -> str:
    """«с вашего IP 85.1.2.3 (RU)» after a direct check in this run, else «с вашего IP»."""
    if _direct is not None and getattr(_direct, "ok", False):
        return f"с вашего IP {_direct.exit_ip}" + (f" ({_direct.exit_country})" if _direct.exit_country else "")
    return "с вашего IP"


def account_warnings(acc: Any, logged_in: bool, record: Any) -> list[str]:
    """Why moving `acc` onto `record` is risky: another country, a signed-in account changing its IP, a proxy the
    browser cannot use."""
    if acc.proxy_id == record.id:
        return []
    found: list[str] = []
    country = country_of(record)
    if acc.expected_country and country and acc.expected_country.upper() != country.upper():
        found.append(f"ждёт {acc.expected_country.upper()}")
    if logged_in:
        found.append("вошёл: смена IP")
    if browser_warning(record):
        found.append("SOCKS5: браузер не пойдёт")
    return found


def account_proxy_text(acc: Any, state: ProxyState) -> str:
    if acc.proxy_unavailable:
        return "🔒 прокси не читается"
    record = state.proxy(acc.proxy_id)
    return proxy_name(record) if record is not None else "с вашего IP"


# --------------------------------------------------------------------------- checks


def _progress(done: int, total: int) -> None:
    w.safe_print(f"\r Проверено {done} из {total}…", end="" if done < total else "\n", flush=True)


async def _record(db: Any, proxy_id: int, result: Any) -> Any:
    return await db.record_proxy_check(
        proxy_id, result.ok, scheme=result.scheme, exit_ip=result.exit_ip, exit_country=result.exit_country,
        latency_ms=result.latency_ms, error=None if result.ok else result.message,
    )


def result_line(name: str, result: Any) -> tuple[str, str]:
    """(«ok»|«err», «[PL] 1.2.3.4:80 · HTTP · 210 мс · внешний IP 5.6.7.8» | «1.2.3.4:80 — причина»)."""
    if result.ok:
        return "ok", (f"{name} · {type_name(result.scheme)} · {result.latency_ms} мс · внешний IP {result.exit_ip}"
                      + ("" if result.exit_country else " · страна не определена"))
    return "err", f"{name} — {result.message}"


def check_all(config_path: Path, state: ProxyState) -> Notice:
    """«Проверить все»: every readable proxy in parallel plus the direct connection; results are stored."""
    global _direct
    from modules import proxy_check as pc

    records = [r for r in state.proxies if not r.password_unavailable]
    tui.status_line("info", f"Проверяем {len(records)} прокси и прямое соединение…")

    async def job(db: Any) -> tuple[list[Any], Any]:
        results, direct = await asyncio.gather(
            pc.check_proxies(records, on_done=_progress) if records else asyncio.sleep(0, []), pc.check_direct()
        )
        for record, result in zip(records, results):
            await _record(db, record.id, result)
        return results, direct

    results, _direct = w._pools_db(config_path, job)
    good = sum(1 for r in results if r.ok)
    you = f"ваш IP {_direct.exit_ip} ({_direct.exit_country or '??'})" if _direct.ok else "прямого выхода в интернет нет"
    if not records:
        return "info", f"Прокси в списке нет · {you}"
    bad = len(results) - good
    return ("ok" if not bad else "warn"), f"Проверено {len(results)}: работают {good}, не работают {bad} · {you}"


# --------------------------------------------------------------------------- add


def _country_may_be_password(raw: str, draft: ProxyDraft) -> bool:
    """`ip:port:login:pa:ss` with a last field that reads as a country: the parser took it as the country."""
    from modules.geo_guard import normalize_country

    text = raw.strip()
    if not draft.country or any(ch.isspace() for ch in text) or "@" in text:
        return False
    fields = text.split("://", 1)[-1].split(":")
    return len(fields) >= 5 and normalize_country(fields[-1]) == draft.country


def distribute(proxies: list[Any], accounts: list[Any]) -> tuple[list[tuple[Any, Any]], list[str]]:
    """One new proxy per account without a proxy; an account that expects a country gets only a proxy of that
    country. Returns ([(account, proxy)], names left out because no proxy of their country is free)."""
    free = list(proxies)
    plan: list[tuple[Any, Any]] = []
    skipped: list[str] = []
    for acc in sorted(accounts, key=lambda a: not a.expected_country):
        if not free:
            break
        if acc.expected_country:
            match = next((p for p in free if (country_of(p) or "").upper() == acc.expected_country.upper()), None)
            if match is None:
                skipped.append(acc.name)
                continue
        else:
            match = free[0]
        free.remove(match)
        plan.append((acc, match))
    return plan, skipped


def check_one(config_path: Path, record: Any) -> Notice:
    from modules import proxy_check as pc

    if record.password_unavailable:
        return "err", "Пароль не расшифровывается на этом компьютере — «Изменить» и вставьте прокси заново"
    tui.status_line("info", f"Проверяем {record.address}…")
    updated, result = w._pools_db(config_path, lambda db: pc.check_and_record(db, record.id))
    kind, text = result_line(proxy_name(updated), result)
    return kind, text + (f" · ⚠ {result.browser_warning}" if result.browser_warning else "")
