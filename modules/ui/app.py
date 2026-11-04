"""Entry point of the tabbed console interface: context shared by the tabs, the status on the right, the run loop."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from modules.ui.icons import icon
from modules.ui.shell import Cell, Controller, Notice, Run, Step, Tab, run_shell


def in_thread(fn: Callable[[], Any]) -> Any:
    """Call `fn` (it may use asyncio.run) from a key handler: prompt_toolkit's event loop is running there."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn()
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(fn).result()


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

        return in_thread(lambda: _pools_db(self.config_path, fn))

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


def _run(run: Run) -> Step:
    from modules import tui

    if run.title:
        tui.section_header(run.title)
    return run.work()


def run_app(config_path: Path) -> None:
    """Show the interface until Q."""
    from modules.wizard import clear_terminal

    ctx = AppContext(config_path)
    _ensure_schema(ctx)
    _load_icon_choice(ctx)
    controller = Controller(build_tabs(ctx), status=lambda: status_cells(ctx))
    controller.on_icons = lambda: _switch_icons(ctx, controller)
    run_shell(controller, _run, clear_terminal)


ICONS_SETTING = "ui_icons"


def _ensure_schema(ctx: AppContext) -> None:
    """Bring an older database up to date before the tabs read it with plain sqlite3 (e.g. `video_pool.source`)."""
    try:
        ctx.db(lambda db: db.count_pool_videos())
    except Exception:
        pass  # no readable config or database yet: the tabs show what is missing


def _load_icon_choice(ctx: AppContext) -> None:
    """A set picked with «I» earlier wins over the detection (unless YTBOT_ICONS forces one)."""
    import os

    from modules.ui import icons

    if os.environ.get("YTBOT_ICONS"):
        return
    try:
        saved = ctx.db(lambda db: db.get_setting(ICONS_SETTING))
    except Exception:
        return
    if saved in {"emoji", "plain"}:
        icons.use_emoji(saved == "emoji")


def _switch_icons(ctx: AppContext, controller: Controller) -> Notice:
    """«I»: colour emoji ↔ plain symbols, remembered in the database."""
    from modules.ui import icons

    icons.use_emoji(not icons.EMOJI)
    choice = "emoji" if icons.EMOJI else "plain"
    try:
        ctx.db(lambda db: db.set_setting(ICONS_SETTING, choice))
    except Exception:
        pass
    controller.tabs = build_tabs(ctx)  # tab icons are taken when the tabs are built
    return Notice("ok", "Значки: цветные" if icons.EMOJI else "Значки: простые символы")
