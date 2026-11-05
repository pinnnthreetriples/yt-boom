"""Tab «Ответы»: who writes the replies of the current project (DeepSeek or its own templates), rewording, the
templates — all saved into the project; below, the DeepSeek key, common to every project (config.yaml)."""
from __future__ import annotations

import asyncio
from typing import Any

from modules.projects import DEEPSEEK_PLACEHOLDER
from modules.ui.app import AppContext
from modules.ui.icons import icon
from modules.ui.shell import Cell, Confirm, Field, Hotkey, Notice, Row, Run, Screen, Step, Tab, View, fit
from modules.ui.tab_projects import no_project_view, switch_row

_WRITERS = ["ИИ DeepSeek", "мои шаблоны"]
_COMMON = "общее для всех проектов"

# Language of the posted replies (processor.reply_language): code, label. Common to every project (config.yaml).
_LANGUAGES = [
    ("auto", "авто: как в комментарии / шаблоне"),
    ("ru", "русский"),
    ("en", "English"),
    ("es", "Español"),
    ("pt", "Português"),
    ("de", "Deutsch"),
    ("fr", "Français"),
    ("tr", "Türkçe"),
    ("uk", "Українська"),
]

# Share of replies where one Russian word gets a few letters in Latin (processor.latin_mix_probability). The code
# does this to the finished reply, DeepSeek is never asked to mix alphabets; works for AI replies and templates.
_MIX_LEVELS = [
    (0.0, "выкл"),
    (0.1, "редко · 10%"),
    (0.3, "иногда · 30%"),
    (0.6, "часто · 60%"),
]


def _real_key(cfg: Any) -> str:
    key = (cfg.deepseek.api_key or "").strip()
    return "" if key == DEEPSEEK_PLACEHOLDER else key


def _mask(key: str) -> str:
    key = (key or "").strip()
    return f"{key[:5]}…{key[-4:]}" if len(key) > 12 else "••••"


def _save(ctx: AppContext, **fields: Any) -> Notice | None:
    """Write into the current project; a bad value comes back as a notice."""
    project = ctx.project()
    if project is None:
        return Notice("warn", "Проектов нет")
    try:
        ctx.update_project(project.id, **fields)
    except ValueError as exc:
        return Notice("warn", str(exc))
    return None


def _needs_key(ctx: AppContext) -> bool:
    """Some project writes with DeepSeek (AI replies or reworded templates): the key row is shown."""
    return any(p.reply_mode == "ai" or p.paraphrase_custom for p in ctx.projects())


def build_tab(ctx: AppContext) -> Tab:
    # last check of the key: (key, ok, message); a key that failed it waits here for «сохранить всё равно»
    state: dict[str, Any] = {"check": None, "rejected": None}

    def set_writer(index: int) -> Notice:
        mode = "ai" if index == 0 else "custom"
        failed = _save(ctx, reply_mode=mode)
        if failed:
            return failed
        if mode == "custom" and not ctx.project().custom_replies:
            return Notice("warn", "Шаблонов нет: Enter на строке «шаблоны» — добавить")
        return Notice("ok", "Ответы пишет ИИ DeepSeek" if mode == "ai" else "Ответы из моих шаблонов")

    def check_key(key: str) -> Notice:
        from modules.wizard import check_deepseek_api

        ok, message = asyncio.run(check_deepseek_api(key, ctx.cfg().deepseek.base_url))
        state["check"] = (key, ok, message)
        if ok:
            ctx.update({"deepseek.api_key": key})
            state["rejected"] = None
            return Notice("ok", "Ключ сохранён · " + message)
        state["rejected"] = key
        return Notice("err", fit(f"Ключ не принят: {message} · Enter на ключе — сохранить всё равно", 74))

    def key_enter() -> Step:
        rejected = state["rejected"]
        if rejected:
            def pick(i: int) -> Step:
                state["rejected"] = None
                if i == 1:
                    ctx.update({"deepseek.api_key": rejected})
                    return Notice("warn", "Непроверенный ключ сохранён")
                if i == 2:
                    return new_key()
                return None

            return Confirm([Cell(f"Ключ {_mask(rejected)} не прошёл проверку. Сохранить?", "warn")],
                           ["Нет", "Сохранить", "Ввести другой"], pick)
        return new_key()

    def new_key() -> Field:
        def submit(text: str) -> Step:
            key = text.strip()
            if not key:
                return Notice("warn", "Ключ не введён, оставлен прежний")
            return Run(lambda: check_key(key), f"{icon('key')} Проверяю ключ DeepSeek")

        return Field("ключ DeepSeek (sk-…)", submit, secret=True)

    def set_shuffle(index: int) -> Notice:
        on = index == 0
        failed = _save(ctx, paraphrase_custom=on)
        if failed:
            return failed
        if on and not _real_key(ctx.cfg()):
            return Notice("warn", "Включено, но ключа DeepSeek нет — шаблон уйдёт как есть")
        return Notice("ok" if on else "info", "ИИ меняет слова шаблона каждый раз" if on else "Шаблон уходит как есть")

    def common_header() -> list[Row]:
        return [Row("gap", [Cell("")], selectable=False),
                Row("common", [Cell("── " + _COMMON, "dim")], selectable=False)]

    def text_style_rows(cfg: Any) -> list[Row]:
        """Language and letter mixing: saved into config.yaml, so they apply to every project and to both writers."""
        lang = getattr(cfg.processor, "reply_language", "auto")
        languages = list(_LANGUAGES)
        if lang not in dict(languages):
            languages.append((lang, lang))
        mix = float(getattr(cfg.processor, "latin_mix_probability", 0.0))
        levels = list(_MIX_LEVELS)
        if not any(abs(value - mix) < 1e-9 for value, _ in levels):
            levels.append((mix, f"{mix:.0%}"))
        lang_index = [code for code, _ in languages].index(lang)
        mix_index = next(i for i, (value, _) in enumerate(levels) if abs(value - mix) < 1e-9)

        def set_language(index: int) -> Notice:
            code = languages[index][0]
            ctx.update({"processor.reply_language": code})
            notice = Notice("ok", f"Язык ответов: {languages[index][1]}")
            c = ctx.cfg()
            if code != "auto" and any(
                p.reply_mode == "custom" and not (p.paraphrase_custom and _real_key(c)) for p in ctx.projects()
            ):
                notice = Notice("warn", "Шаблоны переводятся только при включённой рандомизации и ключе DeepSeek")
            return notice

        def set_mix(index: int) -> Notice:
            value, label = levels[index]
            ctx.update({"processor.latin_mix_probability": value})
            return Notice("info", "Смешивание букв выключено") if value == 0 else Notice("ok", f"Смешивание букв: {label}")

        rows = [
            Row("language", [Cell(icon("reply_to"), "", 3), Cell("язык: " + languages[lang_index][1])],
                hint="Язык ответов · ←→ значение", values=[label for _, label in languages],
                value_index=lang_index, on_value=set_language),
            Row("mix", [Cell(icon("edited"), "", 3), Cell("смешивание букв: " + levels[mix_index][1])],
                hint="Часть букв слова пишется латиницей (prивет) · ←→ значение", values=[label for _, label in levels],
                value_index=mix_index, on_value=set_mix),
        ]
        if len(ctx.projects()) > 1 and not _needs_key(ctx):
            rows = common_header() + rows
        return rows

    def key_rows(cfg: Any) -> list[Row]:
        key = _real_key(cfg)
        check = state["check"]
        mark = ""
        if check and check[0] == key and key:
            mark = icon("sent") if check[1] else icon("failed")
        hint = f"Ключ DeepSeek, {_COMMON} · Enter — новый · Пробел — где взять"
        if state["rejected"]:
            hint = "Ключ не принят · Enter — сохранить всё равно или ввести другой"
        rows = [Row("key", [Cell(icon("key"), "", 3), Cell(_mask(key) if key else "не задан", "" if key else "warn"), Cell(mark)],
                    hint=hint, enter=key_enter, detail=lambda: [[Cell("ключ берут на platform.deepseek.com → API keys")]])]
        if len(ctx.projects()) > 1:  # the key is not the project's: set it apart
            rows = common_header() + rows
        return rows

    def build() -> View:
        project = ctx.project()
        if project is None:
            return no_project_view()
        cfg = ctx.cfg()
        ai = project.reply_mode == "ai"
        templates = list(project.custom_replies)
        rows = [row for row in [switch_row(ctx)] if row]
        rows.append(Row(
            "writer", [Cell(icon("ai" if ai else "templates"), "", 3), Cell(_WRITERS[0 if ai else 1])],
            hint="Кто пишет ответы", values=_WRITERS, value_index=0 if ai else 1, on_value=set_writer,
        ))
        project_rows = []
        if not ai:
            project_rows.append(Row(
                "shuffle", [Cell(icon("shuffle"), "", 3), Cell("менять слова" if project.paraphrase_custom else "как есть")],
                hint="Рандомизация: ИИ меняет слова шаблона каждый раз", values=["менять слова", "как есть"],
                value_index=0 if project.paraphrase_custom else 1, on_value=set_shuffle,
            ))
        project_rows.append(Row(
            "templates", [Cell(icon("templates"), "", 3), Cell(f"шаблоны {len(templates)}", "warn" if not ai and not templates else "")],
            hint="Мои шаблоны · Enter — список", enter=lambda: Screen(lambda: templates_view(ctx)),
            detail=lambda: [[Cell(fit(f"«{t}»", 66))] for t in list((ctx.project() or project).custom_replies)[:3]],
        ))
        if _needs_key(ctx):
            keys = key_rows(cfg)
            # one project: the key stays where it was, right under the writer
            rows += (keys + project_rows) if len(keys) == 1 else (project_rows + keys)
        else:
            rows += project_rows
        rows += text_style_rows(cfg)
        return View(rows=rows, keys="←→ значение · Enter изменить")

    def warn() -> bool:
        has_key = bool(_real_key(ctx.cfg()))
        return any(
            (p.reply_mode == "ai" and not has_key) or (p.reply_mode == "custom" and not p.custom_replies)
            for p in ctx.projects() if p.enabled
        )

    return Tab("Ответы", icon("tab_replies"), build, warn=warn)


def templates_view(ctx: AppContext) -> View:
    """The current project's templates: Enter edits one, + adds, D / Delete removes."""
    project = ctx.project()
    if project is None:
        return no_project_view()
    templates = list(project.custom_replies)

    def edit(index: int) -> Field:
        def submit(text: str) -> Notice:
            text = " ".join(text.split())
            if not text:
                return Notice("warn", "Пустой текст не сохранён")
            current = list(ctx.project().custom_replies)
            if index < len(current):
                current[index] = text
                return _save(ctx, custom_replies=current) or Notice("ok", "Шаблон сохранён")
            return Notice("warn", "Шаблон уже удалён")

        return Field("текст шаблона", submit, initial=templates[index])

    def add(_row: Any) -> Field:
        def submit(text: str) -> Notice:
            text = " ".join(text.split())
            if not text:
                return Notice("warn", "Пустой текст не добавлен")
            return _save(ctx, custom_replies=[*ctx.project().custom_replies, text]) or Notice("ok", "Шаблон добавлен")

        return Field("новый шаблон", submit)

    def remove(row: Any) -> Step:
        if row is None or not row.id.startswith("t"):
            return None
        index = int(row.id[1:])

        def pick(i: int) -> Step:
            if i != 1:
                return None
            current = list(ctx.project().custom_replies)
            if index < len(current):
                removed = current.pop(index)
                return _save(ctx, custom_replies=current) or Notice("ok", f"Удалён: «{fit(removed, 50)}»")
            return None

        return Confirm([Cell("Удалить шаблон?", "warn")], ["Нет", "Удалить"], pick)

    rows = [Row(f"t{i}", [Cell(fit(text, 70))], hint="Enter — изменить · D — удалить", enter=lambda i=i: edit(i))
            for i, text in enumerate(templates)]
    if not rows:
        rows.append(Row("empty", [Cell("шаблонов нет · + добавить", "dim")], selectable=False))
    title = f"{icon('templates')} шаблоны · {len(templates)}"
    if len(ctx.projects()) > 1:
        title = f"{icon('templates')} {project.name} › шаблоны · {len(templates)}"
    return View(
        rows=rows, title=[Cell(title)],
        keys="Enter изменить · + добавить · D удалить",
        hotkeys=[Hotkey("+", add), Hotkey("D", remove), Hotkey("delete", remove)],
    )
