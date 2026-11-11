"""Projects in the interface: the «Проекты» tab (rows, menu, members, new / copy / rename / delete), several projects
in «Запуск» (rows, plan, questions, «Чтобы начать», journal), «Аккаунты» grouped by project, the status by the
projects «в запуске», the shell (six tabs, GoTab by name) — and one project looks as before."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import yaml

from config import load_config
from database import Database
from modules.projects import project_seed
from modules.ui import icons, shell
from modules.ui.app import AppContext, build_tabs, status_cells
from modules.ui.shell import Confirm, Controller, Run, frags_text, render


class UiCase(unittest.TestCase):
    def setUp(self):
        icons.use_emoji(False)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "cfg.yaml"
        data = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
        data["app"]["db_path"] = str(self.root / "t.db")
        data["app"]["log_file"] = str(self.root / "t.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["browser"]["accounts"] = []
        data["deepseek"]["api_key"] = "sk-test-key-123456"
        data["processor"]["reply_mode"] = "ai"
        data["reply_mode"] = "ai"
        data["collector"]["search_query"] = "iphone 17 обзор"
        data["limits"]["max_concurrent_sessions"] = 1
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.db_path = self.root / "t.db"
        self.ids: dict[str, int] = {}
        self.db(lambda db: db.list_projects())
        self.ctx = AppContext(self.config_path)
        self.main = self.ctx.projects()[0]

    def tearDown(self):
        self.temp_dir.cleanup()

    def db(self, fn):
        async def run():
            async with Database(self.db_path, project_seed=project_seed(load_config(self.config_path))) as db:
                return await fn(db)

        return asyncio.run(run())

    def account(self, name: str, *projects: int, logged: bool = True) -> int:
        path = self.root / "sessions" / name
        acc = self.db(lambda db: db.upsert_account(name, str(path)))
        if logged:
            path.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path / "cookies.sqlite")
            conn.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT)")
            conn.execute("INSERT INTO moz_cookies VALUES ('.youtube.com', 'LOGIN_INFO')")
            conn.commit()
            conn.close()
        for project_id in projects:
            self.db(lambda db: db.add_project_accounts(project_id, [acc.id]))
        self.ids[name] = acc.id
        return acc.id

    def project(self, name: str, **fields):
        return self.db(lambda db: db.create_project(name, **fields))

    def get(self, name: str):
        return next((p for p in self.ctx.projects() if p.name == name), None)

    def controller(self, tab: str | None = None) -> Controller:
        c = Controller(build_tabs(self.ctx), status=lambda: status_cells(self.ctx))
        if tab:
            c.apply(shell.GoTab(tab))
        return c

    @staticmethod
    def screen(c: Controller) -> str:
        return frags_text(render(c))

    @staticmethod
    def type(c: Controller, text: str) -> None:
        for ch in text:
            c.press("space" if ch == " " else ch)


class ShellTest(UiCase):
    def test_six_tabs_by_number_and_by_name(self):
        c = self.controller()
        self.assertEqual([t.name for t in c.tabs], ["Запуск", "Аккаунты", "Прокси", "Проекты", "Цель", "Ответы"])
        c.press("6")
        self.assertEqual(c.tabs[c.tab].name, "Ответы")
        c.press("4")
        self.assertEqual(c.tabs[c.tab].name, "Проекты")
        c.apply(shell.GoTab("Цель"))
        self.assertEqual(c.tab, 4)
        c.press("?")
        self.assertIn("1–6 — сразу", self.screen(c))

    def test_the_tab_line_fits_80_columns(self):
        self.account("one", self.main.id)
        for emoji in (False, True):
            icons.use_emoji(emoji)
            c = self.controller()
            shell.WIDTH = 79
            try:
                lines = self.screen(c).split("\n")
            finally:
                shell.WIDTH = shell.BASE_WIDTH
            self.assertLessEqual(shell.width(lines[0]), 79, lines[0])
            self.assertIn("Ответы", lines[0])
        icons.use_emoji(False)


class OneProjectTest(UiCase):
    """With a single project the interface looks as before: no project rows, no switch, no groups."""

    def test_launch_screen_snapshot(self):
        self.account("one", self.main.id)
        out = self.screen(self.controller())
        lines = [line.rstrip() for line in out.split("\n")]
        start = lines.index(" ▸ ◇  пробный ‹›")
        self.assertEqual([line for line in lines[start:start + 8] if line], [
            " ▸ ◇  пробный ‹›",
            "   ▤  ответов на видео: 3 · видео на тему: 5 · лимит без своего лимита",
            "   ▣  показывать",
            "   ●  Проверка видимости: включена",
            "   ≡  журнал 0",
            "   сегодня  ✓ 0    ◇ 0    ✕ 0    ● 0    ! 0",
        ])
        self.assertNotIn("Основной", out)

    def test_no_switch_and_no_groups(self):
        self.account("one", self.main.id)
        c = self.controller("Цель")
        self.assertNotIn("1/1", self.screen(c))
        c.apply(shell.GoTab("Аккаунты"))
        self.assertNotIn("▣ Основной", self.screen(c))
        c.apply(shell.GoTab("Проекты"))
        out = self.screen(c)
        self.assertIn("Основной", out)
        self.assertIn("новый проект", out)


class ProjectsTabTest(UiCase):
    def test_rows_and_space(self):
        self.account("one", self.main.id)
        fit = self.project("Фитнес", search_topics=["тренировка дома"], reply_mode="custom")
        c = self.controller("Проекты")
        out = self.screen(c)
        self.assertIn("Основной", out)
        self.assertIn("◎ 1", out)
        self.assertIn("? 1 тема", out)
        self.assertIn("* ИИ", out)
        self.assertIn("≡ 0 шабл.", out)
        c.select(f"p{fit.id}")
        self.assertIn("нет аккаунтов: Enter → аккаунты", self.screen(c))
        c.press("space")
        out = self.screen(c)
        self.assertIn("темы: «тренировка дома»", out)
        self.assertIn("аккаунтов нет", out)
        self.assertIn("не готов: нет аккаунтов, нет шаблонов", out)
        self.assertTrue(c.tabs[3].warn())

    def test_new_project_goes_to_target(self):
        c = self.controller("Проекты")
        c.press("+")
        self.type(c, "Крипта")
        c.press("enter")
        self.assertEqual(c.tabs[c.tab].name, "Цель")
        self.assertIn("Проект «Крипта» создан", c.notice.text)
        self.assertEqual(self.ctx.project().name, "Крипта")
        c.apply(shell.GoTab("Проекты"))
        c.press("+")
        self.type(c, "КРИПТА")
        c.press("enter")
        self.assertIn("уже есть", c.notice.text)

    def test_menu_values_order_rename_copy(self):
        crypto = self.project("Крипта")
        c = self.controller("Проекты")
        c.select(f"p{crypto.id}")
        c.press("enter")
        rows = {r.id: i for i, r in enumerate(c.menu.rows)}
        c.menu_sel = rows["pm-enabled"]
        c.press("right")
        self.assertFalse(self.get("Крипта").enabled)
        c.menu_sel = rows["pm-daily"]
        c.press("right")
        c.press("right")
        self.assertEqual(self.get("Крипта").daily_per_account, 2)
        c.press("enter")
        for _ in range(3):
            c.press("backspace")
        self.type(c, "7")
        c.press("enter")
        self.assertEqual(self.get("Крипта").daily_per_account, 7)
        c.press("enter")
        c.menu_sel = rows["pm-order"]
        c.press("left")
        self.assertEqual([p.name for p in self.ctx.projects()], ["Крипта", "Основной"])
        c.menu_sel = rows["pm-rename"]
        c.press("enter")
        for _ in range(6):
            c.press("backspace")
        self.type(c, "Монеты")
        c.press("enter")
        self.assertIn("Переименован: Крипта → Монеты", c.notice.text)
        c.select(f"p{crypto.id}")
        c.press("enter")
        c.menu_sel = rows["pm-copy"]
        c.press("enter")
        c.press("enter")  # the offered name «Монеты копия»
        self.assertIsNotNone(self.get("Монеты копия"))
        self.assertEqual(self.get("Монеты копия").daily_per_account, 7)

    def test_delete_says_what_goes_and_the_last_one_stays(self):
        crypto = self.project("Крипта")
        self.account("one", crypto.id)
        self.db(lambda db: db.add_video_to_pool("https://www.youtube.com/watch?v=aaaaaaaaaaa", 2, project_id=crypto.id))
        c = self.controller("Проекты")
        c.select(f"p{crypto.id}")
        c.press("enter")
        c.menu_sel = [r.id for r in c.menu.rows].index("pm-delete")
        c.press("enter")
        self.assertIsInstance(c.confirm, Confirm)
        self.assertIn("1 акк. выйдут из проекта, 1 ссылка удалится, журнал останется", self.screen(c))
        c.press("enter")  # «Нет»
        self.assertIsNotNone(self.get("Крипта"))
        c.press("enter")
        c.menu_sel = [r.id for r in c.menu.rows].index("pm-delete")
        c.press("enter")
        c.press("right")
        c.press("enter")
        self.assertIn("удалён", c.notice.text)
        c.press("enter")
        c.menu_sel = [r.id for r in c.menu.rows].index("pm-delete")
        c.press("enter")
        self.assertEqual(c.notice.text, "Последний проект удалить нельзя")

    def test_members_screen_toggles_and_marks(self):
        crypto = self.project("Крипта", daily_per_account=5)
        a, b = self.account("alpha"), self.account("beta")
        c = self.controller("Проекты")
        c.select(f"p{crypto.id}")
        c.press("enter")
        c.menu_sel = [r.id for r in c.menu.rows].index("pm-members")
        c.press("enter")
        self.assertIn("Крипта › аккаунты · 0 из 2", self.screen(c))
        c.select(f"a{a}")
        c.press("enter")
        self.assertEqual([x.name for x in self.db(lambda db: db.list_project_accounts(crypto.id))], ["alpha"])
        self.assertIn("в проекте 0/5 · всего за день 0", c.current_row().hint)
        c.press("space")
        self.assertEqual(self.db(lambda db: db.list_project_accounts(crypto.id)), [])
        c.press("X")
        c.select(f"a{a}")
        c.press("space")
        c.select(f"a{b}")
        c.press("space")
        c.press("enter")
        c.press("enter")  # «включить в проект · 2»
        self.assertEqual(len(self.db(lambda db: db.list_project_accounts(crypto.id))), 2)


class SeveralProjectsLaunchTest(UiCase):
    def setUp(self):
        super().setUp()
        self.crypto = self.project("Крипта", search_topics=["биткоин"])
        self.fitness = self.project("Фитнес", search_topics=["йога"], reply_mode="custom")
        self.account("one", self.main.id, self.crypto.id)
        self.account("two", self.crypto.id)

    def test_rows_plan_and_toggle(self):
        c = self.controller()
        out = self.screen(c)
        self.assertIn("Крипта", out)
        self.assertIn("▶ в запуске", out)
        self.assertIn("Фитнес: нет аккаунтов", out)
        self.assertIn("Фитнес: нет шаблонов", out)
        self.assertIn("Фитнес: нет аккаунтов", self.screen(c).split("\n")[0] + self.screen(c).split("\n")[1])
        c.select(f"p{self.crypto.id}")
        c.press("space")
        self.assertIn("темы: «биткоин»; свои ссылки: 0 ждут; лимит без своего лимита", self.screen(c))
        c.select(f"p{self.fitness.id}")
        c.press("right")
        self.assertFalse(self.get("Фитнес").enabled)
        self.assertNotIn("Фитнес: нет", self.screen(c))

    def test_live_question_counts_projects_and_accounts(self):
        self.db(lambda db: db.update_project(self.fitness.id, enabled=False))
        c = self.controller()
        c.select("start")
        c.press("right")
        c.press("enter")
        self.assertIn("Публикуем: 2 проекта, до 2 акк.?", self.screen(c))

    def test_one_project_from_its_menu(self):
        c = self.controller()
        c.select(f"p{self.crypto.id}")
        c.press("enter")
        c.press("enter")  # «запустить только этот»
        self.assertIsInstance(c.pending_run, Run)
        c.pending_run = None
        c.select(f"p{self.crypto.id}")
        c.press("enter")
        c.press("down")
        c.press("enter")  # «открыть в «Цели»»
        self.assertEqual(c.tabs[c.tab].name, "Цель")
        self.assertEqual(self.ctx.project().name, "Крипта")

    def test_nothing_marked(self):
        for project in self.ctx.projects():
            self.db(lambda db, p=project: db.update_project(p.id, enabled=False))
        c = self.controller()
        self.assertIn("нет проектов в запуске", self.screen(c))
        self.assertEqual(status_cells(self.ctx)[0].text, "нет проектов в запуске")
        c.select("start")
        c.press("enter")
        self.assertIn("Нет проектов в запуске", c.notice.text)

    def test_journal_by_project_with_account_names(self):
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        rows = [(self.crypto.id, self.ids["one"], "ответ крипты"), (self.main.id, self.ids["two"], "ответ основного"),
                (999, self.ids["two"], "ответ удалённого")]
        for project_id, account_id, text in rows:
            conn.execute("INSERT INTO activity_log (created_at, video_id, target_id, mode, status, reply_text, project_id,"
                         " account_id) VALUES (?, 'v', 't', 'dry_run', 'simulated', ?, ?, ?)", (now, text, project_id, account_id))
        conn.commit()
        conn.close()
        c = self.controller()
        c.select("today")
        c.press("space")
        out = self.screen(c)
        self.assertIn("Крипта", out)
        self.assertIn("удалён", out)
        c.select("journal")
        c.press("enter")
        out = self.screen(c)
        self.assertIn("ответ удалённого", out)
        self.assertIn("удалён", out)
        c.press("right")  # project: Основной
        out = self.screen(c)
        self.assertIn("ответ основного", out)
        self.assertNotIn("ответ крипты", out)
        self.assertIn("two", out)
        c.press("down")
        c.press("down")
        c.press("enter")
        self.assertIn("проект Основной · аккаунт two", self.screen(c))


class AccountsTest(UiCase):
    def test_grouped_by_project_once_with_the_count(self):
        crypto = self.project("Крипта")
        self.account("alpha", self.main.id, crypto.id)
        self.account("beta", crypto.id)
        self.account("loose")
        c = self.controller("Аккаунты")
        out = self.screen(c)
        self.assertIn("▣ Основной · 1", out)
        self.assertIn("▣ Крипта · 2", out)
        self.assertIn("▣ без проекта · 1", out)
        self.assertEqual(out.count("alpha"), 1)
        self.assertIn("▣2", out)
        c.select(f"a{self.ids['alpha']}")
        c.press("space")
        self.assertIn("проекты: Основной, Крипта", self.screen(c))
        c.select(f"a{self.ids['loose']}")
        self.assertIn("без проекта — не работает", self.screen(c))
        self.assertTrue(c.tabs[1].warn())

    def test_account_menu_projects_screen(self):
        crypto = self.project("Крипта")
        acc = self.account("alpha")
        c = self.controller("Аккаунты")
        c.select(f"a{acc}")
        c.press("enter")
        c.menu_sel = [r.id for r in c.menu.rows].index("m-projects")
        self.assertIn("без проекта — не работает", self.screen(c))
        c.press("enter")
        self.assertIn("alpha › проекты", self.screen(c))
        c.select(f"p{crypto.id}")
        c.press("space")
        self.assertEqual([p.name for p in self.db(lambda db: db.projects_of_account(acc))], ["Крипта"])
        c.press("enter")
        self.assertEqual(self.db(lambda db: db.projects_of_account(acc)), [])

    def test_bulk_add_and_remove(self):
        crypto = self.project("Крипта")
        a, b = self.account("alpha"), self.account("beta")
        c = self.controller("Аккаунты")
        c.press("X")
        c.select(f"a{a}")
        c.press("space")
        c.select(f"a{b}")
        c.press("space")
        c.press("enter")
        c.menu_sel = [r.id for r in c.menu.rows].index("b-project-add")
        c.press("enter")
        c.menu_sel = [r.id for r in c.menu.rows].index(f"bp-{crypto.id}")
        c.press("enter")
        self.assertIn("В «Крипта» добавлено 2 из 2 акк.", c.notice.text)
        self.assertEqual(len(self.db(lambda db: db.list_project_accounts(crypto.id))), 2)


if __name__ == "__main__":
    unittest.main()
