"""Proxy library around the database: config.yaml sync and migration, and who goes out through which proxy.

Roles: the search proxy (`Database.search_proxy_url`) serves only the InnerTube collector (search, reading comments);
every account uses only its own proxy (`config.resolve_account_proxy`) or goes direct. There is no global fallback.
`describe_proxy_routes` puts that into words for the wizard.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from loguru import logger

from config import AccountSettings, ProxyUnavailableError, load_config, mask_proxy_url
from database import SEARCH_PROXY_KEY, AccountRecord, Database, ProxyRecord

CONFIG_MIGRATION_KEY = "config_proxies_migrated_at"

RouteKind = Literal["proxy", "direct", "unavailable"]


@dataclass(frozen=True)
class ProxyRoute:
    """Where requests of the search or of one account leave from. `text` is Russian, ready to show:
    «через прокси 1.2.3.4:8080 (PL)», «напрямую, с вашего IP», or why it cannot run."""

    kind: RouteKind
    text: str
    proxy_id: int | None = None


@dataclass(frozen=True)
class ProxyRoutes:
    search: ProxyRoute
    accounts: list[tuple[str, ProxyRoute]]


@dataclass
class ConfigMigrationReport:
    """What `migrate_config_proxies` did. Names only, never an address."""

    imported_accounts: list[str] = field(default_factory=list)  # yaml proxies moved onto DB accounts
    search_proxy_id: int | None = None  # `browser.proxy` became this search proxy
    assigned_global: list[str] = field(default_factory=list)  # accounts that got `browser.proxy` (had none)
    config_rewritten: bool = False
    config_backup: Path | None = None


def _proxy_text(record: ProxyRecord) -> str:
    country = record.country or record.last_exit_country
    label = f" «{record.label}»" if record.label else ""
    return f"через прокси{label} {record.address}" + (f" ({country})" if country else "")


async def describe_proxy_routes(db: Database, accounts: list[str] | None = None, active_only: bool = False) -> ProxyRoutes:
    """The search route and one route per account, for the wizard tables. Never raises for an unreadable proxy:
    that becomes kind "unavailable" with the reason (the bot itself refuses to start such an account / search).
    `accounts` limits the account routes to these names, `active_only` to the active accounts."""
    proxies = {record.id: record for record in await db.list_proxies()}
    search_id = next((record.id for record in proxies.values() if record.is_search), None)
    stored_search = await db.get_setting(SEARCH_PROXY_KEY)
    if search_id is not None:
        record = proxies[search_id]
        search = (
            ProxyRoute("proxy", _proxy_text(record), record.id)
            if not record.password_unavailable
            else ProxyRoute("unavailable", "прокси поиска не читается: поиск не запустится", record.id)
        )
    elif stored_search:
        search = ProxyRoute("unavailable", "прокси поиска удалён: выберите другой или «напрямую»")
    else:
        search = ProxyRoute("direct", "напрямую, с вашего IP")
    routes: list[tuple[str, ProxyRoute]] = []
    for account in await db.list_accounts(is_active=True if active_only else None):
        if accounts is not None and account.name not in accounts:
            continue
        routes.append((account.name, account_route(account, proxies.get(account.proxy_id or -1))))
    return ProxyRoutes(search, routes)


@dataclass(frozen=True)
class RouteSummary:
    """Two lines for the launch screen and the run log: where the search goes out and how the accounts post.
    `*_warn`: something goes out from the user's own IP or cannot start."""

    search: str
    search_warn: bool
    accounts: str
    accounts_warn: bool


def accounts_word(count: int) -> str:
    """«1 аккаунт», «3 аккаунта», «5 аккаунтов»."""
    tail = count % 100
    if 11 <= tail <= 14 or tail % 10 in (0, 5, 6, 7, 8, 9):
        word = "аккаунтов"
    else:
        word = "аккаунт" if tail % 10 == 1 else "аккаунта"
    return f"{count} {word}"


def summarize_routes(routes: ProxyRoutes, max_names: int = 3) -> RouteSummary:
    """«через прокси 1.2.3.4:8080 (PL)» / «⚠ напрямую, с вашего IP» for the search, and
    «5 аккаунтов через прокси, 1 с вашего IP (alice)» for the accounts (names of those that are not on a proxy)."""

    def names(items: list[str]) -> str:
        shown = ", ".join(items[:max_names])
        return f" ({shown}{f' и ещё {len(items) - max_names}' if len(items) > max_names else ''})"

    search = routes.search
    search_text = search.text if search.kind == "proxy" else f"⚠ {search.text}"
    via = [name for name, route in routes.accounts if route.kind == "proxy"]
    direct = [name for name, route in routes.accounts if route.kind == "direct"]
    broken = [name for name, route in routes.accounts if route.kind == "unavailable"]
    if not routes.accounts:
        return RouteSummary(search_text, search.kind != "proxy", "аккаунтов нет", False)
    parts = [f"{accounts_word(len(via))} через прокси"] if via else []
    if direct:
        parts.append(f"{len(direct) if via else accounts_word(len(direct))} с вашего IP{names(direct)}")
    if broken:
        parts.append(f"{len(broken)} не запустится — прокси не читается{names(broken)}")
    return RouteSummary(search_text, search.kind != "proxy", ", ".join(parts), bool(direct or broken))


async def log_proxy_routes(db: Database, accounts: list[str] | None = None) -> RouteSummary | None:
    """Log at the start of a run where the search goes out and how the active accounts post (`accounts`: only these).
    Never fatal: a failure to describe the routes is logged at debug level and gives None."""
    try:
        summary = summarize_routes(await describe_proxy_routes(db, accounts, active_only=accounts is None))
    except Exception as exc:
        logger.debug("Маршруты прокси не описаны: {}", type(exc).__name__)
        return None
    logger.log("WARNING" if summary.search_warn else "INFO", "Поиск: {}", summary.search)
    logger.log("WARNING" if summary.accounts_warn else "INFO", "Ответы: {}", summary.accounts)
    return summary


def account_route(account: AccountRecord, record: ProxyRecord | None = None) -> ProxyRoute:
    """Route of one account; `record` is its library proxy when known (for the address / country in the text)."""
    if account.proxy_unavailable:
        return ProxyRoute("unavailable", "прокси не читается: аккаунт не запустится", account.proxy_id)
    if account.proxy_url is None:
        return ProxyRoute("direct", "напрямую, с вашего IP")
    if record is not None:
        return ProxyRoute("proxy", _proxy_text(record), record.id)
    return ProxyRoute("proxy", f"через прокси {mask_proxy_url(account.proxy_url)}", account.proxy_id)


async def sync_config_accounts(db: Database, accounts: list[AccountSettings]) -> list[str]:
    """config.yaml -> DB without ever overwriting what the DB knows: missing accounts are created (with their yaml
    proxy and active flag); an existing account keeps its proxy and active flag, except that a proxy written in the
    yaml (explicitly, non-empty) is imported when the account has none. Returns the names that got a proxy."""
    imported: list[str] = []
    for acc in accounts:
        existing = await db.get_account(acc.name)
        explicit_proxy = acc.proxy_url.strip() if "proxy_url" in acc.model_fields_set and acc.proxy_url else None
        if existing is None:
            args: dict[str, Any] = {"name": acc.name, "session_path": acc.session_path, "is_active": acc.is_active}
            if explicit_proxy:
                args["proxy_url"] = explicit_proxy
            await db.upsert_account(**args)
            if explicit_proxy:
                imported.append(acc.name)
        elif explicit_proxy and existing.proxy_url is None and not existing.proxy_unavailable:
            try:
                await db.upsert_account(name=acc.name, proxy_url=explicit_proxy)
            except ValueError:
                logger.warning("Аккаунт {!r}: прокси из config.yaml не разобран и не импортирован", acc.name)
                continue
            imported.append(acc.name)
    return imported


def _yaml_account_names(raw: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    browser = raw.get("browser") if isinstance(raw.get("browser"), dict) else {}
    for items in (raw.get("accounts"), browser.get("accounts")):
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item["name"]))
    return names


def strip_config_proxies(raw: dict[str, Any]) -> bool:
    """Remove every proxy address from a parsed config.yaml in place: `proxy_url` of the account entries and
    `browser.proxy` (set to null). True when something was removed."""
    changed = False
    browser = raw.get("browser") if isinstance(raw.get("browser"), dict) else None
    if browser is not None and browser.get("proxy"):
        browser["proxy"] = None
        changed = True
    for items in (raw.get("accounts"), browser.get("accounts") if browser else None):
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and "proxy_url" in item:
                del item["proxy_url"]
                changed = True
    return changed


async def migrate_config_proxies(config_path: str | Path) -> ConfigMigrationReport:
    """Move proxies out of config.yaml into the library; safe to run on every start.

    `browser.proxy` becomes a library proxy and the search proxy; on the first migration it is also assigned
    explicitly to every account that has no own proxy (they used it implicitly before: the behaviour stays, it just
    becomes visible). Account entries' `proxy_url` go onto their DB accounts when those have none. Then the yaml loses
    all proxy addresses (a copy `config.yaml.bak-proxies-<date>` is kept). The database migrates itself on open.
    """
    path = Path(config_path)
    report = ConfigMigrationReport()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return report
    cfg = load_config(path)
    async with Database(cfg.app.db_path) as db:
        first = await db.get_setting(CONFIG_MIGRATION_KEY) is None
        yaml_names = _yaml_account_names(raw)
        report.imported_accounts = await sync_config_accounts(
            db, [acc for acc in cfg.browser.accounts if acc.name in yaml_names and acc.proxy_url]
        )
        if cfg.browser.proxy:
            try:
                record, _ = await db.add_proxy_url(cfg.browser.proxy)
            except ValueError:
                logger.warning("browser.proxy в config.yaml не разобран: перенесите прокси вручную")
            else:
                await db.set_search_proxy(record.id)
                report.search_proxy_id = record.id
                if first:
                    proxyless = [
                        acc.id for acc in await db.list_accounts() if acc.proxy_url is None and not acc.proxy_unavailable
                    ]
                    report.assigned_global = await db.assign_proxy(record.id, proxyless, reset_auth=False)
        if first:
            await db.set_setting(CONFIG_MIGRATION_KEY, datetime.now(timezone.utc).isoformat())
    if strip_config_proxies(raw):
        backup = path.with_name(f"{path.name}.bak-proxies-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
        report.config_rewritten, report.config_backup = True, backup
        logger.info("Прокси убраны из {} (копия: {}); они хранятся в библиотеке прокси", path.name, backup.name)
    return report


def run_config_proxy_migration(config_path: str | Path) -> ConfigMigrationReport | None:
    """Blocking start-up hook: `migrate_config_proxies`, never fatal (a failure is logged, the bot goes on)."""
    if not Path(config_path).is_file():
        return None
    try:
        return asyncio.run(migrate_config_proxies(config_path))
    except Exception as exc:
        logger.warning("Перенос прокси из config.yaml не выполнен: {}", type(exc).__name__)
        return None


__all__ = [
    "CONFIG_MIGRATION_KEY",
    "ConfigMigrationReport",
    "ProxyRoute",
    "ProxyRoutes",
    "RouteSummary",
    "ProxyUnavailableError",
    "account_route",
    "accounts_word",
    "describe_proxy_routes",
    "log_proxy_routes",
    "migrate_config_proxies",
    "run_config_proxy_migration",
    "strip_config_proxies",
    "summarize_routes",
    "sync_config_accounts",
]
