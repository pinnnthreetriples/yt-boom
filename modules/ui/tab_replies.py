"""Tab «Ответы»: who writes the replies (DeepSeek or own templates), the key, rewording, the templates."""
from __future__ import annotations

import asyncio
from typing import Any

from modules.ui.app import AppContext
from modules.ui.icons import icon
from modules.ui.shell import Cell, Confirm, Field, Hotkey, Notice, Row, Run, Screen, Tab, View, fit

_WRITERS = ["ИИ DeepSeek", "мои шаблоны"]
_PLACEHOLDER = "sk-your-deepseek-key"  # the key in config.example.yaml: the config requires one, it is not a real key


def _real_key(cfg: Any) -> str:
    key = (cfg.deepseek.api_key or "").strip()
    return "" if key == _PLACEHOLDER else key


def _save_templates(ctx: AppContext, templates: list[str], mode: str | None = None) -> None:
    # both keys: the config falls back to the top-level list when processor.custom_replies is empty
    updates: dict[str, Any] = {"processor.custom_replies": templates, "custom_replies": templates}
    if mode:
        updates.update({"processor.reply_mode": mode, "reply_mode": mode})
    ctx.update(updates)


def _mask(key: str) -> str:
    key = (key or "").strip()
    return f"{key[:5]}…{key[-4:]}" if len(key) > 12 else "••••"


def build_tab(ctx: AppContext) -> Tab:
    # last check of the key: (key, ok, message); a key that failed it waits here for «сохранить всё равно»
    state: dict[str, Any] = {"check": None, "rejected": None}

    def set_writer(index: int) -> Notice:
        mode = "ai" if index == 0 else "custom"
        ctx.update({"processor.reply_mode": mode, "reply_mode": mode})
        if mode == "custom" and not ctx.cfg().processor.custom_replies:
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

    def key_enter():
        rejected = state["rejected"]
        if rejected:
            def pick(i: int):
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
        def submit(text: str):
            key = text.strip()
            if not key:
                return Notice("warn", "Ключ не введён, оставлен прежний")
            return Run(lambda: check_key(key), f"{icon('key')} Проверяю ключ DeepSeek")

        return Field("ключ DeepSeek (sk-…)", submit, secret=True)

    def set_shuffle(index: int) -> Notice:
        on = index == 0
        ctx.update({"processor.paraphrase_custom": on})
        if on and not _real_key(ctx.cfg()):
            return Notice("warn", "Включено, но ключа DeepSeek нет — шаблон уйдёт как есть")
        return Notice("ok" if on else "info", "ИИ меняет слова шаблона каждый раз" if on else "Шаблон уходит как есть")

    def build() -> View:
        cfg = ctx.cfg()
        ai = cfg.processor.reply_mode == "ai"
        templates = list(cfg.processor.custom_replies or [])
        paraphrase = bool(cfg.processor.paraphrase_custom)
        rows = [Row(
            "writer", [Cell(icon("ai" if ai else "templates"), "", 3), Cell(_WRITERS[0 if ai else 1])],
            hint="Кто пишет ответы", values=_WRITERS, value_index=0 if ai else 1, on_value=set_writer,
        )]
        if ai or paraphrase:
            key = _real_key(cfg)
            check = state["check"]
            mark = ""
            if check and check[0] == key and key:
                mark = icon("sent") if check[1] else icon("failed")
            hint = "Ключ DeepSeek · Enter — новый · Пробел — где взять"
            if state["rejected"]:
                hint = "Ключ не принят · Enter — сохранить всё равно или ввести другой"
            rows.append(Row(
                "key", [Cell(icon("key"), "", 3), Cell(_mask(key) if key else "не задан", "" if key else "warn"), Cell(mark)],
                hint=hint, enter=key_enter,
                detail=lambda: [[Cell("ключ берут на platform.deepseek.com → API keys")]],
            ))
        if not ai:
            rows.append(Row(
                "shuffle", [Cell(icon("shuffle"), "", 3), Cell("менять слова" if paraphrase else "как есть")],
                hint="Рандомизация: ИИ меняет слова шаблона каждый раз", values=["менять слова", "как есть"],
                value_index=0 if paraphrase else 1, on_value=set_shuffle,
            ))
        rows.append(Row(
            "templates", [Cell(icon("templates"), "", 3), Cell(f"шаблоны {len(templates)}", "warn" if not ai and not templates else "")],
            hint="Мои шаблоны · Enter — список", enter=lambda: Screen(lambda: templates_view(ctx)),
            detail=lambda: [[Cell(fit(f"«{t}»", 66))] for t in list(ctx.cfg().processor.custom_replies or [])[:3]],
        ))
        return View(rows=rows, keys="←→ значение · Enter изменить")

    def warn() -> bool:
        cfg = ctx.cfg()
        if cfg.processor.reply_mode == "ai":
            return not _real_key(cfg)
        return not cfg.processor.custom_replies

    return Tab("Ответы", icon("tab_replies"), build, warn=warn)


def templates_view(ctx: AppContext) -> View:
    """The templates: Enter edits one, + adds, D / Delete removes."""
    templates = list(ctx.cfg().processor.custom_replies or [])

    def edit(index: int) -> Field:
        def submit(text: str) -> Notice:
            text = " ".join(text.split())
            if not text:
                return Notice("warn", "Пустой текст не сохранён")
            current = list(ctx.cfg().processor.custom_replies or [])
            if index < len(current):
                current[index] = text
                _save_templates(ctx, current)
            return Notice("ok", "Шаблон сохранён")

        return Field("текст шаблона", submit, initial=templates[index])

    def add(_row) -> Field:
        def submit(text: str) -> Notice:
            text = " ".join(text.split())
            if not text:
                return Notice("warn", "Пустой текст не добавлен")
            _save_templates(ctx, [*list(ctx.cfg().processor.custom_replies or []), text])
            return Notice("ok", "Шаблон добавлен")

        return Field("новый шаблон", submit)

    def remove(row):
        if row is None or not row.id.startswith("t"):
            return None
        index = int(row.id[1:])

        def pick(i: int):
            if i != 1:
                return None
            current = list(ctx.cfg().processor.custom_replies or [])
            if index < len(current):
                removed = current.pop(index)
                _save_templates(ctx, current)
                return Notice("ok", f"Удалён: «{fit(removed, 50)}»")
            return None

        return Confirm([Cell("Удалить шаблон?", "warn")], ["Нет", "Удалить"], pick)

    rows = [Row(f"t{i}", [Cell(fit(text, 70))], hint="Enter — изменить · D — удалить", enter=lambda i=i: edit(i))
            for i, text in enumerate(templates)]
    if not rows:
        rows.append(Row("empty", [Cell("шаблонов нет · + добавить", "dim")], selectable=False))
    return View(
        rows=rows, title=[Cell(f"{icon('templates')} шаблоны · {len(templates)}")],
        keys="Enter изменить · + добавить · D удалить",
        hotkeys=[Hotkey("+", add), Hotkey("D", remove), Hotkey("delete", remove)],
    )
