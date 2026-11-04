"""Tab «Цель» of the current project: topics, videos per topic, freshness, views, whom to answer, replies per video,
own links, reference links, a preview. Every change is written into the project, not into config.yaml."""
from __future__ import annotations

import asyncio
import re
from typing import Any

from modules.ui.app import AppContext
from modules.ui.icons import icon
from modules.ui.shell import Cell, Confirm, Field, Hotkey, Menu, Notice, Row, Run, Screen, Step, Tab, View, fit
from modules.ui.tab_projects import no_project_view, switch_row

_LIMITS = [str(n) for n in range(1, 51)]
_VIDEOS = [str(n) for n in range(1, 51)]  # videos per topic and run
# Reference links: (use_similar, use_title_words) in the order ←→ goes through them.
_REFERENCES = [(False, False), (True, False), (False, True), (True, True)]


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


def age_label(hours: int | None) -> str:
    """'любые' / 'до 2 дня' / 'до 30 ч' for a freshness limit."""
    from modules.wizard import _TARGET_AGES

    if not hours:
        return "любые"
    label = next((text for value, text in _TARGET_AGES if value == hours), f"{hours} ч")
    return f"до {label}"


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


def _first_link(ctx: AppContext, project_id: int) -> str | None:
    """The project's next own link (else its first one): the preview reads its comments."""
    rows = ctx.query(
        "SELECT video_url FROM video_pool WHERE project_id = ? AND source = 'links'"
        " ORDER BY (status IN ('pending', 'in_progress') AND comments_done < comments_target) DESC, id LIMIT 1",
        (project_id,),
    )
    return str(rows[0][0]) if rows else None


def _fetch_preview(ctx: AppContext, project: Any, url: str | None, limit: int) -> tuple[str | None, list[Any]]:
    """(video, comments): the given own link, else the first video the first topic finds within the filters."""
    from modules.projects import open_database
    from modules.youtube_collector import YouTubeCollector

    cfg = ctx.cfg()

    async def fetch() -> tuple[str | None, list[Any]]:
        async with open_database(cfg) as db:
            proxy = await db.search_proxy_url()
        async with YouTubeCollector(cfg.collector, proxy) as collector:
            video = url
            if video is None:
                found = await collector.search_videos(
                    query=project.search_topics[0], limit=1, min_views=project.min_views, max_views=project.max_views,
                    max_age_hours=project.max_age_hours,
                )
                video = found[0]["url"] if found else None
            if video is None:
                return None, []
            return video, await collector.fetch_video_sub_comments(video, max_sub_comments=limit)

    return asyncio.run(fetch())


def references_active(project: Any) -> bool:
    return bool(project.links_total and (project.use_similar or project.use_title_words))


def build_tab(ctx: AppContext) -> Tab:
    from modules.wizard import _TARGET_AGES, _TARGET_MODE_EXAMPLES, _TARGET_MODES, _target_short_url

    state: dict[str, Any] = {"preview": None}

    def save(**fields: Any) -> Notice | None:
        """Write into the current project; a bad value comes back as a notice."""
        project = ctx.project()
        if project is None:
            return Notice("warn", "Проектов нет")
        try:
            ctx.update_project(project.id, **fields)
        except ValueError as exc:
            return Notice("warn", str(exc))
        return None

    # ---- filters

    def edit_views() -> Field:
        project = ctx.project()

        def submit(text: str) -> Notice:
            if not text.strip():
                return save(min_views=0, max_views=None) or Notice("ok", "Просмотры: любые")
            try:
                low, high = parse_views(text)
            except ValueError:
                return Notice("warn", "Не понял диапазон: напишите «от до», например «10к 500к»")
            return save(min_views=low, max_views=high) or Notice("ok", f"Просмотры: {views_label(low, high)}")

        current = f"{project.min_views or 0} {project.max_views or ''}".strip()
        return Field("просмотры «от до», например 10к 500к · пусто — любые", submit, initial=current)

    def set_age(index: int) -> Notice | None:
        return save(max_age_hours=_TARGET_AGES[index][0])

    def set_videos(index: int) -> Notice:
        return save(max_videos=index + 1) or Notice("ok", f"Видео на тему за запуск: {index + 1}")

    def edit_videos() -> Field:
        def submit(text: str) -> Notice:
            if not text.strip().isdigit() or not 1 <= int(text) <= 50:
                return Notice("warn", "Нужно число от 1 до 50")
            return set_videos(int(text) - 1)

        return Field("видео на тему за запуск, 1–50", submit, initial=str(ctx.project().max_videos))

    # ---- links

    def paste_links() -> Field:
        def submit(text: str) -> Notice:
            if not text.strip():
                return Notice("info", "Ничего не вставлено")
            from modules.pool_manager import PoolManager

            project = ctx.project()
            res = ctx.db(lambda db: PoolManager(db).add_videos_from_text(text, project.comments_per_video, project.id))
            return Notice("ok" if res.added else "warn", f"+{res.added} добавлено · {res.existing} уже были · {res.invalid} не распознано")

        return Field("ссылки на видео, по одной в строке", submit, multiline=True)

    def clear_links() -> Confirm:
        project = ctx.project()

        def pick(i: int) -> Notice | None:
            if i != 1:
                return None
            return Notice("ok", f"Удалено видео: {ctx.db(lambda db: db.clear_video_pool(project.id))}")

        return Confirm([Cell(f"Очистить список видео проекта «{project.name}»?", "warn")], ["Нет", "Очистить"], pick)

    def links_menu() -> Menu:
        project = ctx.project()
        return Menu([
            Row("paste", [Cell(icon("add"), "", 3), Cell("вставить ссылки")], hint="Добавить видео в список", enter=paste_links),
            Row("show", [Cell(icon("preview"), "", 3), Cell("показать список"), Cell(str(project.links_total), "dim")],
                hint="Видео в списке и сколько ответов написано", enter=lambda: Screen(lambda: links_view(ctx))),
            Row("clear", [Cell(icon("trash"), "", 3), Cell("очистить")], hint="Удалить весь список проекта", enter=clear_links),
        ])

    def set_references(index: int) -> Notice:
        similar, words = _REFERENCES[index]
        kinds = ", ".join(name for on, name in ((similar, "похожие"), (words, "слова из названий")) if on)
        return save(use_similar=similar, use_title_words=words) or Notice("ok", f"Ориентир: {kinds or 'выкл'}")

    def next_references() -> Notice:
        project = ctx.project()
        return set_references((_REFERENCES.index((project.use_similar, project.use_title_words)) + 1) % len(_REFERENCES))

    # ---- reply mode and limit

    def set_mode(index: int) -> Notice | None:
        return save(target_mode=_TARGET_MODES[index][0])

    def set_limit(index: int) -> Notice:
        limit = index + 1
        project = ctx.project()
        failed = save(comments_per_video=limit)
        if failed:
            return failed
        changed = ctx.db(lambda db: db.set_pool_comments_target(limit, project.id))
        return Notice("ok", f"Ответов на видео: {limit}" + (f" · в списке обновлено: {changed}" if changed else ""))

    def edit_limit() -> Field:
        def submit(text: str) -> Notice:
            if not text.strip().isdigit() or not 1 <= int(text) <= 50:
                return Notice("warn", "Нужно число от 1 до 50")
            return set_limit(int(text) - 1)

        return Field("ответов на видео, 1–50", submit, initial=str(ctx.project().comments_per_video))

    # ---- preview

    def preview() -> Notice | Run:
        project = ctx.project()
        url = _first_link(ctx, project.id)
        if url is None and not project.search_topics:
            return Notice("info", "Предпросмотр — по первому видео проекта: нет ни своих ссылок, ни тем")
        limit = max(project.comments_per_video, 5)

        def work() -> Notice:
            try:
                video, found = _fetch_preview(ctx, project, url, limit)
            except Exception as exc:
                return Notice("err", fit(f"Комментарии не загрузились: {exc}", 70))
            if video is None:
                return Notice("warn", f"По теме «{fit(project.search_topics[0], 40)}» видео не найдено")
            state["preview"] = (video, found)
            return Notice("ok" if found else "warn", f"Комментариев: {len(found)} · {_target_short_url(video)}")

        what = _target_short_url(url) if url else f"тема «{fit(project.search_topics[0], 30)}»"
        return Run(work, f"{icon('preview')} Загружаю комментарии · {what}")

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

    def topics_row(project: Any) -> Row:
        topics = project.search_topics
        if topics:
            shown = ", ".join(f"«{fit(t, 16)}»" for t in topics[:2]) + (" …" if len(topics) > 2 else "")
            text, style = f"темы {len(topics)} · {shown}", ""
        else:
            text, style = "тем нет", "" if project.links_total else "warn"
        return Row("topics", [Cell(icon("topic"), "", 3), Cell(text, style)],
                   hint="Темы поиска: за запуск обходятся все · Enter — список", enter=lambda: Screen(lambda: topics_view(ctx)))

    def filter_rows(project: Any) -> list[Row]:
        ages = [label for _, label in _TARGET_AGES]
        age_index = next((i for i, (v, _) in enumerate(_TARGET_AGES) if v == project.max_age_hours), 0)
        videos = max(1, min(50, project.max_videos))
        return [
            Row("videos", [Cell(icon("videos"), "", 3), Cell(f"{videos} видео на тему за запуск")],
                hint="Сколько найденных видео брать с каждой темы · Enter — ввести число", values=_VIDEOS,
                value_index=videos - 1, on_value=set_videos, enter=edit_videos),
            Row("age", [Cell(icon("fresh"), "", 3), Cell(age_label(project.max_age_hours))],
                hint="Свежесть видео: как давно опубликованы", values=ages, value_index=age_index, on_value=set_age),
            Row("views", [Cell(icon("views"), "", 3), Cell(views_label(project.min_views, project.max_views))],
                hint="Просмотры видео · Enter — изменить", enter=edit_views),
        ]

    def links_row(project: Any) -> Row:
        done = project.links_total - project.links_waiting
        return Row(
            "pool", [Cell(icon("links"), "", 3), Cell("свои ссылки", "", 13),
                     Cell(f"{project.links_waiting} ждут", "" if project.links_waiting else "dim", 10),
                     Cell(f"{icon('sent')} {done}", "dim")],
            hint="Видео ждут ответа · пройдено · Enter — вставить, показать, очистить", enter=links_menu,
        )

    def references_row(project: Any) -> Row:
        def box(on: bool) -> str:
            return "▣" if on else "▢"

        text = f"ориентир: похожие {box(project.use_similar)} · слова из названия {box(project.use_title_words)}"
        if not project.links_total:
            return Row("refs", [Cell(icon("similar"), "", 3), Cell(text, "dim")],
                       hint="Ориентир ищет новые видео по своим ссылкам — сначала вставьте ссылки")
        index = _REFERENCES.index((project.use_similar, project.use_title_words))
        return Row(
            "refs", [Cell(icon("similar"), "", 3), Cell(text)],
            hint="По своим ссылкам: похожие видео рядом и поиск по словам из названия · ←→ / Пробел",
            values=["выкл", "похожие", "слова", "оба"], value_index=index, on_value=set_references,
            space=next_references,
        )

    def build() -> View:
        project = ctx.project()
        if project is None:
            return no_project_view()
        rows = [row for row in [switch_row(ctx)] if row]
        rows.append(topics_row(project))
        if project.search_topics or references_active(project):
            rows += filter_rows(project)
        mode = "first_reply" if project.target_mode == "first_sub_comment" else project.target_mode
        mode_index = next((i for i, (v, _, _) in enumerate(_TARGET_MODES) if v == mode), 0)
        rows.append(Row(
            "mode", [Cell(icon("reply_to"), "", 3), Cell(_TARGET_MODES[mode_index][1].lower())],
            hint="Кому отвечать · Пробел — пример", values=[label for _, label, _ in _TARGET_MODES], value_index=mode_index,
            on_value=set_mode,
            detail=lambda: [[Cell(line)] for line in _TARGET_MODE_EXAMPLES.get(mode, [])],
        ))
        limit = max(1, min(50, project.comments_per_video))
        rows.append(Row(
            "limit", [Cell(icon("limit"), "", 3), Cell(f"{limit} на видео")],
            hint="Ответов на видео · Enter — ввести число", values=_LIMITS, value_index=limit - 1,
            on_value=set_limit, enter=edit_limit,
        ))
        rows += [links_row(project), references_row(project)]
        rows.append(Row("preview", [Cell(icon("preview"), "", 3), Cell("предпросмотр", "accent")],
                        hint="Комментарии первого видео проекта: кому ответим", enter=preview))
        return View(rows=rows, bottom=([[Cell("")]] + preview_lines()) if state["preview"] else [],
                    keys="←→ значение · Enter изменить")

    def warn() -> bool:
        from modules.projects import project_plan

        return any(not project_plan(p).steps for p in ctx.projects() if p.enabled)

    return Tab("Цель", icon("tab_target"), build, warn=warn)


# --------------------------------------------------------------------------- topics


def topics_view(ctx: AppContext) -> View:
    """The current project's topics: Enter edits one, + adds, D / Delete removes; empty and repeated ones are refused."""
    project = ctx.project()
    if project is None:
        return no_project_view()
    topics = list(project.search_topics)

    def store(new: list[str], done: str) -> Notice:
        from database import clean_topics

        try:
            cleaned = clean_topics(new)
        except ValueError as exc:
            return Notice("warn", str(exc))
        if len(cleaned) < len(new):
            return Notice("warn", "Такая тема уже есть")
        try:
            ctx.update_project(project.id, search_topics=cleaned)
        except ValueError as exc:
            return Notice("warn", str(exc))
        return Notice("ok", done)

    def edit(index: int) -> Field:
        def submit(text: str) -> Step:
            text = " ".join(text.split())
            if not text:
                return Notice("warn", "Пустая тема не сохранена")
            if text == topics[index]:
                return None
            return store([*topics[:index], text, *topics[index + 1:]], f"Тема: «{fit(text, 50)}»")

        return Field("тема поиска", submit, initial=topics[index])

    def add(_row: Any) -> Field:
        def submit(text: str) -> Step:
            text = " ".join(text.split())
            if not text:
                return Notice("warn", "Пустая тема не добавлена")
            return store([*topics, text], f"Тема добавлена: «{fit(text, 50)}»")

        return Field("новая тема поиска", submit)

    def remove(row: Any) -> Step:
        if row is None or not row.id.startswith("t"):
            return None
        index = int(row.id[1:])

        def pick(i: int) -> Step:
            if i != 1:
                return None
            return store([t for j, t in enumerate(topics) if j != index], f"Удалена тема «{fit(topics[index], 40)}»")

        return Confirm([Cell(f"Удалить тему «{fit(topics[index], 40)}»?", "warn")], ["Нет", "Удалить"], pick)

    rows = [Row(f"t{i}", [Cell(icon("topic"), "", 3), Cell(fit(text, 66))], hint="Enter — изменить · D — удалить",
                enter=lambda i=i: edit(i)) for i, text in enumerate(topics)]
    if not rows:
        rows.append(Row("empty", [Cell("тем нет · + добавить", "dim")], selectable=False))
    return View(
        rows=rows, title=[Cell(f"{icon('topic')} {project.name} › темы · {len(topics)}")],
        keys="Enter изменить · + добавить · D удалить",
        hotkeys=[Hotkey("+", add), Hotkey("=", add), Hotkey("D", remove), Hotkey("delete", remove)],
    )


def links_view(ctx: AppContext) -> View:
    """The current project's own links: one row per video with its reply progress."""
    from modules.wizard import _target_short_url

    project = ctx.project()
    if project is None:
        return no_project_view()
    videos = ctx.query(
        "SELECT id, video_url, comments_done, comments_target, status FROM video_pool"
        " WHERE project_id = ? AND source = 'links' ORDER BY id LIMIT 200",
        (project.id,),
    )
    rows = []
    for vid, url, done, target, status in videos:
        finished = status == "completed" or done >= target
        rows.append(Row(f"v{vid}", [Cell(icon("sent" if finished else "videos"), "", 3),
                                    Cell(_target_short_url(url), "dim" if finished else "", 30, grow=True), Cell(f"{done}/{target}", "dim")],
                        hint="Готово" if finished else "Ждёт ответов"))
    if not rows:
        rows.append(Row("empty", [Cell("список пуст", "dim")], selectable=False))
    return View(rows=rows, title=[Cell(f"{icon('links')} {project.name} › свои ссылки")], keys="")


__all__ = ["age_label", "build_tab", "links_view", "parse_views", "topics_view", "views_label"]
