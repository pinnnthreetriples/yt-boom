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
        self._cfg: tuple[tuple[int, int], Any] | None = None  # (file stamp, config): one read per change of the file
        self.schema_error = ""  # why the database could not be brought up to date at start ("" = it was)

    def cfg(self) -> Any:
        """The config as the file says now (read again only when the file changed). Read-only for the tabs."""
        from config import load_config

        stat = self.config_path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._cfg is None or self._cfg[0] != stamp:
            self._cfg = (stamp, load_config(self.config_path))
        return self._cfg[1]

    def update(self, updates: dict[str, Any]) -> None:
        from modules.wizard import update_config_file

        update_config_file(self.config_path, updates)

    def db(self, fn: Callable[[Any], Any]) -> Any:
        """Run `fn(db)` (awaited if it is a coroutine) on a connected Database."""
        from modules.wizard import _pools_db

        return in_thread(lambda: _pools_db(self.config_path, fn))

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """Read-only sqlite3 query, safe inside the menu; [] without a database."""
        return self._read(sql, params, named=False)

    def query_rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """`query` with the rows as dicts by column name."""
        return self._read(sql, params, named=True)

    def _read(self, sql: str, params: tuple[Any, ...], named: bool) -> list[Any]:
        try:
            uri = f"file:{quote(Path(self.cfg().app.db_path).as_posix(), safe='/:')}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
            try:
                if named:
                    conn.row_factory = sqlite3.Row
                    return [dict(row) for row in conn.execute(sql, params).fetchall()]
                return conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return []

    # ---- projects

    def projects(self) -> list[Any]:
        """Every project in queue order (ProjectRecord with its counters); [] without a database."""
        from database import PROJECT_ORDER, PROJECT_SELECT, ProjectRecord

        return [ProjectRecord.model_validate(row) for row in self.query_rows(PROJECT_SELECT + PROJECT_ORDER)]

    def project(self) -> Any:
        """The current project (open in «Цель» / «Ответы»): the one picked last, else the first; None without any."""
        from database import UI_PROJECT_KEY

        projects = self.projects()
        saved = self.query("SELECT value FROM app_settings WHERE key = ?", (UI_PROJECT_KEY,))
        wanted = int(saved[0][0]) if saved and str(saved[0][0]).isdigit() else None
        return next((p for p in projects if p.id == wanted), projects[0] if projects else None)

    def set_project(self, project_id: int) -> None:
        from database import UI_PROJECT_KEY

        self.db(lambda db: db.set_setting(UI_PROJECT_KEY, str(project_id)))

    def update_project(self, project_id: int, **fields: Any) -> Any:
        """Write fields of one project (ValueError with a Russian text for a bad value)."""
        return self.db(lambda db: db.update_project(project_id, **fields))

    def active_members(self, project_id: int) -> int:
        rows = self.query(
            "SELECT COUNT(*) FROM project_accounts pa JOIN accounts a ON a.id = pa.account_id"
            " WHERE pa.project_id = ? AND a.is_active = 1",
            (project_id,),
        )
        return int(rows[0][0]) if rows else 0

    def problems(self, project: Any) -> list[str]:
        """Why the project cannot run as it is ([] = ready): members, videos, templates, the DeepSeek key."""
        from modules.projects import has_deepseek_key, project_problems

        return project_problems(project, self.active_members(project.id), has_deepseek_key(self.cfg()))


def launch_problem(ctx: AppContext) -> str:
    """The first thing that stops the projects «в запуске» ("" = none): «нет проектов в запуске», or a project's
    problem — prefixed with its name once there are several projects («Фитнес: нет аккаунтов»)."""
    projects = ctx.projects()
    marked = [p for p in projects if p.enabled]
    if projects and not marked:
        return "нет проектов в запуске"
    for project in marked:
        problems = ctx.problems(project)
        if problems:
            return problems[0] if len(projects) == 1 else f"{project.name}: {problems[0]}"
    return ""


def status_cells(ctx: AppContext) -> list[Cell]:
    """The right side of the tab line: the first thing that blocks a run (by the projects «в запуске»), else accounts
    on the own IP, else ready."""
    from modules.wizard import _logged_in_names

    try:
        cfg = ctx.cfg()
    except Exception:
        return [Cell("config.yaml не читается", "err")]
    if ctx.schema_error:
        return [Cell("база не обновилась", "err")]
    accounts = ctx.query("SELECT is_active, proxy_id FROM accounts")
    if not accounts:
        return [Cell("нет аккаунтов", "warn")]
    if not _logged_in_names(cfg):
        return [Cell(icon("no_login") + " нет входа", "warn")]
    problem = launch_problem(ctx)
    if problem:
        return [Cell(problem, "warn")]
    own_ip = sum(1 for active, proxy_id in accounts if active and proxy_id is None)
    if own_ip:
        return [Cell(f"{icon('own_ip')} {own_ip}", "warn")]
    return [Cell("● готов", "ok")]


def build_tabs(ctx: AppContext) -> list[Tab]:
    from modules.ui import tab_accounts, tab_launch, tab_projects, tab_proxies, tab_replies, tab_target

    return [
        tab_launch.build_tab(ctx),
        tab_accounts.build_tab(ctx),
        tab_proxies.build_tab(ctx),
        tab_projects.build_tab(ctx),
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
    if ctx.schema_error:
        controller.notice = Notice("err", ctx.schema_error)
    controller.on_icons = lambda: _switch_icons(ctx, controller)
    run_shell(controller, _run, clear_terminal)


ICONS_SETTING = "ui_icons"


def _ensure_schema(ctx: AppContext) -> str:
    """Bring an older database up to date before the tabs read it with plain sqlite3 (`video_pool.source`, the
    projects and «Основной» seeded from config.yaml with the accounts of a fresh config, `video_pool.project_id`, the
    journal's project / account). A failure is kept in `ctx.schema_error` (shown at start and as the status) and
    returned; an unreadable config is left to the tabs."""
    from modules.wizard import seed_config_accounts

    try:
        cfg = ctx.cfg()
    except Exception:
        return ""  # no readable config yet: the tabs show what is missing
    try:
        ctx.db(lambda db: seed_config_accounts(db, cfg))
        ctx.schema_error = ""
    except Exception as exc:
        ctx.schema_error = f"База не обновилась: {exc} · {_backup_words(exc, Path(cfg.app.db_path))}"
    return ctx.schema_error


def _backup_words(exc: Exception, db_path: Path) -> str:
    """Where the copy made before the migration is (the newest `<db>.bak-*` when the error does not say)."""
    backup = getattr(exc, "backup", None)
    if backup is None:
        copies = sorted(db_path.parent.glob(db_path.name + ".bak-*"), key=lambda p: p.stat().st_mtime)
        backup = copies[-1] if copies else None
    return f"копия: {backup}" if backup else "копии нет"


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
