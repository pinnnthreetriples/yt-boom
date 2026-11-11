"""Tab «Проекты»: one row per project (in the launch, members, topics, own links, replies, today's replies).

Enter on a project opens its menu (target and replies, members, in the launch, daily limit per account, queue order,
rename, copy, delete); «+» adds a project. The project switch row of «Цель» / «Ответы» and the project texts shown
by «Запуск» live here too.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from modules.ui import shell
from modules.ui.app import AppContext
from modules.ui.icons import icon
from modules.ui.shell import Cell, Confirm, Field, GoTab, Hotkey, Menu, Notice, Row, Screen, Step, Tab, View, fit

_DAILY_PRESETS = [None, 1, 2, 3, 5, 10]  # «лимит в день на акк.»: no own limit, then the usual numbers
_MEMBERS_SHOWN = 5


def plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural: 1 тема, 2 темы, 5 тем."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def topics_text(project: Any) -> str:
    n = len(project.search_topics)
    return f"{n} {plural(n, 'тема', 'темы', 'тем')}" if n else "—"


def replies_text(project: Any) -> str:
    if project.reply_mode == "ai":
        return f"{icon('ai')} ИИ"
    return f"{icon('templates')} {len(project.custom_replies)} шабл."


def daily_text(project: Any) -> str:
    return f"{project.daily_per_account}/акк. в день" if project.daily_per_account else "без своего лимита"


def references_text(project: Any) -> str:
    kinds = [name for on, name in ((project.use_similar, "похожие"), (project.use_title_words, "слова из названий")) if on]
    if not kinds:
        return "выкл"
    return ", ".join(kinds) + ("" if project.links_total else " (нет своих ссылок)")


def plan_text(project: Any) -> str:
    """What a launch does for the project, in one line: «темы: …; ориентиры: …; свои ссылки: 8 ждут; лимит …»."""
    parts = [f"темы: {', '.join(f'«{t}»' for t in project.search_topics)}" if project.search_topics else "тем нет"]
    if project.use_similar or project.use_title_words:
        parts.append(f"ориентиры: {references_text(project)}")
    parts.append(f"свои ссылки: {project.links_waiting} ждут")
    parts.append(f"лимит {daily_text(project)}")
    return "; ".join(parts)


def _today_sent(ctx: AppContext) -> dict[int, int]:
    """Live replies of today by project."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = ctx.query(
        "SELECT project_id, COUNT(*) FROM activity_log WHERE created_at >= ? AND status = 'success'"
        " AND project_id IS NOT NULL GROUP BY project_id",
        (today,),
    )
    return {int(pid): int(n) for pid, n in rows}


def _member_names(ctx: AppContext, project_id: int) -> list[str]:
    rows = ctx.query(
        "SELECT a.name FROM project_accounts pa JOIN accounts a ON a.id = pa.account_id WHERE pa.project_id = ?"
        " ORDER BY a.id",
        (project_id,),
    )
    return [str(name) for (name,) in rows]


def members_line(names: list[str]) -> str:
    if not names:
        return "аккаунтов нет"
    shown = ", ".join(names[:_MEMBERS_SHOWN])
    rest = len(names) - _MEMBERS_SHOWN
    return f"аккаунты: {shown}" + (f" и ещё {rest}" if rest > 0 else "")


def filters_line(project: Any) -> str:
    from modules.ui.tab_target import age_label, views_label
    from modules.wizard import _TARGET_MODES

    mode = next((label for value, label, _ in _TARGET_MODES if value == project.target_mode), project.target_mode)
    return (f"свежесть: {age_label(project.max_age_hours)} · просмотры: {views_label(project.min_views, project.max_views)}"
            f" · {project.max_videos} видео на тему · {project.comments_per_video} на видео · {mode.lower()}")


def project_detail(ctx: AppContext, project_id: int) -> list[list[Cell]]:
    """Space on a project: topics, references, own links, filters, members (up to 5 names), limit, replies."""
    project = next((p for p in ctx.projects() if p.id == project_id), None)
    if project is None:
        return [[Cell("проект удалён", "warn")]]
    cols = max(40, shell.WIDTH - 8)
    topics = ", ".join(f"«{t}»" for t in project.search_topics) or "нет"
    replies = "ИИ DeepSeek" if project.reply_mode == "ai" else (
        f"шаблоны: {len(project.custom_replies)}" + (" · ИИ меняет слова" if project.paraphrase_custom else ""))
    lines = [
        f"темы: {topics}",
        f"ориентиры: {references_text(project)} · свои ссылки: {project.links_waiting} ждут из {project.links_total}",
        filters_line(project),
        members_line(_member_names(ctx, project.id)),
        f"лимит: {daily_text(project)} · ответы: {replies}",
    ]
    problems = ctx.problems(project)
    if problems:
        lines.append(f"не готов: {', '.join(problems)}")
    return [[Cell(fit(text, cols), "warn" if text.startswith("не готов") else "")] for text in lines]


def _fix_hint(problems: list[str]) -> str:
    where = {"нет аккаунтов": "аккаунты", "нет аккаунтов в работе": "аккаунты", "нет видео": "цель",
             "нет шаблонов": "ответы", "нет ключа DeepSeek": "ответы"}
    return " · ".join(f"{p}: Enter → {where.get(p, 'цель')}" for p in problems)


def project_row(ctx: AppContext, project: Any, sent: int) -> Row:
    problems = ctx.problems(project)
    style = "warn" if problems else ("dim" if not project.enabled else "")
    return Row(
        f"p{project.id}",
        [
            Cell(icon("project"), "", 3),
            Cell(project.name, style, 18),
            Cell("▶" if project.enabled else "‖", "" if project.enabled else "dim", 4),
            Cell(f"{icon('tab_accounts')} {project.accounts_count}", "warn" if not project.accounts_count else "", 7),
            Cell(f"{icon('topic')} {topics_text(project)}", "", 11),
            Cell(f"{icon('links')} {project.links_waiting}", "", 7),
            Cell(replies_text(project), "warn" if "нет шаблонов" in problems else "", 12),
            Cell(f"{icon('sent')} {sent}", "dim"),
        ],
        hint=_fix_hint(problems) if problems else
        f"{project.name} · {'в запуске' if project.enabled else 'пропуск'} · Пробел — подробно · Enter — действия",
        detail=lambda: project_detail(ctx, project.id),
        enter=lambda: project_menu(ctx, project.id),
    )


# --------------------------------------------------------------------------- tab


def build_tab(ctx: AppContext) -> Tab:
    def build() -> View:
        sent = _today_sent(ctx)
        rows = [project_row(ctx, p, sent.get(p.id, 0)) for p in ctx.projects()]
        rows.append(Row("add", [Cell(icon("add"), "", 3), Cell("новый проект", "accent")],
                        hint="Новый проект: свои аккаунты, темы и ответы", enter=lambda: new_project_field(ctx)))
        return View(
            rows=rows,
            keys="Enter · Пробел · + новый",
            hotkeys=[Hotkey("+", lambda _r: new_project_field(ctx)), Hotkey("=", lambda _r: new_project_field(ctx))],
        )

    def warn() -> bool:
        from modules.ui.app import launch_problem

        return bool(launch_problem(ctx))

    return Tab("Проекты", icon("tab_projects"), build, warn)


def new_project_field(ctx: AppContext) -> Field:
    def submit(text: str) -> Step:
        if not text.strip():
            return None
        try:
            project = ctx.db(lambda db: db.create_project(text))
        except ValueError as exc:
            return Notice("warn", str(exc))
        ctx.set_project(project.id)
        return GoTab("Цель", Notice("ok", f"Проект «{project.name}» создан: задайте темы или ссылки, затем аккаунты"))

    return Field("название проекта", submit)


# --------------------------------------------------------------------------- one project


def _get(ctx: AppContext, project_id: int) -> Any:
    return next((p for p in ctx.projects() if p.id == project_id), None)


def _set(ctx: AppContext, project_id: int, **fields: Any) -> Notice | None:
    try:
        ctx.update_project(project_id, **fields)
    except ValueError as exc:
        return Notice("warn", str(exc))
    return None


def project_menu(ctx: AppContext, project_id: int) -> Step:
    project = _get(ctx, project_id)
    if project is None:
        return Notice("warn", "Проект не найден")
    projects = ctx.projects()
    position = [p.id for p in projects].index(project_id)

    def set_enabled(i: int) -> Notice:
        return _set(ctx, project_id, enabled=i == 0) or Notice(
            "ok" if i == 0 else "info", f"«{project.name}»: " + ("в запуске" if i == 0 else "пропускается при запуске"))

    def move(i: int) -> Notice | None:
        if i == 1:
            return None
        moved = ctx.db(lambda db: db.move_project(project_id, -1 if i == 0 else 1))
        return None if moved else Notice("info", "Уже " + ("первый" if i == 0 else "последний") + " в очереди")

    daily = _daily_values(project)
    rows = [
        Row("pm-target", [Cell(icon("tab_target"), "", 3), Cell("цель и ответы")],
            hint="Сделать текущим и открыть «Цель»", enter=lambda: open_target(ctx, project_id)),
        Row("pm-members", [Cell(icon("tab_accounts"), "", 3), Cell("аккаунты", "", 22), Cell(str(project.accounts_count))],
            hint="Кто отвечает в этом проекте · Enter — список", enter=lambda: Screen(lambda: members_view(ctx, project_id))),
        Row("pm-enabled", [Cell("▶", "", 3), Cell("в запуске", "", 22), Cell("да" if project.enabled else "нет")],
            hint="Участвует ли проект в общем запуске", values=["да", "нет"], value_index=0 if project.enabled else 1,
            on_value=set_enabled),
        Row("pm-daily", [Cell(icon("limit"), "", 3), Cell("лимит в день на акк.", "", 22), Cell(daily[1][daily[0]])],
            hint="Не больше N ответов в день с аккаунта в этом проекте · ←→ значение · Enter — своё число",
            values=daily[1], value_index=daily[0], on_value=lambda i: set_daily(ctx, project_id, daily[2][i]),
            enter=lambda: daily_field(ctx, project_id)),
        Row("pm-order", [Cell(icon("order"), "", 3), Cell("порядок", "", 22), Cell(f"{position + 1}/{len(projects)} в очереди")],
            hint="Порядок запуска · ← выше · → ниже", values=["выше", "·", "ниже"], value_index=1, on_value=move),
        Row("pm-rename", [Cell(icon("rename"), "", 3), Cell("переименовать")], hint="Новое имя проекта",
            enter=lambda: rename_field(ctx, project_id)),
        Row("pm-copy", [Cell(icon("copy"), "", 3), Cell("копия")], hint="Новый проект с этими настройками, без аккаунтов и ссылок",
            enter=lambda: copy_field(ctx, project_id)),
        Row("pm-delete", [Cell(icon("trash"), "", 3), Cell("удалить")], hint="Удалить проект (журнал останется)",
            enter=lambda: delete_project(ctx, project_id)),
    ]
    return Menu(rows, rebuild=lambda: _menu_or_empty(project_menu(ctx, project_id)))


def _menu_or_empty(step: Step) -> Menu:
    return step if isinstance(step, Menu) else Menu([])


def _daily_values(project: Any) -> tuple[int, list[str], list[int | None]]:
    """(index, labels, values) of «лимит в день на акк.»: the presets plus the project's own number if it differs."""
    values: list[int | None] = list(_DAILY_PRESETS)
    if project.daily_per_account not in values:
        values = sorted([v for v in values if v is not None] + [project.daily_per_account])
        values.insert(0, None)
    labels = ["нет" if v is None else str(v) for v in values]
    return values.index(project.daily_per_account), labels, values


def set_daily(ctx: AppContext, project_id: int, value: int | None) -> Notice:
    return _set(ctx, project_id, daily_per_account=value) or Notice(
        "ok", f"Лимит в проекте: {value} в день на акк." if value else "Без своего лимита: только лимиты аккаунтов")


def daily_field(ctx: AppContext, project_id: int) -> Field:
    project = _get(ctx, project_id)

    def submit(text: str) -> Step:
        text = text.strip()
        if not text or text in {"0", "-", "нет"}:
            return set_daily(ctx, project_id, None)
        if not text.isdigit() or not 1 <= int(text) <= 1000:
            return Notice("warn", "Нужно число от 1 до 1000, пусто — без своего лимита")
        return set_daily(ctx, project_id, int(text))

    return Field("ответов в день с аккаунта в этом проекте · пусто — без своего лимита", submit,
                 initial=str(project.daily_per_account or "") if project else "")


def open_target(ctx: AppContext, project_id: int) -> Step:
    project = _get(ctx, project_id)
    if project is None:
        return Notice("warn", "Проект не найден")
    ctx.set_project(project_id)
    return GoTab("Цель", Notice("info", f"Проект «{project.name}»"))


def rename_field(ctx: AppContext, project_id: int) -> Field:
    project = _get(ctx, project_id)

    def submit(text: str) -> Step:
        if not text.strip() or text.strip() == project.name:
            return None
        try:
            renamed = ctx.db(lambda db: db.rename_project(project_id, text))
        except ValueError as exc:
            return Notice("warn", str(exc))
        return Notice("ok", f"Переименован: {project.name} → {renamed.name}")

    return Field("новое название проекта", submit, initial=project.name if project else "")


def copy_field(ctx: AppContext, project_id: int) -> Field:
    project = _get(ctx, project_id)

    def submit(text: str) -> Step:
        if not text.strip():
            return None
        try:
            copy = ctx.db(lambda db: db.copy_project(project_id, text))
        except ValueError as exc:
            return Notice("warn", str(exc))
        return Notice("ok", f"Проект «{copy.name}» создан: настройки «{project.name}», без аккаунтов и ссылок")

    return Field("название копии", submit, initial=fit(f"{project.name} копия", 40) if project else "")


def delete_project(ctx: AppContext, project_id: int) -> Step:
    from database import LastProjectError

    project = _get(ctx, project_id)
    if project is None:
        return Notice("warn", "Проект не найден")
    if len(ctx.projects()) <= 1:
        return Notice("warn", "Последний проект удалить нельзя")

    def pick(i: int) -> Step:
        if i != 1:
            return None
        try:
            result = ctx.db(lambda db: db.delete_project(project_id))
        except LastProjectError as exc:
            return Notice("warn", str(exc))
        return Notice("ok", f"Проект «{project.name}» удалён · аккаунтов вышло {result.accounts} · ссылок {result.links}")

    question = (f"Удалить «{project.name}»? {project.accounts_count} акк. выйдут из проекта, "
                f"{project.links_total} {plural(project.links_total, 'ссылка удалится', 'ссылки удалятся', 'ссылок удалятся')},"
                " журнал останется")
    return Confirm([Cell(question, "warn")], ["Нет", "Удалить"], pick)


# --------------------------------------------------------------------------- members


def member_quota_text(ctx: AppContext, project: Any, account: Any) -> str:
    """«в проекте 2/5 · всего за день 3/8»: the project's ceiling and the account's own daily limit."""
    from database import project_quota_today
    from modules.lifecycle_engine import AccountLifecycleEngine

    rows = ctx.query("SELECT daily_done, daily_reset_at FROM project_accounts WHERE project_id = ? AND account_id = ?",
                     (project.id, account.id))
    done = project_quota_today(rows[0][0], rows[0][1]) if rows else 0
    in_project = f"в проекте {done}/{project.daily_per_account}" if project.daily_per_account else f"в проекте {done}"
    view = AccountLifecycleEngine.summarize(account)
    if view.quota_limit:
        total = f"всего за день {view.quota_done or 0}/{view.quota_limit}"
    else:
        total = f"всего за день {view.quota_done or 0}" + (" (без лимита)" if view.quota_limit is None else "")
    return f"{in_project} · {total}"


def members_view(ctx: AppContext, project_id: int) -> View:
    """Every account with ▣ (in the project) / ▢: Enter or Space toggles it, X ticks several."""
    project = _get(ctx, project_id)
    if project is None:
        return View(rows=[Row("none", [Cell("Проект не найден", "warn")], selectable=False)])
    members = {int(aid) for (aid,) in ctx.query("SELECT account_id FROM project_accounts WHERE project_id = ?", (project_id,))}
    counts = {int(aid): int(n) for aid, n in ctx.query("SELECT account_id, COUNT(*) FROM project_accounts GROUP BY account_id")}
    accounts = ctx.db(lambda db: db.list_accounts())

    def toggle(account: Any) -> Callable[[], Notice]:
        def run() -> Notice:
            return change_members(ctx, project_id, [account.id], account.id not in members)

        return run

    rows = []
    for account in accounts:
        inside = account.id in members
        other = counts.get(account.id, 0) - (1 if inside else 0)
        rows.append(Row(
            f"a{account.id}",
            [Cell("▣" if inside else "▢", "ok" if inside else "dim", 3), Cell(account.name, "" if account.is_active else "dim", 20, grow=True),
             Cell("пауза" if not account.is_active else "", "dim", 7),
             Cell(f"{icon('project')}{other}" if other else "", "dim")],
            hint=(member_quota_text(ctx, project, account) + " · Enter — исключить") if inside else
            ("не в проекте" + (f" · ещё в проектах: {other}" if other else "") + " · Enter — включить"),
            enter=toggle(account), space=toggle(account),
        ))
    if not rows:
        rows.append(Row("empty", [Cell("аккаунтов нет · добавьте во вкладке «Аккаунты»", "dim")], selectable=False))

    def marks(ids: list[str]) -> list[Row]:
        chosen = [int(i[1:]) for i in ids if i.startswith("a")]
        return [
            Row("mk-add", [Cell(icon("add"), "", 3), Cell(f"включить в проект · {len(chosen)}")], hint="Включить отмеченные",
                enter=lambda: change_members(ctx, project_id, chosen, True)),
            Row("mk-remove", [Cell(icon("trash"), "", 3), Cell(f"исключить из проекта · {len(chosen)}")],
                hint="Исключить отмеченные", enter=lambda: change_members(ctx, project_id, chosen, False)),
        ]

    return View(rows=rows, title=[Cell(f"{icon('project')} {project.name} › аккаунты · {len(members)} из {len(accounts)}")],
                keys="Enter/Пробел — включить/исключить · X отметить", marks=marks)


def change_members(ctx: AppContext, project_id: int, account_ids: list[int], add: bool) -> Notice:
    if add:
        n = ctx.db(lambda db: db.add_project_accounts(project_id, account_ids))
        return Notice("ok", f"В проект добавлено: {n}")
    n = ctx.db(lambda db: db.remove_project_accounts(project_id, account_ids))
    return Notice("ok" if n else "info", f"Из проекта исключено: {n}")


# --------------------------------------------------------------------------- the switch of «Цель» / «Ответы»


def switch_row(ctx: AppContext) -> Row | None:
    """«▸ 📁 Крипта ‹ 1/3 ›»: ←→ changes the current project, Enter opens «Проекты». None while there is one."""
    projects = ctx.projects()
    current = ctx.project()
    if len(projects) < 2 or current is None:
        return None
    index = [p.id for p in projects].index(current.id)

    def pick(i: int) -> Notice:
        ctx.set_project(projects[i].id)
        return Notice("info", f"Проект «{projects[i].name}»")

    return Row(
        "project", [Cell(icon("project"), "", 3), Cell(current.name, "accent", 20, grow=True),
                    Cell(f"{index + 1}/{len(projects)}", "dim")],
        hint="Текущий проект · ←→ другой · Enter — «Проекты»", values=[p.name for p in projects], value_index=index,
        on_value=pick, enter=lambda: GoTab("Проекты"),
    )


def no_project_view() -> View:
    return View(rows=[Row("none", [Cell("Проектов нет: вкладка «Проекты» → + новый", "warn")], selectable=False)])
