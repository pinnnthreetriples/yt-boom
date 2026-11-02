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
from urllib.parse import quote, unquote, urlparse

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
CLR_BLUE = "\033[94m"


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


def menu_item(key: str, text: str, key_code: str | None = None, text_code: str = "") -> str:
    """Format a menu line with a highlighted key ("1." / "[S]"); exit-style keys are red."""
    if key_code is None:
        key_code = CLR_RED if key.strip("[].") in {"0", "Q"} else CLR_BOLD + CLR_YELLOW
    body = color(text, text_code) if text_code else text
    return f" {color(key, key_code)} {body}"


def clear_terminal() -> None:
    """Clear terminal screen cleanly."""
    if sys.stdout.isatty():
        os.system("cls" if os.name == "nt" else "clear")
    else:
        safe_print("\n" * 2)


def mask_secret(secret: str | None, visible_start: int = 7, visible_end: int = 4) -> str:
    """Mask sensitive string (API key, password)."""
    if not secret:
        return "[не задан]"
    s = secret.strip()
    if len(s) <= visible_start + visible_end:
        return "****"
    return f"{s[:visible_start]}...{s[-visible_end:]}"


def mask_proxy(proxy_str: str | None) -> str:
    """Mask proxy credentials for safe console rendering (understands URL, user:pass@host:port and host:port:user:pass)."""
    if not proxy_str:
        return "Прямое соединение (без прокси)"
    from config import mask_proxy_url

    return mask_proxy_url(proxy_str)


def normalize_proxy(user_input: str | None) -> str | None:
    """
    Parse and validate proxy URL entered by user.
    Supports formats:
      - '1.2.3.4:8080' -> 'http://1.2.3.4:8080'
      - 'user:pass@1.2.3.4:8080' -> 'http://user:pass@1.2.3.4:8080'
      - 'http://user:pass@1.2.3.4:8080'
      - 'socks5://user:pass@1.2.3.4:1080'
      - '' / 'none' / '0' / 'direct' -> None (direct connection)
    """
    if not user_input:
        return None
    val = user_input.strip()
    if not val or val.lower() in {"none", "0", "direct", "off", "нет", "прямое"}:
        return None

    if "://" not in val:
        val = f"http://{val}"

    parsed = urlparse(val)
    if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError(f"Неподдерживаемый протокол прокси: '{parsed.scheme}'. Используйте http:// или socks5://")
    if not parsed.hostname:
        raise ValueError("В адресе прокси не указан IP или домен сервера")
    if not parsed.port or not (1 <= parsed.port <= 65535):
        raise ValueError("В адресе прокси не указан или неверен порт (1-65535)")

    return val


def build_proxy_url(scheme: str, host_port: str, login: str = "", password: str = "") -> str:
    """Assemble and validate a proxy URL from separately entered parts."""
    auth = ""
    if login:
        auth = quote(login, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    url = normalize_proxy(f"{scheme}://{auth}{host_port.strip()}")
    if not url:
        raise ValueError("Не указан адрес прокси")
    return url


def prompt_proxy_details() -> str | None:
    """Ask for proxy parts one by one: IP:port, login, password, protocol. None = cancelled."""
    safe_print(color(" Новый прокси · пустой ввод — отмена", CLR_DIM))

    while True:
        host_port = prompt_user(f"{color('1/4', CLR_CYAN)} IP:порт (45.130.253.31:8000)")
        if not host_port:
            return None
        try:
            build_proxy_url("http", host_port)
            break
        except ValueError as exc:
            tui.status_line("err", str(exc))

    login = prompt_user(f"{color('2/4', CLR_CYAN)} Логин (Enter — без авторизации)")
    password = prompt_user(f"{color('3/4', CLR_CYAN)} Пароль (Enter — без пароля)") if login else ""

    schemes = {"1": "http", "2": "https", "3": "socks5"}
    kinds: list[MenuRow | None] = [("1", "HTTP", "", ""), ("2", "HTTPS", "", ""), ("3", "SOCKS5", "", "")]
    while True:
        kind, idx = arrow_menu(
            lambda: kinds, [f" {color('4/4', CLR_CYAN)} Тип прокси"], esc_key="0",
        )
        kind = kind or kinds[idx][0]
        if kind == "0":
            return None
        if kind in schemes:
            break
        tui.status_line("warn", "Выберите 1, 2 или 3")

    return build_proxy_url(schemes[kind], host_port, login, password)


def format_target_mode_ru(mode: str) -> str:
    """Return friendly Russian description for target mode."""
    modes = {
        "first_reply": "Первый ответ под залайканным (first_reply)",
        "first_sub_comment": "Первый ответ под залайканным (first_reply)",
        "popular_parent": "Главные залайканные комментарии к видео (popular_parent)",
        "sub_comment": "Любые популярные ответы под комментариями (sub_comment)",
        "mixed": "Смешанный режим (главные + ответы) (mixed)",
    }
    return modes.get(str(mode).lower(), str(mode))


def check_proxy_connectivity(proxy_url: str | None, timeout: float = 6.0) -> tuple[bool, str]:
    """Test external network connectivity through given proxy."""
    from curl_cffi import requests

    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    tag = "Прокси" if proxy_url else "Прямое соединение"

    try:
        resp = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=timeout)
        if resp.status_code == 200:
            ip = resp.json().get("ip", "неизвестно")
            return True, f"{tag} работает! Внешний IP: {ip}"
    except Exception:
        pass

    try:
        resp = requests.get("https://httpbin.org/ip", proxies=proxies, timeout=timeout)
        if resp.status_code == 200:
            ip = resp.json().get("origin", "неизвестно")
            return True, f"{tag} работает! Внешний IP: {ip}"
    except Exception as exc:
        err_msg = str(exc)
        if "Failed to connect" in err_msg or "Connection refused" in err_msg:
            return False, "Не удалось подключиться к прокси (сервер недоступен)"
        if "timed out" in err_msg.lower():
            return False, "Таймаут соединения (прокси отвечает слишком медленно)"
        return False, f"Ошибка соединения: {err_msg[:60]}"

    return False, "Не удалось проверить сетевое соединение"


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
MENU_WIDTH = tui.MAX_WIDTH
_fit = tui.fit

# CLR_* status colors used by wizard rows -> tui theme names.
_THEME_BY_CLR = {
    CLR_GREEN: "ok", CLR_YELLOW: "warn", CLR_RED: "err", CLR_BOLD + CLR_RED: "err",
    CLR_DIM: "dim", CLR_CYAN: "accent", CLR_BOLD + CLR_CYAN: "accent", CLR_MAGENTA: "ai", CLR_WHITE: "",
}


_CLR_BY_THEME = {"ok": CLR_GREEN, "warn": CLR_YELLOW, "err": CLR_RED, "accent": CLR_CYAN, "dim": CLR_DIM}


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


def print_banner() -> None:
    """One-line app title (kept for callers; the setup stages use print_step_header only)."""
    tui.section_header("YouTube Reply Bot", "мастер настройки")


def print_step_header(step_idx: int, total: int, title: str, hint: str) -> None:
    """«Этап n/N · Название» with a short dim hint; a hint too long for the line goes under the rule."""
    head = f"Этап {step_idx}/{total} · {title}"
    if hint and len(head) + len(hint) + 3 > MENU_WIDTH:
        tui.section_header(head)
        safe_print(color(f" {_fit(hint, MENU_WIDTH - 1)}", CLR_DIM))
    else:
        tui.section_header(head, hint)


_setup_in_wizard = False  # True while run_step_by_step_wizard runs: headers show «Этап n/5»


def _setup_screen(step: int, title: str, hint: str, status: list[Any], notice: tuple[str, str] | None = None) -> None:
    """Clear and draw a setup screen: header, status table, last action result."""
    clear_terminal()
    if _setup_in_wizard:
        print_step_header(step, 5, title, hint)
    else:
        tui.section_header(title, hint)
    if status:
        tui.kv_table(status)
    if notice:
        tui.status_line(*notice)


def _setup_menu(rows: Callable[[], list[MenuRow | None]], selected: int) -> tuple[str, int]:
    """Stage menu (Esc = «0»); an empty answer picks the row under the cursor."""
    choice, selected = arrow_menu(rows, [""], selected, esc_key="0")
    if not choice:
        row = rows()[selected]
        choice = row[0] if row else ""
    return choice, selected


def _setup_index(rows: list[MenuRow | None], key: str) -> int:
    return next((i for i, r in enumerate(rows) if r and r[0] == key), 0)


def step_proxy(config_path: Path) -> None:
    """Stage 2 «Прокси»: attach a proxy to each account from stage 1. «0»/Esc raises WizardBack."""
    rows: list[MenuRow | None] = [
        ("2", "Назначить прокси", "аккаунт → IP:порт → логин → пароль → тип", CLR_DIM),
        ("3", "Убрать прокси", "аккаунт пойдёт напрямую", CLR_DIM),
        ("4", "Проверить связь", "внешний IP через прокси аккаунта", CLR_DIM),
        None,
        ("1", "Продолжить →", "", ""),
        ("0", "← Назад", "", ""),
    ]

    selected: int | None = None
    notice: tuple[str, str] | None = None
    redraw = True
    while True:
        if redraw:
            accounts = _setup_accounts(config_path)
            _setup_screen(2, "Прокси", "у каждого аккаунта свой", [])
            _setup_proxy_table(accounts)
            if notice:
                safe_print()
                tui.status_line(*notice)
            if selected is None:
                missing = any(not acc.proxy_url for acc, _ in accounts)
                selected = _setup_index(rows, "2" if missing else "1")
            redraw, notice = False, None

        choice, selected = _setup_menu(lambda: rows, selected)

        if choice == "1":
            return
        if choice == "0":
            raise WizardBack()

        if choice in {"2", "3", "4"}:
            if not accounts:
                notice, redraw = ("warn", "Аккаунтов нет — сначала добавьте их на этапе «Аккаунты»"), True
                continue
            question = {"2": "Какому аккаунту назначить прокси?", "3": "У какого аккаунта убрать прокси?"}.get(
                choice, "Чей прокси проверить?",
            )
            acc = accounts[0][0] if len(accounts) == 1 else _setup_pick_account(accounts, question)
            if acc is None:
                continue

            if choice == "3":
                _save_account(config_path, acc.name, acc.session_path, None, is_active=acc.is_active)
                notice = ("ok", f"«{acc.name}»: прокси убран, прямое соединение")
            elif choice == "4":
                tui.status_line("info", f"Проверяем соединение «{acc.name}»…")
                ok, msg = check_proxy_connectivity(acc.proxy_url)
                notice = ("ok" if ok else "err", msg)
            else:
                notice = _setup_assign_proxy(config_path, acc)
            redraw = True
            continue

        tui.status_line("warn", f"Неизвестная команда: {choice}")


def _setup_assign_proxy(config_path: Path, acc: Any) -> tuple[str, str] | None:
    """Ask the proxy parts for one account, test it and save; returns the notice (None = cancelled)."""
    try:
        norm = prompt_proxy_details()
    except ValueError as exc:
        return "err", f"Ошибка формата: {exc}"
    except WizardBack:
        return None
    if not norm:
        return None

    tui.status_line("info", "Проверяем прокси…")
    ok, msg = check_proxy_connectivity(norm)
    tui.status_line("ok" if ok else "err", msg)
    if not ok:
        safe_print(color(" Проверьте логин/пароль и тип прокси (HTTP / SOCKS5)", CLR_DIM))
        try:
            if not _yes(prompt_user("Сохранить всё равно? (y/N)", "n")):
                return None
        except WizardBack:
            return None
    _save_account(config_path, acc.name, acc.session_path, norm, is_active=acc.is_active)
    return "ok", f"«{acc.name}»: прокси сохранён"


def _setup_proxy_table(accounts: list[tuple[Any, bool]]) -> None:
    """Counter «прокси у N из M», then Аккаунт / Прокси per account (first 8, then «… ещё K»)."""
    if not accounts:
        tui.kv_table([("Аккаунтов", "пока нет — добавьте на этапе «Аккаунты»", "warn")])
        return
    with_proxy = sum(1 for acc, _ in accounts if acc.proxy_url)
    tui.kv_table([("Прокси", f"у {with_proxy} из {len(accounts)} аккаунтов", "" if with_proxy else "warn")])
    shown = accounts[:_SETUP_MAX_ACCOUNTS]
    name_w = min(24, max(len("Аккаунт"), *(len(acc.name) for acc, _ in shown)))
    proxy_w = MENU_WIDTH - 3 - name_w - 2
    safe_print()
    safe_print(color(f"   {'Аккаунт':<{name_w}}  Прокси", CLR_DIM))
    for acc, _ in shown:
        name = color(f"{_fit(acc.name, name_w):<{name_w}}", CLR_BOLD)
        if acc.proxy_url:
            proxy = _fit(mask_proxy(acc.proxy_url), proxy_w)
        else:
            proxy = color("без прокси", CLR_YELLOW)
        safe_print(f"   {name}  {proxy}")
    if len(accounts) > _SETUP_MAX_ACCOUNTS:
        safe_print(color(f"   … ещё {len(accounts) - _SETUP_MAX_ACCOUNTS}", CLR_DIM))


def _setup_account_rows(accounts: list[tuple[Any, bool]]) -> list[MenuRow | None]:
    """One selectable row per account (key A1, A2, …): name, login state and proxy."""
    proxy_w = MENU_WIDTH - 3 - 24 - 2 - 11 - 2
    rows: list[MenuRow | None] = []
    for i, (acc, ok) in enumerate(accounts, 1):
        proxy = _fit(mask_proxy(acc.proxy_url) if acc.proxy_url else "без прокси", proxy_w)
        state = "● вошёл" if ok else "○ нет входа"
        rows.append((f"A{i}", _fit(acc.name, 24), f"{state:<11}  {proxy}", CLR_GREEN if ok else CLR_YELLOW))
    return rows


def step_account(config_path: Path, session_root: Path) -> bool:
    """Stage 1 «Аккаунты»: every account is a row (Enter/Space opens its actions), plus add / import / warm-up.

    Returns True if at least one account (or the main `session_root` profile) is logged in.
    «0»/Esc raises WizardBack.
    """
    tail: list[MenuRow | None] = [
        ("1", "Войти в аккаунт", "новый или уже добавленный", CLR_DIM),
        ("5", "Импорт аккаунтов", "файл или вставка строк", CLR_DIM),
        ("6", "Прогрев", "карантин и aging одним кликом", CLR_DIM),
        ("8", "Жизненный цикл", "стадии, квоты, heartbeat", CLR_DIM),
        ("9", "Профиль канала", "аватарка, описание, никнейм", CLR_DIM),
        None,
        ("4", "Продолжить →", "", ""),
        ("0", "← Назад", "", ""),
    ]

    cursor_key: str | None = None
    selected = 0
    notice: tuple[str, str] | None = None
    warned_empty = False
    redraw = True
    while True:
        if redraw:
            accounts = _setup_accounts(config_path)
            logged = sum(1 for _, ok in accounts if ok)
            rows: list[MenuRow | None] = [*_setup_account_rows(accounts), *([None] if accounts else []), *tail]
            _setup_screen(1, "Аккаунты", "нужны, чтобы публиковать ответы", [])
            _setup_accounts_table(accounts)
            if notice:
                safe_print()
                tui.status_line(*notice)
            selected = _setup_index(rows, cursor_key or ("4" if accounts else "1"))
            redraw, notice = False, None

        choice, selected = _setup_menu(lambda: rows, selected)
        cursor_key = choice

        if choice == "0":
            raise WizardBack()
        if choice == "4":
            if not accounts and not warned_empty:
                warned_empty = True
                tui.status_line("warn", "Аккаунтов нет — публиковать будет нечем. Ещё раз «Продолжить» — дальше")
                continue
            return logged > 0 or check_youtube_session(session_root)[0]

        if choice.startswith("A") and choice[1:].isdigit() and 1 <= int(choice[1:]) <= len(accounts):
            notice = _setup_account_actions(config_path, session_root, accounts, accounts[int(choice[1:]) - 1])
            redraw = True
            continue

        if choice == "5":
            notice, redraw = _setup_import(config_path), True
            continue

        if choice == "6":
            notice, redraw = _setup_warmup_all(config_path), True
            continue

        if choice == "8":
            _setup_lifecycle(config_path)
            redraw = True
            continue

        if choice == "9":
            notice = _setup_profile(config_path, accounts) if accounts else ("warn", "Аккаунтов нет")
            redraw = True
            continue

        if choice == "1":
            try:
                login_account_wizard(config_path)
            except (WizardBack, ValueError):
                pass
            fresh = _setup_accounts(config_path)
            fresh_logged = sum(1 for _, ok in fresh if ok)
            added = len(fresh) > len(accounts)
            if added:
                notice = ("ok", f"Аккаунт добавлен · всего {len(fresh)}")
            else:
                notice = ("ok", "Вход выполнен, сессия сохранена") if fresh_logged > logged else ("warn", "Вход не выполнен")
            if added or fresh_logged > logged:
                cursor_key = "4"
            redraw = True
            continue

        tui.status_line("warn", f"Неизвестная команда: {choice}")


def _setup_account_actions(
    config_path: Path, session_root: Path, accounts: list[tuple[Any, bool]], entry: tuple[Any, bool],
) -> tuple[str, str] | None:
    """Actions of one account: sign in automatically, check login, rename, log out.

    Returns the notice to show (None = nothing happened).
    """
    acc = entry[0]
    rows: list[MenuRow | None] = [
        ("4", "Войти автоматически", "по почте и паролю аккаунта", CLR_DIM),
        ("1", "Проверить вход", "", ""),
        ("2", "Переименовать", "имя для вас: ник или почта", CLR_DIM),
        ("3", "Выйти из аккаунта", "удалить сохранённую сессию", CLR_DIM),
        None,
        ("0", "← Назад", "", ""),
    ]
    choice, _ = arrow_menu(lambda: rows, [f" {color(acc.name, CLR_BOLD)}"], esc_key="0")
    if choice == "4":
        return _setup_auto_login(config_path, acc)
    if choice == "1":
        ok = check_youtube_session(Path(acc.session_path))[0]
        return ("ok", f"«{acc.name}»: вход выполнен") if ok else ("warn", f"«{acc.name}»: нет входа")
    if choice == "2":
        return _setup_rename(config_path, accounts, acc)
    if choice == "3":
        try:
            if not _yes(prompt_user(f"Удалить сессию «{acc.name}»? (y/N)", "n")):
                return None
        except WizardBack:
            return None
        ok, msg = _setup_clear_session(session_root, acc)
        return ("ok" if ok else "err", msg)
    return None


def _setup_auto_login(config_path: Path, acc: Any) -> tuple[str, str] | None:
    """«Войти автоматически»: sign `acc` in with its stored email, password and (if any) TOTP secret.

    An account without a stored password is asked for `почта пароль [TOTP]` first (shown as typed, like the import).
    Returns the notice to show (None = cancelled).
    """
    from config import load_config
    from youtube_bot import _register_credentials_account, auth_account_cli

    if not (acc.email and acc.password):
        safe_print(color(" У аккаунта нет пароля. Введите: почта пароль [TOTP]. Пустая строка — отмена.", CLR_DIM))
        try:
            line = _read_secret_line(" › ").strip()
        except WizardBack:
            return None
        if not line:
            return None
        cfg = load_config(config_path)
        try:
            _pools_db(config_path, lambda db: _register_credentials_account(db, cfg, line, name=acc.name))
        except ValueError as exc:
            return "err", f"Строка не принята: {exc}"
    tui.status_line("info", f"Автоматический вход «{acc.name}»…")
    try:
        ok = asyncio.run(auth_account_cli(acc.name, str(config_path)))
    except Exception as exc:
        return "err", f"«{acc.name}»: вход не удался ({exc.__class__.__name__}), подробности в логе"
    return ("ok", f"«{acc.name}»: вход выполнен") if ok else ("err", f"«{acc.name}»: вход не удался, подробности в логе")


_SETUP_MAX_ACCOUNTS = 8


def _setup_accounts(config_path: Path) -> list[tuple[Any, bool]]:
    """Accounts (DB, seeded from config.yaml — a fresh config gives «default») with their login state."""
    return [(a, check_youtube_session(Path(a.session_path))[0]) for a in load_accounts(config_path)]


def _setup_accounts_table(accounts: list[tuple[Any, bool]]) -> None:
    """Counter «N · вошли M из N»; the accounts themselves are the menu rows below."""
    logged = sum(1 for _, ok in accounts if ok)
    if not accounts:
        tui.kv_table([("Аккаунтов", "пока нет — добавьте первый", "warn")])
        return
    tui.kv_table([("Аккаунтов", f"{len(accounts)} · вошли {logged} из {len(accounts)}", "" if logged else "warn")])
    safe_print()


def _setup_pick_account(
    accounts: list[tuple[Any, bool]], question: str, status: Callable[[Any], tuple[str, str]] | None = None
) -> Any:
    """Arrow picker of an account; None = cancelled. `status(account)` → (text, theme style) replaces the login column."""
    def state(acc: Any, ok: bool) -> tuple[str, str]:
        if status is None:
            return ("● вошёл" if ok else "○ нет входа"), (CLR_GREEN if ok else CLR_YELLOW)
        text, style = status(acc)
        return text, _CLR_BY_THEME.get(style, CLR_DIM)

    picks: list[MenuRow | None] = [
        (str(i), _fit(acc.name, 24), *state(acc, ok)) for i, (acc, ok) in enumerate(accounts, 1)
    ]
    picks += [None, ("0", "← Отмена", "", "")]
    pick, _ = arrow_menu(
        lambda: picks, [f" {question}"], esc_key="0",
    )
    return accounts[int(pick) - 1][0] if pick.isdigit() and 1 <= int(pick) <= len(accounts) else None


def _setup_rename(config_path: Path, accounts: list[tuple[Any, bool]], acc: Any) -> tuple[str, str] | None:
    """«Переименовать»: ask the new name for `acc`, rename it in the DB and config.yaml.

    Returns the notice to show, or None when cancelled. Session folders are not touched.
    """
    try:
        new_name = prompt_user(f"Новое имя для «{acc.name}» — для вас: ник или почта").strip()
    except WizardBack:
        return None
    if not new_name:
        return "warn", "Имя не может быть пустым"
    if new_name == acc.name:
        return None
    if any(a.name == new_name for a, _ in accounts):
        return "warn", f"Имя «{new_name}» уже занято"
    try:
        _pools_db(config_path, lambda db: db.rename_account(acc.name, new_name))
    except ValueError as exc:
        return "err", f"Не удалось переименовать: {exc}"
    _setup_rename_in_config(config_path, acc, new_name)
    return "ok", f"Переименован: {acc.name} → {new_name}"


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
        target.append({
            "name": new_name,
            "session_path": str(acc.session_path),
            "proxy_url": acc.proxy_url,
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


WARM_UP_SECONDS = 45.0  # same default as `--warm-up-duration`
_LIFECYCLE_MAX_ROWS = 15


def _read_secret_line(prompt: str) -> str:
    """One line typed or pasted and shown as is; Ctrl+C / EOF cancel like prompt_user."""
    try:
        return input(prompt)
    except (KeyboardInterrupt, EOFError):
        safe_print(f"\n{color('[!]', CLR_YELLOW)} Операция отменена.")
        raise WizardBack()


def _read_account_paste() -> list[str]:
    """Pasted `login:password:TOTP_SECRET` lines, one per account, until an empty line."""
    safe_print(color(" Вставьте по строке на аккаунт: почта, пароль, дальше по желанию TOTP, резервная почта, прокси.", CLR_DIM))
    safe_print(color(" Разделитель — пробел, табуляция или двоеточие. Пустая строка — конец.", CLR_DIM))
    lines: list[str] = []
    while True:
        line = _read_secret_line(f" {len(lines) + 1:>3} › ")
        if not line.strip():
            _drain_console_input()
            return lines
        lines.append(line)


def _drain_console_input() -> None:
    """Drop what is still unread after the paste ended (a blank line in the middle): it must not feed the next prompt."""
    if not _pools_console_paste():
        return
    import msvcrt
    time.sleep(0.2)  # the rest of a long paste is still arriving
    while msvcrt.kbhit():
        msvcrt.getwch()


def _read_accounts_file() -> list[str] | None:
    """Ask for a path (default accounts.txt) and read it; None = cancelled or unreadable (the reason is shown)."""
    path = prompt_user("Путь к файлу с аккаунтами (почта, пароль и т. д. в строке)", "accounts.txt").strip("\"' ")
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError:
        tui.status_line("err", "Файл не в кодировке UTF-8 — сохраните его как UTF-8")
        return None
    except OSError as exc:
        tui.status_line("err", f"Не удалось прочитать файл: {exc.strerror or exc.__class__.__name__}")
        return None


def _setup_register(config_path: Path, lines: list[str]) -> Any:
    """Register the account lines through the CLI importer (existing accounts are skipped, not overwritten)."""
    from config import load_config
    from youtube_bot import register_account_lines

    cfg = load_config(config_path)
    return _pools_db(config_path, lambda db: register_account_lines(db, cfg, lines, skip_existing=True))


def _setup_profile(config_path: Path, accounts: list[tuple[Any, bool]]) -> tuple[str, str] | None:
    """«Профиль канала»: pick an account (Space/Enter), then edit its card; back in the picker for the next one.

    Returns the last notice to show on the accounts screen, or None.
    """
    from modules.account_menu_logic import profile_status_label

    notice: tuple[str, str] | None = None
    while True:
        if len(accounts) == 1:
            account = accounts[0][0]
        else:
            account = _setup_pick_account(accounts, "Профиль канала — какой аккаунт?", profile_status_label)
        if account is None:
            return notice
        notice = _profile_card(config_path, account.name) or notice
        if len(accounts) == 1:
            return notice
        accounts = _setup_accounts(config_path)


def _profile_card(config_path: Path, account_name: str) -> tuple[str, str] | None:
    """Card of one account's channel profile: current values, edit rows, «Применить на YouTube». Esc/«0» leaves."""
    from modules.account_menu_logic import (
        plural_changes,
        profile_action_label,
        profile_card_lines,
        profile_pending_fields,
    )

    selected = 0
    notice: tuple[str, str] | None = None
    last: tuple[str, str] | None = None
    while True:
        account = _pools_db(config_path, lambda db: db.get_account(account_name))
        if account is None:
            return "err", f"Аккаунт «{account_name}» не найден"
        signed_in = account.auth_status == "authenticated"
        pending = profile_pending_fields(account)
        clear_terminal()
        tui.section_header(f"Профиль канала · {account.name}", "● вошёл" if signed_in else "○ нет входа — применить нельзя")
        label_w = max(len(label) for label, *_ in profile_card_lines(account))
        for label, value, hint, style in profile_card_lines(account):
            safe_print(f"   {label:<{label_w}}  {_fit(value, 30):<30}  {color(hint, _CLR_BY_THEME.get(style, CLR_DIM))}")
        if notice:
            safe_print()
            tui.status_line(*notice)
        rows: list[MenuRow | None] = [
            ("1", profile_action_label(account, "name"), "", ""),
            ("2", profile_action_label(account, "description"), "", ""),
            ("3", profile_action_label(account, "avatar"), "", ""),
            None,
            ("4", "Применить на YouTube", plural_changes(len(pending)) if pending else "нет правок", CLR_YELLOW if pending else CLR_DIM),
            ("0", "← Назад", "", ""),
        ]
        choice, selected = _setup_menu(lambda: rows, selected)
        notice = None
        if choice == "0":
            return last
        edit = {"1": _profile_edit_name, "2": _profile_edit_description, "3": _profile_edit_avatar}.get(choice)
        if edit is not None:
            notice = edit(config_path, account)
        elif choice == "4":
            notice = _profile_apply(config_path, account, pending)
        if notice and notice[0] in {"ok", "err"}:
            last = notice


def _profile_edit_name(config_path: Path, account: Any) -> tuple[str, str] | None:
    from modules.account_menu_logic import NICKNAME_MAX, validate_nickname

    try:
        text = prompt_user(f"Никнейм канала (до {NICKNAME_MAX} символов, пусто — отмена)")
    except WizardBack:
        return None
    if not text:
        return None
    try:
        name = validate_nickname(text)
    except ValueError as exc:
        return "warn", str(exc)
    _pools_db(config_path, lambda db: db.set_account_profile_draft(account.id, name=name))
    return "ok", "Никнейм сохранён — «Применить на YouTube», когда всё готово"


def _profile_edit_description(config_path: Path, account: Any) -> tuple[str, str] | None:
    from modules.account_menu_logic import DESCRIPTION_MAX, validate_description

    safe_print(color(
        f" Описание: вводите или вставляйте строки; пустая строка — абзац, две подряд — конец; «-» — очистить"
        f" (до {DESCRIPTION_MAX} символов). Сразу пустая строка — отмена",
        CLR_DIM,
    ))
    lines: list[str] = []
    try:
        while True:
            line = input("   ")
            if not line.strip() and (not lines or not lines[-1].strip()):  # first blank cancels, a second one ends
                break
            lines.append(line)
    except (KeyboardInterrupt, EOFError):
        return None
    if not any(line.strip() for line in lines):
        return None
    try:
        description = validate_description(lines)
    except ValueError as exc:
        return "warn", str(exc)
    _pools_db(config_path, lambda db: db.set_account_profile_draft(account.id, description=description))
    return "ok", "Описание сохранено — «Применить на YouTube», когда всё готово"


def _profile_edit_avatar(config_path: Path, account: Any) -> tuple[str, str] | None:
    from modules.account_menu_logic import applied_snapshot, clean_path_input
    from modules.profile_manager import AvatarError, forget_avatar, import_avatar

    try:
        text = prompt_user("Путь к картинке (png/jpg/gif/bmp до 4 МБ, можно перетащить файл в окно; пусто — отмена)")
    except WizardBack:
        return None
    if not text:
        return None
    try:
        stored = import_avatar(clean_path_input(text), account.name)
    except (AvatarError, OSError) as exc:
        return "warn", str(exc) if isinstance(exc, AvatarError) else f"Файл не прочитан: {exc.__class__.__name__}"
    _pools_db(config_path, lambda db: db.set_account_profile_draft(account.id, avatar=stored))
    forget_avatar(account.profile_avatar, keep=(stored, applied_snapshot(account).get("avatar")))
    return "ok", "Аватарка сохранена — «Применить на YouTube», когда всё готово"


def _profile_apply(config_path: Path, account: Any, pending: list[str]) -> tuple[str, str] | None:
    """Publish the draft in one browser session; returns the notice (None = cancelled)."""
    from config import load_config
    from modules.account_menu_logic import format_profile_summary, plural_changes
    from modules.profile_manager import ProfileEditor

    if not pending:
        return "info", "Нет правок для применения"
    if account.auth_status != "authenticated":
        return "warn", "Сначала войдите в аккаунт (пункт «Войти в аккаунт»)"
    try:
        question = f"Применить на YouTube: {plural_changes(len(pending))} (откроется браузер, ~1–2 мин)? (y/N)"
        if not _yes(prompt_user(question, "n")):
            return None
        cfg = load_config(config_path)
        tui.status_line("info", "Применяю профиль — не закрывайте окно браузера")
        result = _pools_db(config_path, lambda db: ProfileEditor(cfg.browser).apply(account, db))
    except WizardBack:
        return "info", "Отменено"
    except KeyboardInterrupt:
        return "warn", "Применение прервано"
    except Exception as exc:
        return "err", f"Не удалось применить: {exc.__class__.__name__} (подробности в логе)"
    return format_profile_summary(
        result.account_name, result.success, result.changed_fields, result.details, result.skipped_fields
    )


def _setup_pick_warmup(added: list[Any]) -> list[str]:
    """Numbered list of the fresh accounts and the question who to warm up; returns their names (may be empty)."""
    from modules.account_menu_logic import parse_warm_selection

    safe_print()
    for number, acc in enumerate(added, 1):
        safe_print(f"   {color(f'{number:>2}.', CLR_YELLOW)} {_fit(acc.name, MENU_WIDTH - 8)}")
    while True:
        try:
            answer = prompt_user("В прогрев: a — всех, n — никого, или номера через пробел (1 3 5)", "n")
        except WizardBack:
            return []
        try:
            return [added[i].name for i in parse_warm_selection(answer, len(added))]
        except ValueError as exc:
            tui.status_line("warn", str(exc))


def _setup_import(config_path: Path) -> tuple[str, str] | None:
    """«Импорт аккаунтов»: a file or a pasted list → register → optional warm-up of the fresh accounts.

    Prints the summary itself (the screen is redrawn afterwards) and returns a one-line notice.
    """
    from modules.account_menu_logic import clean_pasted_lines, count_account_lines, format_import_summary

    rows: list[MenuRow | None] = [
        ("1", "Из файла", "accounts.txt — строка на аккаунт", CLR_DIM),
        ("2", "Вставить строки", "почта пароль [TOTP], пустая строка — конец", CLR_DIM),
        None,
        ("0", "← Отмена", "", ""),
    ]
    choice = _pools_choice(rows, [color(" Импорт аккаунтов", CLR_BOLD)])
    try:
        lines = _read_accounts_file() if choice == "1" else _read_account_paste() if choice == "2" else None
    except WizardBack:
        lines = None
    if lines is None:
        return None
    lines = clean_pasted_lines(lines)
    account_lines = count_account_lines(lines)
    if not account_lines:
        for kind, text in format_import_summary(0, [], [], 0):
            tui.status_line(kind, text)
        _pause()
        return None

    try:
        result = _setup_register(config_path, lines)
    except KeyboardInterrupt:
        return "warn", "Импорт прерван — часть аккаунтов могла добавиться, проверьте список"
    except Exception as exc:  # a broken DB or config: the lines are not in the message
        return "err", f"Импорт не выполнен ({exc.__class__.__name__}) — часть аккаунтов могла добавиться, проверьте список"
    safe_print()
    for kind, text in format_import_summary(len(result.added), result.skipped, result.errors, account_lines):
        tui.status_line(kind, text)

    if result.added:
        names = _setup_pick_warmup(result.added)
        if names:
            tui.status_line("info", f"Прогрев: {len(names)} акк. — не закрывайте окна браузера")
            tui.status_line(*_setup_run_warmup(config_path, names))
    _pause()
    return ("ok" if result.added else "warn"), f"Импорт: добавлено {len(result.added)} из {account_lines}"


def _setup_run_warmup(config_path: Path, names: list[str] | None = None) -> tuple[str, str]:
    """Run the warm-up and return its (kind, summary); the per-account lines are printed as they come.

    `names` None: every signed-in account in quarantine or ageing (like `--warm-quarantined`); otherwise the picked
    accounts — a not yet signed-in one is signed in first. Both go through the CLI's `warm_accounts`.
    """
    from config import load_config
    from modules.account_menu_logic import format_warm_summary
    from youtube_bot import warm_accounts, warm_candidates, warm_selected_cli

    def report(line: str) -> None:
        safe_print(f" {line}")

    try:
        if names is None:
            targets = warm_candidates(_pools_db(config_path, lambda db: db.list_active_accounts()))
            if not targets:
                return "warn", "Нет вошедших аккаунтов в карантине или aging"
            question = f"Прогреть аккаунтов: {len(targets)} (~{WARM_UP_SECONDS:.0f} с каждый, откроется браузер)? (y/N)"
            if not _yes(prompt_user(question, "n")):
                return "info", "Отменено"
            cfg = load_config(config_path)
            results = _pools_db(config_path, lambda db: warm_accounts(db, cfg, targets, WARM_UP_SECONDS, report=report))
        else:
            results = asyncio.run(warm_selected_cli(str(config_path), names, WARM_UP_SECONDS, report=report))
    except WizardBack:
        return "info", "Отменено"
    except KeyboardInterrupt:
        return "warn", "Прогрев прерван"
    except Exception as exc:
        return "err", f"Прогрев не удался: {exc.__class__.__name__} (подробности в логе)"
    return format_warm_summary(results)


def _setup_warmup_all(config_path: Path) -> tuple[str, str]:
    """«Прогрев»: all signed-in quarantine/aging accounts after a short confirmation; summary stays until Enter."""
    kind, text = _setup_run_warmup(config_path)
    tui.status_line(kind, text)
    _pause()
    return kind, text


def _setup_lifecycle(config_path: Path) -> None:
    """«Жизненный цикл»: read-only table of stage, today's quota, heartbeat and pause/breaker per account."""
    from modules.account_menu_logic import lifecycle_rows
    from modules.session_heartbeat import check_session_offline

    try:
        accounts = _pools_db(config_path, lambda db: db.list_accounts())
    except Exception as exc:
        tui.status_line("err", f"Не удалось прочитать аккаунты: {exc.__class__.__name__}")
        _pause()
        return
    clear_terminal()
    tui.section_header("Жизненный цикл", "стадия · квота за сутки · heartbeat · пауза")
    if not accounts:
        tui.status_line("warn", "Аккаунтов нет — добавьте или импортируйте")
        _pause()
        return
    rows = lifecycle_rows(accounts, lambda a: check_session_offline(a.session_path, a.name).is_alive)
    shown = rows[:_LIFECYCLE_MAX_ROWS]
    stage_w, quota_w, beat_w = 10, 5, 15
    name_w = min(18, max(len("Аккаунт"), *(len(row.name) for row in shown)))  # accounts are often named by e-mail
    pause_w = MENU_WIDTH - 3 - name_w - stage_w - quota_w - beat_w - 8
    styles = {"ok": CLR_GREEN, "warn": CLR_YELLOW, "err": CLR_RED, "accent": CLR_CYAN, "dim": CLR_DIM, "": ""}

    def cell(text: str, width: int, style: str = "") -> str:
        padded = f"{_fit(text, width):<{width}}"
        return color(padded, styles.get(style, "")) if styles.get(style) else padded

    safe_print(color(
        f"   {'Аккаунт':<{name_w}}  {'Стадия':<{stage_w}}  {'Квота':<{quota_w}}  {'Heartbeat':<{beat_w}}  Пауза", CLR_DIM,
    ))
    for row in shown:
        safe_print(
            f"   {cell(row.name, name_w, '')}  {cell(row.stage, stage_w, row.stage_style)}  {cell(row.quota, quota_w)}"
            f"  {cell(row.heartbeat, beat_w, row.heartbeat_style)}  {cell(row.pause, pause_w, row.pause_style)}".rstrip()
        )
    if len(rows) > len(shown):
        safe_print(color(f"   … ещё {len(rows) - len(shown)}", CLR_DIM))
    safe_print(color("   Квота — комментарии за сутки / лимит стадии. Heartbeat: сессия · последний прогрев.", CLR_DIM))
    _pause()


def step_deepseek(config_path: Path, current_key: str, base_url: str) -> str:
    """Step 3: DeepSeek API key configuration & validation. «0»/Esc raises WizardBack."""
    from config import load_config

    def rows() -> list[MenuRow | None]:
        cfg = load_config(config_path)
        if getattr(cfg.processor, "reply_mode", "ai") == "ai":
            mode = "ИИ DeepSeek"
        else:
            mode = f"свой текст · шаблонов: {len(getattr(cfg.processor, 'custom_replies', []))}"
        return [
            ("2", "Новый ключ", "получить: platform.deepseek.com", CLR_DIM),
            ("3", "Проверить связь", "", ""),
            ("4", "Режим ответов", mode, CLR_DIM),
            None,
            ("1", "Продолжить →", "", ""),
            ("0", "← Назад", "", ""),
        ]

    selected = _setup_index(rows(), "1")
    tui.take_status()
    first = True
    while True:
        # every pass redraws the screen: only the latest result stays, under the header
        _setup_screen(
            3, "DeepSeek", "нейросеть пишет ответы",
            [("Ключ", mask_secret(current_key), "" if current_key else "warn")],
            None if first else tui.take_status(),
        )
        first = False
        tui.take_status()

        choice, selected = _setup_menu(rows, selected)

        if choice == "0":
            raise WizardBack()
        if choice == "1":
            return current_key

        if choice == "4":
            step_reply_mode(config_path)
            tui.take_status()  # the reply-mode screen's last notice must not reappear here
            continue

        if choice == "3":
            tui.status_line("info", "Проверяем ключ…")
            ok, msg = asyncio.run(check_deepseek_api(current_key, base_url))
            tui.status_line("ok" if ok else "err", msg)
            continue

        if choice == "2":
            try:
                new_key = prompt_user("Ключ DeepSeek (sk-…)").strip()
            except WizardBack:
                continue
            if not new_key:
                tui.status_line("warn", "Ключ не введён, оставлен прежний")
                continue

            tui.status_line("info", "Проверяем новый ключ…")
            ok, msg = asyncio.run(check_deepseek_api(new_key, base_url))
            tui.status_line("ok" if ok else "err", msg)
            if not ok:
                try:
                    if not _yes(prompt_user("Сохранить всё равно? (y/N)", "n")):
                        continue
                except WizardBack:
                    continue
            update_config_file(config_path, {"deepseek.api_key": new_key})
            tui.status_line("ok", "Ключ сохранён")
            return new_key

        tui.status_line("warn", f"Неизвестная команда: {choice}")


def step_reply_mode(config_path: Path) -> tuple[str, list[str]]:
    """Reply mode screen: DeepSeek AI or own text templates. «0»/Esc returns (mode, templates)."""
    from config import load_config

    def save_replies(replies: list[str], mode: str | None = None) -> None:
        updates: dict[str, Any] = {"processor.custom_replies": replies, "custom_replies": replies}
        if mode:
            updates.update({"processor.reply_mode": mode, "reply_mode": mode})
        update_config_file(config_path, updates)

    def ask_text(question: str) -> str:
        try:
            return prompt_user(question).strip()
        except WizardBack:
            return ""

    selected: int | None = None
    notice: tuple[str, str] | None = None
    while True:
        cfg = load_config(config_path)
        current_mode = getattr(cfg.processor, "reply_mode", "ai")
        current_replies = list(getattr(cfg.processor, "custom_replies", []))
        ai = current_mode == "ai"
        paraphrase = bool(getattr(cfg.processor, "paraphrase_custom", True))

        status: list[Any] = [
            ("Режим", "ИИ DeepSeek" if ai else "свой текст"),
            ("Шаблоны", str(len(current_replies)) if current_replies else "нет", "" if ai or current_replies else "warn"),
        ]
        if not ai:
            status.append(("Рандомизация", "вкл: ИИ меняет слова каждый раз" if paraphrase else "выкл: шаблон уходит как есть"))
        status += [(f"  {i}.", r) for i, r in enumerate(current_replies[:5], 1)]
        if len(current_replies) > 5:
            status.append(("", f"… ещё {len(current_replies) - 5}", "dim"))
        _setup_screen(3, "Режим ответов", "ИИ или свои шаблоны", status, notice)
        notice = None

        rows: list[MenuRow | None] = [
            ("1", "ИИ DeepSeek", "● выбран" if ai else "", CLR_DIM),
            ("2", "Свой текст", "вместо ИИ — ваши шаблоны" if ai else "● выбран", CLR_DIM),
            ("3", "Добавить шаблон", "", ""),
            ("4", "Удалить шаблон", "", ""),
            ("P", "Рандомизация текста", "● вкл: ИИ меняет слова каждый раз" if paraphrase else "выкл: шаблон уходит как есть", CLR_DIM),
            None,
            ("0", "← Назад", "", ""),
        ]
        if selected is None:
            selected = 0 if ai else 1
        choice, selected = _setup_menu(lambda: rows, selected)

        if choice == "0":
            return current_mode, current_replies

        if choice == "1":
            update_config_file(config_path, {"processor.reply_mode": "ai", "reply_mode": "ai"})
            notice = ("ok", "Ответы пишет ИИ DeepSeek")
        elif choice == "2":
            if current_replies:
                update_config_file(config_path, {"processor.reply_mode": "custom", "reply_mode": "custom"})
                notice = ("ok", "Ответы из своих шаблонов")
            elif text := ask_text("Первый шаблон"):
                save_replies([text], "custom")
                notice = ("ok", "Шаблон добавлен, режим «свой текст»")
            else:
                notice = ("warn", "Шаблон не введён, режим не изменён")
        elif choice == "P":
            # explicit on / off instead of a blind flip: a second Enter can't silently undo the choice
            picks: list[MenuRow | None] = [
                ("1", "Включена", "ИИ меняет слова каждый раз", CLR_DIM),
                ("2", "Выключена", "шаблон уходит как есть", CLR_DIM),
            ]
            pick, _ = arrow_menu(lambda: picks, [" Рандомизация текста"], 0 if paraphrase else 1, esc_key="0")
            if pick not in {"1", "2"}:
                continue
            update_config_file(config_path, {"processor.paraphrase_custom": pick == "1"})
            notice = ("ok", "Рандомизация текста включена") if pick == "1" else ("info", "Рандомизация текста выключена")
            if pick == "1" and not getattr(cfg.deepseek, "api_key", ""):
                notice = ("warn", "Включено, но ключ DeepSeek не задан - пока шаблон уйдёт как есть")
        elif choice == "3":
            if text := ask_text("Текст шаблона"):
                save_replies(current_replies + [text])
                notice = ("ok", "Шаблон добавлен")
            else:
                notice = ("warn", "Пустой текст не добавлен")
        elif choice == "4":
            if not current_replies:
                notice = ("warn", "Шаблонов нет")
                continue
            picks: list[MenuRow | None] = [(str(i), _fit(r, MENU_WIDTH - 6), "", "") for i, r in enumerate(current_replies, 1)]
            picks += [None, ("A", "Все шаблоны", "", CLR_DIM), ("0", "← Отмена", "", "")]
            pick, _ = arrow_menu(
                lambda: picks, [" Какой шаблон удалить?"], esc_key="0",
            )
            if pick == "A":
                try:
                    if _yes(prompt_user("Удалить все шаблоны? (y/N)", "n")):
                        save_replies([])
                        notice = ("ok", "Все шаблоны удалены")
                except WizardBack:
                    pass
            elif pick.isdigit() and 1 <= int(pick) <= len(current_replies):
                removed = current_replies.pop(int(pick) - 1)
                save_replies(current_replies)
                notice = ("ok", f"Удалён: {_fit(removed, 50)}")
            elif pick not in {"", "0"}:
                notice = ("err", "Неверный номер")
        elif choice == "5":
            try:
                if _yes(prompt_user("Удалить все шаблоны? (y/N)", "n")):
                    save_replies([])
                    notice = ("ok", "Шаблоны удалены")
            except WizardBack:
                pass
        else:
            notice = ("warn", f"Неизвестная команда: {choice}")


def step_video(config_path: Path, current_limit: int) -> int:
    """Section «Цель»: video list, reply limit, auto-search and target mode; returns the limit.

    Esc / «Готово» returns the current limit, hidden key 0 raises WizardBack.
    """
    from config import load_config

    def rows() -> list[MenuRow | None]:
        cfg = load_config(config_path)
        mode = getattr(cfg.collector, "target_mode", "first_reply")
        pool_n = _target_pool_info(cfg.app.db_path)[0]
        if query := getattr(cfg.collector, "search_query", None):  # views stay in the submenu: the row must fit
            age = getattr(cfg.collector, "max_age_hours", None)
            video_row = ("1", "Видео", f"автопоиск \"{_fit(query, 24)}\"" + (f" · ≤{_target_age_label(age)}" if age else ""), CLR_DIM)
        else:
            video_row = ("1", "Видео", f"{pool_n} в очереди" if pool_n else "список пуст", CLR_DIM if pool_n else CLR_YELLOW)
        return [
            video_row,
            ("6", "Режим цели", _target_mode_label(mode), CLR_DIM),
            ("2", "Лимит", f"{current_limit} на видео", CLR_DIM),
            ("3", "Предпросмотр", "комментарии к видео", CLR_DIM),
            None,
            *([("4", "Продолжить →", "", ""), ("0", "← Назад", "", "")] if _setup_in_wizard else [("4", "← Готово", "", "")]),
        ]

    def enter() -> None:
        clear_terminal()
        if _setup_in_wizard:
            print_step_header(4, 5, "Цель", "что и где комментировать")
        else:
            tui.section_header("Цель", "что и где комментировать")

    enter()
    selected = 0
    redraw = False
    while True:
        if redraw:  # wipe the previous menu and results; keep only the latest result under the header
            notice = tui.take_status()
            enter()
            if notice:
                tui.status_line(*notice)
        redraw = True
        tui.take_status()
        choice, selected = arrow_menu(
            rows, [], selected,
            extra_keys=frozenset("0"), esc_key="4",
        )

        if choice == "0":
            raise WizardBack()
        if choice in {"4", ""}:
            return current_limit

        if choice == "1":
            action = _target_video_menu(config_path)
            if action == "1":
                _pools_paste(config_path, comments_target=current_limit)
            elif action == "2":
                _target_show_pool(config_path)
            elif action == "3":
                _pools_clear(config_path)
            elif action == "5":
                _target_edit_search(config_path)
            elif action == "7":
                _target_pick_age(config_path)
            continue

        if choice == "2":
            lim_str = prompt_user("Ответов на видео (1-50)", str(current_limit))
            try:
                lim = int(lim_str)
            except ValueError:
                tui.status_line("err", "Нужно число от 1 до 50")
                continue
            if not 1 <= lim <= 50:
                tui.status_line("err", "Нужно число от 1 до 50")
                continue
            current_limit = lim
            update_config_file(config_path, {
                "limits.max_iterations_per_session": lim,
                "limits.total_iterations": lim,
                "collector.comments_per_video": lim,
            })
            changed = _pools_db(config_path, lambda db: db.set_pool_comments_target(lim))
            tui.status_line("ok", f"Лимит: {lim} на видео" + (f" · видео в списке обновлено: {changed}" if changed else ""))
            continue

        if choice == "3":
            _target_preview(config_path, current_limit)
            continue

        if choice == "6":
            _target_pick_mode(config_path)
            continue

        tui.status_line("warn", f"Неизвестная команда: {choice}")


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


def _target_mode_label(mode: str) -> str:
    """Short Russian label of a target mode (first_sub_comment is an alias of first_reply)."""
    mode = "first_reply" if str(mode).lower() == "first_sub_comment" else str(mode).lower()
    return next((label for value, label, _ in _TARGET_MODES if value == mode), mode)


def _target_short_url(url: str) -> str:
    """Video URL without the youtube.com/watch?v= prefix."""
    return _fit(re.sub(r"^https?://(www\.)?(youtube\.com/watch\?v=)?", "", url), 28)


def _target_search(cfg: Any) -> str:
    """'"query" · min..max [· ≤24 ч]' when auto-search is on, else ''."""
    query = getattr(cfg.collector, "search_query", None)
    if not query:
        return ""
    max_v = getattr(cfg.collector, "max_views", None)
    age = getattr(cfg.collector, "max_age_hours", None)
    text = f"\"{_fit(query, 24)}\" · {getattr(cfg.collector, 'min_views', 0)}..{max_v or 'макс'} просм."
    return text + (f" · ≤{_target_age_label(age)}" if age else "")


# Auto-search freshness: (max video age in hours or None = any, label).
_TARGET_AGES: list[tuple[int | None, str]] = [
    (None, "Любые"), (12, "12 часов"), (24, "24 часа"), (48, "2 дня"), (168, "7 дней"),
]


def _target_age_label(hours: int | None) -> str:
    """'24 часа' for a preset, '36 ч' for a hand-written value, 'любые' for None."""
    if not hours:
        return "любые"
    return next((label for value, label in _TARGET_AGES if value == hours), f"{hours} ч")


def _target_pick_age(config_path: Path) -> None:
    """Arrow menu «Свежесть» for auto-search; the cursor starts on the current value, Esc keeps it."""
    from config import load_config

    current = getattr(load_config(config_path).collector, "max_age_hours", None)
    values = [value for value, _ in _TARGET_AGES]
    rows: list[MenuRow | None] = [
        (str(i), label if value is None else f"Не старше {label}", "" if value else "без фильтра по дате", CLR_DIM)
        for i, (value, label) in enumerate(_TARGET_AGES, 1)
    ]
    tui.status_line("info", "Как давно опубликованы видео в автопоиске:")
    choice, _ = arrow_menu(lambda: rows, [], values.index(current) if current in values else 0, esc_key="0")
    if not choice.isdigit() or not 1 <= int(choice) <= len(values):
        return
    new_age = values[int(choice) - 1]
    update_config_file(config_path, {"collector.max_age_hours": new_age})
    tui.status_line("ok", f"Свежесть: {_target_age_label(new_age)}")


_TARGET_PENDING_SQL = "status IN ('pending', 'in_progress') AND comments_done < comments_target"


def _target_pool_info(db_path: Any) -> tuple[int, str | None]:
    """(videos waiting in the pool, URL of the next one); (0, None) if the DB has no pool yet.

    Plain read-only sqlite3 (same filter as Database.list_pool_videos('pending')): this runs
    while menus are rebuilt, sometimes inside prompt_toolkit's event loop, where asyncio.run fails.
    """
    try:
        uri = f"file:{quote(Path(db_path).as_posix(), safe='/:')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM video_pool WHERE {_TARGET_PENDING_SQL}").fetchone()[0]
            row = conn.execute(f"SELECT video_url FROM video_pool WHERE {_TARGET_PENDING_SQL} ORDER BY id LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0, None
    return int(count), (row[0] if row else None)


def _accounts_proxy_counts(db_path: Any) -> tuple[int, int]:
    """(accounts with a proxy, all accounts); read-only sqlite3 for the same reason as the pending count."""
    try:
        uri = f"file:{quote(Path(db_path).as_posix(), safe='/:')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            total, with_proxy = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(proxy_url IS NOT NULL AND proxy_url <> ''), 0) FROM accounts"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return 0, 0
    return int(with_proxy), int(total)


def _proxy_summary(cfg: Any, fallback: str | None = None) -> str:
    """Short proxy state for the dashboard: per-account proxies; the global one only while no account exists."""
    with_proxy, total = _accounts_proxy_counts(cfg.app.db_path)
    if total == 0:
        return "через прокси" if (fallback or cfg.browser.proxy) else "без прокси"
    if with_proxy == 0:
        return "без прокси"
    if total == 1 or with_proxy == total:
        return "через прокси" if total == 1 else f"прокси у всех ({total})"
    return f"прокси: {with_proxy} из {total}"


def _proxy_row(cfg: Any, fallback: str | None = None) -> tuple[str, str, str]:
    """Summary row «Прокси»: yellow while any account goes without a proxy."""
    text = _proxy_summary(cfg, fallback)
    return "Прокси", text, "warn" if text == "без прокси" or text.startswith("прокси:") else ""


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


def _reply_templates_row(templates: list[str]) -> tuple[str, str]:
    """«Ответы» value for own-text mode: the template itself (the first one, then how many more)."""
    if not templates:
        return "свой текст · шаблонов нет", "warn"
    first = _fit(str(templates[0]), 40)
    if len(templates) == 1:
        return f"свой текст: «{first}»", ""
    return f"свой текст: «{first}» · и ещё {len(templates) - 1}", ""


def _target_plan(cfg: Any) -> tuple[str, int, str]:
    """What a launch runs on, in priority order: auto-search, else the video list.

    Returns ("search", 0, query) / ("pool", waiting videos, next URL) / ("none", 0, "") when there is nothing.
    """
    query = getattr(cfg.collector, "search_query", None)
    if query:
        return "search", 0, query
    count, first = _target_pool_info(cfg.app.db_path)
    if count and first:
        return "pool", count, first
    return "none", 0, ""


_TARGET_NONE = "нет видео · вставьте список или включите автопоиск"


def _target_pool_text(count: int, limit: int) -> str:
    return f"{count} видео · {limit} ответов на каждое"


def _target_video_menu(config_path: Path) -> str:
    """«Видео» submenu: paste / show / clear the video list, or auto-search and its freshness; returns the key.

    Auto-search, when on, is used instead of the list.
    """
    from config import load_config

    cfg = load_config(config_path)
    count = _target_pool_info(cfg.app.db_path)[0]
    search = _target_search(cfg)
    rows: list[MenuRow | None] = [
        ("1", "+ Вставить", "ссылки по одной в строке", CLR_DIM),
        ("2", "≡ Показать", f"в очереди: {count}" + (" · не используется" if search else ""), CLR_DIM),
        ("3", "× Очистить", "", ""),
        None,
        ("5", "Автопоиск", search or "выключен", CLR_DIM),
        *([("7", "Свежесть", _target_age_label(getattr(cfg.collector, "max_age_hours", None)), CLR_DIM)] if search else []),
        None,
        ("0", "← Назад", "", ""),
    ]
    choice = _pools_choice(rows, [color(" Видео", CLR_BOLD)])
    return choice if choice in {"1", "2", "3", "5", "7"} else ""


def _target_show_pool(config_path: Path, shown: int = 10) -> None:
    """First `shown` pool videos with their reply progress, then Enter to go back."""
    videos = _pools_db(config_path, lambda db: db.list_pool_videos(status="all"))
    if not videos:
        tui.status_line("info", "Список видео пуст")
        return
    for i, v in enumerate(videos[:shown], 1):
        done = v.status == "completed" or v.comments_done >= v.comments_target
        line = f" {i:>2}. {_target_short_url(v.video_url):<28} {v.comments_done}/{v.comments_target}"
        safe_print(color(line + (" · готово" if done else ""), CLR_DIM if done else ""))
    if len(videos) > shown:
        safe_print(color(f"     … ещё {len(videos) - shown}", CLR_DIM))
    prompt_user("Enter — назад", "")


def _target_edit_search(config_path: Path) -> None:
    """Ask query and view range; '0' / empty without a current query turns auto-search off."""
    from config import load_config

    cfg = load_config(config_path)
    search_q = getattr(cfg.collector, "search_query", None)
    min_v = getattr(cfg.collector, "min_views", 0)
    max_v = getattr(cfg.collector, "max_views", None)

    q = prompt_user("Запрос для поиска (0 - выключить)", search_q or "")
    if not q or q.lower() in {"0", "none", "off", "нет", "сброс"}:
        update_config_file(config_path, {"collector.search_query": None})
        tui.status_line("ok", "Автопоиск выключен, работаем по списку видео")
        return

    min_s = prompt_user("Мин. просмотров", str(min_v))
    max_s = prompt_user("Макс. просмотров (Enter - без предела)", str(max_v) if max_v else "")
    try:
        min_val = int(min_s) if min_s.strip() else 0
    except ValueError:
        min_val = 0
    try:
        max_val: int | None = int(max_s) if max_s.strip() else None
    except ValueError:
        max_val = None

    update_config_file(config_path, {
        "collector.search_query": q.strip(),
        "collector.min_views": min_val,
        "collector.max_views": max_val,
    })
    tui.status_line("ok", f"Автопоиск: \"{q.strip()}\" · {min_val}..{max_val or 'макс'} просм.")


def _target_pick_mode(config_path: Path) -> None:
    """Arrow menu of target modes; the cursor starts on the current one, Esc keeps it."""
    from config import load_config

    current = getattr(load_config(config_path).collector, "target_mode", "first_reply")
    current = "first_reply" if current == "first_sub_comment" else current
    values = [value for value, _, _ in _TARGET_MODES]
    rows: list[MenuRow | None] = [
        (str(i), label, hint, CLR_DIM) for i, (_, label, hint) in enumerate(_TARGET_MODES, 1)
    ]
    tui.status_line("info", "На какие комментарии отвечать:")
    choice, _ = arrow_menu(
        lambda: rows, [], values.index(current) if current in values else 0, esc_key="0",
        detail=lambda key: _TARGET_MODE_EXAMPLES.get(values[int(key) - 1], []) if key.isdigit() else [],
    )
    if choice not in {"1", "2", "3", "4"}:
        return
    new_mode = values[int(choice) - 1]
    update_config_file(config_path, {"collector.target_mode": new_mode})
    tui.status_line("ok", f"Режим цели: {_target_mode_label(new_mode)}")


def _target_preview(config_path: Path, limit: int) -> None:
    """Print sub-comments of the next video in the list, then wait for Enter."""
    from config import load_config

    video_url = _target_pool_info(load_config(config_path).app.db_path)[1]
    if not video_url:
        tui.status_line("info", "Список видео пуст")
        return
    tui.status_line("info", f"Загружаем комментарии: {_target_short_url(video_url)}")
    try:
        from youtube_bot import list_sub_comments
        asyncio.run(list_sub_comments(video_url, str(config_path), limit=limit))
    except Exception as exc:
        tui.status_line("err", f"Ошибка загрузки: {_fit(str(exc), 50)}")
    _pause()


def step_launch(
    config_path: Path,
    proxy_val: str | None,
    session_root: Path,
    api_key: str,
    limit: int,
) -> bool:
    """Section «Запуск» (step 5): compact summary, then Dry-Run / Live (confirmed) / stats / log.

    Returns True after a run; «Назад» / Esc raises WizardBack.
    """
    from config import load_config

    rows: list[MenuRow | None] = []

    def enter() -> bool:
        cfg = load_config(config_path)
        threads = _threads_summary(cfg)
        rows[:] = [
            ("1", "Пробный запуск", "Dry-Run · без публикации", CLR_DIM),
            ("2", "Боевой запуск", "Live · ответы публикуются", CLR_DIM),
            *([("5", "Потоки", threads, CLR_DIM)] if threads else []),
            ("3", "Статистика", "счётчики базы", CLR_DIM),
            ("4", "Журнал", "история ответов", CLR_DIM),
            None,
            ("0", "← Назад", "", ""),
        ]
        names = _logged_in_names(cfg)
        logged_in = bool(names) or check_youtube_session(session_root)[0]
        if getattr(cfg.processor, "reply_mode", "ai") == "ai":
            reply = (f"ИИ DeepSeek · {mask_secret(api_key)}", "" if api_key else "warn")
        else:
            reply = _reply_templates_row(getattr(cfg.processor, "custom_replies", []))
        clear_terminal()
        if _setup_in_wizard:
            print_step_header(5, 5, "Запуск", "проверьте настройки")
        else:
            tui.section_header("Запуск", "проверьте настройки")
        kind, count, _ = _target_plan(cfg)
        target = _target_search(cfg) if kind == "search" else (
            _target_pool_text(count, limit) if kind == "pool" else _TARGET_NONE
        )
        tui.kv_table([
            _proxy_row(cfg, proxy_val),
            ("YouTube", f"● вошёл: {_fit(', '.join(names), 40)}" if names else "● вошёл" if logged_in else "○ нет входа",
             "ok" if logged_in else "warn"),
            ("Ответы", *reply),
            ("Цель", target, "warn" if kind == "none" else ""),
            ("Режим", _target_mode_label(getattr(cfg.collector, "target_mode", "first_reply"))),
            ("Лимит", f"{limit} на видео"),
            *([("Потоки", threads)] if threads else []),
        ])
        safe_print()
        return logged_in

    is_logged_in = enter()
    selected = 0
    while True:
        choice, selected = arrow_menu(
            lambda: rows, [], selected, esc_key="0",
        )

        if choice == "0":
            raise WizardBack()
        if choice == "1":
            _target_run(config_path, limit, dry_run=True)
            return True
        if choice == "2":
            if not _target_confirm_live(is_logged_in):
                tui.status_line("info", "Боевой запуск отменён")
                continue
            _target_run(config_path, limit, dry_run=False)
            return True
        if choice in {"3", "4", "5"}:
            if choice == "3":
                _show_stats(config_path)
            elif choice == "4":
                activity_log_viewer(config_path)
            else:
                step_threads(config_path)
            is_logged_in = enter()
            continue
        if choice:
            tui.status_line("warn", f"Неизвестная команда: {choice}")


def _target_confirm_live(logged_in: bool) -> bool:
    """Live needs a 'y'; without a YouTube login it first warns and asks once more."""
    if not logged_in:
        tui.status_line("warn", "Вход в YouTube не выполнен - ответы не опубликуются")
        if not _yes(prompt_user("Всё равно продолжить? (y/N)", "n")):
            return False
    return _yes(prompt_user("Боевой запуск: ответы будут опубликованы. Запускаем? (y/N)", "n"))


def _target_run(config_path: Path, limit: int, dry_run: bool) -> None:
    """Run the bot on the target picked by _target_plan (search / video list), then wait for Enter.

    The video pool runs with all active accounts; threads, account mode and target mode come from the config.
    """
    from rich.markup import escape

    from config import load_config

    cfg = load_config(config_path)
    kind, count, query = _target_plan(cfg)
    if kind == "none":
        tui.status_line("warn", _TARGET_NONE.capitalize())
        return
    mode = getattr(cfg.collector, "target_mode", "first_reply")
    name = "Пробный запуск" if dry_run else "Боевой запуск"
    target = _target_search(cfg) if kind == "search" else _target_pool_text(count, limit)
    if kind == "pool" and _threads_summary(cfg):
        target += f" · потоки: {_threads_summary(cfg)}"
    tui.panel(f"{name} · {'без публикации' if dry_run else 'ответы публикуются'}", [f"[dim]{escape(target)}[/]"])
    try:
        if kind == "pool":
            from youtube_bot import run_video_pool_cli
            done = asyncio.run(run_video_pool_cli(config_path=str(config_path), dry_run=dry_run))
            tui.status_line("info", f"Ответов: {done}")
        else:
            from youtube_bot import run_multi_video_pipeline
            asyncio.run(
                run_multi_video_pipeline(
                    query,
                    config_path=str(config_path),
                    min_views=getattr(cfg.collector, "min_views", 0),
                    max_views=getattr(cfg.collector, "max_views", None),
                    max_videos=5,
                    comments_per_video=limit,
                    target_mode=mode,
                    dry_run=dry_run,
                )
            )
        tui.status_line("ok", f"{name} завершён")
    except Exception as exc:
        tui.status_line("err", f"Ошибка: {_fit(str(exc), 55)}")
    _pause()


_LOG_STATUS = {"success": "отправлен", "simulated": "пробный", "failed": "ошибка"}
# Journal status filter menu: (key, label, status value or None = all records).
_LOG_FILTERS: list[tuple[str, str, str | None]] = [
    ("1", "Все записи", None), ("2", "Отправленные", "success"), ("3", "Пробные", "simulated"), ("4", "Ошибки", "failed"),
]


def _log_time(value: Any, short: bool = False) -> str:
    """'2026-10-31T14:03:..' -> '2026-10-31 14:03' (short: '10-31 14:03')."""
    text = str(value or "")[:16].replace("T", " ")
    return (text[5:] if short else text) or "—"


def view_record_detail(db_path: str, record_id: int) -> None:
    """Card of one activity log record; Enter returns to the journal."""
    from rich.markup import escape

    from database import Database

    db = Database(db_path)

    async def _fetch():
        await db.connect()
        try:
            rows = await db._fetch_all("SELECT * FROM activity_log WHERE id = ?", (record_id,))
            return dict(rows[0]) if rows else None
        finally:
            await db.close()

    row = asyncio.run(_fetch())
    if not row:
        tui.status_line("err", f"Запись #{record_id} не найдена")
        prompt_user("Enter — назад", "")
        return

    clear_terminal()
    st = str(row.get("status") or "unknown")
    tui.section_header(f"Запись #{record_id}", f"{_log_time(row.get('created_at'))} UTC · {_LOG_STATUS.get(st, st)}")
    fields: list[tuple[str, str] | tuple[str, str, str]] = [
        ("Режим", str(row.get("mode") or "—")),
        ("Видео", str(row.get("video_title") or "—")),
        ("Ссылка", str(row.get("video_url") or row.get("video_id") or "—")),
        ("Автор", str(row.get("target_author") or "—")),
        ("ID", f"видео {row.get('video_id') or '—'} · коммент. {row.get('target_id') or '—'}"),
        ("Длительность", f"{row.get('duration_sec') or 0:.1f} с"),
    ]
    if row.get("error"):
        fields.append(("Ошибка", str(row["error"]), "err"))
    tui.kv_table(fields)
    tui.panel("Комментарий", [escape(str(row.get("target_text") or "—"))])
    tui.panel("Ответ", [escape(str(row.get("reply_text") or "—"))])
    prompt_user("Enter — назад", "")


def activity_log_viewer(config_path: Path) -> None:
    """Journal: one row per record (Enter — details), N/P pages, F status filter, C clear; DB summary on top."""
    from config import load_config
    from database import Database

    cfg = load_config(config_path)
    db_path = str(cfg.app.db_path)
    page = 1
    page_size = 8
    status_filter: str | None = None
    selected = 0
    notice = ""

    async def _load_page(page: int) -> tuple[int, int, list[Any], dict[str, Any]]:
        db = Database(db_path)
        await db.connect()
        await db.init_schema()
        try:
            total = await db.count_activity_logs(status_filter=status_filter)
            page = min(page, max(1, -(-total // page_size)))
            entries = await db.list_activity_logs(page=page, page_size=page_size, status_filter=status_filter)
            return total, page, entries, await db.stats()
        finally:
            await db.close()

    async def _clear() -> int:
        db = Database(db_path)
        await db.connect()
        try:
            return await db.clear_activity_log()
        finally:
            await db.close()

    while True:
        try:
            total_count, page, entries, stats = asyncio.run(_load_page(page))
        except Exception as exc:
            tui.status_line("err", f"Ошибка чтения базы данных: {exc}")
            _pause()
            return
        total_pages = max(1, -(-total_count // page_size))

        clear_terminal()
        flt = next((label for _, label, val in _LOG_FILTERS if val == status_filter), "")
        tui.section_header(
            "Журнал отработок",
            f"стр. {page}/{total_pages} · записей: {total_count}" + (f" · {flt.lower()}" if status_filter else ""),
        )
        summary = " · ".join(
            f"{name}: {', '.join(f'{k} {v}' for k, v in stats.get(key, {}).items()) or '—'}"
            for name, key in (("ответы", "activity"), ("очередь", "queue"), ("тексты", "payloads"))
        )
        header = [color(_fit(f" БД: {summary}", MENU_WIDTH), CLR_DIM)]
        if not entries:
            header.append(" Записей нет.")
        if notice:
            header.append(f" {notice}")
            notice = ""
        header.append("")

        rows: list[MenuRow | None] = [
            (
                str(e.id),
                f"{_log_time(e.created_at, short=True)}  {_fit(e.target_author or '@unknown', 16)}",
                f"{_LOG_STATUS.get(e.status, e.status)} · {(e.reply_text or e.error or '').replace(chr(10), ' ')}",
                CLR_RED if e.status == "failed" else CLR_DIM,
            )
            for e in entries
        ]
        if rows:
            rows.append(None)
        if page < total_pages:
            rows.append(("N", "Следующая страница", f"{page + 1}/{total_pages}", CLR_DIM))
        if page > 1:
            rows.append(("P", "Предыдущая страница", f"{page - 1}/{total_pages}", CLR_DIM))
        rows += [
            ("F", "Фильтр", flt.lower() if status_filter else "все записи", CLR_DIM),
            ("C", "Очистить журнал", "", ""),
            ("0", "← Назад", "", ""),
        ]
        choice, selected = arrow_menu(
            lambda: rows, header, selected,
            extra_keys=frozenset("NP"), esc_key="0",
        )

        if choice in {"0", "Q", "QUIT", "EXIT", "НАЗАД"}:
            return
        if choice == "N":
            if page < total_pages:
                page, selected = page + 1, 0
            else:
                notice = color("· Это последняя страница", CLR_DIM)
        elif choice == "P":
            if page > 1:
                page, selected = page - 1, 0
            else:
                notice = color("· Это первая страница", CLR_DIM)
        elif choice == "F":
            status_filter = _log_pick_filter(status_filter)
            page, selected = 1, 0
        elif choice == "C":
            if _yes(prompt_user("Очистить весь журнал? (y/N)", "n")):
                notice = color(f"● Журнал очищен, удалено записей: {asyncio.run(_clear())}", CLR_GREEN)
                page, selected = 1, 0
        elif choice.isdigit() or choice.startswith("V"):  # "V 12" is the old text command
            rec = choice.lstrip("V").strip() or prompt_user("ID записи")
            if rec.isdigit():
                view_record_detail(db_path, int(rec))
            else:
                notice = color(f"! Неверный ID записи: {rec}", CLR_YELLOW)
        elif choice:
            notice = color(f"! Неизвестная команда: {choice}", CLR_YELLOW)


def _log_pick_filter(current: str | None) -> str | None:
    """Status filter picker for the journal; Esc keeps the current filter."""
    keys = [k for k, _, _ in _LOG_FILTERS]
    selected = next((i for i, (_, _, val) in enumerate(_LOG_FILTERS) if val == current), 0)
    choice, _ = arrow_menu(
        lambda: [(k, label, "● сейчас" if val == current else "", CLR_DIM) for k, label, val in _LOG_FILTERS],
        [color(" Фильтр по статусу", CLR_BOLD)], selected, esc_key="",
    )
    if choice not in keys:
        return current
    return _LOG_FILTERS[keys.index(choice)][2]


def load_accounts(config_path: Path) -> list[Any]:
    """Load accounts from the DB, seeding it from config.yaml when the DB has none."""
    from config import load_config
    from database import Database

    cfg = load_config(config_path)
    db = Database(cfg.app.db_path)

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
            return accounts
        finally:
            await db.close()

    return asyncio.run(_fetch())


def list_accounts_wizard(config_path: Path) -> list[dict[str, Any]]:
    """Compact accounts table (≤ 70 columns): login, replies, proxy; inactive names are dim."""
    accounts = load_accounts(config_path)
    results: list[dict[str, Any]] = []
    if not accounts:
        tui.status_line("warn", "Аккаунтов пока нет — добавьте хотя бы один")
        return results

    safe_print(color(f" {'#':<2} {'Аккаунт':<18} {'Вход':<6} {'Ответов':>7}  Прокси", CLR_DIM))
    for idx, acc in enumerate(accounts, 1):
        is_logged, _ = check_youtube_session(Path(acc.session_path))
        name = color(f"{_fit(acc.name, 18):<18}", "" if acc.is_active else CLR_DIM)
        login = color(f"{'● да' if is_logged else '○ нет':<6}", CLR_DIM if is_logged else CLR_YELLOW)
        proxy = _fit(mask_proxy(acc.proxy_url) if acc.proxy_url else "без прокси", 22)
        safe_print(f" {idx:<2} {name} {login} {acc.total_replies:>7}  {proxy}")
        results.append({"account": acc, "is_logged_in": is_logged})
    safe_print(color(f" Всего: {len(accounts)} · активных: {sum(1 for a in accounts if a.is_active)}", CLR_DIM))
    return results


def _save_account(
    config_path: Path, name: str, session_path: str, norm_proxy: str | None, is_active: bool = True,
) -> None:
    """Upsert an account into the DB and mirror it into config.yaml."""
    from config import load_config
    from database import Database

    cfg = load_config(config_path)
    db = Database(cfg.app.db_path)

    async def _upsert():
        await db.connect()
        await db.init_schema()
        try:
            return await db.upsert_account(
                name=name,
                session_path=session_path,
                proxy_url=norm_proxy,
                is_active=is_active,
            )
        finally:
            await db.close()

    asyncio.run(_upsert())

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw_cfg = yaml.safe_load(f) or {}
        raw_browser = raw_cfg.setdefault("browser", {})
        accounts_list = raw_browser.setdefault("accounts", [])
        existing = next((a for a in accounts_list if isinstance(a, dict) and a.get("name") == name), None)
        if existing:
            existing["session_path"] = session_path
            existing["proxy_url"] = norm_proxy
            existing["is_active"] = is_active
        else:
            accounts_list.append({
                "name": name,
                "session_path": session_path,
                "proxy_url": norm_proxy,
                "is_active": is_active,
            })
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw_cfg, f, allow_unicode=True, sort_keys=False)
    except Exception as exc:
        safe_print(f" {color('[!]', CLR_YELLOW)} Не удалось синхронизировать config.yaml: {exc}")


def _pools_choice(
    rows: list[MenuRow | None],
    header: list[str],
    selected: int = 0,
) -> str:
    """Pick a row with arrows (Esc = "0"); an empty text answer picks the highlighted row."""
    choice, sel = arrow_menu(lambda: rows, header, selected, esc_key="0")
    if not choice and 0 <= sel < len(rows) and rows[sel] is not None:
        choice = rows[sel][0]
    return choice


def _pools_login(config_path: Path, name: str) -> bool:
    """Open the browser window for logging in to account `name`."""
    tui.status_line("info", f"Окно браузера для «{name}»: войдите в Google и закройте окно")
    try:
        from youtube_bot import interactive_login
        asyncio.run(interactive_login(str(config_path), account_name=name))
    except Exception as exc:
        tui.status_line("err", f"Ошибка авторизации: {exc}")
        return False
    tui.status_line("ok", f"Вход для «{name}» завершён")
    return True


def _pools_db(config_path: Path, fn: Callable[[Any], Any]) -> Any:
    """Run `await fn(db)` against the configured database."""
    from config import load_config
    from database import Database

    db_path = load_config(config_path).app.db_path

    async def _run() -> Any:
        db = Database(db_path)
        await db.connect()
        await db.init_schema()
        try:
            return await fn(db)
        finally:
            await db.close()

    return asyncio.run(_run())


def _pools_console_paste() -> bool:
    """Windows console with the real prompt_user: a paste can be read via msvcrt.kbhit()."""
    import types
    return sys.platform == "win32" and sys.stdin.isatty() and isinstance(prompt_user, types.FunctionType)


def _pools_read_paste(prompt: str = "Вставьте ссылки на видео и нажмите Enter") -> str:
    """Read a pasted multi-line list finished with a single Enter; an empty first line cancels.

    In a Windows console the pasted lines are already in the input buffer, so after the first
    line we keep reading while msvcrt.kbhit() sees more keys (short pause between lines).
    Elsewhere (tests, pipes, other OS) lines are read through prompt_user until an empty one.
    """
    first = prompt_user(prompt)
    if not first.strip():
        return ""
    lines = [first]
    if _pools_console_paste():
        import msvcrt
        while True:
            deadline = time.monotonic() + 0.2
            while not msvcrt.kbhit() and time.monotonic() < deadline:
                time.sleep(0.02)
            if not msvcrt.kbhit():
                break
            try:
                line = input()
            except (EOFError, KeyboardInterrupt):
                break
            if not line.strip():
                break
            lines.append(line)
    else:
        while True:
            line = prompt_user("…")
            if not line.strip():
                break
            lines.append(line)
    return "\n".join(lines)


def _pools_paste(config_path: Path, comments_target: int | None = None) -> bool:
    """Paste a list of videos into the video pool; prints the result line.

    New videos get `comments_target` replies each (default: the «Лимит» from the config).
    """
    from config import load_config
    from modules.pool_manager import PoolManager

    text = _pools_read_paste()
    if not text:
        tui.status_line("info", "Отменено")
        return False
    target = comments_target or load_config(config_path).limits.max_iterations_per_session or 2

    res = _pools_db(config_path, lambda db: PoolManager(db).add_videos_from_text(text, comments_target=target))
    tui.status_line(
        "ok" if res.added else "warn",
        f"+{res.added} добавлено · {res.existing} уже были · {res.invalid} не распознано",
    )
    return True


def _pools_clear(config_path: Path) -> None:
    """Delete the video list after a y/N confirmation."""
    if not _yes(prompt_user("Очистить список видео? (y/N)", "n")):
        tui.status_line("info", "Отменено")
        return
    tui.status_line("ok", f"Удалено видео: {_pools_db(config_path, lambda db: db.clear_video_pool())}")


def _pools_yes(question: str) -> bool:
    """Y/n question answered through prompt_user (default yes)."""
    return prompt_user(f"{question} (Y/n)", "y").lower() in {"y", "yes", "да", "д", "1"}


def login_account_wizard(config_path: Path) -> bool:
    """Pick an account with arrows (or «＋ Новый аккаунт») and open the browser to log in."""
    accounts = load_accounts(config_path)
    if not accounts:
        return add_account_stepwise(config_path)
    else:
        rows: list[MenuRow | None] = [("N", "＋ Новый аккаунт", "имя и вход в Google", CLR_DIM)]
        for idx, acc in enumerate(accounts, 1):
            logged = check_youtube_session(Path(acc.session_path))[0]
            rows.append((str(idx), _fit(acc.name, 24), "● вошёл" if logged else "○ нет входа", CLR_DIM if logged else CLR_YELLOW))
        rows += [None, ("0", "← Назад", "", "")]
        choice = _pools_choice(rows, [color(" Вход в аккаунт", CLR_BOLD)], selected=1)
        if choice == "0":
            return False
        if choice.isdigit() and 1 <= int(choice) <= len(accounts):
            acc_name = accounts[int(choice) - 1].name
        elif choice == "N":
            return add_account_stepwise(config_path)
        else:  # text fallback: an account typed by name
            acc_name = next((a.name for a in accounts if a.name.upper() == choice.upper()), "")
            if not acc_name:
                tui.status_line("warn", f"Аккаунт не найден: {choice}")
    if not acc_name:
        return False
    return _pools_login(config_path, acc_name)


def add_account_stepwise(config_path: Path) -> bool:
    """Add an account step by step: name, optional login (the proxy is attached on stage 2).

    The session folder is not asked: test_sessions/<name> (an existing account keeps its own).
    """
    def step(n: int) -> str:
        return color(f"{n}/2", CLR_CYAN)

    safe_print(color(" Новый аккаунт · пустое имя — отмена", CLR_DIM))
    name = prompt_user(f"{step(1)} Имя аккаунта — для вас: ник или почта (например: main, shop_2)").strip()
    if not name:
        return False
    existing = next((a for a in load_accounts(config_path) if a.name == name), None)
    if existing:
        tui.status_line("warn", f"«{name}» уже есть — его настройки будут обновлены")

    session_path = str(existing.session_path) if existing else f"test_sessions/{name}"
    _save_account(config_path, name, session_path, existing.proxy_url if existing else None)
    tui.status_line("ok", f"Аккаунт «{name}» сохранён")

    if _pools_yes(f"{step(2)} Войти в аккаунт в браузере сейчас?"):
        _pools_login(config_path, name)
    return True


# Account modes for 2+ accounts: (config value, short label, one-line hint).
_ACCOUNT_MODES: list[tuple[str, str, str]] = [
    ("each", "Каждый — весь список", "все аккаунты проходят все видео"),
    ("share", "Список делится", "ответы под видео делятся между аккаунтами"),
]

# Space in the threads screen: what the row does, on one small example.
_THREADS_EXAMPLES: dict[str, list[str]] = {
    "1": [
        "Сколько аккаунтов работают одновременно (у каждого своё окно).",
        "1 — аккаунты по очереди: первый закончил, начинает второй.",
        "2 — два аккаунта сразу, и так далее.",
    ],
    "each": [
        "3 видео, 2 аккаунта (A и B), лимит 2 ответа на видео.",
        "A пишет по 2 ответа под каждым из 3 видео — 6 ответов,",
        "B — ещё по 2 под теми же видео, на другие комментарии.",
        "Итого 12 ответов, по 4 под каждым видео.",
    ],
    "share": [
        "3 видео, 2 аккаунта (A и B), лимит 2 ответа на видео.",
        "Под каждым видео всего 2 ответа, их пишут A и B по очереди.",
        "Итого 6 ответов на всех, примерно по 3 у каждого аккаунта.",
    ],
}


def _active_accounts_count(db_path: Any) -> int:
    """Active accounts in the DB (read-only sqlite3, safe inside menus); 0 without a DB."""
    try:
        uri = f"file:{quote(Path(db_path).as_posix(), safe='/:')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            count = conn.execute("SELECT COUNT(*) FROM accounts WHERE is_active = 1").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return 0
    return int(count)


def _threads_summary(cfg: Any) -> str:
    """'2 · каждый — весь список' with 2+ active accounts; '' with one account (nothing to set)."""
    if _active_accounts_count(cfg.app.db_path) < 2:
        return ""
    label = next((lbl for value, lbl, _ in _ACCOUNT_MODES if value == cfg.limits.account_mode), cfg.limits.account_mode)
    return f"{cfg.limits.max_concurrent_sessions} · {label.lower()}"


def step_threads(config_path: Path) -> None:
    """Screen «Потоки» (2+ accounts): how many accounts work at once and how they share the videos.

    Space on a row shows a short example; Esc / «Готово» returns.
    """
    from config import load_config

    def rows() -> list[MenuRow | None]:
        cfg = load_config(config_path)
        mode = cfg.limits.account_mode
        return [
            ("1", "Потоки", f"{cfg.limits.max_concurrent_sessions} · аккаунтов одновременно", CLR_DIM),
            *[(str(i), label, ("● " if value == mode else "") + hint, CLR_DIM)
              for i, (value, label, hint) in enumerate(_ACCOUNT_MODES, 2)],
            None,
            ("0", "← Готово", "", ""),
        ]

    def detail(key: str) -> list[str]:
        if key == "1":
            return _THREADS_EXAMPLES["1"]
        if key in {"2", "3"}:
            return _THREADS_EXAMPLES[_ACCOUNT_MODES[int(key) - 2][0]]
        return []

    selected = 0
    while True:
        clear_terminal()
        tui.section_header("Потоки", "Пробел — пояснение")
        notice = tui.take_status()
        if notice:
            tui.status_line(*notice)
        choice, selected = arrow_menu(rows, [], selected, esc_key="0", detail=detail)
        if not choice:  # plain fallback: Enter picks the row under the cursor
            row = rows()[selected]
            choice = row[0] if row else ""
        if choice == "0":
            return
        if choice == "1":
            active = _active_accounts_count(load_config(config_path).app.db_path)
            raw = prompt_user(f"Потоков (1-{max(active, 1)})", str(load_config(config_path).limits.max_concurrent_sessions))
            try:
                value = int(raw)
            except ValueError:
                value = 0
            if 1 <= value <= max(active, 1):
                update_config_file(config_path, {"limits.max_concurrent_sessions": value})
                tui.status_line("ok", f"Потоков: {value}")
            else:
                tui.status_line("err", f"Нужно число от 1 до {max(active, 1)}")
        elif choice in {"2", "3"}:
            value, label, _ = _ACCOUNT_MODES[int(choice) - 2]
            update_config_file(config_path, {"limits.account_mode": value})
            tui.status_line("ok", f"Режим: {label.lower()}")


def run_step_by_step_wizard(config_path: Path) -> None:
    """Stages 1 → 5, one screen each; «Назад»/Esc returns to the previous stage (from stage 1 — exit).

    Every stage saves its changes to config.yaml, so each one starts from a fresh config.
    """
    global _setup_in_wizard
    from config import load_config

    stage = 1
    _setup_in_wizard = True
    try:
        while 1 <= stage <= 5:
            cfg = load_config(config_path)
            limit = cfg.limits.max_iterations_per_session or 2
            clear_terminal()
            try:
                if stage == 1:
                    step_account(config_path, cfg.browser.session_root)
                elif stage == 2:
                    step_proxy(config_path)
                elif stage == 3:
                    step_deepseek(config_path, cfg.deepseek.api_key, cfg.deepseek.base_url)
                elif stage == 4:
                    step_video(config_path, limit)
                else:
                    step_launch(config_path, cfg.browser.proxy, cfg.browser.session_root, cfg.deepseek.api_key, limit)
            except WizardBack:
                stage -= 1
                continue
            stage += 1
    finally:
        _setup_in_wizard = False


def _toggle_headless(config_path: Path) -> None:
    """Flip browser.headless in config.yaml."""
    with open(config_path, "r", encoding="utf-8") as f:
        raw_cfg = yaml.safe_load(f) or {}
    browser = raw_cfg.setdefault("browser", {})
    browser["headless"] = not browser.get("headless", False)
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw_cfg, f, allow_unicode=True, sort_keys=False)


def _yes(answer: str) -> bool:
    return answer.lower() in {"y", "yes", "да"}


def _pause() -> None:
    prompt_user("Нажмите Enter для продолжения", "")


def _readiness(cfg: Any) -> tuple[str, str]:
    """Header indicator: the first problem that blocks a run, or 'ready'."""
    if getattr(cfg.processor, "reply_mode", "ai") == "ai":
        if not cfg.deepseek.api_key:
            return "! ключ DeepSeek не задан", CLR_YELLOW
    elif not getattr(cfg.processor, "custom_replies", []):
        return "! нет шаблонов ответа", CLR_YELLOW
    if hasattr(cfg, "collector") and _target_plan(cfg)[0] == "none":
        return "! нет видео", CLR_YELLOW
    if not _logged_in_names(cfg):
        return "! нет входа в YouTube", CLR_YELLOW
    return "● готов к запуску", CLR_GREEN


def _dashboard_header(cfg: Any) -> list[str]:
    """Title with the readiness indicator aligned to the right edge."""
    title = " YOUTUBE REPLY BOT"
    text, clr = _readiness(cfg)
    pad = " " * max(2, MENU_WIDTH - len(title) - len(text))
    return [color(title, CLR_BOLD + CLR_MAGENTA) + pad + color(text, clr), ""]


def _dashboard_rows(config_path: Path, live: bool = False) -> list[MenuRow | None]:
    """One status line per dashboard section; only problems and Live mode get a color."""
    from config import load_config

    cfg = load_config(config_path)
    is_logged_in = bool(_logged_in_names(cfg))
    limit = cfg.limits.max_iterations_per_session or 2
    mode_txt = getattr(cfg.collector, "target_mode", "first_reply")
    search_q = getattr(cfg.collector, "search_query", None)

    if getattr(cfg.processor, "reply_mode", "ai") == "ai":
        reply = ("ИИ DeepSeek", CLR_DIM) if cfg.deepseek.api_key else ("ИИ DeepSeek · ключ не задан", CLR_YELLOW)
    else:
        n_tpl = len(getattr(cfg.processor, "custom_replies", []))
        reply = (f"свой текст · шаблонов: {n_tpl}", CLR_DIM) if n_tpl else ("свой текст · нет шаблонов", CLR_YELLOW)

    kind, pool_count, _ = _target_plan(cfg)
    if kind == "search":
        target = f"поиск {_target_search(cfg)} · {limit} ответов · {mode_txt}"
    elif kind == "pool":
        target = f"{_target_pool_text(pool_count, limit)} · {_target_mode_label(mode_txt)}"
    else:
        target = _TARGET_NONE

    account = " · ".join([
        "● вошёл" if is_logged_in else "○ нет входа",
        _proxy_summary(cfg),
        "без окна" if getattr(cfg.browser, "headless", False) else "с окном",
    ])

    return [
        ("L", "Запуск", "БОЕВОЙ  ←→" if live else "пробный  ←→", CLR_BOLD + CLR_RED if live else CLR_DIM),
        ("4", "Цель", target, CLR_YELLOW if kind == "none" else CLR_DIM),
        ("3", "Ответы", *reply),
        ("U", "Аккаунт", account, CLR_DIM if is_logged_in else CLR_YELLOW),
        ("H", "История", "журнал и статистика", CLR_DIM),
        ("E", "Ещё…", "предпросмотр, мастер с нуля", CLR_DIM),
    ]


DASHBOARD_HOTKEYS: list[tuple[str, str]] = [
    ("←→", "сменить режим запуска (пробный / боевой)"),
    ("D / R", "сразу пробный / боевой запуск"),
    ("4", "цель: видео, автопоиск, лимит"),
    ("3 / M", "ответы: ключ DeepSeek / режим ответов"),
    ("1 / 2 / B", "аккаунты / прокси / окно браузера"),
    ("H / T", "журнал / статистика БД"),
    ("5 / S", "предпросмотр / мастер с нуля"),
    ("Q", "выход"),
]


def _launch(config_path: Path, dry_run: bool) -> None:
    """Dry-Run or Live run on the video list or search; Live needs confirmation."""
    from config import load_config

    cfg = load_config(config_path)
    limit = cfg.limits.max_iterations_per_session or 2
    if not dry_run and not _target_confirm_live(bool(_logged_in_names(cfg))):
        return
    _target_run(config_path, limit, dry_run)


def _preview_comments(config_path: Path) -> None:
    """Print sub-comments of the next video in the list."""
    from config import load_config

    cfg = load_config(config_path)
    tui.section_header("Предпросмотр", "ответы под комментариями")
    _target_preview(config_path, cfg.limits.max_iterations_per_session or 2)


# Status names per stats group -> Russian words, in display order.
_TARGET_STAT_WORDS: dict[str, dict[str, str]] = {
    "activity": {"success": "опубликовано", "simulated": "пробных", "failed": "ошибок"},
    "payloads": {"pending": "ждут", "submitted": "отправлено", "validated": "проверено", "failed": "ошибок"},
    "queue": {"queued": "в очереди", "running": "идёт", "done": "готово", "failed": "ошибок"},
    "accounts": {"active": "активных", "inactive": "выкл."},
}


def _target_counts(counts: dict[str, int], words: dict[str, str]) -> tuple[str, str]:
    """'опубликовано 3 · ошибок 1' and its style (warn when there are failures); 'нет' if empty."""
    parts = [f"{words[k]} {counts[k]}" for k in words if counts.get(k)]
    parts += [f"{k} {v}" for k, v in counts.items() if k not in words and v]
    return (" · ".join(parts) or "нет"), ("warn" if counts.get("failed") else "" if parts else "dim")


async def _target_load_stats(config_path: Path) -> tuple[dict[str, Any], list[Any]]:
    """Database counters and the five latest payloads."""
    from config import load_config
    from database import Database

    db = Database(load_config(config_path).app.db_path)
    await db.connect()
    await db.init_schema()
    try:
        return await db.stats(), await db.list_payloads(limit=5)
    finally:
        await db.close()


def _show_stats(config_path: Path) -> None:
    """Compact database summary: counters by status plus the latest replies."""
    from rich.markup import escape

    try:
        stats, recent = asyncio.run(_target_load_stats(config_path))
    except Exception as exc:
        tui.status_line("err", f"Ошибка чтения базы: {_fit(str(exc), 45)}")
        _pause()
        return

    tui.section_header("Статистика", Path(stats["db_path"]).name)
    videos = sum(stats.get("video_pool", {}).values())
    tui.kv_table([
        ("Журнал", *_target_counts(stats.get("activity", {}), _TARGET_STAT_WORDS["activity"])),
        ("Ответы", *_target_counts(stats.get("payloads", {}), _TARGET_STAT_WORDS["payloads"])),
        ("Очередь", *_target_counts(stats.get("queue", {}), _TARGET_STAT_WORDS["queue"])),
        ("Аккаунты", *_target_counts(stats.get("accounts", {}), _TARGET_STAT_WORDS["accounts"])),
        ("Видео", f"в списке: {videos}" if videos else "нет", "" if videos else "dim"),
    ])
    if recent:
        lines = []
        for p in recent:
            word = _TARGET_STAT_WORDS["payloads"].get(p.status, p.status)
            style = "err" if p.status == "failed" else "dim"
            text = escape(_fit(" ".join(p.text.split()), tui.MAX_WIDTH - 22))
            lines.append(f"[{style}]#{p.id:<4} {word:<10}[/] {text}")
        safe_print()
        tui.panel("Последние ответы", lines)
    safe_print()
    _pause()


def account_menu(config_path: Path) -> None:
    """Sub-screen «Аккаунт»: accounts, their proxies, browser window (Enter/Space toggles it in place)."""
    from config import load_config

    accounts: list[tuple[Any, bool]] = []

    def rows() -> list[MenuRow | None]:
        # Rebuilt inside the running menu (B toggle), where the DB (asyncio.run) can't be read: accounts are cached.
        cfg = load_config(config_path)
        logged = sum(1 for _, ok in accounts if ok)
        login = f"вошли: {logged}" if logged else "○ нет входа"
        window = "без окна" if getattr(cfg.browser, "headless", False) else "с окном"
        threads = _threads_summary(cfg)
        return [
            ("1", "Аккаунты", f"{len(accounts)} · {login}" if accounts else "пока нет", CLR_DIM if logged else CLR_YELLOW),
            ("2", "Прокси", _proxy_summary(cfg), CLR_DIM),
            *([("P", "Потоки", threads, CLR_DIM)] if threads else []),
            ("B", "Окно браузера", f"{window} · Enter — переключить", CLR_DIM),
            None,
            ("0", "← Назад", "", ""),
        ]

    selected = 0
    while True:
        accounts = _setup_accounts(config_path)
        clear_terminal()
        tui.section_header("Аккаунт", "аккаунты · прокси · потоки · окно браузера")
        choice, selected = arrow_menu(
            rows, [""], selected,
            actions={"B": lambda: _toggle_headless(config_path)}, esc_key="0",
        )
        if not choice:  # plain fallback: Enter opens the row under the cursor
            row = rows()[selected]
            choice = row[0] if row else ""
        if choice in {"0", "Q", "НАЗАД", "BACK"}:
            return
        if choice == "B":
            _toggle_headless(config_path)
            continue
        if choice not in {"1", "2", "P"}:
            continue
        cfg = load_config(config_path)
        clear_terminal()
        try:
            if choice == "1":
                step_account(config_path, cfg.browser.session_root)
            elif choice == "2":
                step_proxy(config_path)
            else:
                step_threads(config_path)
        except WizardBack:
            pass


def more_menu(config_path: Path) -> None:
    """Sub-screen «Ещё…»: comment preview, step-by-step setup."""
    def rows() -> list[MenuRow | None]:
        return [
            ("5", "Предпросмотр", "комментарии к текущей цели", CLR_DIM),
            ("S", "Мастер с нуля", "пошаговая настройка, этапы 1 → 5", CLR_DIM),
            None,
            ("0", "← Назад", "", ""),
        ]

    selected = 0
    while True:
        clear_terminal()
        tui.section_header("Ещё", "предпросмотр · мастер")
        choice, selected = arrow_menu(
            rows, [""], selected, esc_key="0",
        )
        if choice in {"0", "Q", "НАЗАД", "BACK"}:
            return
        if choice not in {"5", "S"}:
            continue
        clear_terminal()
        try:
            if choice == "5":
                _preview_comments(config_path)
            else:
                run_step_by_step_wizard(config_path)
        except WizardBack:
            pass


def _open_dashboard_item(config_path: Path, cmd: str) -> bool:
    """Open the screen behind a dashboard key; False if the key is unknown."""
    from config import load_config

    cfg = load_config(config_path)
    limit = cfg.limits.max_iterations_per_session or 2
    ai_mode = getattr(cfg.processor, "reply_mode", "ai") == "ai"
    screens: dict[str, Callable[[], Any]] = {
        "D": lambda: _launch(config_path, dry_run=True),
        "R": lambda: _launch(config_path, dry_run=False),
        "4": lambda: step_video(config_path, limit),
        "3": lambda: step_deepseek(config_path, cfg.deepseek.api_key, cfg.deepseek.base_url) if ai_mode else step_reply_mode(config_path),
        "M": lambda: step_reply_mode(config_path),
        "U": lambda: account_menu(config_path),
        "1": lambda: step_account(config_path, cfg.browser.session_root),
        "2": lambda: step_proxy(config_path),
        "H": lambda: activity_log_viewer(config_path),
        "T": lambda: _show_stats(config_path),
        "E": lambda: more_menu(config_path),
        "5": lambda: _preview_comments(config_path),
        "S": lambda: run_step_by_step_wizard(config_path),
    }
    if cmd not in screens:
        return False
    clear_terminal()
    try:
        screens[cmd]()
    except WizardBack:
        pass
    return True


def run_dashboard(config_path: Path) -> None:
    """Main dashboard: six sections with one status line each; old hotkeys stay as hidden accelerators."""
    from config import load_config

    aliases = {
        "QUIT": "Q", "EXIT": "Q", "ВЫХОД": "Q",
        "MODE": "M", "РЕЖИМ": "M", "TEXT": "M", "ТЕКСТ": "M",
        "BROWSER": "B", "БРАУЗЕР": "B", "HEADLESS": "B",
        "LOG": "H", "ЖУРНАЛ": "H", "ИСТОРИЯ": "H", "HISTORY": "H",
        "ACCOUNTS": "U", "АККАУНТЫ": "U",
    }
    selected = 0
    live = False
    notice = ""

    def flip_mode() -> None:
        nonlocal live
        live = not live

    while True:
        clear_terminal()
        try:
            cfg = load_config(config_path)
        except Exception as exc:
            safe_print(f"{color('[!]', CLR_RED)} Ошибка загрузки config.yaml: {exc}")
            return

        cmd, selected = arrow_menu(
            lambda: _dashboard_rows(config_path, live),
            _dashboard_header(cfg) + ([f" {notice}", ""] if notice else []),
            selected,
            actions={"B": lambda: _toggle_headless(config_path)},
            side_actions={"L": flip_mode},
            extra_keys=frozenset("DRM12BT5SQ"),
            help_lines=DASHBOARD_HOTKEYS,
        )
        cmd = aliases.get(cmd, cmd)
        notice = ""

        if not cmd:
            continue
        if cmd == "Q":
            safe_print(f"\n{color('[OK]', CLR_GREEN)} До свидания!")
            break
        if cmd == "L":
            cmd = "R" if live else "D"
        if cmd == "B":  # fallback mode: aliases like BROWSER reach here
            _toggle_headless(config_path)
            continue
        if not _open_dashboard_item(config_path, cmd):
            notice = f"{color('[!]', CLR_YELLOW)} Неизвестная команда: {cmd}"


def run_wizard_app(config_path_str: str = "config.yaml") -> None:
    """Entry point for the interactive CMD wizard."""
    configure_console()
    p = Path(config_path_str)
    if not p.is_file():
        safe_print(f"{color('[!]', CLR_RED)} Файл конфигурации не найден: {p}")
        return

    try:
        run_step_by_step_wizard(p)
        run_dashboard(p)
    except (KeyboardInterrupt, EOFError, WizardBack):
        safe_print(f"\n\n{color('[!]', CLR_YELLOW)} Работа с мастером завершена.")
