"""The text being typed into a shell `Field`: a cursor that moves over whole characters, as a terminal shows them.

A "character" here is what the eye sees as one: an emoji with its skin tone, a ZWJ family (👨‍👩‍👧), a flag (two
regional letters), a heart with its emoji selector (❤️), a letter with a combining accent. The cursor never stops
inside one, Backspace and Delete remove one whole, and the text wraps by terminal columns (an emoji takes two).

Keys (names as `Controller.press` gets them): ←/→ one character, Ctrl+←/→ one word, Home/End (Ctrl+A/Ctrl+E) the
start/end, ↑/↓ the row above/below of a wrapped text, Backspace/Delete one character, Ctrl+W (Ctrl+Backspace) the word
before the cursor, Ctrl+Delete the word after it, Ctrl+U everything before the cursor, Ctrl+K everything after it.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from prompt_toolkit.utils import get_cwidth

ZWJ = "‍"
_SKIN_TONES = range(0x1F3FB, 0x1F400)
_REGIONAL = range(0x1F1E6, 0x1F200)
_TAGS = range(0xE0020, 0xE0080)  # subdivision flags (🏴󠁧󠁢󠁳󠁣󠁴󠁿)


def _extends(ch: str) -> bool:
    """`ch` belongs to the character before it (an accent, a selector, a skin tone, a tag, a joiner)."""
    code = ord(ch)
    return (
        ch == ZWJ
        or unicodedata.category(ch) in ("Mn", "Me", "Mc")
        or 0xFE00 <= code <= 0xFE0F
        or 0xE0100 <= code <= 0xE01EF
        or code in _SKIN_TONES
        or code in _TAGS
    )


def boundaries(text: str) -> list[int]:
    """Indexes where a visible character starts, plus len(text): the places the cursor may stand."""
    stops = [0] if text else []
    regional_run = 0
    for i in range(1, len(text)):
        ch, prev = text[i], text[i - 1]
        regional = ord(ch) in _REGIONAL
        regional_run = regional_run + 1 if ord(prev) in _REGIONAL else 0
        joined = prev == ZWJ or _extends(ch) or (regional and regional_run % 2 == 1)
        if not joined:
            stops.append(i)
    stops.append(len(text))
    return stops


def chars(text: str) -> list[str]:
    """The visible characters of `text` (see `boundaries`)."""
    stops = boundaries(text)
    return [text[a:b] for a, b in zip(stops, stops[1:])]


def char_width(char: str) -> int:
    """Terminal columns of one visible character: an emoji sequence takes two, whatever it is built of."""
    base = get_cwidth(char[0]) if char else 0
    if len(char) > 1 and ("️" in char or ZWJ in char or any(ord(c) in _SKIN_TONES or ord(c) in _REGIONAL for c in char)):
        return 2
    return max(base, 0)


def typeable(ch: str) -> bool:
    """A character the keyboard may put into the text: anything but control codes and lone surrogate halves.

    `str.isprintable` is not enough: it refuses the zero-width joiner that glues an emoji family together."""
    return unicodedata.category(ch) not in ("Cc", "Cs")


def _is_word(char: str) -> bool:
    return char[0].isalnum() or char[0] == "_"


@dataclass
class LineEditor:
    text: str = ""
    cursor: int = -1  # an index into `text`, on a character boundary but while an emoji is typed; -1: at the end

    def __post_init__(self) -> None:
        self.cursor = len(self.text) if self.cursor < 0 else self._snap(self.cursor)

    # ---- positions

    def _snap(self, index: int) -> int:
        """The nearest boundary at or before `index`."""
        return max((b for b in boundaries(self.text) if b <= index), default=0)

    def _stops(self) -> list[int]:
        return boundaries(self.text) or [0]

    def _prev(self) -> int:
        return max((b for b in self._stops() if b < self.cursor), default=0)

    def _next(self) -> int:
        return min((b for b in self._stops() if b > self.cursor), default=len(self.text))

    def _word_left(self) -> int:
        stops = [b for b in self._stops() if b < self.cursor]
        pieces = [(b, self.text[b:self._after(b)]) for b in stops]
        index = len(pieces) - 1
        while index >= 0 and not _is_word(pieces[index][1]):
            index -= 1
        while index >= 0 and _is_word(pieces[index][1]):
            index -= 1
        return pieces[index + 1][0] if index + 1 < len(pieces) else self.cursor

    def _word_right(self) -> int:
        stops = [b for b in self._stops() if b >= self.cursor and b < len(self.text)]
        pieces = [(b, self.text[b:self._after(b)]) for b in stops]
        index = 0
        while index < len(pieces) and not _is_word(pieces[index][1]):
            index += 1
        while index < len(pieces) and _is_word(pieces[index][1]):
            index += 1
        return pieces[index][0] if index < len(pieces) else len(self.text)

    def _after(self, stop: int) -> int:
        return min((b for b in self._stops() if b > stop), default=len(self.text))

    # ---- editing

    def insert(self, value: str) -> None:
        """Put `value` at the cursor; the cursor ends right after it. While an emoji sequence is being typed piece
        by piece (👨, the joiner, 👩…) that spot may sit inside the growing sequence: the next piece goes there."""
        value = "".join(ch for ch in value if typeable(ch))
        if not value:
            return
        self.text = self.text[: self.cursor] + value + self.text[self.cursor:]
        self.cursor += len(value)

    def set(self, value: str) -> None:
        self.text = value
        self.cursor = len(value)

    def _cut(self, start: int, end: int) -> None:
        self.text = self.text[:start] + self.text[end:]
        self.cursor = self._snap(start)

    def press(self, key: str, cols: int = 0) -> bool:
        """Apply a cursor or deletion key; False when `key` is not one (the caller handles it). `cols`: the width
        a wrapped text has, for ↑/↓ (0: the text is one row)."""
        if key == "left":
            self.cursor = self._prev()
        elif key == "right":
            self.cursor = self._next()
        elif key == "c-left":
            self.cursor = self._word_left()
        elif key == "c-right":
            self.cursor = self._word_right()
        elif key in ("home", "c-a"):
            self.cursor = 0
        elif key in ("end", "c-e"):
            self.cursor = len(self.text)
        elif key in ("up", "down"):
            self._vertical(-1 if key == "up" else 1, cols)
        elif key == "backspace":
            self._cut(self._prev(), self.cursor)
        elif key == "delete":
            self._cut(self.cursor, self._next())
        elif key == "c-w":
            self._cut(self._word_left(), self.cursor)
        elif key == "c-delete":
            self._cut(self.cursor, self._word_right())
        elif key == "c-u":
            self._cut(0, self.cursor)
        elif key == "c-k":
            self._cut(self.cursor, len(self.text))
        else:
            return False
        return True

    # ---- wrapping

    def rows(self, cols: int) -> list[tuple[int, int]]:
        """The text cut into rows of at most `cols` columns (whole characters): (start, end) indexes. The cursor at
        the very end needs one column of its own, so a full last row is followed by an empty one."""
        cols = max(cols, 2)
        out: list[tuple[int, int]] = []
        start, used = 0, 0
        stops = self._stops()
        for a, b in zip(stops, stops[1:]):
            w = char_width(self.text[a:b])
            if used + w > cols:
                out.append((start, a))
                start, used = a, 0
            used += w
        out.append((start, len(self.text)))
        if used + 1 > cols and self.text:
            out.append((len(self.text), len(self.text)))
        return out

    def cursor_row(self, cols: int) -> int:
        rows = self.rows(cols)
        for index, (start, end) in enumerate(rows):
            if start <= self.cursor < end or (self.cursor == end and index == len(rows) - 1):
                return index
        return len(rows) - 1

    def _column(self, start: int, index: int) -> int:
        return sum(char_width(c) for c in chars(self.text[start:index]))

    def _vertical(self, step: int, cols: int) -> None:
        """↑/↓ in a wrapped text: the same column of the row above/below; past the first/last row: start/end."""
        if cols <= 0:
            self.cursor = 0 if step < 0 else len(self.text)
            return
        rows = self.rows(cols)
        row = self.cursor_row(cols)
        target = row + step
        if target < 0:
            self.cursor = 0
            return
        if target >= len(rows):
            self.cursor = len(self.text)
            return
        column = self._column(rows[row][0], self.cursor)
        start, end = rows[target]
        position, used = start, 0
        for a, b in zip(self._stops(), self._stops()[1:]):
            if a < start or a >= end:
                continue
            w = char_width(self.text[a:b])
            if used + w > column:
                break
            used += w
            position = b
        last_row = target == len(rows) - 1
        self.cursor = position if (position < end or last_row) else self._prev_of(end)

    def _prev_of(self, index: int) -> int:
        return max((b for b in self._stops() if b < index), default=0)


__all__ = ["LineEditor", "boundaries", "char_width", "chars", "typeable"]
