from __future__ import annotations

import argparse
import asyncio
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from loguru import logger

from config import AppConfig, load_config, mask_proxy_url, resolve_account_proxy
from database import AccountRecord, ActivityLogCreate, Database, RecordNotFoundError
from modules.collector import CollectorError
from modules.account_parser import looks_like_credentials, parse_account_line
from modules.account_warmer import AccountWarmer, WarmUpResult
from modules.google_authenticator import GoogleAuthenticator, GoogleAuthError, read_session_owner, write_session_owner
from modules.interaction_simulator import InteractionConfig, InteractionSimulator
from modules.lifecycle_engine import AccountLifecycleEngine
from modules.process_guard import bind_to_kill_on_close_job
from modules.processor import DeepSeekProcessor, resolve_reply_text, should_paraphrase
from modules.session_heartbeat import check_account_heartbeat, check_session_offline, HeartbeatStatus
from modules.youtube_collector import YouTubeCollector, extract_video_id
from modules.youtube_runner import YouTubeRunner


from modules.logger_config import configure_windows_encoding, setup_bot_logging


def format_author(author: str | None) -> str:
    if not author:
        return "@unknown"
    return author if author.startswith("@") else f"@{author}"


def setup_cli_logging(level: str = "INFO", log_file: str | None = None) -> None:
    setup_bot_logging(level=level, log_file=log_file)


async def interactive_login(config_path: str = "config.yaml", account_name: str | None = None) -> None:
    """Launch Camoufox headfully on YouTube so user can log in once to save cookies.

    The browser starts with the same options as automated runs (proxy, prefs, pinned fingerprint). When the
    window is closed with a live LOGIN_INFO cookie, the account is marked `authenticated`.
    """
    from camoufox.async_api import AsyncCamoufox

    cfg = load_config(config_path)
    cfg.browser.headless = False
    db = Database(cfg.app.db_path)
    await db.connect()
    await db.init_schema()

    session_dir: Path
    proxy_url: str | None = None
    acc: AccountRecord | None = None

    if account_name:
        acc = await db.get_account(account_name)
        if acc is None:
            cfg_acc = next((a for a in getattr(cfg.browser, "accounts", []) if a.name == account_name), None)
            if cfg_acc:
                session_dir = Path(cfg_acc.session_path)
                proxy_url = cfg_acc.proxy_url
            else:
                session_dir = cfg.browser.session_root / account_name
                proxy_url = cfg.browser.proxy
        else:
            session_dir = Path(acc.session_path)
            proxy_url = acc.proxy_url
    else:
        session_dir = cfg.browser.session_root / "youtube"
        proxy_url = cfg.browser.proxy

    session_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Запуск браузера Camoufox для авторизации в YouTube (аккаунт: {!r})...", account_name or "default")
    logger.info("Папка сохранения сессии: {}", session_dir)
    logger.info("Пожалуйста, выполните вход в свой аккаунт YouTube в открывшемся окне. После входа закройте браузер.")

    runner = YouTubeRunner(
        cfg.browser,
        _resolve_youtube_target(cfg),
        session_dir=session_dir,
        proxy=resolve_account_proxy(proxy_url, cfg.browser.proxy),
        account_name=account_name or "default",
    )
    opts = runner.launch_options()
    authenticator = GoogleAuthenticator(InteractionSimulator())
    logged_in = False

    try:
        async with AsyncCamoufox(**opts) as context:
            pages = getattr(context, "pages", [])
            page = pages[0] if pages else await context.new_page()
            await page.goto("https://www.youtube.com", wait_until="domcontentloaded")
            logger.info("Окно браузера открыто. Ожидание завершения авторизации пользователем...")
            try:
                while any(not p.is_closed() for p in getattr(context, "pages", [])):
                    logged_in = logged_in or await authenticator.is_session_authenticated(context)
                    await asyncio.sleep(0.5)
                logged_in = logged_in or await authenticator.is_session_authenticated(context)
            except Exception:
                pass
        logger.success("Сессия авторизации успешно сохранена в {}", session_dir)

        upsert_args: dict[str, Any] = {"name": account_name or "default", "session_path": str(session_dir)}
        if acc is None:
            upsert_args["proxy_url"] = proxy_url  # an existing account keeps its proxy and active flag
        saved = await db.upsert_account(**upsert_args)
        if logged_in:
            await db.mark_account_authenticated(saved.id)
            if saved.email:
                write_session_owner(session_dir, saved.email)
            logger.info("Аккаунт {!r}: вход подтверждён (LOGIN_INFO), статус authenticated", saved.name)
        else:
            logger.warning("Аккаунт {!r}: после закрытия окна кука LOGIN_INFO не найдена, статус не изменён", saved.name)
    finally:
        await db.close()


async def add_account_cli(
    name: str,
    session_path: str | None = None,
    proxy_url: str | None = None,
    config_path: str = "config.yaml",
) -> None:
    """Add or update an account in the database."""
    cfg = load_config(config_path)
    db = Database(cfg.app.db_path)
    await db.connect()
    await db.init_schema()
    try:
        eff_session = session_path or str(cfg.browser.session_root / name)
        account = await db.upsert_account(
            name=name,
            session_path=eff_session,
            proxy_url=proxy_url,
            is_active=True,
        )
        print(
            f"[OK] Account {account.name!r} registered successfully "
            f"(id={account.id}, session={account.session_path}, proxy={mask_proxy_url(account.proxy_url) or 'direct'})"
        )
    finally:
        await db.close()


def _resolve_youtube_target(cfg: Any) -> Any:
    for target in cfg.browser.targets:
        if target.name == "youtube" or "youtube.com" in target.base_url:
            return target
    from config import TargetSettings
    return TargetSettings(
        name="youtube",
        base_url="https://www.youtube.com",
        item_url_template="https://www.youtube.com/watch?v={external_id}",
        submit_input_selector="#reply-dialog #contenteditable-root",
        submit_button_selector="#reply-dialog #submit-button button",
        confirmation_selector="ytd-comment-view-model",
    )


async def _register_credentials_account(
    db: Database,
    cfg: AppConfig,
    spec: str,
    name: str | None = None,
    session_path: str | None = None,
    proxy_url: str | None = None,
) -> AccountRecord:
    """Register `email:password:totp[...]`. A known account keeps its session path, proxy and active flag unless told otherwise."""
    parsed = parse_account_line(spec, proxy=proxy_url)
    account_name = (name or parsed.default_name).strip()
    existing = await db.get_account(account_name)
    upsert_args: dict[str, Any] = {
        "name": account_name,
        "email": parsed.email,
        "password": parsed.password,
        "totp_secret": parsed.totp_secret,
        "recovery_email": parsed.recovery_email,
    }
    if session_path or existing is None or existing.name != account_name:
        upsert_args["session_path"] = session_path or str(cfg.browser.session_root / account_name)
    if parsed.proxy is not None:
        upsert_args["proxy_url"] = parsed.proxy
    return await db.upsert_account(**upsert_args)


def mask_email(email: str | None) -> str:
    """`b***@gmail.com`: enough to tell accounts apart in a table."""
    if not email or "@" not in email:
        return "-"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


async def add_credentials_account_cli(
    spec: str,
    name: str | None = None,
    session_path: str | None = None,
    proxy_url: str | None = None,
    config_path: str = "config.yaml",
) -> bool:
    """`--add-account email:password:totp_secret[:recovery_email]`: register the account with status `pending`."""
    cfg = load_config(config_path)
    async with Database(cfg.app.db_path) as db:
        try:
            account = await _register_credentials_account(db, cfg, spec, name, session_path, proxy_url)
        except ValueError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return False
        print(
            f"[OK] Account {account.name!r} registered (id={account.id}, status={account.auth_status}, "
            f"session={account.session_path}, proxy={mask_proxy_url(account.proxy_url) or 'direct'}). "
            f"Next: --auth-account {account.name}"
        )
        return True


async def _authenticate_account(db: Database, cfg: AppConfig, account: AccountRecord) -> bool:
    """Sign the account in. `failed` is stored only when Google refused (GoogleAuthError); a browser, proxy or
    network failure is logged and leaves the stored status alone."""
    if not account.email or not account.password:
        logger.error("У аккаунта {!r} не заданы email/пароль: добавьте его через --add-account email:password:totp", account.name)
        return False
    simulator = InteractionSimulator(InteractionConfig(key_delay_ms_min=50, key_delay_ms_max=100))
    owner = read_session_owner(account.session_path)
    force_relogin = owner is not None and owner != account.email.lower()  # the saved session is another account's
    runner = YouTubeRunner(
        cfg.browser,
        _resolve_youtube_target(cfg),
        session_dir=account.session_path,
        account_name=account.name,
        simulator=simulator,
        proxy=resolve_account_proxy(account.proxy_url, cfg.browser.proxy),
    )
    authenticated = False
    new_status: str | None = None
    try:
        await runner.start()
        authenticator = GoogleAuthenticator(simulator)
        authenticated = await authenticator.authenticate(
            runner.context,
            account.email,
            account.password,
            account.totp_secret,
            account.recovery_email,
            force_relogin=force_relogin,
        )
        new_status = "authenticated"
        if authenticator.detected_country:
            await db.set_account_geo_country(account.id, authenticator.detected_country)
    except GoogleAuthError as exc:
        logger.error("Авторизация {!r} не удалась: {}", account.name, exc)
        new_status = "failed"
    except Exception as exc:
        logger.error("Авторизация {!r} прервана ошибкой браузера или сети, статус не изменён: {}", account.name, exc)
    finally:
        try:
            await runner.close()
        except Exception as exc:
            logger.debug("Ошибка при закрытии браузера аккаунта {!r}: {}", account.name, exc)
    if new_status == "authenticated":
        await db.mark_account_authenticated(account.id)
    elif new_status is not None:
        await db.set_account_auth_status(account.id, new_status)
    if authenticated:
        write_session_owner(account.session_path, account.email)
        try:
            await db.sync_account_fingerprint(account.id, Path(account.session_path))
        except Exception as exc:
            logger.debug("Не удалось синхронизировать отпечаток аккаунта {!r}: {}", account.name, exc)
        await _intro_warm_up(db, cfg, account)
    return authenticated


INTRO_WARM_UP_SECONDS = 60.0


async def _intro_warm_up(db: Database, cfg: AppConfig, account: AccountRecord) -> None:
    """A minute of organic browsing right after sign-in; an account past quarantine and ageing needs none."""
    fresh = await db.get_account(account.id)
    if fresh is None or AccountLifecycleEngine.calculate_stage(fresh)[0] not in ("quarantine", "aging"):
        return
    res = await AccountWarmer(cfg.browser).warm_up(fresh, duration_seconds=INTRO_WARM_UP_SECONDS, db=db)
    if not res.success:
        logger.warning("Вводный прогрев аккаунта {!r} не удался: {}", account.name, res.details)


async def _find_account(db: Database, identifier: str) -> AccountRecord | None:
    account = await db.get_account(identifier)
    if account is not None:
        return account
    wanted = identifier.strip().lower()
    return next((a for a in await db.list_accounts() if a.email and a.email.lower() == wanted), None)


async def auth_account_cli(identifier: str, config_path: str = "config.yaml", headless: bool | None = None) -> bool:
    """`--auth-account <name_or_email>`: sign the account into YouTube (proxy and fingerprint come from the account)."""
    cfg = load_config(config_path)
    if headless is not None:
        cfg.browser.headless = headless
    async with Database(cfg.app.db_path) as db:
        account = await _find_account(db, identifier)
        if account is None:
            print(f"[ERROR] Account {identifier!r} not found. Add it with --add-account email:password:totp", file=sys.stderr)
            return False
        ok = await _authenticate_account(db, cfg, account)
        print(f"[{'OK' if ok else 'FAILED'}] {account.name}: {'authenticated' if ok else 'authentication failed'}")
        return ok


def _pause_between_accounts(account: AccountRecord) -> float:
    """Seconds to wait before signing the next account in: longer when it shares the machine's own IP."""
    return random.uniform(20.0, 45.0) if account.proxy_url is None else random.uniform(5.0, 15.0)


@dataclass
class AccountImportResult:
    """Outcome of registering a batch of `email:password:totp` lines; line numbers are 1-based, secrets never stored here."""

    added: list[AccountRecord] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)  # (line number, reason): the account already exists
    errors: list[tuple[int, str]] = field(default_factory=list)  # (line number, reason): the line was not accepted


async def register_account_lines(
    db: Database, cfg: AppConfig, lines: Iterable[str], skip_existing: bool = False
) -> AccountImportResult:
    """Register every account line (blank lines and `#` comments are ignored); one bad line never stops the rest.

    With `skip_existing` a line whose account is already in the database (or repeated earlier in the batch) is
    reported as skipped instead of overwriting its credentials.
    """
    result = AccountImportResult()
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            if skip_existing:
                name = parse_account_line(line).default_name
                if await db.get_account(name) is not None:
                    result.skipped.append((number, "аккаунт уже есть"))
                    continue
            result.added.append(await _register_credentials_account(db, cfg, line))
        except (ValueError, RecordNotFoundError) as exc:
            result.errors.append((number, str(exc)))  # the line itself holds a password: it is never echoed
    return result


async def import_accounts_file_cli(
    path: str, config_path: str = "config.yaml", headless: bool | None = None, force: bool = False
) -> bool:
    """`--import-accounts-file`: register every line of the file, then sign the accounts in one by one.

    Accounts that are already `authenticated` are skipped unless `force`; one failing line or account never stops the rest.
    """
    try:
        lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        print(f"[ERROR] Cannot read {path}: {exc}", file=sys.stderr)
        return False
    cfg = load_config(config_path)
    if headless is not None:
        cfg.browser.headless = headless
    async with Database(cfg.app.db_path) as db:
        imported = await register_account_lines(db, cfg, lines)
        registered = {account.id: account for account in imported.added}
        failed_lines = len(imported.errors)
        for number, reason in imported.errors:
            print(f"[ERROR] line {number}: {reason}", file=sys.stderr)
        print(f"[OK] Registered {len(registered)} account(s), skipped {failed_lines} invalid line(s)")

        queue = [a for a in registered.values() if force or a.auth_status != "authenticated"]
        if len(queue) < len(registered):
            print(f"[OK] {len(registered) - len(queue)} account(s) already authenticated: skipped (--force signs them in again)")
        failed_auth = 0
        for index, account in enumerate(queue):
            if index:
                await asyncio.sleep(_pause_between_accounts(account))
            logger.info("Авторизация аккаунта {!r} ({}/{})", account.name, index + 1, len(queue))
            if not await _authenticate_account(db, cfg, account):
                failed_auth += 1
        print(f"[{'OK' if not failed_auth else 'FAILED'}] Authenticated {len(queue) - failed_auth} of {len(queue)}")
    return not failed_lines and not failed_auth


async def heartbeat_cli(identifier: str | None, config_path: str = "config.yaml", online: bool = False) -> bool:
    """Session status from the profile's LOGIN_INFO cookie; with `online` also asks YouTube over HTTP (no browser)."""
    cfg = load_config(config_path)
    async with Database(cfg.app.db_path) as db:
        if identifier and identifier != "ALL":
            account = await _find_account(db, identifier)
            if account is None:
                print(f"[ERROR] Account {identifier!r} not found.", file=sys.stderr)
                return False
            accounts = [account]
        else:
            accounts = await db.list_active_accounts()
            if not accounts:
                accounts = await db.list_accounts()

        if not accounts:
            print("[INFO] No accounts found in database.")
            return False

        print("\n" + "=" * 95)
        print("                           ACCOUNT SESSION HEARTBEAT")
        print("=" * 95)
        header = f"{'Account':<16} | {'Status':<8} | {'Days Left':<10} | {'Expires At (UTC)':<22} | {'Details'}"
        print(header)
        print("-" * 95)
        all_alive = True
        for acc in accounts:
            if online:
                res = await check_account_heartbeat(acc, default_proxy=cfg.browser.proxy, locale=cfg.browser.locale)
            else:
                res = check_session_offline(acc.session_path, acc.name)
            status_str = "ALIVE" if res.is_alive else "EXPIRED"
            if not res.is_alive:
                all_alive = False
            days_str = f"{res.days_remaining:.1f}d" if res.days_remaining is not None else "-"
            exp_str = res.expires_at or "-"
            print(f"{acc.name:<16} | {status_str:<8} | {days_str:<10} | {exp_str:<22} | {res.details}")
        print("=" * 95 + "\n")
        return all_alive


async def warm_up_cli(
    identifier: str,
    config_path: str = "config.yaml",
    duration: float = 45.0,
    target_video: str | None = None,
    headless: bool | None = None,
) -> bool:
    """Run humanized browser warm-up for an account."""
    cfg = load_config(config_path)
    if headless is not None:
        cfg.browser.headless = headless
    async with Database(cfg.app.db_path) as db:
        account = await _find_account(db, identifier)
        if account is None:
            print(f"[ERROR] Account {identifier!r} not found.", file=sys.stderr)
            return False
        warmer = AccountWarmer(cfg.browser)
        res = await warmer.warm_up(account, duration_seconds=duration, target_video_url=target_video, db=db)
        print(f"[{'OK' if res.success else 'FAILED'}] {res.account_name}: {res.details}")
        return res.success


async def warm_quarantined_cli(
    config_path: str = "config.yaml", duration: float = 45.0, headless: bool | None = None
) -> bool:
    """`--warm-quarantined`: organic warm-up, one account after another, for every account in quarantine or ageing."""
    cfg = load_config(config_path)
    if headless is not None:
        cfg.browser.headless = headless
    async with Database(cfg.app.db_path) as db:
        targets = warm_candidates(await db.list_active_accounts())
        if not targets:
            print("[INFO] No authenticated accounts in quarantine or aging.")
            return True
        results = await warm_accounts(db, cfg, targets, duration)
        return all(res.success for res in results)


def warm_candidates(accounts: Iterable[AccountRecord], names: Iterable[str] | None = None) -> list[AccountRecord]:
    """Authenticated accounts in quarantine or ageing (the ones a warm-up is for); `names` narrows the pick."""
    wanted = None if names is None else set(names)
    return [
        a
        for a in accounts
        if (wanted is None or a.name in wanted)
        and a.auth_status == "authenticated"
        and AccountLifecycleEngine.calculate_stage(a)[0] in ("quarantine", "aging")
    ]


async def warm_accounts(
    db: Database, cfg: AppConfig, targets: list[AccountRecord], duration: float = 45.0, report: Any = print
) -> list[WarmUpResult]:
    """Warm `targets` up one after another (a pause between them); `report` gets one line per account."""
    warmer = AccountWarmer(cfg.browser)
    results: list[WarmUpResult] = []
    for index, account in enumerate(targets):
        if index:
            await asyncio.sleep(_pause_between_accounts(account))
        res = await warmer.warm_up(account, duration_seconds=duration, db=db)
        results.append(res)
        report(f"[{'OK' if res.success else 'FAILED'}] {res.account_name}: {res.details}")
    return results


async def warm_selected_cli(
    config_path: str,
    names: list[str],
    duration: float = 45.0,
    headless: bool | None = None,
    report: Any = print,
) -> list[WarmUpResult]:
    """Wizard «прогрев выбранных»: the accounts called `names`, in that order.

    An account that has not signed in yet is signed in first (`_authenticate_account`, which also runs the 60 s
    intro warm-up of a fresh account); the others go through the same `warm_accounts` as `--warm-quarantined`.
    An account past ageing needs no warm-up and comes back as a successful "skipped" result.
    """
    cfg = load_config(config_path)
    if headless is not None:
        cfg.browser.headless = headless
    results: list[WarmUpResult] = []
    async with Database(cfg.app.db_path) as db:
        for index, name in enumerate(names):
            account = await db.get_account(name)
            if account is not None and index and (account.auth_status != "authenticated" or warm_candidates([account])):
                await asyncio.sleep(_pause_between_accounts(account))
            if account is None:
                results.append(WarmUpResult(name, False, None, 0.0, "account not found"))
            elif account.auth_status != "authenticated":
                ok = await _authenticate_account(db, cfg, account)
                details = "signed in, intro warm-up done" if ok else "sign-in failed"
                results.append(WarmUpResult(account.name, ok, None, 0.0, details))
            elif warm_candidates([account]):
                results.extend(await warm_accounts(db, cfg, [account], duration, report=lambda _line: None))
            else:
                results.append(WarmUpResult(account.name, True, None, 0.0, "skipped: past quarantine and ageing"))
            report(f"[{'OK' if results[-1].success else 'FAILED'}] {results[-1].account_name}: {results[-1].details}")
    return results


async def lifecycle_cli(config_path: str = "config.yaml") -> None:
    """`--lifecycle`: account ages, stages, quarantine hours left and today's comment quota."""
    cfg = load_config(config_path)
    async with Database(cfg.app.db_path) as db:
        accounts = await db.list_accounts()
        now = datetime.now(timezone.utc)
        width = 100
        print("\n" + "=" * width)
        print("                                 ACCOUNT LIFECYCLE")
        print("=" * width)
        if not accounts:
            print("  No accounts found in database.")
        else:
            print(
                f"{'Account':<16} | {'Auth':<13} | {'Age':>7} | {'Stage':<11} | {'Quarantine':>10} | "
                f"{'Today':>7} | {'Errors':>6} | {'Warm-ups':>8}"
            )
            print("-" * width)
            for acc in accounts:
                view = AccountLifecycleEngine.summarize(acc, now)
                stage = view.stage
                if view.age_hours is None:
                    age, left, quota = "-", "-", "-"
                else:
                    age = f"{view.age_hours:.1f}h"
                    left = f"{view.quarantine_left:.1f}h"
                    quota = f"{view.quota_done}/{view.quota_limit}"
                print(
                    f"{acc.name:<16} | {acc.auth_status:<13} | {age:>7} | {stage:<11} | {left:>10} | "
                    f"{quota:>7} | {acc.consecutive_errors:>6} | {acc.warmup_sessions_done:>8}"
                )
        print("=" * width + "\n")


async def print_accounts_table(config_path: str = "config.yaml") -> None:
    """Pretty-print a table of all accounts with stats from DB."""
    cfg = load_config(config_path)
    db = Database(cfg.app.db_path)
    await db.connect()
    await db.init_schema()
    try:
        accounts = await db.list_accounts()
        if not accounts and hasattr(cfg.browser, "accounts"):
            for acc in cfg.browser.accounts:
                await db.upsert_account(
                    name=acc.name,
                    session_path=acc.session_path,
                    proxy_url=acc.proxy_url,
                    is_active=acc.is_active,
                )
            accounts = await db.list_accounts()

        print("\n" + "=" * 80)
        print("                        YOUTUBE ACCOUNTS OVERVIEW")
        print("=" * 80)
        if not accounts:
            print("  No accounts found in database or configuration.")
        else:
            header = (
                f"{'ID':<4} | {'Name':<16} | {'Status':<8} | {'Auth':<13} | {'Geo':<12} | {'Replies':<8} | "
                f"{'Email':<24} | {'Proxy':<30} | {'Session Path'}"
            )
            print(header)
            print("-" * 110)
            for acc in accounts:
                status = "ACTIVE" if acc.is_active else "DISABLED"
                proxy_display = mask_proxy_url(acc.proxy_url) if acc.proxy_url else "direct (no proxy)"
                geo_display = acc.geo_country or "-"
                if len(proxy_display) > 30:
                    proxy_display = proxy_display[:27] + "..."  # the masked form: never cut credentials open
                row = (
                    f"{acc.id:<4} | "
                    f"{acc.name:<16} | "
                    f"{status:<8} | "
                    f"{acc.auth_status:<13} | "
                    f"{geo_display:<12} | "
                    f"{acc.total_replies:<8} | "
                    f"{mask_email(acc.email):<24} | "
                    f"{proxy_display:<30} | "
                    f"{acc.session_path}"
                )
                print(row)
            print("-" * 110)
            print(f"Total Accounts: {len(accounts)} (Active: {sum(1 for a in accounts if a.is_active)})")
        print("=" * 80 + "\n")
    finally:
        await db.close()


async def run_video_pool_cli(
    config_path: str = "config.yaml",
    videos_file: str | Path | None = None,
    concurrency: int | None = None,
    account_mode: str | None = None,
    dry_run: bool = False,
    headless: bool | None = None,
) -> int:
    """Reply under the video pool with all active accounts; returns the number of replies posted.

    concurrency / account_mode default to limits.max_concurrent_sessions / limits.account_mode.
    """
    from modules.multi_account_orchestrator import MultiAccountOrchestrator

    cfg = load_config(config_path)
    if headless is not None:
        cfg.browser.headless = headless

    db = Database(cfg.app.db_path)
    await db.connect()
    await db.init_schema()

    orchestrator = MultiAccountOrchestrator(
        config_or_path=cfg,
        db=db,
        concurrency=concurrency or cfg.limits.max_concurrent_sessions,
        dry_run=dry_run,
        headless=headless,
        account_mode=account_mode,
    )
    try:
        return await orchestrator.run_video_pool(videos_file=videos_file)
    finally:
        await orchestrator.close()


async def list_sub_comments(video_url: str, config_path: str = "config.yaml", limit: int = 10) -> None:
    """Fetch and list comments under comments for a YouTube video."""
    cfg = load_config(config_path)
    try:
        async with YouTubeCollector(cfg.collector) as collector:
            sub_comments = await collector.fetch_video_sub_comments(video_url, max_sub_comments=limit)
    except CollectorError as exc:
        logger.error("Не удалось получить ответы под комментариями: {}", exc)
        return
    except Exception as exc:
        logger.error("Непредвиденная ошибка при получении ответов: {}", exc)
        return

    print(f"\nFound {len(sub_comments)} comments under comments for video: {video_url}\n" + "=" * 70)
    for i, sc in enumerate(sub_comments, 1):
        print(f"[{i}] Sub-Comment ID: {sc.comment_id}")
        print(f"    Author: {format_author(sc.author)}")
        print(f"    Text: {sc.text}")
        if sc.parent_text:
            p_auth = format_author(sc.parent_author) if sc.parent_author else "unknown"
            print(f"    Parent Comment (by {p_auth}): {sc.parent_text[:80]}...")
        print(f"    Direct URL: {sc.permalink}")
        print("-" * 70)


async def run_reply_bot(
    video_url: str,
    *,
    config_path: str = "config.yaml",
    limit: int = 3,
    target_mode: str | None = None,
    dry_run: bool = False,
    reply_mode: str | None = None,
    custom_reply: str | None = None,
    headless: bool | None = None,
    account_name: str | None = None,
) -> None:
    """Main workflow: fetch comments, generate replies via DeepSeek or custom text, post via Camoufox."""
    cfg = load_config(config_path)
    if headless is not None:
        cfg.browser.headless = headless
    db = Database(cfg.app.db_path)
    await db.connect()
    await db.init_schema()

    rng = random.Random()
    effective_mode = "custom" if custom_reply else (reply_mode or getattr(cfg.processor, "reply_mode", "ai"))
    effective_target_mode = target_mode or getattr(cfg.collector, "target_mode", "first_reply")

    if cfg.browser.headless:
        logger.info("Режим браузера: Автономный / Фоновый (без видимого окна)")
    else:
        logger.info("Режим браузера: С окном (фоновая защита от засыпания активна)")

    # Find matching target from config or synthesize youtube target
    yt_target = None
    for t in cfg.browser.targets:
        if t.name == "youtube" or "youtube.com" in t.base_url:
            yt_target = t
            break
    if yt_target is None:
        from config import TargetSettings
        yt_target = TargetSettings(
            name="youtube",
            base_url="https://www.youtube.com",
            item_url_template="https://www.youtube.com/watch?v={external_id}",
            submit_input_selector="#reply-dialog #contenteditable-root",
            submit_button_selector="#reply-dialog #submit-button button",
            confirmation_selector="ytd-comment-view-model",
        )

    node = await db.upsert_test_node(yt_target.name, yt_target.base_url)

    collector = YouTubeCollector(cfg.collector)
    processor = (
        DeepSeekProcessor(cfg.deepseek, cfg.processor, rng=rng)
        if effective_mode == "ai" or should_paraphrase(cfg.deepseek, cfg.processor)
        else None
    )
    simulator = InteractionSimulator(rng=rng)

    session_dir: str | Path | None = None
    proxy_url: str | None = None
    resolved_acc_name: str = "default"
    acc_record = None

    if account_name:
        acc_record = await db.get_account(account_name)
        if acc_record is None:
            cfg_acc = next((a for a in getattr(cfg.browser, "accounts", []) if a.name == account_name), None)
            if cfg_acc:
                session_dir = cfg_acc.session_path
                proxy_url = cfg_acc.proxy_url
                resolved_acc_name = cfg_acc.name
            else:
                session_dir = str(cfg.browser.session_root / account_name)
                proxy_url = cfg.browser.proxy
                resolved_acc_name = account_name
            acc_record = await db.upsert_account(
                name=resolved_acc_name,
                session_path=str(session_dir),
                proxy_url=proxy_url,
                is_active=True,
            )
        else:
            session_dir = acc_record.session_path
            proxy_url = acc_record.proxy_url
            resolved_acc_name = acc_record.name

    runner_kwargs: dict[str, Any] = {
        "simulator": simulator,
        "rng": rng,
    }
    if session_dir is not None:
        runner_kwargs["session_dir"] = session_dir
    if proxy_url is not None:
        runner_kwargs["proxy"] = proxy_url
    if account_name is not None:
        runner_kwargs["account_name"] = resolved_acc_name

    runner = YouTubeRunner(cfg.browser, yt_target, **runner_kwargs)

    video_id = extract_video_id(video_url) or "unknown"

    if effective_mode == "custom" and not (custom_reply and custom_reply.strip()) and not [r for r in getattr(cfg.processor, "custom_replies", []) if r and r.strip()]:
        logger.error(
            "Включен режим 'Свой текст' (custom), но текст не передан через --custom-reply и список 'custom_replies' в настройках пуст!"
        )
        await collector.close()
        await db.close()
        return

    try:
        items: list[Any] = []
        if effective_target_mode in ("popular_parent", "sub_comment", "mixed", "first_reply", "first_sub_comment"):
            logger.info("Сбор комментариев для {} (режим={})", video_url, effective_target_mode)
            try:
                items = await collector.fetch_popular_comments(video_url, limit=limit * 2, target_mode=effective_target_mode)
            except Exception as exc:
                logger.debug("Сбор популярных комментариев завершился ошибкой, резервный метод: {}", exc)
        else:
            logger.info("Сбор комментариев для {} (режим=first_reply)", video_url)

        if not items:
            items = await collector.collect_from_videos([video_url], max_items=limit * 2)

        if not items:
            logger.warning("Не найдено подходящих комментариев по фильтрам для {}", video_url)
            return

        processed = 0
        for item in items:
            if processed >= limit:
                break

            if dry_run:
                # In dry-run mode, check if already answered
                if await db.has_replied_to(item.external_id):
                    logger.info("Пропуск уже обработанного комментария: {}", item.external_id)
                    continue
                payload = None
            else:
                # Atomic reservation before 2-5s DeepSeek API call so concurrent runs cannot pick the same item
                payload = await db.reserve_payload(
                    node_id=node.id,
                    source_ref=item.external_id,
                    language=item.language,
                    source_text=item.text,
                )
                if payload is None:
                    logger.info("Пропуск уже зарезервированного комментария: {}", item.external_id)
                    continue

            logger.info("🎯 Цель: {} (лайков: {:,}) — {!r}", format_author(item.author), item.like_count, item.text[:70])
            if item.parent_text:
                p_auth = format_author(item.parent_author) if item.parent_author else "unknown"
                logger.info("   ↳ в ответ на {}: {!r}", p_auth, item.parent_text[:60])

            # Generate response via DeepSeek or resolve custom text
            try:
                if effective_mode == "custom":
                    reply_text = resolve_reply_text(
                        item,
                        custom_reply=custom_reply,
                        custom_replies=getattr(cfg.processor, "custom_replies", None),
                        rng=rng,
                    )
                    if processor is not None:
                        reply_text = await processor.paraphrase(reply_text)
                    logger.success("💬 НАШ ОТВЕТ: «{}»", reply_text)
                else:
                    reply_text = await processor.generate(item)
                    logger.success("💬 НАШ ОТВЕТ: «{}»", reply_text)
            except Exception as exc:
                db_err_label = "Custom reply resolution" if effective_mode == "custom" else "LLM generation"
                ru_err_label = "Подготовка собственного текста" if effective_mode == "custom" else "Генерация ответа нейросетью"
                if payload is not None:
                    await db.mark_payload_failed(payload.id, f"{db_err_label} failed: {exc}")
                await db.log_activity(
                    ActivityLogCreate(
                        video_id=video_id,
                        video_title=None,
                        video_url=video_url,
                        target_id=item.external_id,
                        target_author=item.author,
                        target_text=item.text,
                        reply_text=None,
                        mode="dry_run" if dry_run else "live",
                        status="failed",
                        error=f"{db_err_label} failed: {exc}",
                        duration_sec=None,
                        payload_id=payload.id if payload else None,
                    )
                )
                logger.error("{} завершилась ошибкой для {}: {}", ru_err_label, item.external_id, exc)
                continue

            if dry_run:
                # In dry-run mode, simulate browser interaction without polluting database with 'submitted' status
                sub_res = await runner.run(
                    reply_text,
                    external_id=item.external_id,
                    payload_id=None,
                    dry_run=True,
                )
                status = "simulated" if sub_res.success else "failed"
                await db.log_activity(
                    ActivityLogCreate(
                        video_id=video_id,
                        video_title=None,
                        video_url=video_url,
                        target_id=item.external_id,
                        target_author=item.author,
                        target_text=item.text,
                        reply_text=reply_text,
                        mode="dry_run",
                        status=status,
                        error=sub_res.error if not sub_res.success else None,
                        duration_sec=sub_res.duration_seconds,
                        payload_id=None,
                    )
                )
                if sub_res.success:
                    logger.info("[Тестовый режим] Симуляция ответа успешна (ID={})", sub_res.remote_id)
                    processed += 1
                else:
                    logger.error("[Тестовый режим] Ошибка симуляции ответа: {}", sub_res.error)
            else:
                try:
                    payload = await db.update_payload_text(payload.id, reply_text)
                    sub_res = await runner.run(
                        payload.text,
                        external_id=item.external_id,
                        payload_id=payload.id,
                        dry_run=False,
                    )
                except Exception as exc:
                    await db.mark_payload_failed(payload.id, f"Execution failed: {exc}")
                    await db.log_activity(
                        ActivityLogCreate(
                            video_id=video_id,
                            video_title=None,
                            video_url=video_url,
                            target_id=item.external_id,
                            target_author=item.author,
                            target_text=item.text,
                            reply_text=reply_text,
                            mode="live",
                            status="failed",
                            error=str(exc),
                            duration_sec=None,
                            payload_id=payload.id,
                        )
                    )
                    logger.exception("Ошибка при отправке ответа для {}: {}", item.external_id, exc)
                    continue

                if sub_res.success:
                    if acc_record is not None:
                        await db.increment_account_stats(acc_record.id, replies=1)
                    await db.mark_payload_submitted(payload.id, sub_res.remote_id)
                    await db.log_activity(
                        ActivityLogCreate(
                            video_id=video_id,
                            video_title=None,
                            video_url=video_url,
                            target_id=item.external_id,
                            target_author=item.author,
                            target_text=item.text,
                            reply_text=reply_text,
                            mode="live",
                            status="success",
                            error=None,
                            duration_sec=sub_res.duration_seconds,
                            payload_id=payload.id,
                        )
                    )
                    logger.success("Ответ успешно опубликован и сохранен в базе (ID={})", sub_res.remote_id)
                    processed += 1
                else:
                    await db.mark_payload_failed(payload.id, sub_res.error or "submission failed")
                    await db.log_activity(
                        ActivityLogCreate(
                            video_id=video_id,
                            video_title=None,
                            video_url=video_url,
                            target_id=item.external_id,
                            target_author=item.author,
                            target_text=item.text,
                            reply_text=reply_text,
                            mode="live",
                            status="failed",
                            error=sub_res.error or "submission failed",
                            duration_sec=sub_res.duration_seconds,
                            payload_id=payload.id,
                        )
                    )
                    logger.error("Не удалось отправить ответ: {}", sub_res.error)

            if processed < limit:
                pause = rng.uniform(cfg.limits.pause_min_seconds, cfg.limits.pause_max_seconds)
                logger.info("Пауза {:.1f}с перед следующим комментарием...", pause)
                await asyncio.sleep(pause)

        logger.info("Обработка видео завершена: отправлено {} ответов.", processed)
    finally:
        await runner.close()
        await collector.close()
        if processor is not None:
            await processor.close()
        await db.close()


async def run_multi_video_pipeline(
    query: str,
    *,
    config_path: str = "config.yaml",
    min_views: int = 0,
    max_views: int | None = None,
    max_videos: int = 5,
    comments_per_video: int = 2,
    target_mode: str = "popular_parent",
    dry_run: bool = False,
    reply_mode: str | None = None,
    custom_reply: str | None = None,
    headless: bool | None = None,
    account_name: str | None = None,
    max_age_hours: int | None = None,
) -> None:
    """
    Search videos by query, iterate through matched videos,
    and reply to popular comments under each video.

    max_age_hours (default: collector.max_age_hours) keeps only videos published within that many hours.
    """
    cfg = load_config(config_path)
    max_age_hours = max_age_hours or cfg.collector.max_age_hours
    if headless is not None:
        cfg.browser.headless = headless
    db = Database(cfg.app.db_path)
    await db.connect()
    await db.init_schema()

    rng = random.Random()
    effective_mode = "custom" if custom_reply else (reply_mode or getattr(cfg.processor, "reply_mode", "ai"))
    effective_target_mode = target_mode or getattr(cfg.collector, "target_mode", "first_reply")

    if cfg.browser.headless:
        logger.info("Режим браузера: Автономный / Фоновый (без видимого окна)")
    else:
        logger.info("Режим браузера: С окном (фоновая защита от засыпания активна)")

    yt_target = None
    for t in cfg.browser.targets:
        if t.name == "youtube" or "youtube.com" in t.base_url:
            yt_target = t
            break
    if yt_target is None:
        from config import TargetSettings
        yt_target = TargetSettings(
            name="youtube",
            base_url="https://www.youtube.com",
            item_url_template="https://www.youtube.com/watch?v={external_id}",
            submit_input_selector="#reply-dialog #contenteditable-root",
            submit_button_selector="#reply-dialog #submit-button button",
            confirmation_selector="ytd-comment-view-model",
        )

    node = await db.upsert_test_node(yt_target.name, yt_target.base_url)

    collector = YouTubeCollector(cfg.collector)
    processor = (
        DeepSeekProcessor(cfg.deepseek, cfg.processor, rng=rng)
        if effective_mode == "ai" or should_paraphrase(cfg.deepseek, cfg.processor)
        else None
    )
    simulator = InteractionSimulator(rng=rng)

    session_dir: str | Path | None = None
    proxy_url: str | None = None
    resolved_acc_name: str = "default"
    acc_record = None

    if account_name:
        acc_record = await db.get_account(account_name)
        if acc_record is None:
            cfg_acc = next((a for a in getattr(cfg.browser, "accounts", []) if a.name == account_name), None)
            if cfg_acc:
                session_dir = cfg_acc.session_path
                proxy_url = cfg_acc.proxy_url
                resolved_acc_name = cfg_acc.name
            else:
                session_dir = str(cfg.browser.session_root / account_name)
                proxy_url = cfg.browser.proxy
                resolved_acc_name = account_name
            acc_record = await db.upsert_account(
                name=resolved_acc_name,
                session_path=str(session_dir),
                proxy_url=proxy_url,
                is_active=True,
            )
        else:
            session_dir = acc_record.session_path
            proxy_url = acc_record.proxy_url
            resolved_acc_name = acc_record.name

    runner_kwargs: dict[str, Any] = {
        "simulator": simulator,
        "rng": rng,
    }
    if session_dir is not None:
        runner_kwargs["session_dir"] = session_dir
    if proxy_url is not None:
        runner_kwargs["proxy"] = proxy_url
    if account_name is not None:
        runner_kwargs["account_name"] = resolved_acc_name

    runner = YouTubeRunner(cfg.browser, yt_target, **runner_kwargs)

    if effective_mode == "custom" and not (custom_reply and custom_reply.strip()) and not [r for r in getattr(cfg.processor, "custom_replies", []) if r and r.strip()]:
        logger.error(
            "Включен режим 'Свой текст' (custom), но текст не передан через --custom-reply и список 'custom_replies' в настройках пуст!"
        )
        await collector.close()
        await db.close()
        return

    try:
        logger.info(
            "Поиск видео по запросу {!r} [просмотров: {}..{}, не старше: {} ч, макс. видео: {}]",
            query,
            min_views,
            max_views,
            max_age_hours or "∞",
            max_videos,
        )
        videos = await collector.search_videos(
            query=query,
            min_views=min_views,
            max_views=max_views,
            limit=max_videos,
            max_age_hours=max_age_hours,
        )
        if not videos:
            logger.warning("Не найдено видео по запросу {!r} с заданными критериями", query)
            return

        logger.info("Найдено подходящих видео: {}. Запуск процесса ответов...", len(videos))
        total_replies = 0

        for idx, vid_info in enumerate(videos, start=1):
            vid_id = vid_info["video_id"]
            vid_title = vid_info.get("title", f"Video {vid_id}")
            vid_url = vid_info.get("url", f"https://www.youtube.com/watch?v={vid_id}")
            views_cnt = vid_info.get("view_count", 0)

            logger.info(
                "▶ [{}/{}] Видео: {!r} (ID: {}, просмотров: {:,})",
                idx,
                len(videos),
                vid_title,
                vid_id,
                views_cnt,
            )

            # Fetch popular comments
            try:
                items = await collector.fetch_popular_comments(
                    vid_url,
                    limit=comments_per_video * 2,
                    target_mode=effective_target_mode,
                )
            except Exception as exc:
                logger.error("Ошибка сбора популярных комментариев для {}: {}", vid_id, exc)
                items = []

            if not items:
                try:
                    items = await collector.collect_from_videos([vid_url], max_items=comments_per_video * 2)
                except Exception as exc:
                    logger.debug("Резервный сбор комментариев завершился ошибкой для {}: {}", vid_id, exc)

            if not items:
                logger.warning("Не найдено подходящих комментариев для видео {}", vid_id)
                continue

            processed_for_video = 0
            for item in items:
                if processed_for_video >= comments_per_video:
                    break

                if dry_run:
                    if await db.has_replied_to(item.external_id):
                        logger.info("Пропуск уже обработанного комментария: {}", item.external_id)
                        continue
                    payload = None
                else:
                    payload = await db.reserve_payload(
                        node_id=node.id,
                        source_ref=item.external_id,
                        language=item.language,
                        source_text=item.text,
                    )
                    if payload is None:
                        logger.info("Пропуск уже зарезервированного комментария: {}", item.external_id)
                        continue

                logger.info(
                    "🎯 Цель: {} (лайков: {:,}) — {!r}",
                    format_author(item.author),
                    item.like_count,
                    item.text[:70],
                )
                if item.parent_text:
                    p_auth = format_author(item.parent_author) if item.parent_author else "unknown"
                    logger.info("   ↳ в ответ на {}: {!r}", p_auth, item.parent_text[:60])

                try:
                    if effective_mode == "custom":
                        reply_text = resolve_reply_text(
                            item,
                            custom_reply=custom_reply,
                            custom_replies=getattr(cfg.processor, "custom_replies", None),
                            rng=rng,
                        )
                        if processor is not None:
                            reply_text = await processor.paraphrase(reply_text)
                        logger.success("💬 НАШ ОТВЕТ: «{}»", reply_text)
                    else:
                        reply_text = await processor.generate(item)
                        logger.success("💬 НАШ ОТВЕТ: «{}»", reply_text)
                except Exception as exc:
                    db_err_label = "Custom reply resolution" if effective_mode == "custom" else "LLM generation"
                    ru_err_label = "Подготовка собственного текста" if effective_mode == "custom" else "Генерация ответа нейросетью"
                    if payload is not None:
                        await db.mark_payload_failed(payload.id, f"{db_err_label} failed: {exc}")
                    await db.log_activity(
                        ActivityLogCreate(
                            video_id=vid_id,
                            video_title=vid_title,
                            video_url=vid_url,
                            target_id=item.external_id,
                            target_author=item.author,
                            target_text=item.text,
                            reply_text=None,
                            mode="dry_run" if dry_run else "live",
                            status="failed",
                            error=f"{db_err_label} failed: {exc}",
                            duration_sec=None,
                            payload_id=payload.id if payload else None,
                        )
                    )
                    logger.error("{} завершилась ошибкой для {}: {}", ru_err_label, item.external_id, exc)
                    continue

                if dry_run:
                    sub_res = await runner.run(
                        reply_text,
                        external_id=item.external_id,
                        payload_id=None,
                        dry_run=True,
                    )
                    status = "simulated" if sub_res.success else "failed"
                    await db.log_activity(
                        ActivityLogCreate(
                            video_id=vid_id,
                            video_title=vid_title,
                            video_url=vid_url,
                            target_id=item.external_id,
                            target_author=item.author,
                            target_text=item.text,
                            reply_text=reply_text,
                            mode="dry_run",
                            status=status,
                            error=sub_res.error if not sub_res.success else None,
                            duration_sec=sub_res.duration_seconds,
                            payload_id=None,
                        )
                    )
                    if sub_res.success:
                        logger.info("[Тестовый режим] Симуляция ответа успешна (ID={})", sub_res.remote_id)
                        processed_for_video += 1
                        total_replies += 1
                    else:
                        logger.error("[Тестовый режим] Ошибка симуляции ответа: {}", sub_res.error)
                else:
                    try:
                        payload = await db.update_payload_text(payload.id, reply_text)
                        sub_res = await runner.run(
                            payload.text,
                            external_id=item.external_id,
                            payload_id=payload.id,
                            dry_run=False,
                        )
                    except Exception as exc:
                        await db.mark_payload_failed(payload.id, f"Execution failed: {exc}")
                        await db.log_activity(
                            ActivityLogCreate(
                                video_id=vid_id,
                                video_title=vid_title,
                                video_url=vid_url,
                                target_id=item.external_id,
                                target_author=item.author,
                                target_text=item.text,
                                reply_text=reply_text,
                                mode="live",
                                status="failed",
                                error=str(exc),
                                duration_sec=None,
                                payload_id=payload.id,
                            )
                        )
                        logger.exception("Ошибка при отправке ответа для {}: {}", item.external_id, exc)
                        continue

                    if sub_res.success:
                        if acc_record is not None:
                            await db.increment_account_stats(acc_record.id, replies=1)
                        await db.mark_payload_submitted(payload.id, sub_res.remote_id)
                        await db.log_activity(
                            ActivityLogCreate(
                                video_id=vid_id,
                                video_title=vid_title,
                                video_url=vid_url,
                                target_id=item.external_id,
                                target_author=item.author,
                                target_text=item.text,
                                reply_text=reply_text,
                                mode="live",
                                status="success",
                                error=None,
                                duration_sec=sub_res.duration_seconds,
                                payload_id=payload.id,
                            )
                        )
                        logger.success("Ответ успешно опубликован и сохранен в базе (ID={})", sub_res.remote_id)
                        processed_for_video += 1
                        total_replies += 1
                    else:
                        await db.mark_payload_failed(payload.id, sub_res.error or "submission failed")
                        await db.log_activity(
                            ActivityLogCreate(
                                video_id=vid_id,
                                video_title=vid_title,
                                video_url=vid_url,
                                target_id=item.external_id,
                                target_author=item.author,
                                target_text=item.text,
                                reply_text=reply_text,
                                mode="live",
                                status="failed",
                                error=sub_res.error or "submission failed",
                                duration_sec=sub_res.duration_seconds,
                                payload_id=payload.id,
                            )
                        )
                        logger.error("Не удалось отправить ответ: {}", sub_res.error)

                if processed_for_video < comments_per_video:
                    pause = rng.uniform(cfg.limits.pause_min_seconds, cfg.limits.pause_max_seconds)
                    logger.info("Пауза {:.1f}с перед следующим комментарием...", pause)
                    await asyncio.sleep(pause)

            logger.info("Видео {} обработано: отправлено {} ответов.", vid_id, processed_for_video)

            if idx < len(videos):
                base_cd = float(getattr(cfg.limits, "video_cooldown_seconds", cfg.limits.session_cooldown_seconds))
                cooldown = rng.uniform(base_cd * 0.95, base_cd * 1.05) if base_cd > 0 else 0.0
                logger.info("Пауза {:.1f}с перед следующим видео...", cooldown)
                await asyncio.sleep(cooldown)

        logger.info(
            "Обработка всех видео завершена: отправлено {} ответов по {} видео.",
            total_replies,
            len(videos),
        )
    finally:
        await runner.close()
        await collector.close()
        if processor is not None:
            await processor.close()
        await db.close()


async def print_activity_history(
    config_path: str = "config.yaml",
    limit: int = 50,
    status_filter: str | None = None,
) -> None:
    """Print the database audit log of replies in a table."""
    cfg = load_config(config_path)
    db = Database(cfg.app.db_path)
    await db.connect()
    await db.init_schema()
    try:
        entries = await db.list_activity_logs(page=1, page_size=limit, status_filter=status_filter)
        print("\n" + "=" * 90)
        print("                        ЖУРНАЛ ОТРАБОТОК (ACTIVITY AUDIT LOG)")
        print("=" * 90)
        if not entries:
            print(" Журнал отработок пуст.")
            print("=" * 90 + "\n")
            return

        header = f"{'ID':<5} | {'Время (UTC)':<19} | {'Статус':<9} | {'Режим':<7} | {'Автор':<15} | {'Видео':<12} | {'Ответ (фрагмент)'}"
        print(header)
        print("-" * 90)
        for e in entries:
            t = e.created_at[:19].replace("T", " ") if e.created_at else "---"
            author = (e.target_author or "@unknown")[:14]
            vid = (e.video_title or e.video_id)[:11]
            reply = (e.reply_text or e.error or "")[:25].replace("\n", " ")
            print(f"{e.id:<5} | {t:<19} | {e.status:<9} | {e.mode:<7} | {author:<15} | {vid:<12} | {reply}...")
        print("=" * 90 + "\n")
    finally:
        await db.close()


async def print_status(config_path: str = "config.yaml") -> None:
    cfg = load_config(config_path)
    db = Database(cfg.app.db_path)
    await db.connect()
    await db.init_schema()
    try:
        stats = await db.stats()
        print("\nDatabase Statistics:\n" + "=" * 50)
        print(f"Database path: {stats['db_path']}")
        print(f"Test nodes: {stats['nodes']}")
        print(f"Payloads: {stats['payloads']}")
        print(f"Queue items: {stats['queue']}")
        print(f"Activity log: {stats.get('activity', {})}")
        print(f"Active slots: {stats['active_slots']}")
        recent = await db.list_payloads(limit=5)
        if recent:
            print("\nRecent 5 Payloads:")
            for p in recent:
                safe_text = p.text[:50].encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8")
                print(f"  [{p.id}] Status: {p.status} | Source: {p.source_ref} | Text: {safe_text}...")
        print("=" * 50 + "\n")
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="YouTube Comments-Under-Comments AI Reply Bot")
    parser.add_argument("--video", type=str, default=None, help="YouTube video URL or ID")
    parser.add_argument("--search", type=str, default=None, help="Search query to find YouTube videos by topic")
    parser.add_argument("--min-views", type=int, default=0, help="Minimum views required for videos in search")
    parser.add_argument("--max-views", type=int, default=None, help="Maximum views allowed for videos in search")
    parser.add_argument("--max-age-hours", type=int, default=None, help="Search only videos published within N hours (e.g. 12, 24, 48, 168)")
    parser.add_argument("--max-videos", type=int, default=5, help="Maximum number of videos to process in search")
    parser.add_argument("--comments-per-video", type=int, default=2, help="Number of comments to answer per video")
    parser.add_argument(
        "--target-mode",
        type=str,
        choices=["popular_parent", "sub_comment", "mixed"],
        default=None,
        help="Target comments mode: popular top-level comments, sub-comments, or mixed",
    )
    parser.add_argument(
        "--reply-mode",
        type=str,
        choices=["ai", "custom"],
        default=None,
        help="Reply mode: 'ai' (generate via DeepSeek) or 'custom' (user custom text/templates)",
    )
    parser.add_argument(
        "--custom-reply",
        type=str,
        default=None,
        help="Custom reply text or template to use instead of AI generation",
    )
    parser.add_argument("--config", type=str, default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--limit", type=int, default=None, help="Max number of replies to post (single video mode)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate navigation, typing, without final submit click")
    parser.add_argument("--headless", action="store_true", help="Запуск браузера в скрытом/автономном режиме (без видимого окна)")
    parser.add_argument("--login", action="store_true", help="Open headful browser to log into YouTube interactively")
    parser.add_argument("--account", type=str, default=None, help="YouTube account name to use for execution or login")
    parser.add_argument(
        "--add-account",
        type=str,
        default=None,
        metavar="NAME | EMAIL:PASSWORD:TOTP[:RECOVERY_EMAIL][:PROXY] | -",
        help=(
            "Add or update account by name, or register Google credentials email:password:totp_secret[:recovery_email] "
            "(status pending). Pass '-' to read the account line from stdin so the password stays out of the process list"
        ),
    )
    parser.add_argument("--name", type=str, default=None, help="Account name for --add-account with credentials (default: the email)")
    parser.add_argument("--auth-account", type=str, default=None, metavar="NAME_OR_EMAIL", help="Sign the account into YouTube (Camoufox with its proxy and fixed fingerprint, TOTP 2FA)")
    parser.add_argument("--import-accounts-file", type=str, default=None, metavar="FILE", help="Import accounts from a text file (one email:password:totp[:recovery_email][:proxy] per line) and authenticate them one by one; accounts that are already authenticated are skipped")
    parser.add_argument("--force", action="store_true", help="With --import-accounts-file: sign in accounts that are already authenticated too")
    parser.add_argument("--proxy", type=str, default=None, help="Proxy URL (http://user:pass@ip:port or ip:port:user:pass) for adding account or session override")
    parser.add_argument("--session", type=str, default=None, help="Session directory path for adding account")
    parser.add_argument("--heartbeat", "--check-session", dest="heartbeat", nargs="?", const="ALL", default=None, metavar="NAME_OR_ALL", help="Show session status from the LOGIN_INFO cookie, no network (default: all active accounts)")
    parser.add_argument("--heartbeat-online", action="store_true", help="With --heartbeat: also verify the session with an HTTP request to YouTube (Firefox-like, through the account proxy)")
    parser.add_argument("--warm-up", type=str, default=None, metavar="NAME_OR_EMAIL", help="Run humanized browser warm-up for an account (natural watch, mouse movement, comments scroll)")
    parser.add_argument("--warm-up-duration", type=float, default=45.0, help="Duration in seconds for warm-up viewing (default: 45.0)")
    parser.add_argument("--lifecycle", action="store_true", help="Show account ages, lifecycle stages, quarantine hours left and daily comment quota")
    parser.add_argument("--warm-quarantined", action="store_true", help="Run organic warm-up, one account after another, for every account in quarantine or aging (uses --warm-up-duration)")
    parser.add_argument("--list-accounts", action="store_true", help="List all registered accounts with stats from DB")
    parser.add_argument("--videos-file", type=str, default=None, help="Path to videos pool file (e.g. videos.txt)")
    parser.add_argument("--concurrency", "-j", type=int, default=None, help="Accounts working at the same time (default: limits.max_concurrent_sessions)")
    parser.add_argument("--account-mode", choices=("each", "share"), default=None, help="each: every account passes the whole video list; share: the list is split (default: limits.account_mode)")
    parser.add_argument("--list-replies", action="store_true", help="List comments under comments and exit")
    parser.add_argument("--status", action="store_true", help="Show database metrics and status")
    parser.add_argument("--history", "--log", dest="show_history", action="store_true", help="Show recent audit activity logs and exit")
    parser.add_argument("--wizard", "-i", "--interactive", action="store_true", help="Launch interactive CMD setup wizard")
    parser.add_argument("--log-level", type=str, default="INFO", help="Logging level")

    args = parser.parse_args()
    setup_cli_logging(args.log_level)
    bind_to_kill_on_close_job()  # browser children die with this process, even on a crash

    if args.heartbeat is not None:
        ok = asyncio.run(heartbeat_cli(args.heartbeat, args.config, online=args.heartbeat_online))
        sys.exit(0 if ok else 1)

    if args.warm_up:
        ok = asyncio.run(
            warm_up_cli(
                args.warm_up,
                args.config,
                duration=args.warm_up_duration,
                target_video=args.video,
                headless=args.headless or None,
            )
        )
        sys.exit(0 if ok else 1)

    if args.lifecycle:
        asyncio.run(lifecycle_cli(args.config))
        return

    if args.warm_quarantined:
        ok = asyncio.run(
            warm_quarantined_cli(args.config, duration=args.warm_up_duration, headless=args.headless or None)
        )
        sys.exit(0 if ok else 1)

    if args.list_accounts:
        asyncio.run(print_accounts_table(args.config))
        return

    if args.add_account:
        account_spec: str = args.add_account
        if account_spec == "-":
            account_spec = sys.stdin.readline().strip()
            if not account_spec:
                print("[ERROR] --add-account -: stdin is empty (expected email:password:totp_secret[:recovery_email][:proxy])", file=sys.stderr)
                sys.exit(1)
        if account_spec != args.add_account or looks_like_credentials(account_spec):
            ok = asyncio.run(
                add_credentials_account_cli(
                    account_spec,
                    name=args.name,
                    session_path=args.session,
                    proxy_url=args.proxy,
                    config_path=args.config,
                )
            )
            sys.exit(0 if ok else 1)
        asyncio.run(add_account_cli(args.add_account, session_path=args.session, proxy_url=args.proxy, config_path=args.config))
        return

    if args.auth_account:
        sys.exit(0 if asyncio.run(auth_account_cli(args.auth_account, args.config, args.headless or None)) else 1)

    if args.import_accounts_file:
        ok = asyncio.run(import_accounts_file_cli(args.import_accounts_file, args.config, args.headless or None, args.force))
        sys.exit(0 if ok else 1)

    if args.status:
        asyncio.run(print_status(args.config))
        return

    if args.show_history:
        asyncio.run(print_activity_history(args.config))
        return

    if args.login:
        asyncio.run(interactive_login(args.config, account_name=args.account))
        return

    if args.videos_file:
        asyncio.run(
            run_video_pool_cli(
                config_path=args.config,
                videos_file=args.videos_file,
                concurrency=args.concurrency,
                account_mode=args.account_mode,
                dry_run=args.dry_run,
                headless=args.headless if args.headless else None,
            )
        )
        return

    cfg = load_config(args.config)
    default_video = cfg.collector.endpoints[0].url if cfg.collector.endpoints else "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    default_limit = cfg.limits.max_iterations_per_session or 2

    if args.list_replies:
        video_url = args.video or default_video
        limit = args.limit if args.limit is not None else 10
        asyncio.run(list_sub_comments(video_url, args.config, limit=limit))
        return

    search_query = args.search or getattr(cfg.collector, "search_query", None)
    target_mode = args.target_mode or getattr(cfg.collector, "target_mode", "first_reply")

    if args.search:
        search_kwargs: dict[str, Any] = {
            "config_path": args.config,
            "min_views": args.min_views,
            "max_views": args.max_views,
            "max_videos": args.max_videos,
            "comments_per_video": args.comments_per_video,
            "target_mode": target_mode,
            "dry_run": args.dry_run,
        }
        if args.account is not None:
            search_kwargs["account_name"] = args.account
        if args.headless:
            search_kwargs["headless"] = True
        if args.reply_mode is not None:
            search_kwargs["reply_mode"] = args.reply_mode
        if args.custom_reply is not None:
            search_kwargs["custom_reply"] = args.custom_reply
        if args.max_age_hours is not None:
            search_kwargs["max_age_hours"] = args.max_age_hours
        asyncio.run(run_multi_video_pipeline(args.search, **search_kwargs))
        return

    # Check if user explicitly provided a CLI action or if interactive wizard should open
    has_cli_action = (
        args.video is not None
        or args.dry_run
        or args.custom_reply is not None
        or args.reply_mode is not None
        or args.headless
        or args.account is not None
        or args.videos_file is not None
        or args.add_account is not None
        or args.auth_account is not None
        or args.import_accounts_file is not None
        or args.list_accounts
    )

    if args.wizard or (not has_cli_action and sys.stdin.isatty()):
        from modules.wizard import run_wizard_app
        run_wizard_app(args.config)
        return

    if not args.video and search_query:
        min_v = args.min_views if args.min_views != 0 else getattr(cfg.collector, "min_views", 0)
        max_v = args.max_views if args.max_views is not None else getattr(cfg.collector, "max_views", None)
        c_per_v = args.comments_per_video if args.comments_per_video != 2 else getattr(cfg.collector, "comments_per_video", 2)
        fallback_kwargs: dict[str, Any] = {
            "config_path": args.config,
            "min_views": min_v,
            "max_views": max_v,
            "max_videos": args.max_videos,
            "comments_per_video": c_per_v,
            "target_mode": target_mode,
            "dry_run": args.dry_run,
        }
        if args.account is not None:
            fallback_kwargs["account_name"] = args.account
        if args.headless:
            fallback_kwargs["headless"] = True
        if args.reply_mode is not None:
            fallback_kwargs["reply_mode"] = args.reply_mode
        if args.custom_reply is not None:
            fallback_kwargs["custom_reply"] = args.custom_reply
        if args.max_age_hours is not None:
            fallback_kwargs["max_age_hours"] = args.max_age_hours
        asyncio.run(run_multi_video_pipeline(search_query, **fallback_kwargs))
        return

    video_url = args.video or default_video
    limit = args.limit if args.limit is not None else default_limit
    bot_kwargs: dict[str, Any] = {
        "config_path": args.config,
        "limit": limit,
        "target_mode": args.target_mode,
        "dry_run": args.dry_run,
    }
    if args.account is not None:
        bot_kwargs["account_name"] = args.account
    if args.headless:
        bot_kwargs["headless"] = True
    if args.reply_mode is not None:
        bot_kwargs["reply_mode"] = args.reply_mode
    if args.custom_reply is not None:
        bot_kwargs["custom_reply"] = args.custom_reply
    asyncio.run(run_reply_bot(video_url, **bot_kwargs))


if __name__ == "__main__":
    main()
