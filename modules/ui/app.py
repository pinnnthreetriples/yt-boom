"""Entry point of the tabbed console interface: context shared by the tabs, the status on the right, the run loop."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from modules.ui.icons import icon
from modules.ui.shell import Cell, Controller, Notice, Run, Tab, run_shell


class AppContext:
    """What every tab needs: the config file, the database, a little shared state (the launch mode)."""

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self.live = False  # launch mode: dry run (False) or live (True); not saved, every start is a dry run

    def cfg(self) -> Any:
        from config import load_config

        return load_config(self.config_path)

    def update(self, updates: dict[str, Any]) -> None:
        from modules.wizard import update_config_file

        update_config_file(self.config_path, updates)

    def db(self, fn: Callable[[Any], Any]) -> Any:
        """Run `fn(db)` (awaited if it is a coroutine) on a connected Database."""
        from modules.wizard import _pools_db

        return _pools_db(self.config_path, fn)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """Read-only sqlite3 query, safe inside the menu; [] without a database."""
        try:
            uri = f"file:{quote(Path(self.cfg().app.db_path).as_posix(), safe='/:')}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
            try:
                return conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return []


def status_cells(ctx: AppContext) -> list[Cell]:
    """The right side of the tab line: the first thing that blocks a run, else accounts on the own IP, else ready."""
    from modules.wizard import _logged_in_names, _target_plan

    try:
        cfg = ctx.cfg()
    except Exception:
        return [Cell("config.yaml не читается", "err")]
    accounts = ctx.query("SELECT is_active, proxy_id FROM accounts")
    if not accounts:
        return [Cell("нет аккаунтов", "warn")]
    if not _logged_in_names(cfg):
        return [Cell(icon("no_login") + " нет входа", "warn")]
    if _target_plan(cfg)[0] == "none":
        return [Cell("нет видео", "warn")]
    if getattr(cfg.processor, "reply_mode", "ai") == "ai" and not cfg.deepseek.api_key:
        return [Cell("нет ключа DeepSeek", "warn")]
    if not getattr(cfg.processor, "reply_mode", "ai") == "ai" and not getattr(cfg.processor, "custom_replies", []):
        return [Cell("нет шаблонов", "warn")]
    own_ip = sum(1 for active, proxy_id in accounts if active and proxy_id is None)
    if own_ip:
        return [Cell(f"{icon('own_ip')} {own_ip}", "warn")]
    return [Cell("● готов", "ok")]


def build_tabs(ctx: AppContext) -> list[Tab]:
    from modules.ui import tab_accounts, tab_launch, tab_proxies, tab_replies, tab_target

    return [
        tab_launch.build_tab(ctx),
        tab_accounts.build_tab(ctx),
        tab_proxies.build_tab(ctx),
        tab_target.build_tab(ctx),
        tab_replies.build_tab(ctx),
    ]


def _run(run: Run) -> Notice | None:
    from modules import tui

    if run.title:
        tui.section_header(run.title)
    return run.work()


def run_app(config_path: Path) -> None:
    """Show the interface until Q."""
    from modules.wizard import clear_terminal

    ctx = AppContext(config_path)
    controller = Controller(build_tabs(ctx), status=lambda: status_cells(ctx))
    run_shell(controller, _run, clear_terminal)
