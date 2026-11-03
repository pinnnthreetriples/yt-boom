"""Wizard stage 2 «Прокси»: the proxy library on one screen.

Above the list: where the video search goes out and how many accounts post through a proxy. Every proxy is a row
(Enter opens its card: accounts on it by ticks, check, change, search proxy, delete); below: «Добавить прокси» (paste
one line or many: parsed, shown, checked in parallel, saved, offered to the accounts without a proxy), «Аккаунты и
их прокси» and «Проверить все». Data: the library in `database.py`; check: `modules.proxy_check`. A password is
never shown. `modules.wizard.step_proxy` opens this screen; wizard helpers are looked up there at call time, so
tests patch them in one place.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from modules import tui
from modules import wizard as w
from modules.proxy_paste import PasteLine, ProxyDraft, ProxyPasteError, parse_proxy_line, parse_proxy_paste

Notice = tuple[str, str] | None

TYPE_NAMES = {"http": "HTTP", "https": "HTTPS", "socks5": "SOCKS5", "socks4": "SOCKS4"}
LOCKED = "🔒 не расшифрован — вставьте заново"

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


def country_tag(code: str | None) -> str:
    """Colored «[PL]» (the Windows console draws no flag emoji); «[??]» while the country is unknown."""
    return w.color(f"[{code}]", w.CLR_CYAN) if code else w.color("[??]", w.CLR_DIM)


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


def check_status(record: Any) -> tuple[str, str]:
    """Last stored check: («✓ 210 мс» | «✗ таймаут» | «не проверен» | 🔒, theme style)."""
    if record.password_unavailable:
        return LOCKED, "err"
    if record.last_ok is None:
        return "не проверен", "dim"
    if record.last_ok:
        return (f"✓ {record.last_latency_ms} мс" if record.last_latency_ms is not None else "✓ работает"), "ok"
    return f"✗ {short_error(record.last_error)}", "err"


def browser_warning(record: Any) -> str | None:
    from modules.proxy_check import browser_proxy_warning

    return browser_proxy_warning(record.scheme, record.login)


def _clr(style: str) -> str:
    return w._CLR_BY_THEME.get(style, "")


def _names(items: list[str], limit: int = 2) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" и ещё {len(items) - limit}" if len(items) > limit else "")


def direct_text() -> str:
    """«с вашего IP 85.1.2.3 (RU)» after a direct check in this run, else «с вашего IP»."""
    if _direct is not None and getattr(_direct, "ok", False):
        return f"с вашего IP {_direct.exit_ip}" + (f" ({_direct.exit_country})" if _direct.exit_country else "")
    return "с вашего IP"


def search_value(state: ProxyState) -> tuple[str, str]:
    if state.search_lost:
        return "⚠ прокси поиска удалён — выберите другой", "warn"
    if state.search is None:
        return f"⚠ напрямую — {direct_text()}", "warn"
    if state.search.password_unavailable:
        return "🔒 прокси поиска не читается — поиск не запустится", "err"
    return f"через {proxy_name(state.search)}", "dim"


def accounts_value(state: ProxyState) -> tuple[str, str]:
    accounts = [acc for acc, _ in state.accounts]
    if not accounts:
        return "аккаунтов нет — добавьте на этапе «Аккаунты»", "warn"
    direct = [a.name for a in accounts if a.proxy_id is None]
    locked = [a.name for a in accounts if a.proxy_unavailable]
    via = len(accounts) - len(direct) - len(locked)
    text = f"с прокси {via} из {len(accounts)}"
    if direct:
        text += f" · с вашего IP: {_names(direct)}"
    if locked:
        text += f" · 🔒 не читается: {_names(locked)}"
    return text, "warn" if direct or locked else "dim"


def proxy_row(record: Any) -> w.MenuRow:
    status, style = check_status(record)
    label = f"{country_tag(country_of(record))} {w._fit(record.address, 21)}"
    if record.password_unavailable:
        return f"P{record.id}", label, status, _clr(style)
    users = f"{record.accounts_count} акк." if record.accounts_count else "—"
    value = f"{type_name(record.scheme):<7}{status:<15}{users}" + (" · поиск" if record.is_search else "")
    return f"P{record.id}", label, value, _clr(style)


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


# --------------------------------------------------------------------------- main screen

_TAIL: list[w.MenuRow | None] = [
    ("2", "Добавить прокси", "вставьте строку или сразу несколько", w.CLR_DIM),
    ("3", "Аккаунты и их прокси", "кому какой назначен", w.CLR_DIM),
    ("4", "Проверить все", "связь и страна, все разом", w.CLR_DIM),
    None,
    ("1", "Продолжить →", "", ""),
    ("0", "← Назад", "", ""),
]


def main_rows(state: ProxyState) -> list[w.MenuRow | None]:
    search, s_style = search_value(state)
    accounts, a_style = accounts_value(state)
    rows: list[w.MenuRow | None] = [
        ("S", "Поиск видео", search, _clr(s_style)),
        ("U", "Аккаунты", accounts, _clr(a_style)),
        None,
    ]
    rows += [proxy_row(record) for record in state.proxies]
    if state.proxies:
        rows.append(None)
    return rows + _TAIL


def _geo_hint() -> str | None:
    try:
        from modules.geo_guard import geoip_database_status

        ready, text = geoip_database_status()
    except Exception:
        return None
    return None if ready else text


def continue_problems(state: ProxyState) -> list[str]:
    """What «Продолжить» warns about once: accounts without a proxy, with an unreadable or a failing one."""
    direct = [acc.name for acc, _ in state.accounts if acc.proxy_id is None]
    locked = [acc.name for acc, _ in state.accounts if acc.proxy_unavailable]
    dead = [
        acc.name for acc, _ in state.accounts
        if not acc.proxy_unavailable and (record := state.proxy(acc.proxy_id)) is not None and record.last_ok is False
    ]
    problems = []
    if direct:
        problems.append(f"{_names(direct)} — без прокси, с вашего IP")
    if locked:
        problems.append(f"{_names(locked)} — прокси не читается, не запустится")
    if dead:
        problems.append(f"{_names(dead)} — прокси не прошёл проверку")
    return problems


def run_proxy_stage(config_path: Path) -> None:
    """Stage 2 «Прокси». «Продолжить» returns (warning once about accounts without a working proxy); «0»/Esc raises
    WizardBack."""
    cursor_key: str | None = None
    notice: Notice = None
    warned = False
    redraw = True
    while True:
        if redraw:
            state = load_state(config_path)
            rows = main_rows(state)
            w._setup_screen(2, "Прокси", "один прокси — на любое число аккаунтов", [])
            hint = _geo_hint()
            if hint:
                w.safe_print(w.color(f" {hint}", w.CLR_YELLOW))
            if notice:
                tui.status_line(*notice)
            default = "2" if not state.proxies else "3" if any(a.proxy_id is None for a, _ in state.accounts) else "1"
            selected = w._setup_index(rows, cursor_key if cursor_key and cursor_key in {r[0] for r in rows if r} else default)
            redraw, notice = False, None

        choice, selected = w._setup_menu(lambda: rows, selected)
        cursor_key = choice

        if choice == "0":
            raise w.WizardBack()
        if choice == "1":
            problems = continue_problems(state)
            if problems and not warned:
                warned = True
                tui.status_line("warn", "; ".join(problems) + ". Ещё раз «Продолжить» — дальше")
                continue
            return
        redraw = True
        if choice == "S":
            notice = pick_search_proxy(config_path, state)
        elif choice in {"U", "3"}:
            notice = accounts_and_proxies(config_path)
        elif choice == "2":
            notice = add_proxies(config_path, state)
        elif choice == "4":
            notice = check_all(config_path, state)
        elif choice.startswith("P") and choice[1:].isdigit() and state.proxy(int(choice[1:])) is not None:
            notice = proxy_card(config_path, int(choice[1:]))
        else:
            redraw = False
            tui.status_line("warn", f"Неизвестная команда: {choice}")


# --------------------------------------------------------------------------- checks


def _progress(done: int, total: int) -> None:
    w.safe_print(f"\r Проверено {done} из {total}…", end="" if done < total else "\n", flush=True)


async def _record(db: Any, proxy_id: int, result: Any) -> Any:
    return await db.record_proxy_check(
        proxy_id, result.ok, scheme=result.scheme, exit_ip=result.exit_ip, exit_country=result.exit_country,
        latency_ms=result.latency_ms, error=None if result.ok else result.message,
    )


def print_result(name: str, result: Any) -> None:
    """«✓ [PL] 1.2.3.4:80 · HTTP · 210 мс · внешний IP 5.6.7.8» (green) or «✗ 1.2.3.4:80 — причина» (red)."""
    kind, text = result_line(name, result)
    w.safe_print(w.color(f" {'✓' if kind == 'ok' else '✗'} {text}", w.CLR_GREEN if kind == "ok" else w.CLR_RED))
    if result.browser_warning:
        w.safe_print(w.color(f"   ⚠ {result.browser_warning}", w.CLR_YELLOW))


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


def draft_line(draft: ProxyDraft) -> str:
    login = f"логин {draft.login}" if draft.login else "без логина"
    password = "пароль скрыт" if draft.password else "без пароля"
    kind = type_name(draft.scheme) if draft.scheme else "тип ?"
    country = f"страна {draft.country}" if draft.country else "страна ?"
    return f"{draft.address} · {login} · {password} · {kind} · {country}"


def show_parsed(lines: list[PasteLine], raw_lines: list[str], known: set[tuple[str, int, str]]) -> list[ProxyDraft]:
    """Print how every line was read (never the password) and return the drafts to check (no repeats)."""
    from rich.markup import escape

    out: list[str] = []
    drafts: list[ProxyDraft] = []
    for line in lines:
        if line.draft is None:
            out.append(f"[err]✗ {escape(line.error or '')}[/]")
            continue
        draft = line.draft
        note = ""
        if line.duplicate_of is not None:
            note = f" [dim]· повтор строки {line.duplicate_of} — пропустим[/]"
        elif draft.key in known:
            note = " [dim]· уже в списке — обновим[/]"
        out.append(f"{line.line_no}. {escape(draft_line(draft))}{note}")
        if _country_may_be_password(raw_lines[line.line_no - 1], draft):
            out.append(
                f"   [warn]⚠ «{draft.country}» в конце принято за страну. Если это конец пароля — отмените и "
                "вставьте строку как логин:пароль@IP:порт[/]"
            )
        if line.duplicate_of is None:
            drafts.append(draft)
    w.safe_print()
    tui.panel("Распознано", out)
    return drafts


def _ask_country(draft: ProxyDraft, exit_country: str) -> bool:
    """True: keep the seller's country; False: take the checked exit country."""
    rows: list[w.MenuRow | None] = [
        ("1", draft.country or "", "как написал продавец", w.CLR_DIM),
        ("2", exit_country, "по проверке — так прокси видят сайты", w.CLR_DIM),
    ]
    header = [f" {draft.address}: в строке страна {draft.country}, а выход через прокси — {exit_country}. Какую оставить?"]
    return w._pools_choice(rows, header, 1) == "1"


def _check_drafts(drafts: list[ProxyDraft]) -> list[Any]:
    from modules import proxy_check as pc

    tui.status_line("info", f"Проверяем {len(drafts)} прокси…")
    return asyncio.run(pc.check_proxies(drafts, on_done=_progress))


def add_proxies(config_path: Path, state: ProxyState, for_account: Any = None) -> Notice:
    """«Добавить прокси»: paste → show what was read → check in parallel → save → offer to the accounts without one
    (or give the new proxy to `for_account`). Returns the notice."""
    w.safe_print()
    w.safe_print(w.color(" Вставьте прокси — по строке на прокси, как дал продавец. Пустая строка — конец.", w.CLR_DIM))
    w.safe_print(w.color(" Например: 45.130.253.31:8000:логин:пароль:PL  или  socks5://логин:пароль@хост:1080", w.CLR_DIM))
    w.safe_print(w.color(" Тип и страну можно не писать — их покажет проверка.", w.CLR_DIM))
    try:
        text = w._pools_read_paste(" ›")
    except w.WizardBack:
        return None
    if not text.strip():
        return "info", "Отменено"
    lines = parse_proxy_paste(text)
    known = {(p.host, p.port, p.login or "") for p in state.proxies}
    drafts = show_parsed(lines, text.splitlines(), known)
    errors = sum(1 for line in lines if not line.ok)
    if not drafts:
        return "err", "Ни одной строки не распознано — проверьте формат"
    try:
        if not w._pools_yes(f"Проверить и сохранить ({len(drafts)})?"):
            return "info", "Отменено — ничего не сохранено"
    except w.WizardBack:
        return "info", "Отменено — ничего не сохранено"

    results = _check_drafts(drafts)
    for draft, result in zip(drafts, results):
        print_result(f"[{result.exit_country or draft.country or '??'}] {draft.address}", result)

    plan: list[tuple[ProxyDraft, Any, bool]] = []  # (draft, result, keep the seller's country)
    try:
        for draft, result in zip(drafts, results):
            keep = True
            if result.ok and draft.country and result.exit_country and draft.country != result.exit_country:
                keep = _ask_country(draft, result.exit_country)
            plan.append((draft, result, keep))
        failing = [item for item in plan if not item[1].ok]
        if failing and not w._yes(w.prompt_user(
            f"Не работают: {len(failing)} из {len(plan)}. Сохранить их всё равно? (y/N)", "n",
        )):
            plan = [item for item in plan if item[1].ok]
    except w.WizardBack:
        return "info", "Отменено — ничего не сохранено"
    if not plan:
        return "warn", "Ничего не сохранено: рабочих прокси нет"

    async def save(db: Any) -> list[tuple[Any, Any]]:
        saved = []
        for draft, result, keep in plan:
            parts = replace(draft.parts, scheme=draft.scheme or result.scheme)
            record, _ = await db.add_proxy(parts, country=draft.country if keep else None)
            if not keep:
                await db.update_proxy(record.id, country=None)  # the check fills the exit country
            saved.append((await _record(db, record.id, result), result))
        return saved

    saved = w._pools_db(config_path, save)
    good = sum(1 for _, result in saved if result.ok)
    text = f"Сохранено {len(saved)}: работают {good}" + (f", не работают {len(saved) - good}" if good < len(saved) else "")
    if errors:
        text += f" · строк с ошибкой: {errors}"
    if for_account is not None:
        record = next((rec for rec, result in saved if result.ok), saved[0][0])
        changed = w._pools_db(config_path, lambda db: db.assign_proxy(record.id, [for_account.id]))
        if changed:
            text += f" · «{for_account.name}» → {proxy_name(record)}"
    else:
        assigned = offer_assignment(config_path, [rec for rec, result in saved if result.ok and not result.browser_warning])
        if assigned:
            text += f" · назначено аккаунтам: {assigned}"
    return ("ok" if good == len(saved) and not errors else "warn"), text


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


def offer_assignment(config_path: Path, proxies: list[Any]) -> int:
    """«Назначить новые прокси аккаунтам без прокси?»: shows the plan, assigns after a yes; returns how many."""
    if not proxies:
        return 0
    waiting = [acc for acc, _ in w._setup_accounts(config_path) if acc.proxy_id is None]
    plan, skipped = distribute(proxies, waiting)
    if not plan:
        return 0
    lines = [f"{acc.name:<20} → {proxy_name(record)}" for acc, record in plan]
    if skipped:
        lines.append(f"[dim]без пары (нет прокси нужной страны): {_names(skipped, 4)}[/]")
    w.safe_print()
    tui.panel("Назначить новые прокси аккаунтам без прокси?", lines)
    try:
        if not w._pools_yes("Назначить так?"):
            return 0
    except w.WizardBack:
        return 0

    async def assign(db: Any) -> int:
        changed = 0
        for acc, record in plan:
            changed += len(await db.assign_proxy(record.id, [acc.id]))
        return changed

    return w._pools_db(config_path, assign)


# --------------------------------------------------------------------------- card


def proxy_card(config_path: Path, proxy_id: int) -> Notice:
    """Card of one proxy with its actions; returns the last notice for the stage screen."""
    notice: Notice = None
    selected = 0
    while True:
        state = load_state(config_path)
        record = state.proxy(proxy_id)
        if record is None:
            return notice
        users = state.on_proxy(proxy_id)
        status, style = check_status(record)
        when = ago(record.last_check_at)
        country = country_of(record)
        w.clear_terminal()
        tui.section_header(f"Прокси {proxy_name(record)}", "карточка")
        table: list[tuple[str, str] | tuple[str, str, str]] = [
            ("Тип", type_name(record.scheme) if record.scheme else "не известен — определит проверка"),
            ("Логин", record.login or "без логина"),
            ("Пароль", LOCKED if record.password_unavailable else "скрыт" if record.password else "без пароля",
             "err" if record.password_unavailable else ""),
            ("Страна", (country or "не известна") + (" · указана вами" if record.country_source == "user" else "")),
            ("Внешний IP", f"{record.last_exit_ip} ({record.last_exit_country or '??'})" if record.last_exit_ip else "—"),
            ("Проверка", f"{status}{f' · {when}' if when else ''}", style),
            ("Аккаунты", _names([a.name for a in users], 4) if users else "нет", "" if users else "dim"),
        ]
        if record.is_search:
            table.append(("Поиск", "через этот прокси"))
        tui.kv_table(table)
        if not record.last_ok and record.last_error and not record.password_unavailable:
            w.safe_print(w.color(f" {w._fit(record.last_error, w.MENU_WIDTH - 1)}", w.CLR_RED))
        if record.country_source == "user" and record.last_exit_country and record.country != record.last_exit_country:
            w.safe_print(w.color(f" ⚠ Указана страна {record.country}, а выход — {record.last_exit_country}", w.CLR_YELLOW))
        if warning := browser_warning(record):
            w.safe_print(w.color(f" ⚠ {warning}", w.CLR_YELLOW))
        if notice:
            tui.status_line(*notice)
        rows: list[w.MenuRow | None] = [
            ("1", "Аккаунты на этом прокси", f"{len(users)} · отметить галочками", w.CLR_DIM),
            ("2", "Проверить связь", "внешний IP и страна", w.CLR_DIM),
            ("3", "Изменить", "вставить строку заново: пароль, порт", w.CLR_DIM),
            ("4", "Не использовать для поиска", "поиск пойдёт с вашего IP", w.CLR_DIM) if record.is_search
            else ("4", "Сделать прокси поиска", "искать видео через него", w.CLR_DIM),
            ("5", "Удалить из списка", f"отвяжутся: {len(users)}" if users else "", w.CLR_DIM),
            None,
            ("0", "← Назад", "", ""),
        ]
        choice, selected = w.arrow_menu(lambda: rows, [""], selected, esc_key="0")
        if not choice:
            row = rows[selected]
            choice = row[0] if row else ""
        if choice == "0":
            return notice
        if choice == "1":
            notice = proxy_accounts_checklist(config_path, state, record)
        elif choice == "2":
            notice = check_one(config_path, record)
        elif choice == "3":
            notice, proxy_id = edit_proxy(config_path, state, record)
        elif choice == "4":
            notice = set_search(config_path, state, None if record.is_search else record)
        elif choice == "5":
            done = delete_proxy(config_path, record, users)
            if done is not None:
                return done


def check_one(config_path: Path, record: Any) -> Notice:
    from modules import proxy_check as pc

    if record.password_unavailable:
        return "err", "Пароль не расшифровывается на этом компьютере — «Изменить» и вставьте прокси заново"
    tui.status_line("info", f"Проверяем {record.address}…")
    updated, result = w._pools_db(config_path, lambda db: pc.check_and_record(db, record.id))
    kind, text = result_line(proxy_name(updated), result)
    return kind, text + (f" · ⚠ {result.browser_warning}" if result.browser_warning else "")


def delete_proxy(config_path: Path, record: Any, users: list[Any]) -> Notice:
    """Confirm and delete; None = cancelled. The accounts on it are named: they will go direct."""
    w.safe_print()
    if users:
        w.safe_print(w.color(
            f" Отвяжутся и пойдут напрямую, с вашего IP: {_names([a.name for a in users], 6)}", w.CLR_YELLOW))
    if record.is_search:
        w.safe_print(w.color(" Поиск видео тоже пойдёт напрямую, с вашего IP", w.CLR_YELLOW))
    try:
        if not w._yes(w.prompt_user(f"Удалить {record.address} из списка? (y/N)", "n")):
            return None
    except w.WizardBack:
        return None
    result = w._pools_db(config_path, lambda db: db.delete_proxy(record.id))
    text = f"Удалён {record.address}"
    if result.detached_accounts:
        text += f" · теперь с вашего IP: {_names(result.detached_accounts, 4)}"
    if result.was_search:
        text += " · поиск — напрямую"
    return ("warn" if result.detached_accounts or result.was_search else "ok"), text


def edit_proxy(config_path: Path, state: ProxyState, record: Any) -> tuple[Notice, int]:
    """«Изменить»: a new line for this proxy (new password, port…). The accounts and the search role move with it.
    Returns (notice, id of the proxy now)."""
    from rich.markup import escape

    from modules import proxy_check as pc

    try:
        line = w.prompt_user("Новая строка этого прокси (пустая — отмена)").strip()
    except w.WizardBack:
        return None, record.id
    if not line:
        return None, record.id
    try:
        draft = parse_proxy_line(line)
    except ProxyPasteError as exc:
        return ("err", f"Строка не разобрана: {exc}"), record.id
    lines = [f"{escape(draft_line(draft))}"]
    if _country_may_be_password(line, draft):
        lines.append(f"[warn]⚠ «{draft.country}» в конце принято за страну. Если это конец пароля — отмените и "
                     "вставьте как логин:пароль@IP:порт[/]")
    w.safe_print()
    tui.panel("Распознано", lines)
    try:
        if not w._pools_yes("Проверить и сохранить?"):
            return ("info", "Отменено"), record.id
        tui.status_line("info", "Проверяем…")
        result = asyncio.run(pc.check_proxy(draft))
        print_result(f"[{result.exit_country or draft.country or '??'}] {draft.address}", result)
        if not result.ok and not w._yes(w.prompt_user("Прокси не работает. Сохранить всё равно? (y/N)", "n")):
            return ("info", "Отменено — прокси не изменён"), record.id
    except w.WizardBack:
        return ("info", "Отменено"), record.id
    users = state.on_proxy(record.id)

    async def save(db: Any) -> Any:
        parts = replace(draft.parts, scheme=draft.scheme or result.scheme or record.scheme)
        new, _ = await db.add_proxy(parts, country=draft.country)
        if new.id != record.id:  # another address or login: move everything over, then drop the old entry
            if users:
                await db.assign_proxy(new.id, [a.id for a in users], reset_auth=new.host != record.host)
            if record.is_search:
                await db.set_search_proxy(new.id)
            await db.delete_proxy(record.id)
        return await _record(db, new.id, result)

    new = w._pools_db(config_path, save)
    moved = f" · аккаунты ({len(users)}) остались на нём" if users else ""
    return ("ok" if result.ok else "warn", f"Прокси изменён: {proxy_name(new)}{moved}"), new.id


# --------------------------------------------------------------------------- ticks: accounts on a proxy


def proxy_accounts_checklist(config_path: Path, state: ProxyState, record: Any) -> Notice:
    """All accounts with ticks on those using `record`; «Сохранить» assigns the ticked and frees the unticked ones."""
    if not state.accounts:
        return "warn", "Аккаунтов нет — добавьте их на этапе «Аккаунты»"
    if record.password_unavailable:
        return "err", "Пароль этого прокси не расшифрован — сначала «Изменить» и вставьте его заново"
    items: list[tuple[str, str, str]] = []
    for acc, logged_in in state.accounts:
        current = "этот прокси" if acc.proxy_id == record.id else account_proxy_text(acc, state)
        warnings = account_warnings(acc, logged_in, record)
        value = f"{'●' if logged_in else '○'} {current}" + "".join(f" · ⚠ {text}" for text in warnings)
        items.append((w._fit(acc.name, 22), value, "warn" if warnings else "ok" if acc.proxy_id == record.id else "dim"))
    before = {i for i, (acc, _) in enumerate(state.accounts) if acc.proxy_id == record.id}
    header = [
        f" Аккаунты на прокси {country_tag(country_of(record))} {record.address}",
        w.color(" Enter/Пробел — отметить · ● вошёл · ○ нет входа · Esc — отмена", w.CLR_DIM),
    ]
    if warning := browser_warning(record):
        header.append(w.color(f" ⚠ {w._fit(warning, w.MENU_WIDTH - 3)}", w.CLR_YELLOW))
    header.append("")
    picked = tui.checklist(items, before, header, interactive=w._use_arrow_keys(), ask_fn=w.prompt_user)
    if picked is None:
        return "info", "Отменено — ничего не изменилось"
    add = [state.accounts[i][0].id for i in sorted(picked - before)]
    remove = [state.accounts[i][0].id for i in sorted(before - picked)]
    if not add and not remove:
        return "info", "Ничего не изменилось"

    async def save(db: Any) -> tuple[list[str], list[str], list[Any]]:
        assigned = await db.assign_proxy(record.id, add) if add else []
        cleared = await db.clear_account_proxy(remove) if remove else []
        return assigned, cleared, await db.list_accounts()

    assigned, cleared, fresh = w._pools_db(config_path, save)
    want = {state.accounts[i][0].id for i in picked}
    actual = {acc.id for acc in fresh if acc.proxy_id == record.id}
    if want != actual:
        return "err", "База не совпала с отмеченным — откройте список ещё раз"
    text = f"Назначено {len(assigned)}, снято {len(cleared)}"
    if cleared:
        text += f" · теперь с вашего IP: {_names(cleared, 3)}"
    return ("warn" if cleared else "ok"), text


# --------------------------------------------------------------------------- accounts and their proxies


def accounts_and_proxies(config_path: Path) -> Notice:
    """«Аккаунты и их прокси»: every account with its proxy; Enter — pick another one for it."""
    notice: Notice = None
    selected = 0
    while True:
        state = load_state(config_path)
        if not state.accounts:
            return "warn", "Аккаунтов нет — добавьте их на этапе «Аккаунты»"
        rows: list[w.MenuRow | None] = []
        for i, (acc, logged_in) in enumerate(state.accounts, 1):
            text = account_proxy_text(acc, state)
            style = "err" if acc.proxy_unavailable else "warn" if acc.proxy_id is None else "dim"
            rows.append((f"A{i}", w._fit(acc.name, 22), f"{'●' if logged_in else '○'} {text}", _clr(style)))
        rows += [None, ("0", "← Назад", "", "")]
        w.clear_terminal()
        tui.section_header("Аккаунты и их прокси", "Enter — выбрать прокси аккаунту")
        if notice:
            tui.status_line(*notice)
        choice, selected = w.arrow_menu(lambda: rows, [""], selected, esc_key="0")
        if not choice:
            row = rows[selected]
            choice = row[0] if row else ""
        if choice in {"0", ""}:
            return notice
        if choice.startswith("A") and choice[1:].isdigit() and 1 <= int(choice[1:]) <= len(state.accounts):
            acc, logged_in = state.accounts[int(choice[1:]) - 1]
            notice = pick_proxy_for_account(config_path, state, acc, logged_in)


def pick_proxy_for_account(config_path: Path, state: ProxyState, acc: Any, logged_in: bool) -> Notice:
    rows: list[w.MenuRow | None] = []
    for record in state.proxies:
        status, style = check_status(record)
        warnings = account_warnings(acc, logged_in, record)
        mark = "● сейчас · " if acc.proxy_id == record.id else ""
        value = mark + status + "".join(f" · ⚠ {text}" for text in warnings)
        rows.append((f"P{record.id}", f"{country_tag(country_of(record))} {record.address}", value,
                     _clr("warn" if warnings else style)))
    if rows:
        rows.append(None)
    rows += [
        ("N", "Добавить новый…", "вставить строку прокси", w.CLR_DIM),
        ("D", "Без прокси", ("● сейчас · " if acc.proxy_id is None else "") + "с вашего IP", w.CLR_YELLOW),
        None,
        ("0", "← Отмена", "", ""),
    ]
    current = f"P{acc.proxy_id}" if acc.proxy_id is not None else "D"
    header = [f" Прокси для «{acc.name}»" + (f" · ждёт страну {acc.expected_country}" if acc.expected_country else "")]
    choice = w._pools_choice(rows, header, w._setup_index(rows, current))
    if choice == "N":
        return add_proxies(config_path, state, for_account=acc)
    if choice == "D":
        if acc.proxy_id is None:
            return None
        w._pools_db(config_path, lambda db: db.clear_account_proxy([acc.id]))
        return "warn", f"«{acc.name}»: без прокси — пишет с вашего IP"
    record = state.proxy(int(choice[1:])) if choice.startswith("P") and choice[1:].isdigit() else None
    if record is None or record.id == acc.proxy_id:
        return None
    if record.password_unavailable:
        return "err", "Пароль этого прокси не расшифрован — вставьте его заново в карточке прокси"
    if warning := browser_warning(record):
        tui.status_line("warn", warning)
        try:
            if not w._yes(w.prompt_user("Всё равно назначить? (y/N)", "n")):
                return None
        except w.WizardBack:
            return None
    w._pools_db(config_path, lambda db: db.assign_proxy(record.id, [acc.id]))
    return "ok", f"«{acc.name}» → {proxy_name(record)}"


# --------------------------------------------------------------------------- search proxy


def pick_search_proxy(config_path: Path, state: ProxyState | None = None) -> Notice:
    """«Поиск видео»: the proxy that searches videos and reads comments, or «напрямую»."""
    state = state or load_state(config_path)
    rows: list[w.MenuRow | None] = [
        ("D", "Напрямую", ("● сейчас · " if state.search is None else "") + f"⚠ {direct_text()}", w.CLR_YELLOW),
    ]
    for record in state.proxies:
        status, style = check_status(record)
        mark = "● сейчас · " if record.is_search else ""
        users = f" · {record.accounts_count} акк." if record.accounts_count else " · без аккаунтов"
        rows.append((f"P{record.id}", f"{country_tag(country_of(record))} {record.address}", mark + status + users, _clr(style)))
    rows += [None, ("0", "← Отмена", "", "")]
    header = [
        " Через что искать видео и читать комментарии",
        w.color(" Поиск делает много запросов: лучше отдельный прокси, на котором нет аккаунтов", w.CLR_DIM),
    ]
    current = f"P{state.search.id}" if state.search is not None else "D"
    choice = w._pools_choice(rows, header, w._setup_index(rows, current))
    if choice == "D":
        return set_search(config_path, state, None)
    record = state.proxy(int(choice[1:])) if choice.startswith("P") and choice[1:].isdigit() else None
    return set_search(config_path, state, record) if record is not None else None


def set_search(config_path: Path, state: ProxyState, record: Any) -> Notice:
    """Make `record` the search proxy (None: direct); warns before taking a proxy the accounts use."""
    if record is None:
        if state.search is None and not state.search_lost:
            return None
        w._pools_db(config_path, lambda db: db.set_search_proxy(None))
        return "warn", f"Поиск — напрямую, {direct_text()}"
    if record.is_search:
        return None
    if record.password_unavailable:
        return "err", "Пароль этого прокси не расшифрован — вставьте его заново в карточке прокси"
    users = state.on_proxy(record.id)
    if users:
        tui.status_line("warn", f"Поиск делает много запросов и может навредить аккаунтам на этом IP: "
                                f"{_names([a.name for a in users], 3)}")
        try:
            if not w._yes(w.prompt_user("Всё равно искать через него? (y/N)", "n")):
                return None
        except w.WizardBack:
            return None
    w._pools_db(config_path, lambda db: db.set_search_proxy(record.id))
    return "ok", f"Поиск — через {proxy_name(record)}"


__all__ = [
    "ProxyState",
    "accounts_and_proxies",
    "add_proxies",
    "check_all",
    "distribute",
    "load_state",
    "pick_search_proxy",
    "proxy_card",
    "run_proxy_stage",
]
