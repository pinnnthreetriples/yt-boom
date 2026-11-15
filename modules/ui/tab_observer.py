"""«Наблюдатель» in the tab «Запуск»: the account that never posts and only checks, from its own signed-in point of view
and through its own proxy, that the published replies are visible.

The row on the tab shows `● имя · прокси host` / `○ не задан`; Enter opens this screen: who the observer is (a new
`login:password:TOTP` line or a worker account that stops posting), its proxy (picked from the proxy library like an
account's), sign-in (auto / manual / check), checks on / off, the delay after a reply and removal. The observer is in
no project and in no account list of the «Аккаунты» tab. Chrome observers work too: their cookies come from the state
the Chrome runner exported at the last sign-in (`modules/chrome_session.py`).
"""
from __future__ import annotations

import asyncio
from typing import Any

from modules.observer_menu_logic import (
    assign_observer,
    check_counts_text,
    observer_status_text,
    parse_delay,
    register_observer,
    remove_observer,
    set_observer_enabled,
)
from modules.ui.app import AppContext, in_thread
from modules.ui.icons import engine_title, icon
from modules.ui.shell import Cell, Confirm, Field, Menu, Notice, Row, Screen, Step, View, fit

_OBSERVER_SQL = (
    "SELECT a.name, a.is_active, p.host, p.port FROM accounts a LEFT JOIN proxies p ON p.id = a.proxy_id"
    " WHERE a.role = 'observer' ORDER BY a.id LIMIT 1"
)


def status_of(ctx: AppContext) -> tuple[str, str]:
    """Text and style of the observer row on the tab (a read-only query, cheap enough for every redraw)."""
    rows = ctx.query(_OBSERVER_SQL)
    if not rows:
        return observer_status_text(None, "")
    name, active, host, port = rows[0]
    return observer_status_text(str(name), f"{host}:{port}" if host else "", bool(active))


def observer_row(ctx: AppContext) -> Row:
    """The row of the tab «Запуск»; Enter opens the observer screen."""
    text, style = status_of(ctx)
    return Row(
        "observer", [Cell(icon("observer"), "", 3), Cell("Наблюдатель:", "dim"), Cell(text, style)],
        hint="Отдельный аккаунт проверяет, виден ли ответ · свой прокси · сам ничего не публикует · Enter — настроить",
        enter=lambda: Screen(lambda: observer_view(ctx)),
    )


def _observer(ctx: AppContext) -> Any:
    return ctx.db(lambda db: db.get_observer_account())


def observer_view(ctx: AppContext) -> View:
    observer = _observer(ctx)
    counts = ctx.db(lambda db: db.comment_check_counts())
    cfg = ctx.cfg()
    delay = float(cfg.limits.visibility_check_delay_seconds)
    title = [Cell("Запуск › наблюдатель")]
    bottom = [[Cell(icon("seen"), "", 3), Cell("проверено:", "dim"), Cell(check_counts_text(counts))]]
    if observer is None:
        rows = [Row("o-account", [Cell(icon("observer"), "", 3), Cell("аккаунт", "", 10), Cell("○ не задан", "warn"),
                                  Cell("— проверка идёт анонимно", "dim")],
                    hint="Добавить аккаунт-наблюдатель · Enter — выбрать", enter=lambda: assign_menu(ctx))]
        return View(rows=rows, title=title, bottom=bottom, keys="Enter выбрать")

    text, style = observer_status_text(observer.name, _host(observer), observer.is_active)
    signed = observer.auth_status == "authenticated"
    on = observer.is_active and delay > 0

    def set_on(i: int) -> Notice:
        ctx.db(lambda db: set_observer_enabled(db, observer.name, i == 0))
        return Notice("ok" if i == 0 else "info", "Проверки наблюдателя включены" if i == 0 else "Проверки наблюдателя выключены (анонимно)")

    rows = [
        Row("o-account", [Cell(icon("observer"), "", 3), Cell("аккаунт", "", 10), Cell(text, style)],
            hint=f"Браузер: {engine_title(observer.engine)} · Enter — заменить", enter=lambda: assign_menu(ctx)),
        Row("o-proxy", [Cell(icon("tab_proxies"), "", 3), Cell("прокси", "", 10),
                        Cell(_host(observer) or "свой IP — без прокси", "" if observer.proxy_url else "warn")],
            hint="Прокси из вкладки «Прокси» · Enter — выбрать или вставить новый",
            enter=lambda: _proxy_step(ctx, observer.id)),
        Row("o-login", [Cell(icon("login"), "", 3), Cell("вход", "", 10),
                        Cell("● вошёл" if signed else "○ нет входа", "ok" if signed else "warn")],
            hint="Войти автоматически или вручную, проверить вход · после смены прокси войдите заново",
            enter=lambda: login_menu(ctx, observer.id)),
        Row("o-on", [Cell(icon("check"), "", 3), Cell("проверки", "", 10), Cell("включены" if on else "выключены", "" if on else "warn")],
            hint="Выключены — ответы проверяются анонимно · ←→ вкл / выкл", values=["включены", "выключены"],
            value_index=0 if observer.is_active else 1, on_value=set_on),
        Row("o-delay", [Cell(icon("clock"), "", 3), Cell("задержка", "", 10),
                        Cell(f"{delay:.0f} с после ответа" if delay > 0 else "0 — проверки выключены", "" if delay > 0 else "warn")],
            hint="Через сколько секунд после ответа смотреть, виден ли он · Enter — изменить", enter=lambda: delay_field(ctx, delay)),
        Row("o-remove", [Cell(icon("trash"), "", 3), Cell("удалить наблюдателя")],
            hint="Профиль браузера на диске остаётся", enter=lambda: remove_step(ctx, observer.name)),
    ]
    return View(rows=rows, title=title, bottom=bottom, keys="Enter действия · ←→ вкл / выкл")


def _host(observer: Any) -> str:
    from modules.observer_menu_logic import proxy_host

    return proxy_host(observer.proxy_url)


def _proxy_step(ctx: AppContext, account_id: int) -> Step:
    from modules.ui.tab_accounts import proxy_menu

    return proxy_menu(ctx, account_id)


# --------------------------------------------------------------------------- who the observer is


def assign_menu(ctx: AppContext) -> Step:
    """New account from a line, or a worker account that stops posting."""
    return Menu([
        Row("a-new", [Cell(icon("add"), "", 3), Cell("новый аккаунт")],
            hint="Строка: почта пароль [2FA] — отдельный аккаунт только для проверок", enter=lambda: new_field(ctx)),
        Row("a-old", [Cell(icon("tab_accounts"), "", 3), Cell("существующий аккаунт")],
            hint="Из списка: он перестанет публиковать и выйдет из проектов", enter=lambda: existing_menu(ctx)),
    ])


def new_field(ctx: AppContext) -> Step:
    def submit(line: str) -> Step:
        if not line.strip():
            return None
        cfg = ctx.cfg()
        try:
            account = ctx.db(lambda db: register_observer(db, cfg, line.strip()))
        except ValueError as exc:
            return Notice("err", fit(f"Строка не принята: {exc}", 74))
        return Notice("ok", f"Наблюдатель «{account.name}» добавлен — выберите прокси и войдите")

    return Field("почта пароль [2FA]", submit, secret=True)


def existing_menu(ctx: AppContext) -> Step:
    accounts = ctx.db(lambda db: db.list_accounts())
    if not accounts:
        return Notice("warn", "Аккаунтов нет: сначала добавьте аккаунт во вкладке «Аккаунты»")

    def pick(account: Any) -> Step:
        def done(i: int) -> Step:
            if i != 1:
                return None
            observer = ctx.db(lambda db: assign_observer(db, account.name))
            return Notice("ok", f"Наблюдатель: «{observer.name}» — больше не публикует")

        return Confirm([Cell(f"«{account.name}» перестанет публиковать ответы. Назначить?", "warn")], ["Нет", "Назначить"], done)

    return Menu([
        Row(f"x{a.id}", [Cell(icon("tab_accounts"), "", 3), Cell(a.name, "", 26, grow=True)],
            hint="Назначить наблюдателем", enter=lambda a=a: pick(a))
        for a in accounts
    ])


# --------------------------------------------------------------------------- sign-in, delay, removal


def login_menu(ctx: AppContext, account_id: int) -> Step:
    from modules.ui.tab_accounts import auto_login, manual_login

    observer = _observer(ctx)
    if observer is None:
        return Notice("warn", "Наблюдателя нет")
    return Menu([
        Row("l-auto", [Cell(icon("login"), "", 3), Cell("войти автоматически")],
            hint="По почте и паролю наблюдателя, откроется браузер", enter=lambda: auto_login(ctx, account_id)),
        Row("l-manual", [Cell(icon("window_on"), "", 3), Cell("войти вручную")],
            hint="Откроется окно браузера: войдите в Google и закройте окно", enter=lambda: manual_login(ctx, observer.name)),
        Row("l-check", [Cell(icon("check"), "", 3), Cell("проверить вход")],
            hint="Есть ли сохранённая сессия YouTube", enter=lambda: check_login(observer)),
    ])


def check_login(observer: Any) -> Notice:
    """Cookies only, nothing is sent: is the observer's profile signed in to YouTube?"""
    from modules.observer_session import observer_signed_in

    if in_thread(lambda: asyncio.run(observer_signed_in(observer))):
        return Notice("ok", f"Наблюдатель «{observer.name}» вошёл в YouTube")
    hint = "войдите заново: сохранённой сессии Chrome нет" if observer.engine == "chrome" else "нужен вход"
    return Notice("warn", f"Наблюдатель «{observer.name}» не вошёл: {hint}")


def delay_field(ctx: AppContext, current: float) -> Step:
    def submit(text: str) -> Step:
        try:
            delay = parse_delay(text)
        except ValueError as exc:
            return Notice("err", str(exc))
        ctx.update({"limits.visibility_check_delay_seconds": delay})
        return Notice("ok", f"Задержка проверки: {delay:.0f} с" if delay > 0 else "Проверки выключены (задержка 0)")

    return Field("секунд после ответа (0 — не проверять)", submit, initial=f"{current:.0f}")


def remove_step(ctx: AppContext, name: str) -> Step:
    def pick(i: int) -> Step:
        if i != 1:
            return None
        removed = ctx.db(lambda db: remove_observer(db))
        return Notice("ok", f"Наблюдатель «{removed or name}» удалён — проверка идёт анонимно")

    return Confirm([Cell(f"Удалить наблюдателя «{name}»? Профиль браузера останется на диске", "warn")], ["Нет", "Удалить"], pick)
