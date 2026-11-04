"""Console helpers shared by the tabbed interface (`modules.ui`): config writes, sessions, accounts, target labels.

`run_wizard_app` starts the interface (`python youtube_bot.py --wizard`, start_bot.bat).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import yaml

from modules import tui
from modules.tui import WizardBack

# ANSI Color codes
CLR_RESET = "\033[0m"
CLR_BOLD = "\033[1m"
CLR_DIM = "\033[2m"
CLR_CYAN = "\033[96m"
CLR_GREEN = "\033[92m"
CLR_YELLOW = "\033[93m"
CLR_RED = "\033[91m"
CLR_WHITE = "\033[97m"
CLR_MAGENTA = "\033[95m"


def configure_console() -> None:
    """Ensure standard encoding and terminal support for Windows CMD / PowerShell."""
    if sys.platform == "win32":
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        if hasattr(sys.stderr, "reconfigure"):
            try:
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        os.system("")  # Enables VT100 / ANSI escape sequences in Windows cmd


configure_console()


def safe_print(*args: Any, **kwargs: Any) -> None:
    """Safe print helper that never crashes on legacy terminal charmaps."""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        text = sep.join(str(a) for a in args)
        try:
            safe_text = text.encode(encoding, errors="replace").decode(encoding)
            sys.stdout.buffer.write((safe_text + end).encode(encoding, errors="replace"))
            sys.stdout.buffer.flush()
        except Exception:
            pass


def color(text: str, code: str) -> str:
    """Colorize text if running in interactive terminal."""
    if sys.stdout.isatty():
        return f"{code}{text}{CLR_RESET}"
    return text


def clear_terminal() -> None:
    """Clear terminal screen cleanly."""
    if sys.stdout.isatty():
        os.system("cls" if os.name == "nt" else "clear")
    else:
        safe_print("\n" * 2)


_proxy_tags: dict[int, str] = {}  # proxy id -> «[PL] 45.130.253.31:8000»; refreshed by load_accounts


def check_youtube_session(session_root: Path) -> tuple[bool, str]:
    """Check whether a valid YouTube/Google authenticated session exists."""
    if (session_root / "cookies.sqlite").exists():
        session_dir = session_root
    else:
        session_dir = session_root / "youtube"

    if not session_dir.exists():
        return False, "Сессия отсутствует (нужен вход)"

    cookies_db = session_dir / "cookies.sqlite"
    if not cookies_db.exists() or cookies_db.stat().st_size == 0:
        return False, "Файлы сессии отсутствуют (нужен вход)"

    try:
        uri = f"file:{cookies_db.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        cursor = conn.cursor()
        # Total Cookie Protection partitions google.com cookies away from youtube.com: only LOGIN_INFO
        # stored for youtube.com proves the profile is signed in on the target domain.
        cursor.execute(
            "SELECT COUNT(*) FROM moz_cookies WHERE host LIKE '%youtube.com%' AND name = 'LOGIN_INFO'"
        )
        has_login = cursor.fetchone()[0] > 0
        cursor.execute("SELECT COUNT(*) FROM moz_cookies WHERE host LIKE '%youtube%' OR host LIKE '%google%'")
        has_cookies = cursor.fetchone()[0] > 0
        conn.close()

        if has_login:
            return True, "Авторизован (сессия активна)"
        if has_cookies:
            return False, "Гостевой режим (вход в аккаунт НЕ выполнен)"
        return False, "Сессия пуста (нужен вход)"
    except Exception as exc:
        return False, f"Ошибка проверки сессии: {exc}"


def clear_youtube_session(session_root: Path) -> tuple[bool, str]:
    """Remove session profile directory to reset cookies and account."""
    session_dir = session_root / "youtube"
    if not session_dir.exists():
        return True, "Сессия уже отсутствует"
    try:
        shutil.rmtree(session_dir)
        return True, "Сессия YouTube успешно очищена"
    except Exception as exc:
        return False, f"Не удалось удалить сессию (закройте окно браузера): {exc}"


async def check_deepseek_api(api_key: str, base_url: str = "https://api.deepseek.com", timeout: float = 6.0) -> tuple[bool, str]:
    """Fast non-billing model list check to verify API key validity."""
    from openai import AsyncOpenAI

    key = (api_key or "").strip()
    if not key:
        return False, "API ключ пуст"

    client = AsyncOpenAI(api_key=key, base_url=base_url, timeout=timeout)
    t0 = time.monotonic()
    try:
        models = await client.models.list()
        elapsed = time.monotonic() - t0
        names = [m.id for m in models.data]
        sample = names[0] if names else "deepseek-chat"
        return True, f"Ключ активен! Связь установлена ({sample}, {elapsed:.2f}с)"
    except Exception as exc:
        err = str(exc)
        if "401" in err or "Authentication" in err:
            return False, "Неверный API ключ (401 Unauthorized)"
        if "402" in err or "Insufficient" in err:
            return False, "Баланс исчерпан (402 Insufficient Balance)"
        return False, f"Ошибка API: {err[:60]}"
    finally:
        await client.close()


def update_config_file(config_path: Path, updates: dict[str, Any]) -> None:
    """Update YAML config in place and ensure integrity."""
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raw = {}

    for path_key, val in updates.items():
        keys = path_key.split(".")
        target = raw
        for k in keys[:-1]:
            if "[" in k and k.endswith("]"):
                name, idx_s = k[:-1].split("[")
                idx = int(idx_s)
                if name not in target or not isinstance(target[name], list):
                    target[name] = []
                while len(target[name]) <= idx:
                    target[name].append({})
                target = target[name][idx]
            else:
                if k not in target or not isinstance(target[k], dict):
                    target[k] = {}
                target = target[k]
        target[keys[-1]] = val

    from config import repair_text

    raw = repair_text(raw)  # emoji typed in the Windows console arrive as surrogate halves
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")


def prompt_user(text: str, default: str | None = None) -> str:
    """Read user input with graceful default and Ctrl+C handling."""
    suffix = f" [{default}]" if default is not None else ""
    full_prompt = f"{color('?>', CLR_CYAN)} {text}{suffix}: "
    try:
        val = input(full_prompt).strip()
        if not val and default is not None:
            return default
        return val
    except (KeyboardInterrupt, EOFError):
        safe_print(f"\n{color('[!]', CLR_YELLOW)} Операция отменена.")
        raise WizardBack()


# Menu row: (hotkey, label, status value, status color as CLR_*); None is a blank line.
MenuRow = tuple[str, str, str, str]
_fit = tui.fit

# CLR_* status colors used by wizard rows -> tui theme names.
_THEME_BY_CLR = {
    CLR_GREEN: "ok", CLR_YELLOW: "warn", CLR_RED: "err", CLR_BOLD + CLR_RED: "err",
    CLR_DIM: "dim", CLR_CYAN: "accent", CLR_BOLD + CLR_CYAN: "accent", CLR_MAGENTA: "ai", CLR_WHITE: "",
}


def _use_arrow_keys() -> bool:
    """Arrow navigation needs a real console on both ends."""
    return tui.is_interactive()


def arrow_menu(
    build: Callable[[], list[MenuRow | None]],
    header: list[str],
    selected: int = 0,
    actions: dict[str, Callable[[], None]] | None = None,
    extra_keys: frozenset[str] = frozenset(),
    esc_key: str | None = None,
    side_actions: dict[str, Callable[[], None]] | None = None,
    help_lines: list[tuple[str, str]] | None = None,
    detail: Callable[[str], list[str]] | None = None,
) -> tuple[str, int]:
    """Thin wrapper over tui.select_menu: maps CLR_* row colors to theme names; fallback reads via prompt_user."""
    def rows() -> list[MenuRow | None]:
        return [None if r is None else (r[0], r[1], r[2], _THEME_BY_CLR.get(r[3], "")) for r in build()]

    return tui.select_menu(
        rows, header, selected,
        actions=actions, hidden_keys=extra_keys, esc_key=esc_key,
        side_actions=side_actions, help_lines=help_lines,
        interactive=_use_arrow_keys(), ask_fn=lambda text: prompt_user(text), detail=detail,
    )


def _confirm_country_mismatch(message: str) -> bool:
    """Question before a sign-in whose proxy exit country differs from the expected one; the cursor starts on «Отменить»."""
    picks: list[MenuRow | None] = [
        ("1", "Отменить вход", "безопасный вариант", CLR_DIM),
        ("2", "Продолжить вход", "на ваш риск", CLR_DIM),
    ]
    header = [f" {color(line, CLR_YELLOW)}" for line in message.splitlines()]
    try:
        pick, _ = arrow_menu(lambda: picks, header, 0, esc_key="0")
    except WizardBack:
        return False
    return pick == "2"


async def _confirm_country_mismatch_async(message: str) -> bool:
    """The wizard signs in inside `asyncio.run`: the blocking menu runs in a thread so the event loop stays free."""
    return await asyncio.to_thread(_confirm_country_mismatch, message)


def _setup_accounts(config_path: Path) -> list[tuple[Any, bool]]:
    """Accounts (DB, seeded from config.yaml — a fresh config gives «default») with their login state."""
    return [(a, check_youtube_session(Path(a.session_path))[0]) for a in load_accounts(config_path)]


def _setup_rename_in_config(config_path: Path, acc: Any, new_name: str) -> None:
    """Rename the account entry in config.yaml; add one if missing, so a seeded «default» does not come back."""
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    browser = raw.setdefault("browser", {})
    top = raw.get("accounts")
    if not isinstance(browser.get("accounts"), list):
        browser["accounts"] = []
    target = top if isinstance(top, list) and top else browser["accounts"]
    found = False
    for lst in (top, browser.get("accounts")):
        for item in lst if isinstance(lst, list) else []:
            if isinstance(item, dict) and item.get("name") == acc.name:
                item["name"] = new_name
                found = True
    if not found:
        target.append({  # no proxy here: it lives in the encrypted proxy library
            "name": new_name,
            "session_path": str(acc.session_path),
            "is_active": bool(acc.is_active),
        })
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _setup_clear_session(session_root: Path, account: Any) -> tuple[bool, str]:
    """Delete an account's session folder; the main profile (session_root/youtube) keeps the old reset."""
    path = Path(account.session_path) if account else session_root / "youtube"
    if path.resolve() == (session_root / "youtube").resolve():
        return clear_youtube_session(session_root)
    if not path.exists():
        return True, "Сессия уже отсутствует"
    try:
        shutil.rmtree(path)
        return True, f"Сессия «{account.name}» удалена"
    except Exception as exc:
        return False, f"Не удалось удалить сессию (закройте окно браузера): {exc}"


def _setup_register(config_path: Path, lines: list[str], default_mode: str | None = None) -> Any:
    """Register the account lines through the CLI importer (existing accounts are skipped, not overwritten).

    `default_mode` is the age mode of the lines that have no `mode=` of their own.
    """
    from config import load_config
    from youtube_bot import register_account_lines

    cfg = load_config(config_path)
    return _pools_db(
        config_path,
        lambda db: register_account_lines(db, cfg, lines, skip_existing=True, default_mode=default_mode),
    )


# Target modes: (config value, short label, one-line hint).
_TARGET_MODES: list[tuple[str, str, str]] = [
    ("first_reply", "Ответ на ответ", "ОСНОВНОЙ · под топ-комментарием"),
    ("popular_parent", "Топ-комментарии", "на сам комментарий"),
    ("sub_comment", "Топ-ответы", "на любые залайканные ответы"),
    ("mixed", "Смешанный", "комментарии и ответы по лайкам"),
]

# Space in the mode picker: the same tiny scenario, the arrow marks where the bot writes.
_TARGET_MODE_EXAMPLES: dict[str, list[str]] = {
    "first_reply": [
        "Топ-комментарий: «Депнуть микрозайм» (2 тыс. ♥)",
        "  └ ответ: «пропить микрозайм» (15 ♥)   ◄ отвечаем сюда",
        "Бот берёт самый залайканный ответ под топ-комментарием.",
    ],
    "popular_parent": [
        "Топ-комментарий: «Депнуть микрозайм» (2 тыс. ♥)   ◄ отвечаем сюда",
        "  └ ответ: «пропить микрозайм» (15 ♥)",
    ],
    "sub_comment": [
        "Топ-комментарий: «Депнуть микрозайм» (2 тыс. ♥)",
        "  └ ответ: «пропить микрозайм» (15 ♥)   ◄ отвечаем сюда",
        "Берём залайканные ответы по всему видео, под любыми ветками.",
    ],
    "mixed": [
        "Комментарии и ответы вперемешку: что залайканнее,",
        "туда и отвечаем (на комментарий или на ответ).",
    ],
}


def _target_short_url(url: str) -> str:
    """Video URL without the youtube.com/watch?v= prefix."""
    return _fit(re.sub(r"^https?://(www\.)?(youtube\.com/watch\?v=)?", "", url), 28)


# Auto-search freshness: (max video age in hours or None = any, label).
_TARGET_AGES: list[tuple[int | None, str]] = [
    (None, "Любые"), (12, "12 часов"), (24, "24 часа"), (48, "2 дня"), (168, "7 дней"),
]


def _logged_in_names(cfg: Any) -> list[str]:
    """Names of the accounts whose session is logged in to YouTube (read-only sqlite3, safe inside menus).

    Without any account row the main profile counts as «default».
    """
    try:
        uri = f"file:{quote(Path(cfg.app.db_path).as_posix(), safe='/:')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            rows = conn.execute("SELECT name, session_path FROM accounts ORDER BY id").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        rows = []
    if not rows:
        return ["default"] if check_youtube_session(cfg.browser.session_root)[0] else []
    return [name for name, path in rows if check_youtube_session(Path(path))[0]]


def load_accounts(config_path: Path) -> list[Any]:
    """Load accounts from the DB, seeding it from config.yaml when the DB has none."""
    from config import load_config
    from modules.projects import open_database

    cfg = load_config(config_path)
    db = open_database(cfg)

    async def _fetch():
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
            _proxy_tags.clear()
            for record in await db.list_proxies():
                _proxy_tags[record.id] = f"[{record.country or record.last_exit_country or '??'}] {record.address}"
            return accounts
        finally:
            await db.close()

    return asyncio.run(_fetch())


def _pools_db(config_path: Path, fn: Callable[[Any], Any]) -> Any:
    """Run `await fn(db)` against the configured database."""
    from config import load_config
    from modules.projects import open_database

    cfg = load_config(config_path)

    async def _run() -> Any:
        db = open_database(cfg)
        await db.connect()
        await db.init_schema()
        try:
            return await fn(db)
        finally:
            await db.close()

    return asyncio.run(_run())


def run_wizard_app(config_path_str: str = "config.yaml") -> None:
    """Entry point for the interactive CMD wizard."""
    configure_console()
    p = Path(config_path_str)
    if not p.is_file():
        safe_print(f"{color('[!]', CLR_RED)} Файл конфигурации не найден: {p}")
        return

    if not tui.is_interactive():
        safe_print(f"{color('[!]', CLR_YELLOW)} Интерфейсу нужна консоль: запустите start_bot.bat или youtube_bot.py --wizard в окне терминала.")
        return
    try:
        from modules.ui.app import run_app

        run_app(p)
    except (KeyboardInterrupt, EOFError, WizardBack):
        safe_print(f"\n\n{color('[!]', CLR_YELLOW)} Работа с мастером завершена.")
