"""Shared console UI layer: arrow-key menus (prompt_toolkit) and compact output helpers (Rich)."""
from __future__ import annotations

import shutil
import sys
from typing import Callable, Iterable

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

MAX_WIDTH = 70

# Menu row: (hotkey, label, status value, style names like "ok" / "warn dim"); None is a blank line.
MenuRow = tuple[str, str, str, str]
Rows = list[MenuRow | None]

STYLES = {"ok": "green", "warn": "yellow", "err": "bold red", "dim": "bright_black", "accent": "cyan", "ai": "magenta"}
THEME = Theme({**STYLES, "title": "bold", "key": "bold yellow"})
console = Console(theme=THEME, highlight=False)

_PT_STYLE = Style.from_dict({
    "ok": "ansigreen", "warn": "ansiyellow", "err": "ansired bold", "dim": "ansibrightblack",
    "accent": "ansicyan", "ai": "ansimagenta", "key": "ansiyellow bold",
    "selected": "reverse bold", "marker": "ansicyan bold",
})

# ЙЦУКЕН -> QWERTY by physical key position, so letter hotkeys work in the Russian layout.
RU_TO_EN = dict(zip("йцукенгшщзхъфывапролджэячсмитьбю", "qwertyuiop[]asdfghjkl;'zxcvbnm,."))


class WizardBack(Exception):
    """User requested returning to the main dashboard or aborting current step."""


def is_interactive() -> bool:
    """True when both stdin and stdout are a real console."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def normalize_key(ch: str) -> str:
    """Upper-case a typed character, mapping Russian-layout letters to their QWERTY key."""
    low = ch.lower()
    return RU_TO_EN.get(low, low).upper()


def fit(text: str, width: int) -> str:
    """Truncate text to width with a trailing ellipsis."""
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def _style_classes(style: str) -> str:
    return " ".join(f"class:{name}" for name in style.split())


def _out() -> Console:
    """The shared console, capped at MAX_WIDTH columns (or the window width if narrower)."""
    console.width = min(MAX_WIDTH, shutil.get_terminal_size((MAX_WIDTH, 24)).columns)
    return console


# --------------------------------------------------------------------------- menu

# A picked menu leaves no frame behind, so only one menu is ever on screen (tests turn it off to read the last frame).
ERASE_WHEN_DONE = True


def select_menu(
    rows: Rows | Callable[[], Rows],
    header: list[str] | None = None,
    selected: int = 0,
    actions: dict[str, Callable[[], None]] | None = None,
    hidden_keys: Iterable[str] = (),
    esc_key: str | None = None,
    side_actions: dict[str, Callable[[], None]] | None = None,
    help_lines: list[tuple[str, str]] | None = None,
    interactive: bool | None = None,
    ask_fn: Callable[[str], str] | None = None,
    detail: Callable[[str], list[str]] | None = None,
) -> tuple[str, int]:
    """Show a menu in place and return (chosen key, cursor index).

    ↑↓ move, Enter/Space pick the row (with `detail`, Space instead toggles a short explanation
    of the selected row under the menu, following the cursor), a row hotkey picks it directly, `hidden_keys` return
    as-is. `actions` (by key) and `side_actions` (←/→ on the selected row) run in place and
    rebuild `rows`. Esc returns `esc_key` (None = ignored). `?` toggles the key list
    (`help_lines`, or the rows' hotkeys). Header lines may contain ANSI colors.
    Without a console it prints a text list and reads one answer via `ask_fn` ("" on empty).
    """
    build = rows if callable(rows) else (lambda: rows)
    actions = actions or {}
    side_actions = side_actions or {}
    hidden = {k.upper() for k in hidden_keys}
    header = header or []
    state = {"rows": build(), "sel": selected, "help": False, "detail": False}
    selectable = [i for i, r in enumerate(state["rows"]) if r is not None]
    if state["sel"] not in selectable:
        state["sel"] = selectable[0]

    def keys() -> list[str]:
        return [r[0] if r else "" for r in state["rows"]]

    def label_width() -> int:
        return max((len(r[1]) + 2 for r in state["rows"] if r and r[2]), default=16)

    def help_items() -> list[tuple[str, str]]:
        return help_lines or [(r[0], r[1]) for r in state["rows"] if r]

    if interactive is None:
        interactive = is_interactive()
    if not interactive:
        return _plain_menu(state, build, header, keys, label_width, help_items, actions, ask_fn or ask)

    def render() -> StyleAndTextTuples:
        frags: StyleAndTextTuples = []
        for line in header:
            frags += to_formatted_text(ANSI(line)) + [("", "\n")]
        lw = label_width()
        for i, r in enumerate(state["rows"]):
            if r is not None:
                _, label, value, style = r
                on = i == state["sel"]
                frags += [("class:marker", " ▸ ") if on else ("", "   "), ("class:selected" if on else "", label)]
                if value:
                    frags += [("", " " * max(1, lw - len(label))), (_style_classes(style), fit(value, MAX_WIDTH - lw - 3))]
            frags.append(("", "\n"))
        if state["detail"] and detail is not None:
            for line in detail(keys()[state["sel"]]):
                frags += [("", "\n"), ("class:dim", fit("  " + line, MAX_WIDTH))]
        if state["help"]:
            frags.append(("", "\n"))
            for k, desc in help_items():
                frags += [("", "\n   "), ("class:key", f"{k:<10}"), ("", fit(f" {desc}", MAX_WIDTH - 13))]
        return frags

    kb = KeyBindings()

    def pick(event: KeyPressEvent, key: str) -> None:
        if key in actions:
            actions[key]()
            state["rows"] = build()
        else:
            event.app.exit(result=(key, state["sel"]))

    def move(step: int) -> None:
        pos = selectable.index(state["sel"]) + step
        state["sel"] = selectable[pos % len(selectable)]

    kb.add("up")(lambda e: move(-1))
    kb.add("down")(lambda e: move(1))

    @kb.add("left")
    @kb.add("right")
    def _side(event: KeyPressEvent) -> None:
        key = keys()[state["sel"]]
        if key in side_actions:
            side_actions[key]()
            state["rows"] = build()

    @kb.add("enter")
    def _enter(event: KeyPressEvent) -> None:
        pick(event, keys()[state["sel"]])

    @kb.add(" ")
    def _space(event: KeyPressEvent) -> None:
        if detail is not None:
            state["detail"] = not state["detail"]
        else:
            pick(event, keys()[state["sel"]])

    @kb.add("escape", eager=True)
    def _esc(event: KeyPressEvent) -> None:
        if state["help"]:
            state["help"] = False
        elif esc_key is not None:
            event.app.exit(result=(esc_key, state["sel"]))

    @kb.add("c-c")
    def _ctrl_c(event: KeyPressEvent) -> None:
        event.app.exit(exception=KeyboardInterrupt())

    @kb.add("<any>")
    def _char(event: KeyPressEvent) -> None:
        data = event.data
        if len(data) != 1 or not data.isprintable():
            return
        if data == "?":  # Shift+/ in EN, Shift+7 in RU
            state["help"] = not state["help"]
            return
        key = normalize_key(data)
        if key in keys():
            state["sel"] = keys().index(key)
            pick(event, key)
        elif key in hidden or key in actions:
            pick(event, key)

    control = FormattedTextControl(render, show_cursor=False)
    app: Application[tuple[str, int]] = Application(
        layout=Layout(HSplit([Window(control, dont_extend_height=True, wrap_lines=False)])),
        key_bindings=kb,
        style=_PT_STYLE,
        full_screen=False,
        mouse_support=False,
        erase_when_done=ERASE_WHEN_DONE,
    )
    app.ttimeoutlen = 0.05
    return app.run()


def _plain_menu(state, build, header, keys, label_width, help_items, actions, ask_fn) -> tuple[str, int]:
    """Non-interactive menu: text list plus one answer per round."""
    out = _out()
    while True:
        for line in header:
            out.print(Text.from_ansi(line))
        lw = label_width()
        for r in state["rows"]:
            if r is None:
                out.print()
                continue
            line = Text.assemble(" ", (f"[{r[0]}]", "key"), " ", f"{r[1]:<{lw}}")
            if r[2]:
                line.append(" " + r[2], style=r[3] or None)
            out.print(line)
        choice = ask_fn("Выберите действие").strip()
        if choice == "?":
            for k, desc in help_items():
                out.print(Text.assemble("   ", (f"{k:<10}", "key"), f" {desc}"))
            continue
        choice = normalize_key(choice) if len(choice) == 1 else choice.upper()
        if choice in actions:
            actions[choice]()
            state["rows"] = build()
            continue
        ks = keys()
        return choice, ks.index(choice) if choice and choice in ks else state["sel"]


# --------------------------------------------------------------------------- output helpers


def fmt(markup: str) -> str:
    """Render Rich markup (theme names allowed: [ok], [warn], [dim]...) to an ANSI string."""
    out = _out()
    with out.capture() as cap:
        out.print(markup, end="")
    return cap.get()


def section_header(title: str, subtitle: str = "") -> None:
    """Bold title, optional dim subtitle on the same line, thin rule under it."""
    out = _out()
    line = Text.assemble((f" {title}", "title"), (f"  {subtitle}" if subtitle else "", "dim"))
    line.truncate(out.width, overflow="ellipsis")
    out.print(line)
    out.print(Rule(style="dim", characters="─"))


def panel(title: str, lines: list[str]) -> None:
    """Light titled block: a thin rule with the title, then markup lines indented by one space."""
    out = _out()
    out.print(Rule(Text(title, style="title"), align="left", style="dim", characters="─"))
    for line in lines:
        out.print(f" {line}")


def kv_table(rows: list[tuple[str, str]] | list[tuple[str, str, str]]) -> None:
    """Borderless two-column table: dim key, value (optional third item = value style)."""
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column(overflow="ellipsis", no_wrap=True)
    for row in rows:
        table.add_row(f" {row[0]}", Text(str(row[1]), style=row[2] if len(row) > 2 else ""))
    _out().print(table)


_STATUS_MARKS = {"ok": ("●", "ok"), "warn": ("!", "warn"), "err": ("×", "err"), "info": ("·", "accent")}


_last_status: tuple[str, str] | None = None


def status_line(kind: str, text: str) -> None:
    """One-line result: kind is ok / warn / err / info."""
    global _last_status
    _last_status = (kind, text)
    mark, style = _STATUS_MARKS.get(kind, ("·", "dim"))
    _out().print(Text.assemble(" ", (mark, style), " ", text))


def take_status() -> tuple[str, str] | None:
    """The last status line printed since the previous call (None if none); lets a screen redraw
    itself clean and show only the latest result instead of stacking them under old menus."""
    global _last_status
    status, _last_status = _last_status, None
    return status


def ask(question: str, default: str | None = None) -> str:
    """Read a line; empty answer returns `default`. Ctrl+C / EOF raise WizardBack."""
    suffix = f" [{default}]" if default else ""
    try:
        val = _out().input(Text.assemble(("?> ", "accent"), question, (suffix, "dim"), ": ")).strip()
    except (KeyboardInterrupt, EOFError):
        status_line("warn", "Операция отменена.")
        raise WizardBack()
    return default if not val and default is not None else val


def confirm(question: str, default: bool = False) -> bool:
    """Yes/no question accepting y / yes / да / д."""
    answer = ask(f"{question} ({'Y/n' if default else 'y/N'})", "y" if default else "n").lower()
    return answer in {"y", "yes", "да", "д"}


def pause() -> None:
    """Wait for Enter before the screen is cleared."""
    ask("Нажмите Enter для продолжения", "")
