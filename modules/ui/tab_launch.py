"""Tab «Запуск»: start (dry / live), threads, how accounts share videos, browser window, today's counters, journal."""
from __future__ import annotations

import asyncio
import webbrowser
from datetime import datetime, timezone
from typing import Any

from modules.ui.app import AppContext
from modules.ui.icons import icon
from modules.ui.shell import Cell, Confirm, GoTab, Hotkey, Notice, Row, Run, Screen, Tab, View, fit

_SPLIT = [("each", "каждый — весь список"), ("share", "делят список")]
_SPLIT_EXAMPLE = {
    "each": "3 видео, 2 аккаунта, лимит 2 → каждый пишет по 2 под каждым видео, всего 12",
    "share": "3 видео, 2 аккаунта, лимит 2 → под каждым видео 2 ответа на всех, всего 6",
}
_STATUS = {"success": ("sent", "ok", "отправлен"), "simulated": ("dry", "dim", "пробный"), "failed": ("failed", "err", "ошибка")}
_FILTERS = [(None, "все"), ("success", "отправленные"), ("simulated", "пробные"), ("failed", "ошибки")]
_PAGE = 10


def _active_count(ctx: AppContext) -> int:
    rows = ctx.query("SELECT COUNT(*) FROM accounts WHERE is_active = 1")
    return int(rows[0][0]) if rows else 0


def _posting_count(ctx: AppContext) -> int:
    """Accounts a run may post with: in work and sign-in not failed (the stage limits may still hold some back)."""
    rows = ctx.query("SELECT COUNT(*) FROM accounts WHERE is_active = 1 AND COALESCE(auth_status, '') <> 'failed'")
    return int(rows[0][0]) if rows else 0


def _today_counts(ctx: AppContext) -> dict[str, int]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = ctx.query("SELECT status, COUNT(*) FROM activity_log WHERE created_at >= ? GROUP BY status", (today,))
    return {str(status): int(n) for status, n in rows}


def _recent(ctx: AppContext, limit: int, status: str | None = None, offset: int = 0) -> list[tuple[Any, ...]]:
    where, params = ("WHERE status = ?", (status,)) if status else ("", ())
    return ctx.query(
        "SELECT id, created_at, status, mode, reply_text, error, target_author, video_url, video_id, video_title,"
        f" target_text FROM activity_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )


def _time(value: Any, with_date: bool = False) -> str:
    text = str(value or "")[:16].replace("T", " ")
    return (text[8:10] + "." + text[5:7] + " " + text[11:16]) if with_date and len(text) >= 16 else text[11:16] or "—"


def _record_cells(rec: tuple[Any, ...], with_date: bool = False) -> list[Cell]:
    _, created, status, _, reply, error, *_ = rec
    name, style, _ = _STATUS.get(str(status), ("dry", "dim", str(status)))
    text = (error if status == "failed" else reply) or ""
    return [
        Cell(icon(name), "", 3),
        Cell(_time(created, with_date), "dim", 12 if with_date else 6),
        Cell(" ".join(str(text).split()), "err" if status == "failed" else "dim", 46, grow=True),
    ]


def build_tab(ctx: AppContext) -> Tab:
    def start_row() -> Row:
        live = ctx.live

        def change(index: int) -> None:
            ctx.live = index == 1

        def enter():
            if not ctx.live:
                return Run(lambda: run_target(ctx, dry_run=True), f"{icon('dry')} Пробный запуск · без публикации")
            n = _posting_count(ctx)
            if not n:
                return Notice("warn", "Нет аккаунтов в работе с удачным входом: вкладка «Аккаунты»")
            return Confirm(
                [Cell(f"{icon('live')} Публикуем ответы · до {n} акк. (лимиты этапов учитываются)?", "err")], ["Нет", "Да"],
                lambda i: Run(lambda: run_target(ctx, dry_run=False), f"{icon('live')} Боевой запуск") if i == 1 else None,
            )

        return Row(
            "start",
            [Cell(icon("live" if live else "dry"), "", 3), Cell("БОЕВОЙ" if live else "пробный", "err" if live else "")],
            hint="Запуск · боевой: ответы публикуются" if live else "Запуск · пробный: без публикации",
            values=["пробный", "боевой"], value_index=int(live), on_value=change, enter=enter,
        )

    def setup_rows(cfg: Any) -> list[Row]:
        """First run: what is missing before a launch makes sense, each row leads to its tab."""
        from modules.wizard import _target_plan

        missing: list[Row] = []
        if not ctx.query("SELECT 1 FROM accounts LIMIT 1"):
            missing.append(Row("setup-accounts", [Cell(icon("tab_accounts"), "", 3), Cell("добавить аккаунт")],
                               hint="Нужен хотя бы один аккаунт · Enter — перейти", enter=lambda: GoTab(1)))
        if _target_plan(cfg)[0] == "none":
            missing.append(Row("setup-target", [Cell(icon("tab_target"), "", 3), Cell("тема поиска или ссылки на видео")],
                               hint="Где искать видео · Enter — перейти", enter=lambda: GoTab(3)))
        ai = getattr(cfg.processor, "reply_mode", "ai") == "ai"
        if (ai and not cfg.deepseek.api_key) or (not ai and not getattr(cfg.processor, "custom_replies", [])):
            missing.append(Row("setup-replies", [Cell(icon("tab_replies"), "", 3), Cell("ключ DeepSeek или свои шаблоны")],
                               hint="Чем отвечать · Enter — перейти", enter=lambda: GoTab(4)))
        if missing:
            missing.insert(0, Row("setup-title", [Cell("Чтобы начать:", "dim")], selectable=False))
            missing.append(Row("setup-gap", [Cell("")], selectable=False))
        return missing

    def build() -> View:
        cfg = ctx.cfg()
        active = _active_count(ctx)
        threads = int(cfg.limits.max_concurrent_sessions)
        rows = [*setup_rows(cfg), start_row()]

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

        total = ctx.query("SELECT COUNT(*) FROM activity_log")
        rows.append(Row(
            "journal", [Cell(icon("journal"), "", 3), Cell("журнал"), Cell(str(total[0][0] if total else 0), "dim")],
            hint="Журнал ответов", enter=lambda: open_journal(ctx),
        ))

        today = _today_counts(ctx)
        summary = [Cell(f"{icon('sent')} {today.get('success', 0)}", "", 7), Cell(f"{icon('dry')} {today.get('simulated', 0)}", "", 7),
                   Cell(f"{icon('failed')} {today.get('failed', 0)}", "err" if today.get("failed") else "", 7)]
        from modules.wizard import _target_plan

        kind, waiting, _ = _target_plan(cfg)
        if kind == "pool":
            summary.append(Cell(f"{icon('videos')} {waiting}", "", 7))
        bottom = [[Cell("сегодня", "dim", 9), *summary]]
        bottom += [_record_cells(rec) for rec in _recent(ctx, 3)]
        rows.append(Row("gap", [Cell("")], selectable=False))
        return View(rows=rows, bottom=bottom, keys="Enter старт · ←→ режим")

    return Tab("Запуск", icon("tab_launch"), build)


# --------------------------------------------------------------------------- run


def run_target(ctx: AppContext, dry_run: bool) -> Notice:
    """Run on the current target (auto-search, else the video list) with the accounts in work;
    the terminal shows the run's own output."""
    from modules.wizard import _target_plan

    cfg = ctx.cfg()
    kind, _, query = _target_plan(cfg)
    if kind == "none":
        return Notice("warn", "Нет видео: задайте тему поиска или вставьте ссылки во вкладке «Цель»")
    name = "Пробный запуск" if dry_run else "Боевой запуск"
    limit = cfg.limits.max_iterations_per_session or 2
    try:
        if kind == "pool":
            from youtube_bot import run_video_pool_cli

            done = asyncio.run(run_video_pool_cli(config_path=str(ctx.config_path), dry_run=dry_run))
            return Notice("ok", f"{name} завершён · ответов: {done}")
        from modules.youtube_collector import empty_search_hint
        from youtube_bot import run_search_cli

        col = cfg.collector
        res = asyncio.run(run_search_cli(
            query, str(ctx.config_path),
            min_views=col.min_views or 0, max_views=col.max_views, max_age_hours=col.max_age_hours,
            max_videos=col.max_videos, comments_per_video=limit, dry_run=dry_run,
        ))
        if not res.accounts:
            return Notice("warn", "Ни один аккаунт сейчас не может отвечать: вход, «В работе» и этапы во вкладке «Аккаунты»")
        if not res.found:
            return Notice("warn", fit(f"Видео по теме не найдено · {empty_search_hint(col.min_views or 0, col.max_views, col.max_age_hours)}", 110))
        return Notice("ok", f"{name} завершён · видео: {res.found} · ответов: {res.replies}")
    except Exception as exc:
        return Notice("err", fit(f"{name} остановлен: {exc}", 70))


# --------------------------------------------------------------------------- journal


def open_journal(ctx: AppContext) -> Screen:
    state: dict[str, Any] = {"filter": 0, "page": 0}  # kept while the journal is open
    return Screen(lambda: journal_view(ctx, state))


def journal_view(ctx: AppContext, state: dict[str, Any]) -> View:
    status = _FILTERS[state["filter"]][0]
    where, params = ("WHERE status = ?", (status,)) if status else ("", ())
    total = ctx.query(f"SELECT COUNT(*) FROM activity_log {where}", params)
    total_n = int(total[0][0]) if total else 0
    pages = max(1, -(-total_n // _PAGE))
    state["page"] = min(state["page"], pages - 1)

    def set_filter(i: int) -> None:
        state["filter"], state["page"] = i, 0

    counts = {str(s): int(n) for s, n in ctx.query("SELECT status, COUNT(*) FROM activity_log GROUP BY status")}
    rows = [Row(
        "filter", [Cell("показать", "dim", 10), Cell(_FILTERS[state["filter"]][1])],
        hint="Фильтр журнала", values=[label for _, label in _FILTERS], value_index=state["filter"], on_value=set_filter,
    )]
    for rec in _recent(ctx, _PAGE, status, state["page"] * _PAGE):
        rows.append(Row(f"r{rec[0]}", _record_cells(rec, with_date=True)[:2] + [Cell(str(rec[6] or "—"), "", 14), _record_cells(rec)[2]],
                        hint=_STATUS.get(str(rec[2]), ("", "", str(rec[2])))[2] + " · Enter — подробно",
                        enter=lambda rid=rec[0]: Screen(lambda: record_view(ctx, rid))))
    if total_n == 0:
        rows.append(Row("empty", [Cell("записей нет", "dim")], selectable=False))

    def page(step: int):
        state["page"] = max(0, min(pages - 1, state["page"] + step))

    def clear():
        def pick(i: int):
            if i != 1:
                return None
            removed = ctx.db(lambda db: db.clear_activity_log())
            state["page"] = 0
            return Notice("ok", f"Журнал очищен · удалено {removed}")

        return Confirm([Cell("Очистить весь журнал?", "warn")], ["Нет", "Очистить"], pick)

    top = [[Cell(f"{icon('sent')} {counts.get('success', 0)}", "", 8), Cell(f"{icon('dry')} {counts.get('simulated', 0)}", "", 8),
            Cell(f"{icon('failed')} {counts.get('failed', 0)}", "err" if counts.get("failed") else "", 8),
            Cell(f"стр. {state['page'] + 1}/{pages}", "dim")]]
    return View(
        rows=rows, top=top, title=[Cell(f"{icon('journal')} журнал")],
        keys="←→ фильтр · N/P стр. · C очистить",
        hotkeys=[Hotkey("N", lambda _r: page(1)), Hotkey("P", lambda _r: page(-1)), Hotkey("C", lambda _r: clear())],
    )


def record_view(ctx: AppContext, record_id: int) -> View:
    rows = ctx.query(
        "SELECT status, created_at, target_author, target_text, reply_text, error, video_url, video_id, video_title,"
        " duration_sec, mode FROM activity_log WHERE id = ?", (record_id,),
    )
    if not rows:
        return View(rows=[Row("none", [Cell("Запись не найдена", "warn")], selectable=False)])
    status, created, author, target, reply, error, url, video_id, title, duration, mode = rows[0]
    name, style, word = _STATUS.get(str(status), ("dry", "dim", str(status)))
    link = url or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
    lines = [
        Row("head", [Cell(icon(name), "", 3), Cell(f"{word} · {_time(created, True)} · {mode or '—'} · {float(duration or 0):.1f} с", style)], selectable=False),
        Row("gap", [Cell("")], selectable=False),
        Row("target", [Cell(icon("reply_to"), "", 3), Cell(fit(f"{author or '—'}: «{' '.join(str(target or '—').split())}»", 70), "dim")], selectable=False),
        Row("reply", [Cell(icon("templates"), "", 3), Cell(fit(f"«{' '.join(str(reply or '—').split())}»", 70))], selectable=False),
        Row("video", [Cell(icon("videos"), "", 3), Cell(fit(f"{title or '—'} · {link or '—'}", 70), "dim")], selectable=False),
    ]
    if error:
        lines.append(Row("error", [Cell(icon("failed"), "", 3), Cell(fit(str(error), 70), "err")], selectable=False))

    def open_video(_row):
        if link:
            webbrowser.open(link)
            return Notice("ok", "Видео открыто в браузере")
        return Notice("warn", "У записи нет ссылки на видео")

    return View(rows=lines, title=[Cell(f"{icon('journal')} журнал › запись #{record_id}")], keys="O открыть видео",
                hotkeys=[Hotkey("O", open_video)])
