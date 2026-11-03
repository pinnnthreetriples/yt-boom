"""Tab «Прокси» (being moved to the new shell)."""
from __future__ import annotations

from modules.ui.app import AppContext
from modules.ui.icons import icon
from modules.ui.shell import Cell, Row, Tab, View


def build_tab(ctx: AppContext) -> Tab:
    return Tab("Прокси", icon("tab_proxies"), lambda: View(rows=[Row("todo", [Cell("скоро", "dim")], selectable=False)]))
