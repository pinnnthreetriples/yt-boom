"""The tabbed console shell: one screen, tabs on top, rows of icons, one hint line at the bottom.

Keys are the same everywhere: Tab / Shift+Tab (or the tab's number) switch tabs, ↑↓ move, ←→ change the value of the row,
Space shows the row's details, Enter opens its actions, Esc goes back, ? lists the keys, Q quits.
In a text field the keys edit the text instead: ←→ (Ctrl: by words), Home/End and ↑↓ move the cursor, typing and
pasting go in at it, Backspace/Delete remove a whole character (an emoji included) — see `line_editor`.

A tab builds a `View` (rows and hotkeys) from the current data. Enter on a row returns a step: an inline `Menu`
under the row, an inline `Field` or `Confirm`, a nested `Screen`, a `Notice`, or a `Run` — blocking work (a browser,
a network check) that runs outside the menu while the terminal shows its progress; the shell comes back afterwards
with the result as the notice. `Controller` holds the state and handles keys, `render` draws it, `run_shell` binds
both to prompt_toolkit; tests drive `Controller.press` directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Union

from prompt_toolkit.utils import get_cwidth

from modules.ui.line_editor import LineEditor, char_width, chars, typeable

BASE_WIDTH = 76  # the layout is drawn for this many columns
WIDTH = BASE_WIDTH  # run_shell stretches it to the window; cells marked `grow` take the extra room

Frag = tuple[str, str]  # (style names, text)


@dataclass
class Cell:
    text: str
    style: str = ""
    width: int = 0  # columns the cell takes; 0 = its own width
    grow: bool = False  # a wide window adds its extra columns to this cell (a long text column)


@dataclass
class Notice:
    kind: str  # ok / warn / err / info
    text: str


@dataclass
class Row:
    id: str
    cells: list[Cell]
    hint: str = ""  # bottom line while the row is selected: what its icons mean
    detail: Callable[[], list[list[Cell]]] | None = None  # Space: lines under the row
    values: list[str] | None = None  # ←→ cycles them; the shown value is a cell made by the tab
    value_index: int = 0
    on_value: Callable[[int], "Notice | None"] | None = None
    enter: Callable[[], "Step"] | None = None
    selectable: bool = True
    space: Callable[[], "Step"] | None = None  # Space does this instead of showing details (a tick on a list)


@dataclass
class Menu:
    rows: list[Row]
    rebuild: Callable[[], "Menu"] | None = None  # after a value changes in the menu, its rows are built again


@dataclass
class Field:
    """Inline text input under the row. `multiline`: Enter keeps a line, Enter on an empty line submits."""

    prompt: str
    submit: Callable[[str], "Step"]
    initial: str = ""
    secret: bool = False
    multiline: bool = False
    preview: Callable[[list[str]], list[list[Cell]]] | None = None  # live lines under a multi-line field


@dataclass
class Confirm:
    question: list[Cell]
    choices: list[str]
    pick: Callable[[int], "Step"]
    selected: int = 0  # the safe answer goes first and is selected


@dataclass
class Screen:
    build: Callable[[], "View"]


@dataclass
class Run:
    """Blocking work outside the menu; what it returns is shown next (a Notice, or a question, a screen…)."""

    work: Callable[[], "Step"]
    title: str = ""
    row_id: str = ""  # filled by the shell: a question returned by the work is shown under this row


@dataclass
class Back:
    """Leave the current nested screen."""

    notice: Notice | None = None


@dataclass
class GoTab:
    """Switch to another tab, by its name («Цель») or its position (the first-run list on «Запуск» leads to the tab
    that needs setting up)."""

    tab: int | str
    notice: Notice | None = None  # shown on the tab it leads to


Step = Union[Menu, Field, Confirm, Screen, Run, Notice, Back, GoTab, None]


@dataclass
class Hotkey:
    key: str  # one character (upper case) or a prompt_toolkit key name like "delete"
    action: Callable[["Row | None"], Step]


@dataclass
class View:
    rows: list[Row]
    keys: str = ""  # the hint of keys on the right of the bottom line
    title: list[Cell] = field(default_factory=list)  # a nested screen's path, e.g. «vonjimonji › профиль»
    top: list[list[Cell]] = field(default_factory=list)  # lines above the rows
    bottom: list[list[Cell]] = field(default_factory=list)  # lines under the rows
    hotkeys: list[Hotkey] = field(default_factory=list)
    marks: Callable[[list[str]], list[Row]] | None = None  # X: tick rows, then Enter offers these actions


@dataclass
class Tab:
    name: str
    icon: str
    build: Callable[[], View]
    warn: Callable[[], bool] = lambda: False


# --------------------------------------------------------------------------- text helpers


def width(text: str) -> int:
    return get_cwidth(text)


def fit(text: str, cols: int) -> str:
    """Cut text to `cols` columns with an ellipsis (emoji count as two)."""
    if width(text) <= cols:
        return text
    out, used = "", 0
    for ch in text:
        w = get_cwidth(ch)
        if used + w > cols - 1:
            break
        out, used = out + ch, used + w
    return out + "…"


def cells_frags(cells: list[Cell], extra_style: str = "") -> list[Frag]:
    frags: list[Frag] = []
    for i, cell in enumerate(cells):
        text = cell.text
        if cell.width:
            cols = cell.width + (max(0, WIDTH - BASE_WIDTH) if cell.grow else 0)
            text = fit(text, cols - 1)  # a cut text never touches the next column
            text += " " * (cols - width(text))
        elif i < len(cells) - 1:
            text += " "
        style = " ".join(f"class:{s}" for s in f"{cell.style} {extra_style}".split())
        frags.append((style, text))
    return frags


def frags_text(frags: list[Frag]) -> str:
    return "".join(text for _, text in frags)


# --------------------------------------------------------------------------- state


EDIT_INDENT = "      "
EDIT_MARK = "› "


def edit_columns() -> int:
    """Columns a field's text wraps at: the window less the indent, the «› » mark and one spare column."""
    return max(10, WIDTH - width(EDIT_INDENT) - width(EDIT_MARK) - 1)


@dataclass
class _Edit:
    row_id: str
    field: Field
    buffer: LineEditor
    lines: list[str]

    @property
    def text(self) -> str:
        return self.buffer.text


class Controller:
    """All state of the shell and every key; `render` reads it, `run_shell` feeds it keys."""

    def __init__(self, tabs: list[Tab], status: Callable[[], list[Cell]] | None = None, tab: int = 0):
        self.tabs = tabs
        self.status = status or (lambda: [])
        self.tab = tab
        self.stacks: list[list[Screen]] = [[] for _ in tabs]
        self.selected: dict[tuple[int, int], str] = {}  # (tab, depth) -> row id
        self.expanded: set[str] = set()
        self.menu: Menu | None = None
        self.menu_row = ""
        self.menu_sel = 0
        self.edit: _Edit | None = None
        self.confirm: Confirm | None = None
        self.confirm_row = ""
        self.marking = False
        self.marked: list[str] = []
        self.help = False
        self.notice: Notice | None = None
        self.pending_run: Run | None = None
        self.quit = False
        self.on_icons: Callable[[], Notice | None] | None = None  # I: switch colour / plain icons
        self.view = View(rows=[])
        self.status_now: list[Cell] = []
        self.warns: list[bool] = [False] * len(tabs)
        self.refresh()

    # ---- views

    def _depth(self) -> int:
        return len(self.stacks[self.tab])

    def _build(self) -> View:
        stack = self.stacks[self.tab]
        try:
            return stack[-1].build() if stack else self.tabs[self.tab].build()
        except Exception as exc:  # a broken tab must not close the program
            return View(rows=[], top=[[Cell("Ошибка экрана: " + fit(f"{exc.__class__.__name__}: {exc}", 60), "err")]])

    def refresh(self) -> None:
        """Build the view, the status and the tab marks again (after every action: data may have changed)."""
        self.view = self._build()
        try:
            self.status_now = self.status()
            self.warns = [tab.warn() for tab in self.tabs]
        except Exception:
            self.status_now = []
        rows = self.selectable_rows()
        if rows and self.current_id() not in {r.id for r in rows}:
            self.selected[(self.tab, self._depth())] = rows[0].id

    def selectable_rows(self) -> list[Row]:
        return [r for r in self.view.rows if r.selectable]

    def current_id(self) -> str:
        rows = self.selectable_rows()
        sid = self.selected.get((self.tab, self._depth()))
        if sid is None and rows:
            sid = rows[0].id
        return sid or ""

    def current_row(self) -> Row | None:
        sid = self.current_id()
        return next((r for r in self.view.rows if r.id == sid), None)

    def select(self, row_id: str) -> None:
        self.selected[(self.tab, self._depth())] = row_id

    def _close_overlays(self) -> None:
        self.menu, self.edit, self.confirm, self.help = None, None, None, False
        self.marking, self.marked = False, []

    # ---- steps

    def apply(self, step: Step, row_id: str = "") -> None:
        """Show what an action returned."""
        if isinstance(step, Notice):
            self.menu, self.edit, self.confirm = None, None, None
            self.marking, self.marked = False, []  # a bulk action is done with its rows
            self.notice = step
            self.refresh()
        elif isinstance(step, Menu):
            self.menu, self.menu_row, self.menu_sel = step, row_id, 0
            self.edit, self.confirm = None, None
        elif isinstance(step, Field):
            self.edit = _Edit(row_id, step, LineEditor(step.initial), [])
            self.confirm = None
        elif isinstance(step, Confirm):
            self.confirm, self.confirm_row = step, row_id
            self.edit = None
        elif isinstance(step, Screen):
            self._close_overlays()
            self.stacks[self.tab].append(step)
            self.refresh()
        elif isinstance(step, Back):
            self._close_overlays()
            if self.stacks[self.tab]:
                self.stacks[self.tab].pop()
            self.notice = step.notice
            self.refresh()
        elif isinstance(step, GoTab):
            self._close_overlays()
            self.expanded.clear()
            self.tab = self._tab_index(step.tab)
            self.notice = step.notice
            self.refresh()
        elif isinstance(step, Run):
            self.menu, self.edit, self.confirm = None, None, None
            step.row_id = row_id
            self.pending_run = step
        else:  # None: the action closed itself
            self.menu, self.edit, self.confirm = None, None, None
            self.refresh()

    def _tab_index(self, tab: int | str) -> int:
        if isinstance(tab, str):
            return next((i for i, t in enumerate(self.tabs) if t.name == tab), self.tab)
        return tab if 0 <= tab < len(self.tabs) else self.tab

    def finish_run(self, result: Step) -> None:
        """The outer loop ran the work: show what it returned (a Notice, or the next step)."""
        run, self.pending_run = self.pending_run, None
        self.refresh()
        self.apply(result, run.row_id if run else "")

    # ---- keys

    def press(self, key: str) -> None:
        """One key: 'up', 'down', 'left', 'right', 'enter', 'space', 'escape', 'tab', 's-tab', 'backspace',
        'delete', a text field's 'home' / 'end' / 'c-left' / 'c-right' / 'c-w' / 'c-delete' / 'c-u' / 'c-k' / 'c-a' /
        'c-e', or a printable character; pasted text arrives as 'paste:<text>'."""
        if key != "paste" and not key.startswith("paste:"):
            self.notice = None if not (self.edit or self.confirm) else self.notice
        if self.help:
            self.help = False
            return
        if self.edit:
            self._key_edit(key)
        elif self.confirm:
            self._key_confirm(key)
        elif self.menu:
            self._key_menu(key)
        else:
            self._key_rows(key)

    def _move(self, rows: list[Row], current: str, step: int) -> str:
        ids = [r.id for r in rows]
        if not ids:
            return current
        pos = ids.index(current) if current in ids else 0
        return ids[(pos + step) % len(ids)]

    def _cycle(self, row: Row, step: int) -> None:
        if not row.values:
            return
        index = (row.value_index + step) % len(row.values)
        notice = row.on_value(index) if row.on_value else None
        row.value_index = index
        if notice:
            self.notice = notice

    def _key_rows(self, key: str) -> None:
        row = self.current_row()
        if key in {"up", "down"}:
            self.select(self._move(self.selectable_rows(), self.current_id(), -1 if key == "up" else 1))
        elif key in {"tab", "s-tab"} or (len(key) == 1 and key in "123456789" and int(key) <= len(self.tabs)):
            self._close_overlays()
            self.expanded.clear()
            if key == "tab":
                self.tab = (self.tab + 1) % len(self.tabs)
            elif key == "s-tab":
                self.tab = (self.tab - 1) % len(self.tabs)
            else:
                self.tab = int(key) - 1
            self.refresh()
        elif key in {"left", "right"} and row:
            self._cycle(row, -1 if key == "left" else 1)
            self.refresh()
        elif key == "space" and row:
            if self.marking:
                self.marked = [m for m in self.marked if m != row.id] if row.id in self.marked else [*self.marked, row.id]
            elif row.space:
                self.apply(row.space(), row.id)
            elif row.detail:
                self.expanded ^= {row.id}
        elif key == "enter" and row:
            if self.marking and self.view.marks:
                if not self.marked:
                    self.marked = [row.id]
                self.apply(Menu(self.view.marks(list(self.marked))), row.id)
            elif row.enter:
                self.apply(row.enter(), row.id)
            elif row.values:
                self._cycle(row, 1)
                self.refresh()
        elif key == "escape":
            if self.marking:
                self.marking, self.marked = False, []
            elif self._depth():
                self.apply(Back())
        elif key == "?":
            self.help = True
        elif key.upper() == "X" and self.view.marks:
            self.marking = not self.marking
            self.marked = []
        elif key.upper() == "Q" and self._depth() == 0:
            self.quit = True
        elif key.upper() == "I" and self.on_icons and not any(h.key == "I" for h in self.view.hotkeys):
            self.notice = self.on_icons()
            self.refresh()
        else:
            hotkey = next((h for h in self.view.hotkeys if h.key == (key.upper() if len(key) == 1 else key)), None)
            if hotkey:
                self.apply(hotkey.action(row), row.id if row else "")

    def _key_menu(self, key: str) -> None:
        assert self.menu is not None
        rows = [r for r in self.menu.rows if r.selectable]
        if not rows:
            self.menu = None
            return
        self.menu_sel = min(self.menu_sel, len(rows) - 1)
        row = rows[self.menu_sel]
        if key in {"up", "down"}:
            self.menu_sel = (self.menu_sel + (-1 if key == "up" else 1)) % len(rows)
        elif key in {"left", "right"}:
            self._cycle(row, -1 if key == "left" else 1)
            self._rebuild_menu()
        elif key == "space" and row.detail:
            self.expanded ^= {row.id}
        elif key == "enter":
            if row.enter:
                self.apply(row.enter(), self.menu_row)
            elif row.values:
                self._cycle(row, 1)
                self._rebuild_menu()
        elif key == "escape":
            self.menu = None
            self.marking, self.marked = False, []
            self.refresh()

    def _rebuild_menu(self) -> None:
        """A value changed inside a menu: rebuild the view and the menu (its rows show the new values)."""
        self.refresh()
        if self.menu is not None and self.menu.rebuild:
            self.menu = self.menu.rebuild()

    def _key_confirm(self, key: str) -> None:
        assert self.confirm is not None
        n = len(self.confirm.choices)
        if key in {"left", "right", "up", "down", "tab"}:
            self.confirm.selected = (self.confirm.selected + (-1 if key in {"left", "up"} else 1)) % n
        elif key == "enter":
            confirm, self.confirm = self.confirm, None
            self.menu = None
            self.apply(confirm.pick(confirm.selected), self.confirm_row)
        elif key == "escape":
            self.confirm = None

    def _key_edit(self, key: str) -> None:
        """A key in a text field: typing and pasting go in at the cursor, which the arrows, Home/End and the word
        keys move (see `LineEditor`); Enter saves (a multi-line field: keeps the line), Esc cancels."""
        assert self.edit is not None
        edit = self.edit
        buffer = edit.buffer
        if key.startswith("paste:"):
            from config import repair_text

            # a tab is a control code `insert` would drop, gluing «почта\tпароль» from a spreadsheet: it becomes a space
            text = repair_text(key[6:]).replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
            parts = text.split("\n")
            if edit.field.multiline and len(parts) > 1:
                tail = buffer.text[buffer.cursor:]
                buffer.set(buffer.text[: buffer.cursor] + parts[0])
                for part in parts[1:]:
                    if buffer.text.strip():
                        edit.lines.append(buffer.text.strip())
                    buffer.set(part)
                buffer.text, buffer.cursor = buffer.text + tail, len(buffer.text)
            else:
                buffer.insert(" ".join(p for p in parts if p))
        elif key == "escape":
            self.edit = None
        elif key == "enter":
            if edit.field.multiline and buffer.text.strip():
                edit.lines.append(buffer.text.strip())
                buffer.set("")
                return
            value = "\n".join(edit.lines) if edit.field.multiline else buffer.text.strip()
            self.edit = None
            self.menu = None
            self.apply(edit.field.submit(value), edit.row_id)
        elif key == "space":
            buffer.insert(" ")
        elif buffer.press(key, edit_columns()):
            pass
        elif len(key) == 1 and typeable(key):
            buffer.insert(key)


# --------------------------------------------------------------------------- render

_NOTICE_STYLE = {"ok": "ok", "warn": "warn", "err": "err", "info": "accent"}


def _row_frags(row: Row, selected: bool, marked: bool | None, indent: str = "") -> list[Frag]:
    marker = (" ▸ ", "class:marker") if selected else ("   ", "")
    frags: list[Frag] = [("", indent), (marker[1], marker[0])]
    if marked is not None:
        frags.append(("class:ok" if marked else "class:dim", "▣ " if marked else "▢ "))
    body = cells_frags(row.cells, "selected" if selected else "")
    if row.values and selected:
        body.append(("class:dim", " ‹›"))
    return frags + body


def render(c: Controller) -> list[Frag]:
    """The whole screen as prompt_toolkit fragments."""
    out: list[Frag] = []

    def line(frags: list[Frag]) -> None:
        out.extend(frags)
        out.append(("", "\n"))

    # tabs (every one named, the current one highlighted) + status on the right, or under them if no room
    tabs: list[Frag] = [("", " ")]
    for i, tab in enumerate(c.tabs):
        on = i == c.tab
        tabs.append(("class:tab-on" if on else "class:tab", f" {tab.icon} {tab.name} "))
        tabs.append(("class:warn", "•" if c.warns[i] else " "))
    status = cells_frags(c.status_now)
    gap = WIDTH - width(frags_text(tabs)) - width(frags_text(status))
    if gap >= 2:
        line(tabs + [("", " " * gap)] + status)
    else:
        line(tabs)
        line([("", " " * max(1, WIDTH - width(frags_text(status))))] + status)
    line([])

    view = c.view
    if view.title:
        line([("", "   ")] + cells_frags(view.title, "dim"))
        line([])
    for top in view.top:
        line([("", "   ")] + cells_frags(top))
    if view.top:
        line([])

    current = c.current_id()
    for row in view.rows:
        if not row.selectable:
            line([("", "   ")] + cells_frags(row.cells))
            continue
        selected = row.id == current and c.menu is None
        marked = (row.id in c.marked) if c.marking else None
        line(_row_frags(row, selected, marked))
        if row.id in c.expanded and row.detail:
            for d in row.detail():
                line([("", "      ")] + cells_frags(d, "dim"))
        if c.menu is not None and row.id == c.menu_row:
            menu_rows = [r for r in c.menu.rows if r.selectable]
            for i, m in enumerate(c.menu.rows):
                on = m.selectable and menu_rows.index(m) == min(c.menu_sel, len(menu_rows) - 1)
                line(_row_frags(m, on, None, indent="    ") if m.selectable else [("", "       ")] + cells_frags(m.cells))
                if m.id in c.expanded and m.detail:
                    for d in m.detail():
                        line([("", "          ")] + cells_frags(d, "dim"))
        if c.confirm is not None and row.id == c.confirm_row:
            line([("", "      ")] + _confirm_frags(c.confirm))
        if c.edit is not None and row.id == c.edit.row_id:
            _render_edit(c.edit, line)
    row_ids = {r.id for r in view.rows}
    if c.confirm is not None and c.confirm_row not in row_ids:  # a question after a Run whose row is gone
        line([("", "   ")] + _confirm_frags(c.confirm))
    if c.edit is not None and c.edit.row_id not in row_ids:
        _render_edit(c.edit, line)
    if c.marking and view.marks:
        line([])
        line([("", "   "), ("class:dim", f"отмечено {len(c.marked)} · Пробел — отметить · Enter — что сделать · Esc — отмена")])
    for bottom in view.bottom:
        line([("", "   ")] + cells_frags(bottom))

    if c.help:
        line([])
        for keys, text in HELP:
            line([("", "   "), ("class:key", f"{keys:<10}"), ("", text.replace("{tabs}", str(len(c.tabs))))])

    # bottom line: notice or hint on the left, keys on the right
    line([])
    line([("class:dim", "─" * WIDTH)])
    if c.notice:
        left: list[Frag] = [("class:" + _NOTICE_STYLE.get(c.notice.kind, "dim"), " " + c.notice.text)]
    else:
        row = c.current_row()
        hint = ""
        if c.menu is not None:
            menu_rows = [r for r in c.menu.rows if r.selectable]
            if menu_rows:
                hint = menu_rows[min(c.menu_sel, len(menu_rows) - 1)].hint
        elif row:
            hint = row.hint
        left = [("class:dim", " " + hint)] if hint else []
    keys = _keys_hint(c)
    if WIDTH - width(frags_text(left)) - width(keys) < 2 and keys.endswith(" · ? клавиши"):
        keys = keys[: -len(" · ? клавиши")]  # short of room: «?» still works, it just is not written
    gap = WIDTH - width(frags_text(left)) - width(keys)
    if gap < 2 and width(frags_text(left)) > WIDTH:
        left = [(left[0][0], fit(frags_text(left), WIDTH))]
    if gap < 2:
        line(left)
        line([("", " " * max(0, WIDTH - width(keys))), ("class:dim", keys)])
    else:
        line(left + [("", " " * gap), ("class:dim", keys)])
    return out


def _confirm_frags(confirm: Confirm) -> list[Frag]:
    frags: list[Frag] = cells_frags(confirm.question) + [("", "   ")]
    for i, choice in enumerate(confirm.choices):
        on = i == confirm.selected
        frags += [("class:marker" if on else "", "▸ " if on else "  "), ("class:selected" if on else "", choice), ("", "   ")]
    return frags


def _render_edit(edit: _Edit, line: Callable[[list[Frag]], None]) -> None:
    """The field: its prompt, the lines already given (a multi-line field), then the text being typed, wrapped by
    whole characters to the window, with the cursor drawn on the character it stands before («▏» at the end)."""
    field_ = edit.field
    line([("", EDIT_INDENT), ("class:accent", "+ " if field_.multiline else ""), ("class:dim", field_.prompt)])
    for done in edit.lines:
        line([("", EDIT_INDENT), ("class:accent", EDIT_MARK), ("", _masked(done) if field_.secret else done)])
    buffer = edit.buffer
    rows = buffer.rows(edit_columns())
    if len(rows) > 1 and rows[-1][0] == rows[-1][1] == len(buffer.text) and buffer.cursor != len(buffer.text):
        rows = rows[:-1]  # the spare row only holds a cursor standing at the very end
    for index, (start, end) in enumerate(rows):
        mark = ("class:accent", EDIT_MARK) if index == 0 else ("", " " * width(EDIT_MARK))
        frags: list[Frag] = [("", EDIT_INDENT), mark]
        before, after = buffer.text[start:min(max(buffer.cursor, start), end)], ""
        on_row = start <= buffer.cursor < end or (buffer.cursor == end and index == len(rows) - 1)
        if not on_row:
            frags.append(("", _shown(buffer.text[start:end], field_.secret)))
        elif buffer.cursor == len(buffer.text):
            frags += [("", _shown(before, field_.secret)), ("class:cursor", "▏")]
        else:
            under = chars(buffer.text[buffer.cursor:end])[0]
            after = buffer.text[buffer.cursor + len(under):end]
            frags += [("", _shown(before, field_.secret)), ("class:cursor-on", _shown(under, field_.secret)),
                      ("", _shown(after, field_.secret))]
        line(frags)
    if field_.preview:
        lines = [*edit.lines, *([buffer.text.strip()] if buffer.text.strip() else [])]
        for p in field_.preview(lines):
            line([("", EDIT_INDENT)] + cells_frags(p))


def _masked(text: str) -> str:
    return "•" * len(chars(text))


def _shown(text: str, secret: bool) -> str:
    return _masked(text) if secret else text


def _keys_hint(c: Controller) -> str:
    if c.edit:
        if c.edit.field.multiline:
            return "←→ курсор · Enter — строка · пустая Enter — готово · Esc"
        return "←→ курсор · Home/End · Enter сохранить · Esc отмена"
    if c.confirm:
        return "←→ выбор · Enter"
    if c.menu:
        return "←→ значение · Enter · Esc закрыть"
    if c.help:
        return "любая клавиша — закрыть"
    keys = c.view.keys
    if c._depth():
        keys = (keys + " · " if keys else "") + "Esc назад"
    else:
        keys = "Tab вкладки · " + keys
    return (keys + " · ? клавиши").strip(" ·")


HELP: list[tuple[str, str]] = [
    ("Tab", "следующая вкладка (Shift+Tab — назад, 1–{tabs} — сразу)"),
    ("↑ ↓", "выбрать строку"),
    ("← →", "сменить значение в строке"),
    ("Пробел", "подробнее о строке"),
    ("Enter", "действия / изменить"),
    ("Esc", "назад / отмена"),
    ("X", "отметить несколько (где можно)"),
    ("I", "значки: цветные / простые"),
    ("Q", "выход"),
]


# --------------------------------------------------------------------------- prompt_toolkit


STYLE = {
    "ok": "ansigreen", "warn": "ansiyellow", "err": "ansired bold", "dim": "ansibrightblack",
    "accent": "ansicyan", "key": "ansiyellow bold", "marker": "ansicyan bold", "selected": "bg:#3a3a3a",
    "tab": "", "tab-on": "bg:#4e4e4e", "cursor": "ansicyan", "cursor-on": "reverse",
}


def run_shell(controller: Controller, on_run: Callable[[Run], Step], clear: Callable[[], None]) -> None:
    """Show the shell until Q; a `Run` step leaves the menu, runs through `on_run` and comes back."""
    from concurrent.futures import ThreadPoolExecutor

    from prompt_toolkit.application import get_app
    from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.styles import Style

    from modules.tui import normalize_key

    kb = KeyBindings()

    def feed(key: str, event: KeyPressEvent) -> None:
        # tabs read the database through asyncio.run, which cannot run inside prompt_toolkit's own event loop
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(controller.press, key).result()
        if controller.quit or controller.pending_run:
            event.app.exit()

    names = {"up": "up", "down": "down", "left": "left", "right": "right", "enter": "enter", "c-m": "enter",
             "tab": "tab", "s-tab": "s-tab", "backspace": "backspace", "c-h": "backspace", "delete": "delete",
             # the text field's cursor keys (`LineEditor`); elsewhere they do nothing
             "home": "home", "end": "end", "c-left": "c-left", "c-right": "c-right", "c-delete": "c-delete",
             "c-w": "c-w", "c-u": "c-u", "c-k": "c-k", "c-a": "c-a", "c-e": "c-e"}
    for pt_key, name in names.items():
        kb.add(pt_key)(lambda e, n=name: feed(n, e))
    kb.add(" ")(lambda e: feed("space", e))
    kb.add("escape", eager=True)(lambda e: feed("escape", e))

    @kb.add(Keys.BracketedPaste)
    def _paste(event: KeyPressEvent) -> None:
        feed("paste:" + event.data, event)

    @kb.add("c-c")
    def _ctrl_c(event: KeyPressEvent) -> None:
        controller.quit = True
        event.app.exit()

    high: list[str] = []  # the Windows console sends an emoji as two keys: its UTF-16 surrogate halves

    @kb.add("<any>")
    def _char(event: KeyPressEvent) -> None:
        from config import repair_text

        data = event.data
        if len(data) == 1 and 0xD800 <= ord(data) <= 0xDBFF:
            high[:] = [data]
            return
        if high:
            data, high[:] = repair_text(high[0] + data), []
        if len(data) == 1 and 0xDC00 <= ord(data) <= 0xDFFF:
            return  # a lone half
        if controller.edit:
            if len(data) == 1 and typeable(data):
                feed(data, event)  # the zero-width joiner of an emoji family is not `isprintable`, yet belongs here
            elif len(data) > 1:  # a paste without bracketed paste mode, or an emoji sequence sent at once
                feed("paste:" + data, event)
        elif len(data) == 1 and data.isprintable():
            feed("?" if data == "?" else normalize_key(data), event)

    def draw() -> list[Frag]:
        global WIDTH
        try:
            WIDTH = max(BASE_WIDTH, get_app().output.get_size().columns - 1)
        except Exception:
            WIDTH = BASE_WIDTH
        # a row's details (Space) may read the database through asyncio.run, too: draw outside the event loop
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(render, controller).result()

    style = Style.from_dict(STYLE)
    try:
        _loop(controller, on_run, clear, kb, style, draw)
    finally:
        _reset_width()


def _reset_width() -> None:
    global WIDTH
    WIDTH = BASE_WIDTH


def _loop(controller: Controller, on_run: Callable[[Run], Step], clear: Callable[[], None], kb, style, draw) -> None:
    from prompt_toolkit.application import Application
    from prompt_toolkit.layout import HSplit, Layout, Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    from modules.logger_config import console_held

    while not controller.quit:
        clear()
        app: Application[None] = Application(
            layout=Layout(HSplit([Window(FormattedTextControl(draw, show_cursor=False),
                                         dont_extend_height=True, wrap_lines=False)])),
            key_bindings=kb, style=style, full_screen=False, mouse_support=False, erase_when_done=True,
        )
        app.ttimeoutlen = 0.05
        with console_held():  # a log line under the screen (e.g. «движок chrome») would break its redraw
            app.run()
        if controller.pending_run and not controller.quit:
            run = controller.pending_run
            clear()
            try:
                result = on_run(run)
            except KeyboardInterrupt:
                result = Notice("warn", "Прервано")
            except Exception as exc:  # the work failed: say so, keep the interface
                result = Notice("err", fit(f"Ошибка: {exc.__class__.__name__}: {exc}", WIDTH - 2))
            controller.finish_run(result)
