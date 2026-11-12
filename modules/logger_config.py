from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from loguru import logger


def configure_windows_encoding() -> None:
    """Ensure standard output and error use UTF-8 on Windows consoles."""
    if sys.platform == "win32":
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def russian_console_formatter(record: dict[str, Any]) -> str:
    """Minimalist console formatter in Russian with strict requested color scheme:
    - SUCCESS: green line (<green><bold>...</bold></green>)
    - ERROR / CRITICAL: red line (<red><bold>...</bold></red>)
    - INFO: yellow line (<yellow>...</yellow>)
    - WARNING: magenta line (<magenta>...</magenta>)
    - DEBUG: dimmed line (<dim>...</dim>)
    """
    level_name = record["level"].name
    if level_name == "SUCCESS":
        return "<green><bold>{time:HH:mm:ss} | УСПЕХ  | {message}</bold></green>\n{exception}"
    elif level_name in ("ERROR", "CRITICAL"):
        return "<red><bold>{time:HH:mm:ss} | ОШИБКА | {message}</bold></red>\n{exception}"
    elif level_name == "WARNING":
        return "<magenta>{time:HH:mm:ss} | ВНИМ   | {message}</magenta>\n{exception}"
    elif level_name == "DEBUG":
        return "<dim>{time:HH:mm:ss} | ОТЛАД  | {message}</dim>\n{exception}"
    else:  # INFO
        return "<yellow>{time:HH:mm:ss} | ИНФО   | {message}</yellow>\n{exception}"


def russian_file_formatter(record: dict[str, Any]) -> str:
    """Plain-text Russian log format for persistent disk files."""
    level_map = {
        "SUCCESS": "УСПЕХ",
        "ERROR": "ОШИБКА",
        "CRITICAL": "КРИТИЧНО",
        "WARNING": "ВНИМАНИЕ",
        "INFO": "ИНФО",
        "DEBUG": "ОТЛАДКА",
    }
    lvl = level_map.get(record["level"].name, record["level"].name)
    record["extra"]["ru_level"] = lvl
    return "{time:YYYY-MM-DD HH:mm:ss} | {extra[ru_level]: <8} | {module: <18} | {message}\n{exception}"


_console_sink: int | None = None  # loguru id of the stderr handler made by `setup_bot_logging`
_console_level = "INFO"
_held = False


def _add_console_sink(level: str) -> int:
    return logger.add(
        sys.stderr,
        level=level,
        format=russian_console_formatter,
        colorize=True,
        backtrace=False,
        diagnose=False,
    )


@contextmanager
def console_held() -> Iterator[None]:
    """Keep log lines off the console while a menu is drawn in place, and print them when it is closed.

    A line written to the terminal under a running prompt_toolkit screen breaks its redraw: the tab line is
    overwritten and the rows are drawn twice. Without a console handler (tests, nothing set up) it does nothing.
    """
    global _console_sink, _held
    if _console_sink is None or _held:
        yield
        return
    held: list[str] = []
    logger.remove(_console_sink)
    buffer = logger.add(held.append, level=_console_level, format=russian_console_formatter, colorize=True,
                        backtrace=False, diagnose=False)
    _held = True
    try:
        yield
    finally:
        _held = False
        logger.remove(buffer)
        _console_sink = _add_console_sink(_console_level)
        for text in held:
            sys.stderr.write(text)
        sys.stderr.flush()


def setup_bot_logging(
    level: str = "INFO",
    log_file: str | Path | None = None,
    rotation: str = "10 MB",
    retention: str = "14 days",
) -> None:
    """Configure loguru with Russian level tags, full-line colors, and optional disk logging."""
    global _console_sink, _console_level
    configure_windows_encoding()
    logger.remove()

    _console_level = level
    _console_sink = _add_console_sink(level)

    if log_file:
        file_path = Path(log_file)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(file_path),
            level=level,
            format=russian_file_formatter,
            rotation=rotation,
            retention=retention,
            encoding="utf-8",
            enqueue=True,
            backtrace=False,
            diagnose=False,
        )


__all__ = [
    "console_held",
    "configure_windows_encoding",
    "russian_console_formatter",
    "russian_file_formatter",
    "setup_bot_logging",
]
