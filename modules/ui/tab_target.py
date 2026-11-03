"""Tab «Цель»: which videos (search topic or own links), whom to answer, how many replies, a preview."""
from __future__ import annotations

import asyncio
import re
from typing import Any, Callable

from modules.ui.app import AppContext
from modules.ui.icons import icon
from modules.ui.shell import Cell, Confirm, Field, Menu, Notice, Row, Run, Screen, Tab, View, fit

_SOURCES = ["поиск по теме", "свои ссылки"]
_LIMITS = [str(n) for n in range(1, 51)]
_PENDING_SQL = "status IN ('pending', 'in_progress') AND comments_done < comments_target"


def _db(ctx: AppContext, fn: Callable[[Any], Any]) -> Any:
    return ctx.db(fn)


def views_label(low: int, high: int | None) -> str:
    """'10 тыс – 500 тыс', 'от 10 тыс', 'до 1 млн' or 'любые'."""
    def num(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " млн"
        if n >= 1000:
            return f"{n / 1000:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " тыс"
        return str(n)

    if low and high:
        return f"{num(low)} – {num(high)}"
    if low:
        return f"от {num(low)}"
    if high:
        return f"до {num(high)}"
    return "любые"


def parse_views(text: str) -> tuple[int, int | None]:
    """«10к 500к», «10000-500000», «10 тыс», «0 1млн» -> (min, max or None); ValueError on nonsense."""
    numbers = []
    for token in re.findall(r"\d+(?:[.,]\d+)?\s*(?:тыс|млн|к|k|м|m)?", text.lower()):
        match = re.match(r"(\d+(?:[.,]\d+)?)\s*(\D*)", token)
        value, suffix = float(match.group(1).replace(",", ".")), match.group(2).strip()
        value *= 1_000_000 if suffix in {"млн", "м", "m"} else 1000 if suffix in {"тыс", "к", "k"} else 1
        numbers.append(int(value))
    rest = re.sub(r"\d+(?:[.,]\d+)?\s*(?:тыс|млн|к|k|м|m)?|[\s\-–—.]|от|до", "", text.lower())
    if rest or not numbers or len(numbers) > 2:
        raise ValueError(text)
    low = numbers[0]
    high = numbers[1] if len(numbers) == 2 and numbers[1] else None
    if high is not None and high < low:
        raise ValueError(text)
    return low, high


def _pool_counts(ctx: AppContext) -> tuple[int, int]:
    """(videos waiting, videos done) in the video list."""
    waiting = ctx.query(f"SELECT COUNT(*) FROM video_pool WHERE {_PENDING_SQL}")
    total = ctx.query("SELECT COUNT(*) FROM video_pool")
    w = int(waiting[0][0]) if waiting else 0
    return w, (int(total[0][0]) if total else 0) - w


def _fetch_preview(ctx: AppContext, url: str, limit: int) -> list[Any]:
    from database import Database
    from modules.youtube_collector import YouTubeCollector

    cfg = ctx.cfg()

    async def fetch() -> list[Any]:
        async with Database(cfg.app.db_path) as db:
            proxy = await db.search_proxy_url()
        async with YouTubeCollector(cfg.collector, proxy) as collector:
            return await collector.fetch_video_sub_comments(url, max_sub_comments=limit)

    return asyncio.run(fetch())


def build_tab(ctx: AppContext) -> Tab:
    from modules.wizard import _TARGET_AGES, _TARGET_MODE_EXAMPLES, _TARGET_MODES, _target_plan, _target_pool_info, _target_short_url

    state: dict[str, Any] = {"last_query": None, "want_search": False, "preview": None}

    # ---- source

    def set_source(index: int) -> Notice | None:
        query = ctx.cfg().collector.search_query
        if index == 1:
            if query:
                state["last_query"] = query
                ctx.update({"collector.search_query": None})
            state["want_search"] = False
            return Notice("ok", "Видео из своих ссылок")
        if state["last_query"]:
            ctx.update({"collector.search_query": state["last_query"]})
            return Notice("ok", f"Поиск: «{state['last_query']}»")
        state["want_search"] = True
        return Notice("info", "Введите тему поиска: Enter на строке темы")

    def edit_topic() -> Field:
        def submit(text: str) -> Notice:
            text = " ".join(text.split())
            if not text:
                return Notice("warn", "Тема не введена")
            ctx.update({"collector.search_query": text})
            state["last_query"], state["want_search"] = text, False
            return Notice("ok", f"Тема поиска: «{text}»")

        return Field("тема поиска", submit, initial=ctx.cfg().collector.search_query or state["last_query"] or "")

    def edit_views() -> Field:
        collector = ctx.cfg().collector

        def submit(text: str) -> Notice:
            if not text.strip():
                ctx.update({"collector.min_views": 0, "collector.max_views": None})
                return Notice("ok", "Просмотры: любые")
            try:
                low, high = parse_views(text)
            except ValueError:
                return Notice("warn", "Не понял диапазон: напишите «от до», например «10к 500к»")
            ctx.update({"collector.min_views": low, "collector.max_views": high})
            return Notice("ok", f"Просмотры: {views_label(low, high)}")

        current = f"{collector.min_views or 0} {collector.max_views or ''}".strip()
        return Field("просмотры «от до», например 10к 500к · пусто — любые", submit, initial=current)

    def set_age(index: int) -> None:
        ctx.update({"collector.max_age_hours": _TARGET_AGES[index][0]})

    # ---- links

    def paste_links() -> Field:
        def submit(text: str) -> Notice:
            if not text.strip():
                return Notice("info", "Ничего не вставлено")
            from modules.pool_manager import PoolManager

            target = ctx.cfg().limits.max_iterations_per_session or 2
            res = _db(ctx, lambda db: PoolManager(db).add_videos_from_text(text, comments_target=target))
            return Notice("ok" if res.added else "warn", f"+{res.added} добавлено · {res.existing} уже были · {res.invalid} не распознано")

        return Field("ссылки на видео, по одной в строке", submit, multiline=True)

    def clear_links() -> Confirm:
        def pick(i: int) -> Notice | None:
            if i != 1:
                return None
            return Notice("ok", f"Удалено видео: {_db(ctx, lambda db: db.clear_video_pool())}")

        return Confirm([Cell("Очистить список видео?", "warn")], ["Нет", "Очистить"], pick)

    def links_menu() -> Menu:
        waiting, done = _pool_counts(ctx)
        return Menu([
            Row("paste", [Cell(icon("add"), "", 3), Cell("вставить ссылки")], hint="Добавить видео в список", enter=paste_links),
            Row("show", [Cell(icon("preview"), "", 3), Cell("показать список"), Cell(str(waiting + done), "dim")],
                hint="Видео в списке и сколько ответов написано", enter=lambda: Screen(lambda: links_view(ctx))),
            Row("clear", [Cell(icon("trash"), "", 3), Cell("очистить")], hint="Удалить весь список", enter=clear_links),
        ])

    # ---- reply mode and limit

    def set_mode(index: int) -> None:
        ctx.update({"collector.target_mode": _TARGET_MODES[index][0]})

    def set_limit(index: int) -> Notice:
        limit = index + 1
        ctx.update({"limits.max_iterations_per_session": limit, "limits.total_iterations": limit,
                    "collector.comments_per_video": limit})
        changed = _db(ctx, lambda db: db.set_pool_comments_target(limit))
        return Notice("ok", f"Ответов на видео: {limit}" + (f" · в списке обновлено: {changed}" if changed else ""))

    def edit_limit() -> Field:
        def submit(text: str) -> Notice:
            if not text.strip().isdigit() or not 1 <= int(text) <= 50:
                return Notice("warn", "Нужно число от 1 до 50")
            return set_limit(int(text) - 1)

        return Field("ответов на видео, 1–50", submit, initial=str(ctx.cfg().limits.max_iterations_per_session or 2))

    # ---- preview

    def preview() -> Notice | Run:
        cfg = ctx.cfg()
        url = _target_pool_info(cfg.app.db_path)[1]
        if not url:
            return Notice("info", "Предпросмотр — по первому видео из своих ссылок: список пуст")
        limit = cfg.limits.max_iterations_per_session or 2

        def work() -> Notice:
            try:
                found = _fetch_preview(ctx, url, max(limit, 5))
            except Exception as exc:
                return Notice("err", fit(f"Комментарии не загрузились: {exc}", 70))
            state["preview"] = (url, found)
            return Notice("ok" if found else "warn", f"Комментариев: {len(found)} · {_target_short_url(url)}")

        return Run(work, f"{icon('preview')} Загружаю комментарии · {_target_short_url(url)}")

    def preview_lines() -> list[list[Cell]]:
        if not state["preview"]:
            return []
        url, found = state["preview"]
        lines = [[Cell(icon("videos"), "", 3), Cell(_target_short_url(url), "dim")]]
        for sc in found[:6]:
            under = f" · под «{fit(' '.join((sc.parent_text or '').split()), 18)}»" if sc.parent_text else ""
            lines.append([Cell(icon("reply_to"), "", 3),
                          Cell(fit(f"«{' '.join(sc.text.split())}» ♥{sc.like_count}{under}", 68))])
        if not found:
            lines.append([Cell("", "", 3), Cell("комментариев не нашлось", "dim")])
        return lines

    # ---- view

    def build() -> View:
        cfg = ctx.cfg()
        collector = cfg.collector
        query = collector.search_query
        search = bool(query) or state["want_search"]
        rows = [Row(
            "source", [Cell(icon("videos" if search else "links"), "", 3), Cell(_SOURCES[0 if search else 1])],
            hint="Откуда брать видео", values=_SOURCES, value_index=0 if search else 1, on_value=set_source,
        )]
        if search:
            rows.append(Row(
                "topic", [Cell(icon("topic"), "", 3), Cell(f"«{query}»" if query else "тема не задана", "" if query else "warn")],
                hint="Тема поиска · Enter — изменить", enter=edit_topic,
            ))
            ages = [label for _, label in _TARGET_AGES]
            age_index = next((i for i, (v, _) in enumerate(_TARGET_AGES) if v == collector.max_age_hours), 0)
            age_text = ages[age_index] if collector.max_age_hours in {v for v, _ in _TARGET_AGES} else f"{collector.max_age_hours} ч"
            rows.append(Row(
                "age", [Cell(icon("fresh"), "", 3), Cell(age_text.lower() if age_index == 0 else f"до {age_text}")],
                hint="Свежесть видео: как давно опубликованы", values=ages, value_index=age_index, on_value=set_age,
            ))
            rows.append(Row(
                "views", [Cell(icon("views"), "", 3), Cell(views_label(collector.min_views or 0, collector.max_views))],
                hint="Просмотры видео · Enter — изменить", enter=edit_views,
            ))
        else:
            waiting, done = _pool_counts(ctx)
            rows.append(Row(
                "pool", [Cell(icon("videos"), "", 3), Cell(f"{waiting} ждут", "" if waiting else "warn", 10),
                         Cell(f"{icon('sent')} {done}", "dim")],
                hint="Видео ждут ответа · пройдено · Enter — вставить, показать, очистить", enter=links_menu,
            ))

        mode = "first_reply" if collector.target_mode == "first_sub_comment" else collector.target_mode
        mode_index = next((i for i, (v, _, _) in enumerate(_TARGET_MODES) if v == mode), 0)
        rows.append(Row(
            "mode", [Cell(icon("reply_to"), "", 3), Cell(_TARGET_MODES[mode_index][1].lower())],
            hint="Кому отвечать · Пробел — пример", values=[label for _, label, _ in _TARGET_MODES], value_index=mode_index,
            on_value=set_mode,
            detail=lambda: [[Cell(line)] for line in _TARGET_MODE_EXAMPLES.get(mode, [])],
        ))
        limit = max(1, min(50, cfg.limits.max_iterations_per_session or 2))
        rows.append(Row(
            "limit", [Cell(icon("limit"), "", 3), Cell(f"{limit} на видео")],
            hint="Ответов на видео · Enter — ввести число", values=_LIMITS, value_index=limit - 1,
            on_value=set_limit, enter=edit_limit,
        ))
        rows.append(Row("preview", [Cell(icon("preview"), "", 3), Cell("предпросмотр", "accent")],
                        hint="Комментарии следующего видео: кому ответим", enter=preview))
        return View(rows=rows, bottom=([[Cell("")]] + preview_lines()) if state["preview"] else [],
                    keys="←→ значение · Enter изменить")

    return Tab("Цель", icon("tab_target"), build, warn=lambda: _target_plan(ctx.cfg())[0] == "none")


def links_view(ctx: AppContext) -> View:
    """The video list: one row per video with its reply progress."""
    from modules.wizard import _target_short_url

    videos = ctx.query("SELECT id, video_url, comments_done, comments_target, status FROM video_pool ORDER BY id LIMIT 200")
    rows = []
    for vid, url, done, target, status in videos:
        finished = status == "completed" or done >= target
        rows.append(Row(f"v{vid}", [Cell(icon("sent" if finished else "videos"), "", 3),
                                    Cell(_target_short_url(url), "dim" if finished else "", 30), Cell(f"{done}/{target}", "dim")],
                        hint="Готово" if finished else "Ждёт ответов"))
    if not rows:
        rows.append(Row("empty", [Cell("список пуст", "dim")], selectable=False))
    return View(rows=rows, title=[Cell(f"{icon('links')} свои ссылки")], keys="")
