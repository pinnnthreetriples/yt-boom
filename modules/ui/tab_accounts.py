"""Tab «Аккаунты»: one row per account (state, proxy, stage, today's quota, auto warm-up) and everything done to them.

Enter on a row opens its menu (proxy, mode, in work, projects, auto warm-up, warm up now, channel profile, sign-in,
rename, and «Снять ограничение» for an account stopped by errors); «+» adds accounts, «R» refreshes, X ticks several.
With several projects the rows are grouped by project (an account in several shows once, in its first project's
group, with «📁2»); an account in no project does not work and says so.
Browser and network work (sign-in, warm-up, profile publishing, proxy check) runs as a `Run` step.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from modules.ui.app import AppContext
from modules.ui.icons import icon, quota
from modules.ui.shell import Cell, Confirm, Field, Hotkey, Menu, Notice, Row, Run, Screen, Step, Tab, View, fit

WARM_UP_SECONDS = 45.0  # same default as `--warm-up-duration`

_MODES = ("auto", "mature", "unlimited")
_STAGES = {
    "quarantine": ("quarantine", "Карантин · 0–24 ч", "dim"),
    "aging": ("aging", "Привыкает · 24–48 ч", "warn"),
    "probation": ("probation", "Испытательный · 1 в сутки", "accent"),
    "mature": ("mature", "Взрослый · 5 в сутки", "ok"),
}


# --------------------------------------------------------------------------- data


def load(ctx: AppContext) -> list[tuple[Any, bool, Any]]:
    """Accounts (seeded from config.yaml like before) with their login state and their proxy record."""
    from modules.wizard import check_youtube_session, load_accounts

    accounts = load_accounts(ctx.config_path)
    proxies = {p.id: p for p in ctx.db(lambda db: db.list_proxies())}
    return [(a, check_youtube_session(Path(a.session_path))[0], proxies.get(a.proxy_id)) for a in accounts]


def _fresh(ctx: AppContext, account_id: int) -> Any:
    return ctx.db(lambda db: db.get_account(account_id))


def _apply(ctx: AppContext, accounts: list[Any], **changes: Any) -> int:
    from youtube_bot import apply_account_settings

    return ctx.db(lambda db: apply_account_settings(db, accounts, **changes))


def account_state(acc: Any, logged: bool, now: datetime | None = None) -> tuple[str, str, str]:
    """(icon name, words, style) of what the account is doing."""
    from modules.account_menu_logic import next_comment_text
    from modules.lifecycle_engine import AccountLifecycleEngine

    now = now or datetime.now(timezone.utc)
    if not acc.is_active:
        return "paused", "На паузе — не работает", "dim"
    view = AccountLifecycleEngine.summarize(acc, now)
    if view.stage == "restricted":
        return "stopped", f"Остановлен: {acc.consecutive_errors} ошибки подряд", "err"
    if not logged:
        if acc.auth_status == "authenticated":
            return "stale", "Сессия устарела — войти заново", "warn"
        return "no_login", "Нет входа", "err"
    when, _ = next_comment_text(acc, now)
    if when.startswith(("через", "после")):
        return "waiting", f"Ждёт · {when}", "dim"
    return "working", "Работает", "ok"


def stage_info(acc: Any, now: datetime | None = None) -> tuple[str, str, str]:
    """(icon name, words, style) of the lifecycle stage; the mode «уже прогрет» shows as mature."""
    from modules.lifecycle_engine import MODE_MATURE, MODE_UNLIMITED, AccountLifecycleEngine

    view = AccountLifecycleEngine.summarize(acc, now or datetime.now(timezone.utc))
    mode = AccountLifecycleEngine.mode_of(acc)
    if mode == MODE_UNLIMITED or view.stage == "unlimited":
        return "unlimited", "Без лимитов", "accent"
    if mode == MODE_MATURE:
        return "mature", "Уже прогрет · 5 в сутки", "ok"
    stage = view.stage if view.stage in _STAGES else (view.age_stage if view.age_stage in _STAGES else None)
    if stage is None:
        return "quarantine", "Без возраста — вход не через бота", "dim"
    return _STAGES[stage]


def quota_info(acc: Any, now: datetime | None = None) -> tuple[str, str]:
    """(pie glyph, words) of today's quota; ("", "") while there is no quota (not signed in, no limit)."""
    from modules.lifecycle_engine import AccountLifecycleEngine

    view = AccountLifecycleEngine.summarize(acc, now or datetime.now(timezone.utc))
    if view.quota_limit is None:
        return ("", f"Сегодня {view.quota_done}") if view.quota_done else ("", "")
    if view.quota_limit == 0:
        return "", ""
    return quota(view.quota_done or 0, view.quota_limit), f"Сегодня {view.quota_done or 0} из {view.quota_limit}"


def proxy_cell(acc: Any, record: Any) -> tuple[str, str, str]:
    """(text, style, words) of the account's proxy column."""
    if getattr(acc, "proxy_unavailable", False):
        return f"{icon('no_login')} не читается", "err", "Прокси не расшифровывается — вставьте его заново"
    if record is None:
        return f"{icon('own_ip')} свой IP", "warn", "Свой IP — без прокси"
    country = record.country or record.last_exit_country or "??"
    return f"{country} {record.host}", "", f"Прокси {country} {record.address}"


NO_PROJECT = "без проекта — не работает"
NEW_WITHOUT_PROJECT = "без проекта — добавьте в проект, чтобы работали"


def memberships(ctx: AppContext) -> dict[int, list[Any]]:
    """Account id -> its projects, in queue order."""
    projects = {p.id: p for p in ctx.projects()}
    order = list(projects)
    out: dict[int, list[Any]] = {}
    for account_id, project_id in ctx.query("SELECT account_id, project_id FROM project_accounts"):
        if project_id in projects:
            out.setdefault(int(account_id), []).append(projects[project_id])
    for items in out.values():
        items.sort(key=lambda p: order.index(p.id))
    return out


def row_hint(acc: Any, logged: bool, record: Any, projects: list[Any] | None = None) -> str:
    parts = [NO_PROJECT] if projects == [] else []
    parts += [account_state(acc, logged)[1], stage_info(acc)[1]]
    q = quota_info(acc)[1]
    if q:
        parts.append(q)
    if acc.auto_warmup:
        parts.append("Автопрогрев")
    if record is None and acc.is_active:
        parts.append("свой IP")
    return " · ".join(parts)


def account_detail(acc: Any, projects: list[Any] | None = None) -> list[list[Cell]]:
    from modules.account_menu_logic import account_card_rows

    card = {label: (value, style) for label, value, style in account_card_rows(acc)}
    first = f"{card['Режим'][0].lower()} · автопрогрев {card['Автопрогрев'][0]} · лимит {card['Лимит сегодня'][0]}"
    second = f"следующий ответ: {card['Следующий коммент'][0]} · прогрев: {card['Последний прогрев'][0]}"
    lines = [[Cell(first)], [Cell(second)]]
    if acc.consecutive_errors:
        lines.append([Cell(f"ошибок подряд: {acc.consecutive_errors}", "err")])
    if acc.email and acc.email.lower() != acc.name.lower():
        lines.append([Cell(acc.email)])
    if projects is not None:
        lines.append([Cell("проекты: " + ", ".join(p.name for p in projects), "") if projects else Cell(NO_PROJECT, "warn")])
    return lines


def account_row(ctx: AppContext, acc: Any, logged: bool, record: Any, projects: list[Any],
                grouped: bool = False) -> Row:
    """One account; `projects`: the ones it is in (none: it does not work); `grouped`: «📁2» for several."""
    state_icon, _, _ = account_state(acc, logged)
    stage_icon, _, _ = stage_info(acc)
    pie, _ = quota_info(acc)
    proxy, proxy_style, _ = proxy_cell(acc, record)
    cells = [
        Cell(icon(state_icon), "", 3),
        Cell(acc.name, "dim" if not acc.is_active else ("warn" if not projects else ""), 17),
        Cell(proxy, proxy_style, 21, grow=True),
        Cell(icon(stage_icon), "", 3),
        Cell(pie, "accent", 2),
        Cell(icon("warmup") if acc.auto_warmup else "", "", 3),
    ]
    if grouped:
        cells.append(Cell(f"{icon('project')}{len(projects)}" if len(projects) > 1 else "", "dim", 4))
    return Row(
        f"a{acc.id}",
        cells,
        hint=row_hint(acc, logged, record, projects),
        detail=lambda: account_detail(_fresh(ctx, acc.id) or acc, projects),
        enter=lambda: account_menu(ctx, acc.id),
    )


def grouped_rows(ctx: AppContext, entries: list[tuple[Any, bool, Any]], of: dict[int, list[Any]]) -> list[Row]:
    """Rows under project headers «📁 Крипта · 4» (queue order, then «без проекта»); each account once."""
    rows: list[Row] = []
    groups: list[tuple[str, int, list[tuple[Any, bool, Any]]]] = []
    for project in ctx.projects():
        mine = [e for e in entries if of.get(e[0].id) and of[e[0].id][0].id == project.id]
        if mine:
            groups.append((project.name, project.accounts_count, mine))
    loose = [e for e in entries if not of.get(e[0].id)]
    if loose:
        groups.append(("без проекта", len(loose), loose))
    for name, count, mine in groups:
        rows.append(Row(f"h-{name}", [Cell(f"{icon('project')} {name} · {count}", "warn" if name == "без проекта" else "dim")],
                        selectable=False))
        rows += [account_row(ctx, acc, logged, record, of.get(acc.id, []), grouped=True) for acc, logged, record in mine]
    return rows


# --------------------------------------------------------------------------- tab


def build_tab(ctx: AppContext) -> Tab:
    def build() -> View:
        entries = load(ctx)
        of = memberships(ctx)
        if len(ctx.projects()) > 1:
            rows = grouped_rows(ctx, entries, of)
        else:
            rows = [account_row(ctx, acc, logged, record, of.get(acc.id, [])) for acc, logged, record in entries]
        if not rows:
            rows = [Row("add", [Cell(icon("add"), "", 3), Cell("добавить аккаунт", "accent")],
                        hint="Добавить аккаунты: строки «почта пароль 2FA» или файл", enter=lambda: add_field(ctx))]
        return View(
            rows=rows,
            keys="Enter · Пробел · + добавить · X отметить",
            hotkeys=[
                Hotkey("+", lambda _r: add_field(ctx)),
                Hotkey("=", lambda _r: add_field(ctx)),  # the same key without Shift
                Hotkey("R", lambda _r: Notice("ok", "Аккаунты обновлены")),
            ],
            marks=lambda ids: bulk_menu(ctx, [int(i[1:]) for i in ids if i.startswith("a")]).rows,
        )

    def warn() -> bool:
        of = memberships(ctx)
        return any(acc.is_active and (not logged or record is None or not of.get(acc.id)) for acc, logged, record in load(ctx))

    return Tab("Аккаунты", icon("tab_accounts"), build, warn)


# --------------------------------------------------------------------------- one account


def _value_row(row_id: str, icon_name: str, label: str, values: list[str], index: int,
               on_value: Callable[[int], Notice | None], hint: str) -> Row:
    return Row(row_id, [Cell(icon(icon_name), "", 3), Cell(label, "", 14), Cell(values[index])],
               hint=hint, values=values, value_index=index, on_value=on_value)


def account_menu(ctx: AppContext, account_id: int) -> Step:
    from modules.lifecycle_engine import CIRCUIT_BREAKER_ERRORS, MODE_LABELS, AccountLifecycleEngine

    acc = _fresh(ctx, account_id)
    if acc is None:
        return Notice("warn", "Аккаунт не найден")
    proxies = {p.id: p for p in ctx.db(lambda db: db.list_proxies())}
    proxy_text, proxy_style, proxy_words = proxy_cell(acc, proxies.get(acc.proxy_id))
    in_projects = memberships(ctx).get(account_id, [])
    mode = AccountLifecycleEngine.mode_of(acc)
    modes = [MODE_LABELS[m].lower() for m in _MODES]

    def set_mode(i: int) -> Notice:
        _apply(ctx, [_fresh(ctx, account_id)], lifecycle_mode=_MODES[i])
        return Notice("ok", f"«{acc.name}»: режим «{modes[i]}»")

    def set_active(i: int) -> Notice:
        _apply(ctx, [_fresh(ctx, account_id)], is_active=i == 0)
        return Notice("ok" if i == 0 else "warn", f"«{acc.name}»: " + ("в работе" if i == 0 else "на паузе — не берётся в рассылку"))

    def set_warm(i: int) -> Notice:
        _apply(ctx, [_fresh(ctx, account_id)], auto_warmup=i == 0)
        return Notice("ok", f"«{acc.name}»: автопрогрев " + ("включён" if i == 0 else "выключен"))

    rows = [
        Row("m-proxy", [Cell(icon("tab_proxies"), "", 3), Cell("прокси", "", 14), Cell(proxy_text, proxy_style)],
            hint=proxy_words + " · Enter — выбрать", enter=lambda: proxy_menu(ctx, account_id)),
        _value_row("m-mode", "mature", "режим", modes, _MODES.index(mode), set_mode,
                   "По возрасту · уже прогрет: сразу 5 в сутки · без лимитов: нет пауз и квот"),
        _value_row("m-active", "working", "в работе", ["да", "нет"], 0 if acc.is_active else 1, set_active,
                   "На паузе аккаунт не пишет и не прогревается"),
        _value_row("m-warm", "warmup", "автопрогрев", ["вкл", "выкл"], 0 if acc.auto_warmup else 1, set_warm,
                   "Фоновый прогрев во время рассылки"),
        Row("m-warm-now", [Cell(icon("warmup"), "", 3), Cell("прогреть сейчас")],
            hint=f"Ручной прогрев ~{WARM_UP_SECONDS:.0f} с, откроется браузер", enter=lambda: warm_now(ctx, account_id)),
        Row("m-profile", [Cell(icon("profile"), "", 3), Cell("профиль канала")],
            hint="Аватарка, никнейм, описание", enter=lambda: Screen(lambda: profile_view(ctx, account_id))),
        Row("m-login", [Cell(icon("login"), "", 3), Cell("вход"), Cell("войти · проверить · выйти", "dim")],
            hint="Войти автоматически или вручную, проверить вход, выйти", enter=lambda: login_menu(ctx, account_id)),
        Row("m-rename", [Cell(icon("rename"), "", 3), Cell("переименовать")],
            hint="Имя для вас: ник или почта", enter=lambda: rename_field(ctx, account_id)),
        Row("m-projects", [Cell(icon("project"), "", 3), Cell("проекты", "", 14),
                           Cell(str(len(in_projects)) if in_projects else "нет", "" if in_projects else "warn")],
            hint=("В проектах: " + ", ".join(p.name for p in in_projects) if in_projects else NO_PROJECT) + " · Enter — список",
            enter=lambda: Screen(lambda: account_projects_view(ctx, account_id))),
    ]
    if acc.consecutive_errors >= CIRCUIT_BREAKER_ERRORS:
        rows.append(Row("m-release", [Cell(icon("stopped"), "", 3), Cell("снять ограничение", "warn")],
                        hint=f"{acc.consecutive_errors} ошибок подряд — сбросить счётчик", enter=lambda: release(ctx, account_id)))
    return Menu(rows, rebuild=lambda: _menu_or_empty(account_menu(ctx, account_id)))


def _menu_or_empty(step: Step) -> Menu:
    return step if isinstance(step, Menu) else Menu([])


def account_projects_view(ctx: AppContext, account_id: int) -> View:
    """The projects with ▣ (the account is in it) / ▢: Enter or Space toggles."""
    acc = _fresh(ctx, account_id)
    if acc is None:
        return View(rows=[Row("none", [Cell("Аккаунт не найден", "warn")], selectable=False)])
    inside = {p.id for p in memberships(ctx).get(account_id, [])}

    def toggle(project: Any) -> Callable[[], Notice]:
        def run() -> Notice:
            if project.id in inside:
                ctx.db(lambda db: db.remove_project_accounts(project.id, [account_id]))
                return Notice("ok", f"«{acc.name}» вышел из «{project.name}»")
            ctx.db(lambda db: db.add_project_accounts(project.id, [account_id]))
            return Notice("ok", f"«{acc.name}» в проекте «{project.name}»")

        return run

    rows = [Row(f"p{p.id}", [Cell("▣" if p.id in inside else "▢", "ok" if p.id in inside else "dim", 3),
                             Cell(p.name, "", 20, grow=True), Cell("" if p.enabled else "пропуск", "dim")],
                hint=("в проекте · Enter — убрать" if p.id in inside else "не в проекте · Enter — добавить"),
                enter=toggle(p), space=toggle(p))
            for p in ctx.projects()]
    if not rows:
        rows.append(Row("empty", [Cell("проектов нет", "dim")], selectable=False))
    top = [] if inside else [[Cell(icon("project"), "", 3), Cell(NO_PROJECT, "warn")]]
    return View(rows=rows, top=top, title=[Cell(f"{acc.name} › проекты")], keys="Enter/Пробел — добавить/убрать")


def warm_now(ctx: AppContext, account_id: int) -> Step:
    acc = _fresh(ctx, account_id)

    def work() -> Notice:
        from youtube_bot import warm_up_cli

        ok = asyncio.run(warm_up_cli(acc.name, str(ctx.config_path), duration=WARM_UP_SECONDS))
        return Notice("ok", f"«{acc.name}»: прогрев выполнен") if ok else Notice("err", f"«{acc.name}»: прогрев не удался, подробности в логе")

    return Confirm([Cell(f"Прогреть «{acc.name}»? Откроется браузер, ~{WARM_UP_SECONDS:.0f} с")], ["Нет", "Прогреть"],
                   lambda i: Run(work, f"{icon('warmup')} Прогрев · {acc.name}") if i == 1 else None)


def cookie_progress_printer(total_accounts: int) -> Callable[[str, int, int, str], None]:
    """Draws `[=====>    ] 3/5 wikipedia.org` in place on one terminal line (with the account name for several)."""
    from modules.cookie_robot import format_progress_bar
    from modules.wizard import safe_print

    width = 0

    def draw(account: str, step: int, total: int, site: str) -> None:
        nonlocal width
        line = format_progress_bar(step, total, site)
        if total_accounts > 1:
            line = f"{account}  {line}"
        line = f" {line}"
        safe_print("\r" + line.ljust(width), end="", flush=True)
        width = len(line)

    return draw


def cookie_robot_work(ctx: AppContext, names: list[str], then: Callable[[str], Step] | None = None) -> Callable[[], Step]:
    """Work of the «куки-робот» `Run`: the robot over `names` with the progress bar, then a Notice with the outcome.

    With `then` (the add / import flow) the next step is `then(outcome)` instead: the sign-in question follows either way."""

    def work() -> Step:
        from modules.wizard import safe_print
        from youtube_bot import run_cookie_robot

        draw = cookie_progress_printer(len(names))
        try:
            results = asyncio.run(run_cookie_robot(str(ctx.config_path), names, on_progress=draw, report=lambda _line: None))
        except Exception as exc:
            ok, text = False, f"Куки-робот не отработал: {exc.__class__.__name__} (подробности в логе)"
        else:
            good = [r for r in results if r.success]
            ok = bool(good)
            if len(results) == 1:
                text = f"«{results[0].account_name}»: куки-робот — {results[0].details}"
            else:
                text = f"Куки-робот: {len(good)} из {len(results)} акк."
                bad = next((r for r in results if not r.success), None)
                if bad is not None:
                    text += f" · «{bad.account_name}»: {bad.details}"
        safe_print()  # the progress line stays; what comes next starts under it
        if then is not None:
            return then(text)
        return Notice("ok" if ok else "err", fit(text, 110))

    return work


def cookie_robot_menu(ctx: AppContext, account_id: int) -> Step:
    acc = _fresh(ctx, account_id)
    return Confirm([Cell(f"Нагулять куки «{acc.name}»? Откроется браузер, 4–5 сайтов, ~1 мин")], ["Нет", "Нагулять"],
                   lambda i: Run(cookie_robot_work(ctx, [acc.name]), f"{icon('cookie')} Куки-робот · {acc.name}") if i == 1 else None)


def release(ctx: AppContext, account_id: int) -> Step:
    acc = _fresh(ctx, account_id)

    def pick(i: int) -> Step:
        if i != 1:
            return None
        from modules.lifecycle_engine import release_restriction

        async def run(db: Any) -> None:
            await release_restriction(db, await db.get_account(account_id))

        ctx.db(run)
        return Notice("ok", f"«{acc.name}»: ограничение снято")

    return Confirm([Cell(f"Снять ограничение с «{acc.name}»? Счётчик ошибок обнулится", "warn")], ["Нет", "Снять"], pick)


def rename_field(ctx: AppContext, account_id: int) -> Step:
    acc = _fresh(ctx, account_id)

    def submit(text: str) -> Step:
        from modules.wizard import _setup_rename_in_config

        new_name = text.strip()
        if not new_name:
            return Notice("warn", "Имя не может быть пустым")
        if new_name == acc.name:
            return None
        if ctx.db(lambda db: db.get_account(new_name)) is not None:
            return Notice("warn", f"Имя «{new_name}» уже занято")
        try:
            ctx.db(lambda db: db.rename_account(acc.name, new_name))
        except ValueError as exc:
            return Notice("err", f"Не удалось переименовать: {exc}")
        _setup_rename_in_config(ctx.config_path, acc, new_name)
        return Notice("ok", f"Переименован: {acc.name} → {new_name}")

    return Field("Новое имя — для вас: ник или почта", submit, initial=acc.name)


# --------------------------------------------------------------------------- sign-in


def login_menu(ctx: AppContext, account_id: int) -> Step:
    acc = _fresh(ctx, account_id)
    return Menu([
        Row("l-auto", [Cell(icon("login"), "", 3), Cell("войти автоматически")],
            hint="По почте и паролю аккаунта, откроется браузер", enter=lambda: auto_login(ctx, account_id)),
        Row("l-manual", [Cell(icon("window_on"), "", 3), Cell("войти вручную")],
            hint="Откроется окно браузера: войдите в Google и закройте окно", enter=lambda: manual_login(ctx, acc.name)),
        Row("l-cookies", [Cell(icon("cookie"), "", 3), Cell("нагулять куки")],
            hint="Перед входом: браузер обойдёт 4–5 сайтов, чтобы у профиля были куки Google", enter=lambda: cookie_robot_menu(ctx, account_id)),
        Row("l-check", [Cell(icon("check"), "", 3), Cell("проверить вход")],
            hint="Есть ли сохранённая сессия YouTube", enter=lambda: check_login(acc)),
        Row("l-logout", [Cell(icon("no_login"), "", 3), Cell("выйти")],
            hint="Удалить сохранённую сессию", enter=lambda: logout(ctx, account_id)),
    ])


def check_login(acc: Any) -> Notice:
    from modules.wizard import check_youtube_session

    ok = check_youtube_session(Path(acc.session_path))[0]
    return Notice("ok", f"«{acc.name}»: вход выполнен") if ok else Notice("warn", f"«{acc.name}»: нет входа")


def logout(ctx: AppContext, account_id: int) -> Step:
    acc = _fresh(ctx, account_id)

    def pick(i: int) -> Step:
        if i != 1:
            return None
        from modules.wizard import _setup_clear_session

        ok, msg = _setup_clear_session(Path(ctx.cfg().browser.session_root), acc)
        return Notice("ok" if ok else "err", msg)

    return Confirm([Cell(f"Выйти из «{acc.name}»? Сессия удалится", "warn")], ["Нет", "Выйти"], pick)


def _auth_work(ctx: AppContext, name: str) -> Callable[[], Notice]:
    def work() -> Notice:
        from modules.wizard import _confirm_country_mismatch_async
        from youtube_bot import auth_account_cli

        try:
            ok = asyncio.run(auth_account_cli(name, str(ctx.config_path), confirm_country=_confirm_country_mismatch_async))
        except Exception as exc:
            return Notice("err", f"«{name}»: вход не удался ({exc.__class__.__name__}), подробности в логе")
        return Notice("ok", f"«{name}»: вход выполнен") if ok else Notice("err", f"«{name}»: вход не удался, подробности в логе")

    return work


def auto_login(ctx: AppContext, account_id: int) -> Step:
    acc = _fresh(ctx, account_id)
    run = Run(_auth_work(ctx, acc.name), f"{icon('login')} Вход · {acc.name}")
    if acc.email and acc.password:
        return run

    def submit(line: str) -> Step:
        if not line.strip():
            return None
        from youtube_bot import _register_credentials_account

        cfg = ctx.cfg()
        try:
            ctx.db(lambda db: _register_credentials_account(db, cfg, line.strip(), name=acc.name))
        except ValueError as exc:
            return Notice("err", f"Строка не принята: {exc}")
        return run

    return Field("У аккаунта нет пароля: почта пароль [2FA]", submit)


def manual_login(ctx: AppContext, name: str) -> Step:
    def work() -> Notice:
        from youtube_bot import interactive_login

        try:
            asyncio.run(interactive_login(str(ctx.config_path), account_name=name))
        except Exception as exc:
            return Notice("err", f"Вход «{name}» не удался: {exc.__class__.__name__}")
        return Notice("ok", f"Вход для «{name}» завершён")

    return Run(work, f"{icon('window_on')} Окно браузера · {name}: войдите в Google и закройте окно")


# --------------------------------------------------------------------------- proxy of an account


def proxy_menu(ctx: AppContext, account_id: int, accounts: list[int] | None = None) -> Step:
    """Pick a library proxy, no proxy, or paste a new one; `accounts` (bulk) gets the same choice."""
    from modules.proxy_screen import browser_warning

    targets = accounts or [account_id]
    acc = _fresh(ctx, account_id)
    current = acc.proxy_id if acc and not accounts else -1
    rows: list[Row] = []
    for record in ctx.db(lambda db: db.list_proxies()):
        state = "unchecked" if record.last_ok is None else ("online" if record.last_ok else "offline")
        country = record.country or record.last_exit_country or "??"

        def assign(record: Any = record) -> Step:
            if record.password_unavailable:
                return Notice("err", "Пароль этого прокси не расшифрован — вставьте его заново во вкладке «Прокси»")
            do = lambda: _assign(ctx, record, targets)
            if warning := browser_warning(record):
                return Confirm([Cell(warning, "warn")], ["Отмена", "Всё равно назначить"], lambda i: do() if i == 1 else None)
            return do()

        rows.append(Row(
            f"p{record.id}",
            [Cell(icon(state), "", 3), Cell(f"{country} {record.address}", "", 26, grow=True), Cell("● сейчас" if record.id == current else "", "dim")],
            hint=f"{country} {record.address} · аккаунтов {record.accounts_count}" + (" · ищет видео" if record.is_search else ""),
            enter=assign,
        ))

    def direct() -> Notice:
        names = ctx.db(lambda db: db.clear_account_proxy(targets))
        return Notice("warn", f"Без прокси — пишут с вашего IP: {len(names) or len(targets)} акк.")

    rows.append(Row("p-none", [Cell(icon("own_ip"), "", 3), Cell("без прокси · свой IP", "warn", 26, grow=True),
                               Cell("● сейчас" if current is None else "", "dim")],
                    hint="Свой IP — без прокси", enter=direct))
    rows.append(Row("p-new", [Cell(icon("add"), "", 3), Cell("вставить новый", "accent")],
                    hint="Строка прокси: ip:port:логин:пароль", enter=lambda: new_proxy_field(ctx, targets)))
    return Menu(rows)


def _assign(ctx: AppContext, record: Any, targets: list[int]) -> Notice:
    ctx.db(lambda db: db.assign_proxy(record.id, targets))
    country = record.country or record.last_exit_country or "??"
    return Notice("ok", f"{country} {record.address} → {len(targets)} акк.")


def new_proxy_field(ctx: AppContext, targets: list[int]) -> Step:
    from modules.proxy_paste import parse_proxy_paste

    def preview(lines: list[str]) -> list[list[Cell]]:
        out = []
        for item in parse_proxy_paste("\n".join(lines)):
            if item.draft:
                out.append([Cell(icon("sent"), "", 3), Cell(f"{item.draft.country or '??'} {item.draft.address}")])
            else:
                out.append([Cell(icon("failed"), "", 3), Cell(f"строка {item.line_no}: {item.error}", "err")])
        return out

    def submit(text: str) -> Step:
        drafts = [item.draft for item in parse_proxy_paste(text) if item.draft]
        if not drafts:
            return Notice("warn", "Прокси не распознан")
        return Run(lambda: _check_save_assign(ctx, drafts[0], targets), f"{icon('check')} Проверка прокси")

    return Field("строка прокси: ip:port:логин:пароль", submit, secret=False, preview=preview)


def _check_save_assign(ctx: AppContext, draft: Any, targets: list[int]) -> Notice:
    from modules.proxy_check import check_proxy

    result = asyncio.run(check_proxy(draft))

    async def save(db: Any) -> Any:
        record, _ = await db.add_proxy(replace(draft.parts, scheme=draft.scheme or result.scheme), country=draft.country)
        await db.record_proxy_check(
            record.id, result.ok, scheme=result.scheme, exit_ip=result.exit_ip, exit_country=result.exit_country,
            latency_ms=result.latency_ms, error=None if result.ok else result.message,
        )
        if result.ok:
            await db.assign_proxy(record.id, targets)
        return record

    record = ctx.db(save)
    if not result.ok:
        return Notice("err", fit(f"{record.address} сохранён, но не работает: {result.message} — не назначен", 74))
    return Notice("ok", f"{result.exit_country or '??'} {record.address} · {result.latency_ms} мс → {len(targets)} акк.")


# --------------------------------------------------------------------------- channel profile


def profile_view(ctx: AppContext, account_id: int) -> View:
    from modules.account_menu_logic import (
        name_changes_left,
        plural_changes,
        profile_card_lines,
        profile_pending_fields,
    )

    acc = _fresh(ctx, account_id)
    if acc is None:
        return View(rows=[Row("none", [Cell("Аккаунт не найден", "warn")], selectable=False)])
    pending = set(profile_pending_fields(acc))
    lines = {label: (value, hint) for label, value, hint, _ in profile_card_lines(acc)}

    def field_row(field: str, icon_name: str, label: str, enter: Callable[[], Step], hint: str) -> Row:
        value, applied = lines[label]
        cells = [Cell(icon(icon_name), "", 3), Cell(label.lower(), "", 11), Cell(fit(value, 40), "" if value != "—" else "dim", 42)]
        if field in pending:
            cells.append(Cell(icon("edited"), "warn"))
        return Row(f"f-{field}", cells, hint=hint + (" · изменено, не применено" if field in pending else f" · {applied}" if applied else ""),
                   enter=enter)

    left = name_changes_left(acc)
    rows = [
        field_row("avatar", "avatar", "Аватарка", lambda: avatar_field(ctx, account_id), "Картинка png/jpg/gif/bmp до 4 МБ"),
        field_row("name", "nickname", "Никнейм", lambda: nickname_field(ctx, account_id),
                  f"Имя канала · YouTube разрешает ещё {left} смен за 14 дней"),
        field_row("description", "description", "Описание", lambda: description_field(ctx, account_id), "Описание канала"),
        Row("gap", [Cell("")], selectable=False),
        Row("apply", [Cell(icon("upload"), "", 3),
                      Cell(f"применить на YouTube · {plural_changes(len(pending))}" if pending else "нет правок",
                           "accent" if pending else "dim")],
            hint="Откроется браузер, ~1–2 мин", enter=lambda: apply_profile(ctx, account_id)),
    ]
    signed = acc.auth_status == "authenticated"
    top = [] if signed else [[Cell(icon("no_login"), "", 3), Cell("нет входа — применить нельзя, сначала войдите", "warn")]]
    return View(rows=rows, top=top, title=[Cell(f"{acc.name} › профиль канала")], keys="Enter изменить")


def _draft(ctx: AppContext, account_id: int, **fields: Any) -> None:
    ctx.db(lambda db: db.set_account_profile_draft(account_id, **fields))


def nickname_field(ctx: AppContext, account_id: int) -> Step:
    from modules.account_menu_logic import NICKNAME_MAX, validate_nickname

    acc = _fresh(ctx, account_id)

    def submit(text: str) -> Step:
        if not text:
            return None
        try:
            name = validate_nickname(text)
        except ValueError as exc:
            return Notice("warn", str(exc))
        _draft(ctx, account_id, name=name)
        return Notice("ok", "Никнейм сохранён — примените на YouTube, когда всё готово")

    return Field(f"Никнейм канала, до {NICKNAME_MAX} символов", submit, initial=acc.profile_name or "")


def description_field(ctx: AppContext, account_id: int) -> Step:
    from modules.account_menu_logic import DESCRIPTION_MAX, validate_description

    def submit(text: str) -> Step:
        if not text.strip():
            return None
        try:
            description = validate_description(text.splitlines())
        except ValueError as exc:
            return Notice("warn", str(exc))
        _draft(ctx, account_id, description=description)
        return Notice("ok", "Описание сохранено — примените на YouTube, когда всё готово")

    return Field(f"Описание, до {DESCRIPTION_MAX} символов: строки, пустая строка — готово", submit, multiline=True)


def avatar_field(ctx: AppContext, account_id: int) -> Step:
    acc = _fresh(ctx, account_id)

    def submit(text: str) -> Step:
        if not text:
            return None
        from modules.account_menu_logic import applied_snapshot, clean_path_input
        from modules.profile_manager import AvatarError, forget_avatar, import_avatar

        try:
            stored = import_avatar(clean_path_input(text), acc.name)
        except (AvatarError, OSError) as exc:
            return Notice("warn", str(exc) if isinstance(exc, AvatarError) else f"Файл не прочитан: {exc.__class__.__name__}")
        _draft(ctx, account_id, avatar=stored)
        forget_avatar(acc.profile_avatar, keep=(stored, applied_snapshot(acc).get("avatar")))
        return Notice("ok", "Аватарка сохранена — примените на YouTube, когда всё готово")

    return Field("Путь к картинке (можно перетащить файл в окно)", submit)


def apply_profile(ctx: AppContext, account_id: int) -> Step:
    from modules.account_menu_logic import plural_changes, profile_pending_fields

    acc = _fresh(ctx, account_id)
    pending = profile_pending_fields(acc)
    if not pending:
        return Notice("info", "Нет правок для применения")
    if acc.auth_status != "authenticated":
        return Notice("warn", "Сначала войдите в аккаунт")

    def work() -> Notice:
        from modules.account_menu_logic import format_profile_summary
        from modules.profile_manager import ProfileEditor

        cfg = ctx.cfg()
        account = _fresh(ctx, account_id)
        try:
            result = ctx.db(lambda db: ProfileEditor(cfg.browser).apply(account, db))
        except Exception as exc:
            return Notice("err", f"Не удалось применить: {exc.__class__.__name__} (подробности в логе)")
        kind, text = format_profile_summary(
            result.account_name, result.success, result.changed_fields, result.details, result.skipped_fields
        )
        return Notice(kind, fit(text, 74))

    return Confirm([Cell(f"Применить {plural_changes(len(pending))}? Откроется браузер, ~1–2 мин")], ["Нет", "Применить"],
                   lambda i: Run(work, f"{icon('upload')} Профиль канала · {acc.name}") if i == 1 else None)


# --------------------------------------------------------------------------- add accounts


def _read_lines(text: str) -> tuple[list[str], str | None]:
    """The pasted lines, or the lines of a file when the only line is a path; (lines, error)."""
    from modules.account_menu_logic import clean_path_input

    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) == 1:
        path = Path(clean_path_input(lines[0]))
        try:
            if path.suffix and path.is_file():
                return path.read_text(encoding="utf-8-sig").splitlines(), None
        except UnicodeDecodeError:
            return [], "Файл не в кодировке UTF-8 — сохраните его как UTF-8"
        except OSError as exc:
            return [], f"Не удалось прочитать файл: {exc.strerror or exc.__class__.__name__}"
    return lines, None


def add_preview(lines: list[str], known: set[str]) -> list[list[Cell]]:
    from modules.account_menu_logic import clean_pasted_lines
    from modules.account_parser import parse_account_line

    resolved, error = _read_lines("\n".join(lines))
    if error:
        return [[Cell(icon("failed"), "", 3), Cell(error, "err")]]
    out: list[list[Cell]] = []
    if resolved is not None and len(lines) == 1 and resolved != lines:
        out.append([Cell(icon("preview"), "", 3), Cell(f"файл: строк {len(resolved)}", "dim")])
    for number, line in enumerate(clean_pasted_lines(resolved), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            parsed = parse_account_line(line)
        except ValueError as exc:
            out.append([Cell(icon("failed"), "", 3), Cell(fit(f"строка {number}: {exc}", 66), "err")])
            continue
        extra = (" · 2FA" if parsed.totp_secret else "") + (" · прокси" if parsed.proxy else "")
        if parsed.email.lower() in known:
            out.append([Cell(icon("refresh"), "", 3), Cell(f"{parsed.email} · уже есть, пропущу", "dim")])
        else:
            out.append([Cell(icon("sent"), "", 3), Cell(f"{parsed.email} · пароль{extra}")])
    return out[-12:]


def add_field(ctx: AppContext) -> Step:
    from modules.lifecycle_engine import MODE_LABELS

    known = {a.email.lower() for a in ctx.db(lambda db: db.list_accounts()) if a.email}

    def submit(text: str) -> Step:
        from modules.account_menu_logic import clean_pasted_lines, count_account_lines

        lines, error = _read_lines(text)
        if error:
            return Notice("err", error)
        lines = clean_pasted_lines(lines)
        total = count_account_lines(lines)
        if not total:
            return None if not text.strip() else Notice("warn", "Строк с аккаунтами нет")
        modes = [MODE_LABELS[m].lower() for m in _MODES]
        return Confirm([Cell(f"Аккаунтов {total}. Режим для новых (mode= в строке важнее):")], modes,
                       lambda i: register(ctx, lines, _MODES[i], total))

    return Field("строки «почта пароль 2FA» или путь к файлу", submit, multiline=True,
                 preview=lambda lines: add_preview(lines, known))


def register(ctx: AppContext, lines: list[str], mode: str, total: int) -> Step:
    from modules.wizard import _setup_register

    try:
        result = _setup_register(ctx.config_path, lines, mode)
    except Exception as exc:
        return Notice("err", f"Не добавлено ({exc.__class__.__name__}) — часть аккаунтов могла добавиться, проверьте список")
    text = f"Добавлено {len(result.added)} из {total}"
    if result.skipped:
        text += f" · уже были: {len(result.skipped)}"
    if result.errors:
        text += f" · с ошибкой: {len(result.errors)} (строка {result.errors[0][0]}: {result.errors[0][1]})"
    if not result.added:
        return Notice("warn", fit(text, 74))
    names = [a.name for a in result.added]

    def sign_in_question(outcome: str = "") -> Step:
        def warm_then_remind() -> Notice:
            done = warm_selected(ctx, names)
            return Notice(done.kind, fit(f"{done.text} · {NEW_WITHOUT_PROJECT}", 110))

        later = f"{text} · {outcome}" if outcome else text  # the cookie robot's outcome rides in the notice of «Позже»
        # decision 7: a new account joins no project — say it in the notice that closes the adding
        return Confirm([Cell(fit(text + ". Войти и прогреть сейчас?", 60), "ok")], ["Позже", "Войти и прогреть"],
                       lambda i: Run(warm_then_remind, f"{icon('warmup')} Вход и прогрев · {len(names)} акк.")
                       if i == 1 else Notice("ok", fit(f"{later} · {NEW_WITHOUT_PROJECT}", 110)))

    def cookies_pick(i: int) -> Step:
        if i != 1:
            return sign_in_question()
        return Run(cookie_robot_work(ctx, names, then=sign_in_question), f"{icon('cookie')} Куки-робот · {len(names)} акк.")

    # a fresh profile has no cookies at all, which is what makes Google ask for a captcha at the sign-in
    return Confirm([Cell("Нагулять профиль перед входом (куки-робот)?")], ["Нет", "Да"], cookies_pick)


def warm_selected(ctx: AppContext, names: list[str]) -> Notice:
    from modules.account_menu_logic import format_warm_summary
    from modules.wizard import safe_print
    from youtube_bot import warm_selected_cli

    try:
        results = asyncio.run(warm_selected_cli(str(ctx.config_path), names, WARM_UP_SECONDS, report=lambda line: safe_print(f" {line}")))
    except Exception as exc:
        return Notice("err", f"Прогрев не удался: {exc.__class__.__name__} (подробности в логе)")
    kind, text = format_warm_summary(results)
    return Notice(kind, fit(text, 74))


# --------------------------------------------------------------------------- several accounts


def bulk_menu(ctx: AppContext, ids: list[int]) -> Menu:
    from modules.lifecycle_engine import MODE_LABELS

    n = len(ids)

    def accounts() -> list[Any]:
        return [a for a in (_fresh(ctx, i) for i in ids) if a is not None]

    def setting(**changes: Any) -> Callable[[], Notice]:
        def run() -> Notice:
            count = _apply(ctx, accounts(), **changes)
            return Notice("ok", f"Изменено {count} из {n} акк.")

        return run

    def choices(label: str, options: list[tuple[str, dict[str, Any]]]) -> Callable[[], Menu]:
        return lambda: Menu([Row(f"b-{label}-{i}", [Cell(text)], hint=f"{label}: {text} · {n} акк.", enter=setting(**changes))
                             for i, (text, changes) in enumerate(options)])

    def warm() -> Step:
        names = [a.name for a in accounts()]
        return Confirm([Cell(f"Прогреть {len(names)} акк.? Откроются браузеры, по ~{WARM_UP_SECONDS:.0f} с")], ["Нет", "Прогреть"],
                       lambda i: Run(lambda: warm_selected(ctx, names), f"{icon('warmup')} Прогрев · {len(names)} акк.") if i == 1 else None)

    return Menu([
        Row("b-mode", [Cell(icon("mature"), "", 3), Cell("режим…")], hint=f"Режим для {n} акк.",
            enter=choices("режим", [(MODE_LABELS[m].lower(), {"lifecycle_mode": m}) for m in _MODES])),
        Row("b-active", [Cell(icon("working"), "", 3), Cell("в работе…")], hint=f"В работе / пауза для {n} акк.",
            enter=choices("в работе", [("да", {"is_active": True}), ("нет — пауза", {"is_active": False})])),
        Row("b-warmup", [Cell(icon("warmup"), "", 3), Cell("автопрогрев…")], hint=f"Автопрогрев для {n} акк.",
            enter=choices("автопрогрев", [("вкл", {"auto_warmup": True}), ("выкл", {"auto_warmup": False})])),
        Row("b-proxy", [Cell(icon("tab_proxies"), "", 3), Cell("прокси…")], hint=f"Один прокси для {n} акк.",
            enter=lambda: proxy_menu(ctx, ids[0], accounts=ids)),
        Row("b-project-add", [Cell(icon("project"), "", 3), Cell("добавить в проект…")], hint=f"{n} акк. — в проект",
            enter=lambda: projects_menu(ctx, ids, add=True)),
        Row("b-project-remove", [Cell(icon("project"), "", 3), Cell("убрать из проекта…")], hint=f"{n} акк. — из проекта",
            enter=lambda: projects_menu(ctx, ids, add=False)),
        Row("b-warm-now", [Cell(icon("warmup"), "", 3), Cell("прогреть сейчас")], hint="Вход (если нужен) и прогрев", enter=warm),
    ])


def projects_menu(ctx: AppContext, ids: list[int], add: bool) -> Step:
    """Pick the project the marked accounts join (or leave)."""
    projects = ctx.projects()
    if not projects:
        return Notice("warn", "Проектов нет: вкладка «Проекты»")

    def change(project: Any) -> Callable[[], Notice]:
        def run() -> Notice:
            if add:
                count = ctx.db(lambda db: db.add_project_accounts(project.id, ids))
                return Notice("ok", f"В «{project.name}» добавлено {count} из {len(ids)} акк.")
            count = ctx.db(lambda db: db.remove_project_accounts(project.id, ids))
            return Notice("ok" if count else "info", f"Из «{project.name}» убрано {count} из {len(ids)} акк.")

        return run

    return Menu([Row(f"bp-{p.id}", [Cell(icon("project"), "", 3), Cell(p.name, "", 20), Cell(f"{icon('tab_accounts')} {p.accounts_count}", "dim")],
                     hint=("Добавить в" if add else "Убрать из") + f" «{p.name}» · {len(ids)} акк.", enter=change(p))
                 for p in projects])
