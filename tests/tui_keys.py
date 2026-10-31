"""Test helper: feed scripted keys to prompt_toolkit menus without a real console."""
from __future__ import annotations

import io
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import patch

from prompt_toolkit.application import create_app_session
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

UP, DOWN, RIGHT, LEFT = "\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"
ENTER, ESC, SPACE = "\r", "\x1b", " "


@contextmanager
def scripted_keys(*keys: str, erase: bool = False) -> Iterator[io.StringIO]:
    """Run menus on a pipe with `keys`; yields the captured menu rendering.

    All keys arrive at once, so only the last frame is drawn: menus keep it on screen unless `erase`.

    A trailing no-op key flushes a final Esc, and the pipe is closed so a menu that
    runs out of keys fails with EOFError instead of hanging.
    """
    screen = io.StringIO()
    output = Vt100_Output(screen, lambda: Size(rows=40, columns=80))
    with create_pipe_input() as inp, create_app_session(input=inp, output=output), \
         patch("modules.tui.ERASE_WHEN_DONE", erase), \
         patch("modules.wizard._use_arrow_keys", return_value=True):
        inp.send_text("".join(keys) + "z")
        inp.close()
        yield screen
