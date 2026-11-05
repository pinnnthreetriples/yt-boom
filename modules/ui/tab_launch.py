"""Tab «Запуск»: start (dry / live) of the projects «в запуске», one row per project once there are several, threads,
how accounts share videos, browser window, today's counters, journal."""
from __future__ import annotations

import asyncio
import webbrowser
from datetime import datetime, timezone
from typing import Any

from modules.ui import shell
from modules.ui.app import AppContext
from modules.ui.icons import icon
from modules.ui.shell import Cell, Confirm, GoTab, Hotkey, Menu, Notice, Row, Run, Screen, Step, Tab, View, fit
from modules.ui.tab_projects import plan_text

_SPLIT = [("each", "каждый — весь список"), ("share", "делят список")]
_SPLIT_EXAMPLE = {
    "each": "3 видео, 2 аккаунта, лимит 2 → каждый пишет по 2 под каждым видео, всего 12",
    "share": "6 видео, 3 аккаунта, лимит 3 → каждый берёт по 2 видео, под каждым 3 ответа, всего 18",
}
_STATUS = {"success": ("sent", "ok", "отправлен"), "simulated": ("dry", "dim", "пробный"), "failed": ("failed", "err", "ошибка")}
_NOT_VISIBLE = "not_visible"  # a journal filter, not a status: replies whose visibility is not confirmed
_FILTERS = [(None, "все"), ("success", "отправленные"), ("simulated", "пробные"), ("failed", "ошибки"),
            (_NOT_VISIBLE, "не видны")]
_UNCONFIRMED = ("unseen", "unknown")
_PAGE = 10
_LOG_SELECT = (
    "SELECT l.id, l.created_at, l.status, l.mode, l.reply_text, l.error, l.target_author, l.video_url, l.video_id,"
    " l.video_title, l.target_text, l.project_id, p.name, a.name, l.visibility"
    " FROM activity_log l LEFT JOIN projects p ON p.id = l.project_id LEFT JOIN accounts a ON a.id = l.account_id"
)
# Where a problem of a project is fixed: (tab, its icon, words of the «Чтобы начать» row of a single project).
_FIX = {
    "нет аккаунтов": ("Проекты", "tab_projects", "добавить аккаунты в проект"),
    "нет аккаунтов в работе": ("Аккаунты", "tab_accounts", "включить аккаунты проекта в работу"),
    "нет видео": ("Цель", "tab_target", "тема поиска или ссылки на видео"),
    "нет шаблонов": ("Ответы", "tab_replies", "ключ DeepSeek или свои шаблоны"),
    "нет ключа DeepSeek": ("Ответы", "tab_replies", "ключ DeepSeek или свои шаблоны"),
}


def _active_count(ctx: AppContext) -> int:
    rows = ctx.query("SELECT COUNT(*) FROM accounts WHERE is_active = 1")
    return int(rows[0][0]) if rows else 0


def _posting_count(ctx: AppContext, project_ids: list[int]) -> int:
    """Accounts a run of these projects may post with: members in work, sign-in not failed (each account once; the
    stage limits may still hold some back)."""
    if not project_ids:
        return 0
    marks = ",".join("?" for _ in project_ids)
    rows = ctx.query(
        "SELECT COUNT(DISTINCT a.id) FROM accounts a JOIN project_accounts pa ON pa.account_id = a.id"
        f" WHERE pa.project_id IN ({marks}) AND a.is_active = 1 AND COALESCE(a.auth_status, '') <> 'failed'",
        tuple(project_ids),
    )
    return int(rows[0][0]) if rows else 0


def _without_project(ctx: AppContext) -> int:
    rows = ctx.query("SELECT COUNT(*) FROM accounts WHERE id NOT IN (SELECT account_id FROM project_accounts)")
    return int(rows[0][0]) if rows else 0


def _today(ctx: AppContext) -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _today_counts(ctx: AppContext) -> dict[str, int]:
    rows = ctx.query("SELECT status, COUNT(*) FROM activity_log WHERE created_at >= ? GROUP BY status", (_today(ctx),))
    return {str(status): int(n) for status, n in rows}


def _today_visibility(ctx: AppContext) -> tuple[int, int]:
    """Today's replies: (seen by anonymous visitors, not confirmed = unseen + unknown)."""
    rows = ctx.query("SELECT visibility, COUNT(*) FROM activity_log WHERE created_at >= ? AND visibility IS NOT NULL"
                     " GROUP BY visibility", (_today(ctx),))
    counts = {str(v): int(n) for v, n in rows}
    return counts.get("seen", 0), sum(counts.get(v, 0) for v in _UNCONFIRMED)


def _where(status: str | None, project: int | None) -> tuple[str, tuple[Any, ...]]:
    clauses, params = [], []
    if status == _NOT_VISIBLE:
        clauses.append("l.visibility IN ('unseen', 'unknown')")
    elif status:
        clauses.append("l.status = ?")
        params.append(status)
    if project is not None:
        clauses.append("l.project_id = ?")
        params.append(project)
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), tuple(params)


def _recent(ctx: AppContext, limit: int, status: str | None = None, offset: int = 0,
            project: int | None = None) -> list[tuple[Any, ...]]:
    where, params = _where(status, project)
    return ctx.query(f"{_LOG_SELECT}{where} ORDER BY l.id DESC LIMIT ? OFFSET ?", (*params, limit, offset))


def _time(value: Any, with_date: bool = False) -> str:
    text = str(value or "")[:16].replace("T", " ")
    return (text[8:10] + "." + text[5:7] + " " + text[11:16]) if with_date and len(text) >= 16 else text[11:16] or "—"


def _project_name(rec: tuple[Any, ...]) -> str:
    """The record's project: its name, «удалён» for a deleted one, «—» for a run without projects."""
    project_id, name = rec[11], rec[12]
    return str(name) if name else ("удалён" if project_id is not None else "—")


def _visibility_marker(status: Any, visibility: Any) -> Cell:
    """The mark of a published reply: green = seen by anonymous visitors, yellow = not confirmed, «—» = not checked;
    only a sent reply has one."""
    if status != "success":
        return Cell("", "", 3)
    if visibility == "seen":
        return Cell(icon("seen"), "ok", 3)
    if visibility in _UNCONFIRMED:
        return Cell(icon("unseen"), "warn", 3)
    return Cell("—", "dim", 3)


def _record_cells(rec: tuple[Any, ...], with_date: bool = False) -> list[Cell]:
    _, created, status, _, reply, error, *_ = rec
    name, style, _ = _STATUS.get(str(status), ("dry", "dim", str(status)))
    text = (error if status == "failed" else reply) or ""
    return [
        Cell(icon(name), "", 3),
        _visibility_marker(status, rec[14] if len(rec) > 14 else None),
        Cell(_time(created, with_date), "dim", 12 if with_date else 6),
        Cell(" ".join(str(text).split()), "err" if status == "failed" else "dim", 43, grow=True),
    ]


def build_tab(ctx: AppContext) -> Tab:
    def start_row(marked: list[Any], several: bool) -> Row:
        live = ctx.live

        def change(index: int) -> None:
            ctx.live = index == 1

        def enter() -> Step:
            if not marked:
                return Notice("warn", "Нет проектов в запуске: вкладка «Проекты»")
            if not ctx.live:
                return Run(lambda: run_target(ctx, dry_run=True), f"{icon('dry')} Пробный запуск · без публикации")
            n = _posting_count(ctx, [p.id for p in marked])
            if not n:
                return Notice("warn", "Нет аккаунтов в работе с удачным входом: вкладка «Аккаунты»")
            if several:
                question = f"{icon('live')} Публикуем: {len(marked)} {_projects_word(len(marked))}, до {n} акк.?"
            else:
                question = f"{icon('live')} Публикуем ответы · до {n} акк. (лимиты этапов учитываются)?"
            return Confirm([Cell(question, "err")], ["Нет", "Да"],
                           lambda i: Run(lambda: run_target(ctx, dry_run=False), f"{icon('live')} Боевой запуск") if i == 1 else None)

        hint = "Запуск · боевой: ответы публикуются" if live else "Запуск · пробный: без публикации"
        if several:
            hint += f" · проекты в запуске по очереди: {len(marked)}"
        return Row(
            "start",
            [Cell(icon("live" if live else "dry"), "", 3), Cell("БОЕВОЙ" if live else "пробный", "err" if live else "")],
            hint=hint, values=["пробный", "боевой"], value_index=int(live), on_value=change, enter=enter,
        )

    def setup_row(row_id: str, icon_name: str, text: str, hint: str, tab: str, project_id: int | None = None) -> Row:
        def go() -> Step:
            if project_id is not None:
                ctx.set_project(project_id)
            return GoTab(tab)

        return Row(row_id, [Cell(icon(icon_name), "", 3), Cell(text)], hint=hint + " · Enter — перейти", enter=go)

    def setup_rows(projects: list[Any]) -> list[Row]:
        """What is missing before a launch makes sense (by the projects «в запуске»), each row leads to its tab."""
        missing: list[Row] = []
        has_accounts = bool(ctx.query("SELECT 1 FROM accounts LIMIT 1"))
        if not has_accounts:
            missing.append(setup_row("setup-accounts", "tab_accounts", "добавить аккаунт", "Нужен хотя бы один аккаунт",
                                     "Аккаунты"))
        loose = _without_project(ctx)
        if loose:
            missing.append(setup_row("setup-loose", "project", f"аккаунты без проекта: {loose} — добавить в проект",
                                     "Аккаунт без проекта не работает", "Проекты"))
        marked = [p for p in projects if p.enabled]
        if projects and not marked:
            missing.append(setup_row("setup-none", "project", "нет проектов в запуске", "Отметьте проект «в запуске»",
                                     "Проекты"))
        seen: set[str] = set()
        for project in marked:
            for problem in ctx.problems(project):
                if problem == "нет аккаунтов" and (not has_accounts or (loose and len(projects) == 1)):
                    continue  # the rows above already say it
                tab, icon_name, words = _FIX.get(problem, ("Цель", "tab_target", problem))
                if len(projects) > 1:
                    words = f"{project.name}: {problem}"
                if words not in seen:
                    seen.add(words)
                    missing.append(setup_row(f"setup-{project.id}-{len(missing)}", icon_name, words,
                                             problem[0].upper() + problem[1:], tab, project.id))
        if missing:
            missing.insert(0, Row("setup-title", [Cell("Чтобы начать:", "dim")], selectable=False))
            missing.append(Row("setup-gap", [Cell("")], selectable=False))
        return missing

    def project_row(project: Any, sent: int) -> Row:
        def set_enabled(i: int) -> Notice | None:
            try:
                ctx.update_project(project.id, enabled=i == 0)
            except ValueError as exc:
                return Notice("warn", str(exc))
            return None

        problems = ctx.problems(project)
        return Row(
            f"p{project.id}",
            [Cell(icon("project"), "", 3), Cell(project.name, "warn" if problems and project.enabled else "", 18),
             Cell("▶ в запуске" if project.enabled else "‖ пропуск", "" if project.enabled else "dim", 13),
             Cell(f"{icon('tab_accounts')} {project.accounts_count}", "", 6), Cell(f"{icon('topic')} {len(project.search_topics)}", "", 6),
             Cell(f"{icon('links')} {project.links_waiting}", "", 6), Cell(f"{icon('sent')} {sent}", "dim")],
            hint=(", ".join(problems) + " · " if problems else "") + "←→ в запуске / пропуск · Пробел — план · Enter — действия",
            values=["в запуске", "пропуск"], value_index=0 if project.enabled else 1, on_value=set_enabled,
            detail=lambda: [[Cell(fit(plan_text(project), max(40, shell.WIDTH - 8)))]],
            enter=lambda: project_menu(project),
        )

    def project_menu(project: Any) -> Menu:
        def run_one() -> Step:
            if not ctx.live:
                return Run(lambda: run_target(ctx, dry_run=True, project_ids=[project.id]),
                           f"{icon('dry')} Пробный запуск · {project.name}")
            n = _posting_count(ctx, [project.id])
            if not n:
                return Notice("warn", f"«{project.name}»: нет аккаунтов в работе с удачным входом")
            return Confirm([Cell(f"{icon('live')} Публикуем: «{project.name}», до {n} акк.?", "err")], ["Нет", "Да"],
                           lambda i: Run(lambda: run_target(ctx, dry_run=False, project_ids=[project.id]),
                                         f"{icon('live')} Боевой запуск · {project.name}") if i == 1 else None)

        def open_target() -> Step:
            ctx.set_project(project.id)
            return GoTab("Цель", Notice("info", f"Проект «{project.name}»"))

        return Menu([
            Row("pl-run", [Cell(icon("live" if ctx.live else "dry"), "", 3), Cell("запустить только этот")],
                hint=("Боевой" if ctx.live else "Пробный") + " запуск одного проекта", enter=run_one),
            Row("pl-target", [Cell(icon("tab_target"), "", 3), Cell("открыть в «Цели»")],
                hint="Сделать текущим и открыть «Цель»", enter=open_target),
        ])

    def today_rows(projects: list[Any]) -> tuple[list[Row], list[list[Cell]]]:
        """Today's counters: a line under the rows; with several projects a row whose Space splits it by project."""
        today = _today_counts(ctx)
        summary = [Cell(f"{icon('sent')} {today.get('success', 0)}", "", 7), Cell(f"{icon('dry')} {today.get('simulated', 0)}", "", 7),
                   Cell(f"{icon('failed')} {today.get('failed', 0)}", "err" if today.get("failed") else "", 7)]
        seen_n, unconfirmed_n = _today_visibility(ctx)
        summary += [Cell(f"{icon('seen')} {seen_n}", "ok" if seen_n else "", 7),
                    Cell(f"{icon('unseen')} {unconfirmed_n}", "warn" if unconfirmed_n else "", 7)]
        waiting = sum(p.links_waiting for p in projects if p.enabled)
        if waiting:
            summary.append(Cell(f"{icon('videos')} {waiting}", "", 7))
        if len(projects) < 2:
            return [], [[Cell("сегодня", "dim", 9), *summary]]
        row = Row("today", [Cell("сегодня", "dim", 9), *summary], hint="Сегодня, все проекты · Пробел — по проектам",
                  detail=lambda: today_by_project(ctx, projects))
        return [row], []

    def build() -> View:
        cfg = ctx.cfg()
        projects = ctx.projects()
        several = len(projects) > 1
        marked = [p for p in projects if p.enabled]
        active = _active_count(ctx)
        threads = int(cfg.limits.max_concurrent_sessions)
        rows = [*setup_rows(projects), start_row(marked, several)]
        if several:
            sent = _today_by_project(ctx)
            rows += [project_row(p, sent.get(p.id, {}).get("success", 0)) for p in projects]

        if active >= 2:
            counts = [str(n) for n in range(1, active + 1)]
            rows.append(Row(
                "threads", [Cell(icon("threads"), "", 3), Cell(str(min(threads, active)))],
                hint="Браузеров одновременно", values=counts, value_index=min(threads, active) - 1,
                on_value=lambda i: ctx.update({"limits.max_concurrent_sessions": i + 1}),
            ))
            mode = cfg.limits.account_mode
            index = next((i for i, (v, _) in enumerate(_SPLIT) if v == mode), 0)
            rows.append(Row(
                "split", [Cell(icon("videos"), "", 3), Cell(_SPLIT[index][1])],
                hint="Как аккаунты делят видео · Пробел — пример", values=[label for _, label in _SPLIT],
                value_index=index, on_value=lambda i: ctx.update({"limits.account_mode": _SPLIT[i][0]}),
                detail=lambda: [[Cell(_SPLIT_EXAMPLE[ctx.cfg().limits.account_mode])]],
            ))

        headless = bool(getattr(cfg.browser, "headless", False))
        rows.append(Row(
            "window", [Cell(icon("window_off" if headless else "window_on"), "", 3), Cell("скрыто" if headless else "показывать", "dim")],
            hint="Окно браузера скрыто" if headless else "Окно браузера видно", values=["показывать", "скрыть"],
            value_index=int(headless), on_value=lambda i: ctx.update({"browser.headless": i == 1}),
        ))

        check_on = bool(cfg.limits.visibility_check_enabled)
        delay = float(cfg.limits.visibility_check_delay_seconds)
        check_hint = f"После ответа бот через ~{delay:.0f} с смотрит, виден ли он другим зрителям"
        if check_on and delay <= 0:
            check_hint = "Проверка выключена задержкой limits.visibility_check_delay_seconds = 0"
        rows.append(Row(
            "visibility", [Cell(icon("seen"), "", 3), Cell("Проверка видимости:", "dim"),
                           Cell("включена" if check_on else "выключена", "ok" if check_on else "dim")],
            hint=check_hint, values=["включена", "выключена"], value_index=0 if check_on else 1,
            on_value=lambda i: ctx.update({"limits.visibility_check_enabled": i == 0}),
        ))

        total = ctx.query("SELECT COUNT(*) FROM activity_log")
        rows.append(Row(
            "journal", [Cell(icon("journal"), "", 3), Cell("журнал"), Cell(str(total[0][0] if total else 0), "dim")],
            hint="Журнал ответов", enter=lambda: open_journal(ctx),
        ))
        today, bottom = today_rows(projects)
        rows += today
        bottom += [_record_cells(rec) for rec in _recent(ctx, 3)]
        rows.append(Row("gap", [Cell("")], selectable=False))
        return View(rows=rows, bottom=bottom, keys="Enter старт · ←→ режим")

    return Tab("Запуск", icon("tab_launch"), build)


def _projects_word(n: int) -> str:
    from modules.ui.tab_projects import plural

    return plural(n, "проект", "проекта", "проектов")


def _today_by_project(ctx: AppContext) -> dict[int | None, dict[str, int]]:
    out: dict[int | None, dict[str, int]] = {}
    rows = ctx.query("SELECT project_id, status, COUNT(*) FROM activity_log WHERE created_at >= ? GROUP BY project_id, status",
                     (_today(ctx),))
    for project_id, status, n in rows:
        out.setdefault(project_id, {})[str(status)] = int(n)
    return out


def today_by_project(ctx: AppContext, projects: list[Any]) -> list[list[Cell]]:
    """Space on «сегодня»: the same counters for every project (and the runs without one)."""
    counts = _today_by_project(ctx)
    names = {p.id: p.name for p in projects}
    lines = []
    for project_id, by_status in sorted(counts.items(), key=lambda item: (item[0] is None, item[0] or 0)):
        name = names.get(project_id) if project_id is not None else "без проекта"
        lines.append([Cell(fit(name or "удалён", 16), "", 17), Cell(f"{icon('sent')} {by_status.get('success', 0)}", "", 7),
                      Cell(f"{icon('dry')} {by_status.get('simulated', 0)}", "", 7),
                      Cell(f"{icon('failed')} {by_status.get('failed', 0)}", "", 7)])
    return lines or [[Cell("сегодня ответов нет")]]


# --------------------------------------------------------------------------- run


def run_target(ctx: AppContext, dry_run: bool, project_ids: list[int] | None = None) -> Notice:
    """Run the projects «в запуске» one after another (or only `project_ids`) with their members; the terminal shows
    the run's own output. The notice is short: per project what happened, details in the log."""
    from youtube_bot import run_projects

    name = "Пробный запуск" if dry_run else "Боевой запуск"
    progress: dict[str, Any] = {}
    try:
        summary = asyncio.run(run_projects(str(ctx.config_path), dry_run, project_ids, progress=progress))
    except KeyboardInterrupt:
        where = progress.get("project")
        return Notice("warn", f"{name} прерван" + (f" на проекте «{where}»" if where else ""))
    except Exception as exc:
        return Notice("err", fit(f"{name} остановлен: {exc}", 70))
    return summary_notice(ctx, name, summary)


def _skip_words(ctx: AppContext, result: Any) -> str:
    from modules.youtube_collector import empty_search_hint

    if result.skipped == "нет аккаунтов, которые могут отвечать сейчас":
        return "Ни один аккаунт сейчас не может отвечать: вход, «В работе» и этапы во вкладке «Аккаунты»"
    if result.skipped == "видео не найдено":
        project = next((p for p in ctx.projects() if p.name == result.project), None)
        hint = empty_search_hint(project.min_views, project.max_views, project.max_age_hours) if project else "другая тема"
        return f"Видео не найдено · {hint}"
    if result.skipped == "нет аккаунтов":
        return "В проекте нет аккаунтов: вкладка «Проекты» → аккаунты"
    if result.skipped == "нет шаблонов":
        return "Нет шаблонов: вкладка «Ответы»"
    return f"Нет видео ({result.skipped}): задайте темы или вставьте ссылки во вкладке «Цель»"


def summary_notice(ctx: AppContext, name: str, summary: Any) -> Notice:
    """One line for the whole launch: one project as before, several as «Крипта: 5 · Фитнес: пропущен (…)»."""
    cols = 160  # the bottom line cuts it to the window itself
    if summary.stopped_at:
        return Notice("err", fit(f"{name} остановлен на проекте «{summary.stopped_at}»: {summary.error}", cols))
    results = summary.results
    if not results:
        return Notice("warn", "Нет проектов в запуске: вкладка «Проекты»")
    if len(results) == 1:
        res = results[0]
        if res.skipped:
            return Notice("warn", fit(_skip_words(ctx, res), cols))
        return Notice("ok", f"{name} завершён · видео: {res.found} · ответов: {res.replies}")
    parts = [f"{r.project}: пропущен ({r.skipped})" if r.skipped else f"{r.project}: {r.replies}" for r in results]
    kind = "ok" if any(not r.skipped for r in results) else "warn"
    return Notice(kind, fit(f"{name} завершён · ответов: {sum(r.replies for r in results)} · " + " · ".join(parts), cols))


# --------------------------------------------------------------------------- journal


def open_journal(ctx: AppContext) -> Screen:
    state: dict[str, Any] = {"filter": 0, "page": 0, "project": 0}  # kept while the journal is open
    return Screen(lambda: journal_view(ctx, state))


def journal_view(ctx: AppContext, state: dict[str, Any]) -> View:
    projects = ctx.projects()
    several = len(projects) > 1
    choices: list[tuple[int | None, str]] = [(None, "все")] + [(p.id, p.name) for p in projects]
    state["project"] = min(state["project"], len(choices) - 1)
    project = choices[state["project"]][0] if several else None
    status = _FILTERS[state["filter"]][0]
    where, params = _where(status, project)
    total = ctx.query(f"SELECT COUNT(*) FROM activity_log l{where}", params)
    total_n = int(total[0][0]) if total else 0
    pages = max(1, -(-total_n // _PAGE))
    state["page"] = min(state["page"], pages - 1)

    def set_filter(i: int) -> None:
        state["filter"], state["page"] = i, 0

    def set_project(i: int) -> None:
        state["project"], state["page"] = i, 0

    count_where, count_params = _where(None, project)
    counts = {str(s): int(n) for s, n in ctx.query(f"SELECT l.status, COUNT(*) FROM activity_log l{count_where} GROUP BY l.status",
                                                   count_params)}
    seen_n, unconfirmed_n = 0, 0
    for mark, n in ctx.query(f"SELECT l.visibility, COUNT(*) FROM activity_log l{count_where} GROUP BY l.visibility",
                             count_params):
        if mark == "seen":
            seen_n += int(n)
        elif mark in _UNCONFIRMED:
            unconfirmed_n += int(n)
    rows = []
    if several:
        rows.append(Row("project", [Cell("проект", "dim", 10), Cell(choices[state["project"]][1])], hint="Журнал проекта",
                        values=[label for _, label in choices], value_index=state["project"], on_value=set_project))
    rows.append(Row(
        "filter", [Cell("показать", "dim", 10), Cell(_FILTERS[state["filter"]][1])],
        hint="Фильтр журнала", values=[label for _, label in _FILTERS], value_index=state["filter"], on_value=set_filter,
    ))
    for rec in _recent(ctx, _PAGE, status, state["page"] * _PAGE, project):
        who = [Cell(fit(_project_name(rec), 11), "", 12), Cell(fit(str(rec[13] or "—"), 11), "", 12)] if several \
            else [Cell(str(rec[6] or "—"), "", 14)]
        rows.append(Row(f"r{rec[0]}", _record_cells(rec, with_date=True)[:3] + who + [_record_cells(rec)[3]],
                        hint=_STATUS.get(str(rec[2]), ("", "", str(rec[2])))[2] + " · Enter — подробно",
                        enter=lambda rid=rec[0]: Screen(lambda: record_view(ctx, rid))))
    if total_n == 0:
        rows.append(Row("empty", [Cell("записей нет", "dim")], selectable=False))

    def page(step: int) -> None:
        state["page"] = max(0, min(pages - 1, state["page"] + step))

    def clear() -> Confirm:
        def pick(i: int) -> Step:
            if i != 1:
                return None
            removed = ctx.db(lambda db: db.clear_activity_log())
            state["page"] = 0
            return Notice("ok", f"Журнал очищен · удалено {removed}")

        return Confirm([Cell("Очистить весь журнал?", "warn")], ["Нет", "Очистить"], pick)

    top = [[Cell(f"{icon('sent')} {counts.get('success', 0)}", "", 8), Cell(f"{icon('dry')} {counts.get('simulated', 0)}", "", 8),
            Cell(f"{icon('failed')} {counts.get('failed', 0)}", "err" if counts.get("failed") else "", 8),
            Cell(f"{icon('seen')} {seen_n}", "ok" if seen_n else "", 8),
            Cell(f"{icon('unseen')} {unconfirmed_n}", "warn" if unconfirmed_n else "", 8),
            Cell(f"стр. {state['page'] + 1}/{pages}", "dim")]]
    return View(
        rows=rows, top=top, title=[Cell(f"{icon('journal')} журнал")],
        keys="←→ фильтр · N/P стр. · C очистить",
        hotkeys=[Hotkey("N", lambda _r: page(1)), Hotkey("P", lambda _r: page(-1)), Hotkey("C", lambda _r: clear())],
    )


_VISIBILITY_WORDS = {
    "seen": ("seen", "ok", "видна всем зрителям", ""),
    "unseen": ("unseen", "warn", "не найдена в анонимной выдаче", "возможно, скрыта YouTube: виден только автору"),
    "unknown": ("unseen", "warn", "не удалось проверить", "YouTube не отдал список комментариев или сеть недоступна"),
}


def _visibility_rows(status: str, visibility: Any) -> list[Row]:
    """The record's «видимость» lines: what the anonymous check found, or why there was no check."""
    if visibility in _VISIBILITY_WORDS:
        icon_name, style, words, more = _VISIBILITY_WORDS[visibility]
    else:
        icon_name, style, more = "clock", "dim", ""
        words = {"simulated": "не проверялась (пробный режим: ничего не опубликовано)",
                 "failed": "не проверялась (ответ не отправлен)"}.get(status, "не проверялась")
    rows = [Row("visibility", [Cell(icon(icon_name), "", 3), Cell("видимость:", "dim"), Cell(fit(words, 56), style)],
                selectable=False)]
    if more:
        rows.append(Row("visibility-more", [Cell("", "", 3), Cell(fit(more, 60), "dim")], selectable=False))
    return rows


def record_view(ctx: AppContext, record_id: int) -> View:
    rows = ctx.query(
        "SELECT l.status, l.created_at, l.target_author, l.target_text, l.reply_text, l.error, l.video_url, l.video_id,"
        " l.video_title, l.duration_sec, l.mode, l.project_id, p.name, a.name, l.visibility"
        " FROM activity_log l LEFT JOIN projects p ON p.id = l.project_id LEFT JOIN accounts a ON a.id = l.account_id"
        " WHERE l.id = ?", (record_id,),
    )
    if not rows:
        return View(rows=[Row("none", [Cell("Запись не найдена", "warn")], selectable=False)])
    (status, created, author, target, reply, error, url, video_id, title, duration, mode, project_id, project, account,
     visibility) = rows[0]
    name, style, word = _STATUS.get(str(status), ("dry", "dim", str(status)))
    link = url or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
    project_text = project or ("удалён" if project_id is not None else "—")
    lines = [
        Row("head", [Cell(icon(name), "", 3), Cell(f"{word} · {_time(created, True)} · {mode or '—'} · {float(duration or 0):.1f} с", style)], selectable=False),
        Row("who", [Cell(icon("project"), "", 3), Cell(f"проект {project_text} · аккаунт {account or '—'}", "dim")], selectable=False),
        *_visibility_rows(str(status), visibility),
        Row("gap", [Cell("")], selectable=False),
        Row("target", [Cell(icon("reply_to"), "", 3), Cell(fit(f"{author or '—'}: «{' '.join(str(target or '—').split())}»", 70), "dim")], selectable=False),
        Row("reply", [Cell(icon("templates"), "", 3), Cell(fit(f"«{' '.join(str(reply or '—').split())}»", 70))], selectable=False),
        Row("video", [Cell(icon("videos"), "", 3), Cell(fit(f"{title or '—'} · {link or '—'}", 70), "dim")], selectable=False),
    ]
    if error:
        lines.append(Row("error", [Cell(icon("failed"), "", 3), Cell(fit(str(error), 70), "err")], selectable=False))

    def open_video(_row: Any) -> Notice:
        if link:
            webbrowser.open(link)
            return Notice("ok", "Видео открыто в браузере")
        return Notice("warn", "У записи нет ссылки на видео")

    return View(rows=lines, title=[Cell(f"{icon('journal')} журнал › запись #{record_id}")], keys="O открыть видео",
                hotkeys=[Hotkey("O", open_video)])
