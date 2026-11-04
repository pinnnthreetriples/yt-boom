"""Tab «Прокси»: one row per proxy (connection, country, address, search role, accounts on it).

Space shows the type, login, last check and who uses it; Enter opens its actions: accounts on it (yes / no per
account and for the video search), check, change the line, search role, delete. «+» pastes one proxy or many (read
live as you paste, then checked and saved in a run), «C» checks all, «S» picks the search proxy. What a run leaves to
decide (save the failing ones anyway, give the new proxies to the accounts without one) waits as a row on top.
The library itself lives in `database.py`; checks in `modules.proxy_check`; the shared texts and the check runs in
`modules.proxy_screen`. A password is never shown.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any

from modules.proxy_paste import ProxyDraft, ProxyPasteError, parse_proxy_line, parse_proxy_paste
from modules.ui.app import AppContext
from modules.ui.icons import icon
from modules.ui.shell import Back, Cell, Confirm, Field, Hotkey, Menu, Notice, Row, Run, Screen, Tab, View, fit


@dataclass
class _Pending:
    """Left by the last add: proxies that failed the check (saved only if asked) and the plan for the accounts."""

    failing: list[tuple[ProxyDraft, Any]] = field(default_factory=list)
    plan: list[tuple[Any, Any]] = field(default_factory=list)  # (account, proxy record)


_PENDING: dict[int, _Pending] = {}


def _pending(ctx: AppContext) -> _Pending:
    return _PENDING.setdefault(id(ctx), _Pending())


# --------------------------------------------------------------------------- texts


def country_name(code: str | None) -> str:
    """«Польша» for PL when the name is known, else the code itself."""
    if not code:
        return "страна не известна"
    from modules.geo_guard import _ALIASES

    name = next((n for n, c in _ALIASES.items() if c == code.upper() and not n.isascii() and len(n) > 3), None)
    return name.capitalize() if name else code.upper()


def _conn(record: Any) -> tuple[str, str, str]:
    """(icon name, style, words) of the last check."""
    from modules.proxy_screen import short_error

    if record.password_unavailable:
        return "no_login", "err", "пароль не расшифрован — вставьте заново"
    if record.last_ok is None:
        return "unchecked", "dim", "не проверен"
    if record.last_ok:
        ms = f" · {record.last_latency_ms} мс" if record.last_latency_ms is not None else ""
        return "online", "ok", f"на связи{ms}"
    return "offline", "err", f"нет связи · {short_error(record.last_error)}"


def _names(items: list[str], limit: int = 3) -> str:
    shown = ", ".join(items[:limit])
    return shown + (f" и ещё {len(items) - limit}" if len(items) > limit else "")


def _proxy_cells(record: Any, users: int | None = None) -> list[Cell]:
    from modules.proxy_screen import country_of

    name, style, _ = _conn(record)
    users = record.accounts_count if users is None else users
    return [
        Cell(icon(name), style, 3),
        Cell(country_of(record) or "??", "accent" if country_of(record) else "dim", 3),
        Cell(record.address, "", 24, grow=True),
        Cell(icon("search") if record.is_search else "", "", 3),
        Cell(f"{icon('people')}{users}", "" if users else "dim"),
    ]


def _hint(record: Any) -> str:
    from modules.proxy_screen import country_of

    _, _, words = _conn(record)
    parts = [words[0].upper() + words[1:], country_name(country_of(record))]
    if record.is_search:
        parts.append("ищет видео")
    parts.append(f"аккаунтов {record.accounts_count}")
    return " · ".join(parts)


def _detail(ctx: AppContext, proxy_id: int) -> list[list[Cell]]:
    from modules.proxy_screen import ago, browser_warning, load_state, type_name

    state = load_state(ctx.config_path)
    record = state.proxy(proxy_id)
    if record is None:
        return []
    password = "пароль не расшифрован" if record.password_unavailable else "пароль ••••" if record.password else "без пароля"
    lines = [[Cell(f"{type_name(record.scheme)} · {('логин ' + record.login) if record.login else 'без логина'} · {password}")]]
    when = ago(record.last_check_at)
    if record.last_ok:
        exit_ = f"выход {record.last_exit_ip} {country_name(record.last_exit_country)}" if record.last_exit_ip else "выход не известен"
        lines.append([Cell(f"проверен {when or '—'} · {exit_}")])
    elif record.last_ok is False:
        lines.append([Cell(f"проверен {when or '—'} · {fit(record.last_error or 'ошибка', 56)}", "err")])
    else:
        lines.append([Cell("ещё не проверен")])
    if record.country_source == "user" and record.last_exit_country and record.country != record.last_exit_country:
        lines.append([Cell(f"{icon('warn')} указана {record.country}, а выход {record.last_exit_country}", "warn")])
    if warning := browser_warning(record):
        lines.append([Cell(f"{icon('warn')} {fit(warning, 66)}", "warn")])
    users = [a.name for a in state.on_proxy(record.id)]
    who = (["поиск видео"] if record.is_search else []) + users
    lines.append([Cell(f"{icon('people')} " + (_names(who, 4) if who else "никто не использует"))])
    return lines


# --------------------------------------------------------------------------- tab


def build_tab(ctx: AppContext) -> Tab:
    return Tab("Прокси", icon("tab_proxies"), lambda: main_view(ctx), warn=lambda: tab_warn(ctx))


def tab_warn(ctx: AppContext) -> bool:
    """Active accounts without a proxy, a proxy that failed its check, or the search on the own IP."""
    from database import SEARCH_PROXY_KEY

    if ctx.query("SELECT 1 FROM accounts WHERE is_active = 1 AND proxy_id IS NULL LIMIT 1"):
        return True
    if ctx.query("SELECT 1 FROM proxies WHERE last_ok = 0 LIMIT 1"):
        return True
    search = ctx.query("SELECT value FROM app_settings WHERE key = ?", (SEARCH_PROXY_KEY,))
    value = str(search[0][0]) if search and search[0][0] else ""
    return not (value.isdigit() and ctx.query("SELECT 1 FROM proxies WHERE id = ?", (int(value),)))


def _geo_warning() -> str | None:
    try:
        from modules.geo_guard import geoip_database_status

        ready, text = geoip_database_status()
    except Exception:
        return None
    return None if ready else text


def main_view(ctx: AppContext) -> View:
    from modules.proxy_screen import load_state

    state = load_state(ctx.config_path)
    top: list[list[Cell]] = []
    if geo := _geo_warning():
        top.append([Cell(icon("warn"), "", 3), Cell(fit(geo, 70), "warn")])

    rows: list[Row] = []
    pending = _pending(ctx)
    if pending.failing:
        n = len(pending.failing)
        rows.append(Row(
            "pending-failing", [Cell(icon("warn"), "", 3), Cell(f"не работают {n} — не сохранены", "warn")],
            hint="Enter — сохранить их всё равно", enter=lambda: _confirm_failing(ctx),
        ))
    if pending.plan:
        rows.append(Row(
            "pending-plan", [Cell(icon("people"), "", 3), Cell(f"назначить новые прокси аккаунтам без прокси: {len(pending.plan)}", "accent")],
            hint="Enter — назначить · Пробел — кому какой",
            detail=lambda: [[Cell(f"{acc.name} → {r.address}")] for acc, r in _pending(ctx).plan],
            enter=lambda: _confirm_plan(ctx),
        ))
    if state.search_lost:
        rows.append(Row("search-lost", [Cell(icon("warn"), "", 3), Cell("прокси поиска удалён — выберите другой", "warn")],
                        hint="Enter — выбрать прокси поиска", enter=lambda: Screen(lambda: search_view(ctx))))

    for record in state.proxies:
        rows.append(Row(
            f"p{record.id}", _proxy_cells(record), hint=_hint(record),
            detail=lambda pid=record.id: _detail(ctx, pid), enter=lambda pid=record.id: proxy_menu(ctx, pid),
        ))
    if not state.proxies:
        rows.append(Row("none", [Cell(icon("own_ip"), "warn", 3), Cell("прокси нет — всё идёт с вашего IP", "warn")], selectable=False))
        rows.append(Row("add", [Cell(icon("add"), "", 3), Cell("вставить строку прокси", "accent")],
                        hint="Добавить прокси", enter=lambda: add_field(ctx)))
    elif state.search is None and not state.search_lost:
        top.append([Cell(icon("own_ip"), "warn", 3), Cell("поиск видео — с вашего IP · S — выбрать прокси", "warn")])

    return View(
        rows=rows, top=top,
        keys="Enter · Пробел · + · C проверить · S поиск",
        hotkeys=[
            Hotkey("+", lambda _r: add_field(ctx)),
            Hotkey("C", lambda _r: _check_all(ctx)),
            Hotkey("S", lambda _r: Screen(lambda: search_view(ctx))),
        ],
    )


# --------------------------------------------------------------------------- one proxy


def proxy_menu(ctx: AppContext, proxy_id: int) -> Menu | Notice:
    from modules.proxy_screen import load_state

    state = load_state(ctx.config_path)
    record = state.proxy(proxy_id)
    if record is None:
        return Notice("warn", "Прокси уже удалён")
    users = state.on_proxy(proxy_id)
    rows = [
        Row("m-accounts", [Cell(icon("people"), "", 3), Cell("аккаунты на этом прокси"), Cell(str(len(users)), "dim")],
            hint="Назначить или снять аккаунты", enter=lambda: Screen(lambda: assign_view(ctx, proxy_id))),
        Row("m-check", [Cell(icon("check"), "", 3), Cell("проверить")], hint="Связь, внешний IP и страна",
            enter=lambda: Run(lambda: _check_one(ctx, proxy_id), f"{icon('check')} Проверка {record.address}")),
        Row("m-edit", [Cell(icon("edited"), "", 3), Cell("изменить строку")], hint="Новый пароль, порт, логин",
            enter=lambda: Field("новая строка этого прокси", lambda line: _edit_submit(ctx, proxy_id, line), secret=True,
                                preview=lambda lines: _preview(lines, set()))),
        Row("m-search", [Cell(icon("search"), "", 3), Cell("не искать через него" if record.is_search else "искать видео через него")],
            hint="Поиск пойдёт с вашего IP" if record.is_search else "Сделать прокси поиска",
            enter=lambda: _set_search(ctx, None if record.is_search else proxy_id)),
        Row("m-delete", [Cell(icon("trash"), "", 3), Cell("удалить")], hint="Убрать из списка",
            enter=lambda: _confirm_delete(ctx, proxy_id)),
    ]
    return Menu(rows)


def _check_one(ctx: AppContext, proxy_id: int) -> Notice:
    from modules.proxy_screen import check_one, load_state

    record = load_state(ctx.config_path).proxy(proxy_id)
    if record is None:
        return Notice("warn", "Прокси уже удалён")
    kind, text = check_one(ctx.config_path, record)
    return Notice(kind, fit(text, 74))


def _check_all(ctx: AppContext) -> Run:
    from modules.proxy_screen import check_all, load_state

    def work() -> Notice:
        kind, text = check_all(ctx.config_path, load_state(ctx.config_path))
        return Notice(kind, fit(text, 74))

    return Run(work, f"{icon('refresh')} Проверка всех прокси")


def _confirm_delete(ctx: AppContext, proxy_id: int) -> Confirm | Notice:
    from modules.proxy_screen import load_state

    state = load_state(ctx.config_path)
    record = state.proxy(proxy_id)
    if record is None:
        return Notice("warn", "Прокси уже удалён")
    users = [a.name for a in state.on_proxy(proxy_id)]
    question = "Удалить?"
    if users:
        question += f" {_names(users)} останутся без прокси"
    if record.is_search:
        question += " · поиск пойдёт с вашего IP"

    def pick(i: int) -> Notice | None:
        if i != 1:
            return None
        result = ctx.db(lambda db: db.delete_proxy(proxy_id))
        text = f"Удалён {record.address}"
        if result.detached_accounts:
            text += f" · теперь с вашего IP: {_names(result.detached_accounts)}"
        if result.was_search:
            text += " · поиск — с вашего IP"
        return Notice("warn" if result.detached_accounts or result.was_search else "ok", text)

    return Confirm([Cell(question, "warn")], ["Нет", "Удалить"], pick)


def _edit_submit(ctx: AppContext, proxy_id: int, line: str) -> Run | Notice | None:
    if not line:
        return None
    try:
        draft = parse_proxy_line(line)
    except ProxyPasteError as exc:
        return Notice("err", f"Строка не разобрана: {exc}")
    return Run(lambda: _edit_work(ctx, proxy_id, draft), f"{icon('check')} Проверка {draft.address}")


def _edit_work(ctx: AppContext, proxy_id: int, draft: ProxyDraft) -> Notice:
    """Check the new line; a working one replaces the proxy (its accounts and search role move with it)."""
    from modules import proxy_check as pc
    from modules.proxy_screen import _record, load_state, proxy_name

    state = load_state(ctx.config_path)
    record = state.proxy(proxy_id)
    if record is None:
        return Notice("warn", "Прокси уже удалён")
    result = asyncio.run(pc.check_proxy(draft))
    if not result.ok:
        return Notice("err", fit(f"Не работает, прокси не изменён: {result.message}", 74))
    users = state.on_proxy(proxy_id)
    country = draft.country if not (draft.country and result.exit_country and draft.country != result.exit_country) else None

    async def save(db: Any) -> Any:
        parts = replace(draft.parts, scheme=draft.scheme or result.scheme or record.scheme)
        new, _ = await db.add_proxy(parts, country=country)
        if country is None:
            await db.update_proxy(new.id, country=None)
        if new.id != record.id:
            if users:
                await db.assign_proxy(new.id, [a.id for a in users], reset_auth=new.host != record.host)
            if record.is_search:
                await db.set_search_proxy(new.id)
            await db.delete_proxy(record.id)
        return await _record(db, new.id, result)

    new = ctx.db(save)
    text = f"Прокси изменён: {proxy_name(new)}" + (f" · аккаунты ({len(users)}) остались на нём" if users else "")
    if result.browser_warning:
        text += " · ⚠ браузер не пойдёт через SOCKS5 с паролем"
    return Notice("warn" if result.browser_warning else "ok", fit(text, 74))


# --------------------------------------------------------------------------- accounts on a proxy


def assign_view(ctx: AppContext, proxy_id: int) -> View:
    """Every account with «да / нет» on this proxy, plus the video search; a change is saved at once."""
    from modules.proxy_screen import account_proxy_text, account_warnings, browser_warning, country_of, load_state

    state = load_state(ctx.config_path)
    record = state.proxy(proxy_id)
    if record is None:
        return View(rows=[Row("gone", [Cell("Прокси уже удалён", "warn")], selectable=False)])
    title = [Cell(f"{country_of(record) or '??'} {record.address} › аккаунты")]
    rows: list[Row] = []
    for acc, logged_in in state.accounts:
        on = acc.proxy_id == record.id
        warnings = account_warnings(acc, logged_in, record)
        now = "" if on else account_proxy_text(acc, state)
        rows.append(Row(
            f"a{acc.id}",
            [Cell(icon("working") if logged_in else icon("no_login"), "", 3), Cell(acc.name, "", 18),
             Cell("да" if on else "нет", "ok" if on else "dim", 5),
             Cell(f"{icon('warn')} " + ", ".join(warnings) if warnings else (f"сейчас {now}" if now else ""), "warn" if warnings else "dim")],
            hint=("На этом прокси" if on else f"Сейчас: {now}") + (" · " + ", ".join(warnings) if warnings else ""),
            values=["нет", "да"], value_index=int(on), on_value=lambda i, a=acc: _assign(ctx, record, a, i == 1),
        ))
    rows.append(Row(
        "search",
        [Cell(icon("search"), "", 3), Cell("поиск видео", "", 18), Cell("да" if record.is_search else "нет", "ok" if record.is_search else "dim", 5)],
        hint="Искать видео через этот прокси", values=["нет", "да"], value_index=int(record.is_search),
        on_value=lambda i: _set_search_now(ctx, proxy_id if i == 1 else None, proxy_id),
    ))
    if not state.accounts:
        rows.insert(0, Row("none", [Cell("аккаунтов нет — добавьте во вкладке «Аккаунты»", "dim")], selectable=False))
    top = [[Cell(f"{icon('warn')} {fit(w, 66)}", "warn")]] if (w := browser_warning(record)) else []
    return View(rows=rows, title=title, top=top, keys="←→ да / нет")


def _assign(ctx: AppContext, record: Any, acc: Any, on: bool) -> Notice | None:
    from modules.proxy_screen import browser_warning, proxy_name

    if on:
        if record.password_unavailable:
            return Notice("err", "Пароль этого прокси не расшифрован — сначала «изменить строку»")
        ctx.db(lambda db: db.assign_proxy(record.id, [acc.id]))
        if browser_warning(record):
            return Notice("warn", f"«{acc.name}» → {proxy_name(record)} · ⚠ браузер не пойдёт через SOCKS5 с паролем")
        return Notice("ok", f"«{acc.name}» → {proxy_name(record)}")
    if acc.proxy_id != record.id:
        return None
    ctx.db(lambda db: db.clear_account_proxy([acc.id]))
    return Notice("warn", f"«{acc.name}»: без прокси — с вашего IP")


def _set_search_now(ctx: AppContext, proxy_id: int | None, current: int) -> Notice | None:
    """The search switch inside the accounts list: saved at once, with a warning when accounts share the IP."""
    from modules.proxy_screen import load_state, proxy_name

    state = load_state(ctx.config_path)
    if proxy_id is None:
        if state.search is None or state.search.id != current:
            return None
        ctx.db(lambda db: db.set_search_proxy(None))
        return Notice("warn", "Поиск видео — с вашего IP")
    record = state.proxy(proxy_id)
    if record is None or record.is_search:
        return None
    if record.password_unavailable:
        return Notice("err", "Пароль этого прокси не расшифрован — сначала «изменить строку»")
    ctx.db(lambda db: db.set_search_proxy(proxy_id))
    users = [a.name for a in state.on_proxy(proxy_id)]
    if users:
        return Notice("warn", f"Поиск — через {proxy_name(record)} · много запросов на IP аккаунтов: {_names(users)}")
    return Notice("ok", f"Поиск — через {proxy_name(record)}")


# --------------------------------------------------------------------------- search proxy


def _set_search(ctx: AppContext, proxy_id: int | None) -> Confirm | Notice | None:
    """From a proxy's menu: make it the search proxy (asking first when accounts use it) or stop searching through it."""
    from modules.proxy_screen import load_state, proxy_name

    state = load_state(ctx.config_path)
    if proxy_id is None:
        ctx.db(lambda db: db.set_search_proxy(None))
        return Notice("warn", "Поиск видео — с вашего IP")
    record = state.proxy(proxy_id)
    if record is None:
        return Notice("warn", "Прокси уже удалён")
    if record.password_unavailable:
        return Notice("err", "Пароль этого прокси не расшифрован — сначала «изменить строку»")
    users = [a.name for a in state.on_proxy(proxy_id)]

    def save() -> Notice:
        ctx.db(lambda db: db.set_search_proxy(proxy_id))
        return Notice("ok", f"Поиск — через {proxy_name(record)}")

    if users:
        return Confirm([Cell(f"Поиск делает много запросов — на этом IP {_names(users)}. Всё равно?", "warn")],
                       ["Нет", "Да"], lambda i: save() if i == 1 else None)
    return save()


def search_view(ctx: AppContext) -> View:
    """Pick what searches videos and reads comments: a proxy of the list or the own IP."""
    from modules.proxy_screen import direct_text, load_state, proxy_name

    state = load_state(ctx.config_path)
    current = state.search.id if state.search is not None else None
    rows = [Row(
        "direct",
        [Cell(icon("own_ip"), "warn", 3), Cell("свой IP", "warn", 28), Cell("● сейчас" if current is None else "", "dim")],
        hint=f"Поиск делает много запросов к YouTube · {direct_text()}",
        enter=lambda: Back(_set_search(ctx, None)) if current is not None else Back(),
    )]
    for record in state.proxies:
        users = state.on_proxy(record.id)

        def pick(pid: int = record.id, rec: Any = record, names: list[str] = [a.name for a in users]):
            if pid == current:
                return Back()
            if rec.password_unavailable:
                return Notice("err", "Пароль этого прокси не расшифрован — сначала «изменить строку»")

            def save() -> Back:
                ctx.db(lambda db: db.set_search_proxy(pid))
                return Back(Notice("ok", f"Поиск — через {proxy_name(rec)}"))

            if names:
                return Confirm([Cell(f"На этом IP {_names(names)} — поиск может им навредить. Всё равно?", "warn")],
                               ["Нет", "Да"], lambda i: save() if i == 1 else None)
            return save()

        cells = _proxy_cells(record)[:3] + [Cell(f"{icon('people')}{len(users)}", "" if users else "dim", 5),
                                            Cell("● сейчас" if record.id == current else "", "dim")]
        rows.append(Row(f"p{record.id}", cells, hint=_hint(record), enter=pick))
    return View(rows=rows, title=[Cell(f"{icon('search')} поиск видео › через что искать")], keys="Enter выбрать")


# --------------------------------------------------------------------------- add


def add_field(ctx: AppContext) -> Field:
    from modules.proxy_screen import load_state

    known = {(p.host.lower(), p.port, p.login or "") for p in load_state(ctx.config_path).proxies}
    return Field(
        "вставьте прокси, по строке: ip:порт:логин:пароль[:страна] или socks5://логин:пароль@хост:порт",
        lambda text: _add_submit(ctx, text), multiline=True, secret=True, preview=lambda lines: _preview(lines, known),
    )


def _preview(lines: list[str], known: set[tuple[str, int, str]]) -> list[list[Cell]]:
    """How every pasted line is read: country, type, login, a hidden password — or why it is not a proxy."""
    from modules.proxy_screen import _country_may_be_password, type_name

    out: list[list[Cell]] = []
    for line in parse_proxy_paste("\n".join(lines)):
        if line.draft is None:
            out.append([Cell(icon("failed"), "", 3), Cell(fit(line.error or "", 66), "err")])
            continue
        d = line.draft
        note, style = "", ""
        if line.duplicate_of is not None:
            note, style = f"повтор строки {line.duplicate_of}", "dim"
        elif d.key in known:
            note, style = "уже в списке — обновим", "dim"
        elif _country_may_be_password(lines[line.line_no - 1] if line.line_no <= len(lines) else "", d):
            note, style = f"«{d.country}» принято за страну — если это пароль, вставьте как логин:пароль@IP:порт", "warn"
        out.append([
            Cell(icon("sent"), "", 3), Cell(d.country or "??", "accent" if d.country else "dim", 3), Cell(d.address, "", 24),
            Cell(type_name(d.scheme) if d.scheme else "тип ?", "dim", 8), Cell(d.login or "без логина", "dim", 12),
            Cell("••••" if d.password else "", "dim", 5), Cell(note, style),
        ])
    return out


def _add_submit(ctx: AppContext, text: str) -> Run | Notice | None:
    if not text.strip():
        return None
    lines = parse_proxy_paste(text)
    drafts = [line.draft for line in lines if line.draft is not None and line.duplicate_of is None]
    errors = sum(1 for line in lines if not line.ok)
    if not drafts:
        return Notice("err", "Ни одной строки не разобрано — проверьте формат")
    return Run(lambda: _add_work(ctx, drafts, errors), f"{icon('check')} Проверка {len(drafts)} прокси")


def _add_work(ctx: AppContext, drafts: list[ProxyDraft], errors: int) -> Notice:
    """Check in parallel, save the working ones (a checked exit country wins over the seller's), keep the failing
    ones and the plan for the accounts without a proxy as rows to decide on."""
    from modules import proxy_check as pc

    def progress(done: int, total: int) -> None:
        print(f"\r Проверено {done} из {total}…", end="" if done < total else "\n", flush=True)

    results = asyncio.run(pc.check_proxies(drafts, on_done=progress))
    good = [(d, r) for d, r in zip(drafts, results) if r.ok]
    failing = [(d, r) for d, r in zip(drafts, results) if not r.ok]
    saved = _save(ctx, good) if good else []
    pending = _pending(ctx)
    pending.failing = failing
    pending.plan = _plan(ctx, [rec for rec, res in saved if not res.browser_warning])
    text = f"Сохранено {len(saved)}"
    if failing:
        text += f" · не работают {len(failing)} — не сохранены"
    if errors:
        text += f" · строк с ошибкой {errors}"
    if pending.plan:
        text += " · можно назначить аккаунтам — строка сверху"
    socks = sum(1 for _, res in saved if res.browser_warning)
    if socks:
        text += f" · SOCKS5 с паролем ({socks}) — только для поиска"
    return Notice("ok" if saved and not failing and not errors else "warn", text)


def _save(ctx: AppContext, items: list[tuple[ProxyDraft, Any]]) -> list[tuple[Any, Any]]:
    from modules.proxy_screen import _record

    async def save(db: Any) -> list[tuple[Any, Any]]:
        out = []
        for draft, result in items:
            mismatch = bool(draft.country and result.exit_country and draft.country != result.exit_country)
            parts = replace(draft.parts, scheme=draft.scheme or result.scheme)
            record, _ = await db.add_proxy(parts, country=None if mismatch else draft.country)
            if mismatch:
                await db.update_proxy(record.id, country=None)  # the check fills the exit country
            out.append((await _record(db, record.id, result), result))
        return out

    return ctx.db(save)


def _plan(ctx: AppContext, records: list[Any]) -> list[tuple[Any, Any]]:
    from modules.proxy_screen import distribute
    from modules.wizard import _setup_accounts

    if not records:
        return []
    waiting = [acc for acc, _ in _setup_accounts(ctx.config_path) if acc.proxy_id is None]
    plan, _ = distribute(records, waiting)
    return plan


def _confirm_failing(ctx: AppContext) -> Confirm:
    pending = _pending(ctx)

    def pick(i: int) -> Notice | None:
        if i == 0:
            return None
        items, pending.failing = pending.failing, []
        if i == 2:
            return Notice("info", "Неработающие прокси отброшены")
        saved = _save(ctx, items)
        return Notice("warn", f"Сохранено {len(saved)} неработающих — проверьте их позже (C)")

    return Confirm([Cell(f"Сохранить неработающие ({len(pending.failing)})?", "warn")], ["Нет", "Сохранить", "Отбросить"], pick)


def _confirm_plan(ctx: AppContext) -> Confirm:
    pending = _pending(ctx)

    def pick(i: int) -> Notice | None:
        if i == 0:
            return None
        plan, pending.plan = pending.plan, []
        if i == 2:
            return Notice("info", "Назначение отменено")

        async def assign(db: Any) -> int:
            changed = 0
            for acc, record in plan:
                changed += len(await db.assign_proxy(record.id, [acc.id]))
            return changed

        return Notice("ok", f"Назначено аккаунтам: {ctx.db(assign)}")

    return Confirm([Cell(f"Назначить {len(pending.plan)} аккаунтам?", "accent")], ["Нет", "Назначить", "Не нужно"], pick)
