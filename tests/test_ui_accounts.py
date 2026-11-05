"""Tab «Аккаунты» of the tabbed shell: rows and states, menu values, adding, profile draft, bulk actions."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from database import Database
from modules.ui import icons
from modules.ui.app import AppContext
from modules.ui.shell import Controller, frags_text, render
from modules.ui.tab_accounts import build_tab


class AccountsTabCase(unittest.TestCase):
    def setUp(self):
        icons.use_emoji(False)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "t.db"
        self.config_path = self.root / "cfg.yaml"
        with open("config.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["app"]["db_path"] = str(self.db_path)
        data["app"]["log_file"] = str(self.root / "t.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["browser"]["accounts"] = []
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.ctx = AppContext(self.config_path)

    def tearDown(self):
        self.tmp.cleanup()

    def db(self, fn):
        async def run():
            async with Database(self.db_path) as db:
                return await fn(db)

        return asyncio.run(run())

    def add(self, name: str, logged: bool = True, auth: bool = True, **settings) -> int:
        path = self.root / "sessions" / name
        acc = self.db(lambda db: db.upsert_account(name, str(path), email=name if "@" in name else f"{name}@x.com"))
        if logged:
            path.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(path / "cookies.sqlite")
            conn.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT)")
            conn.execute("INSERT INTO moz_cookies VALUES ('.youtube.com', 'LOGIN_INFO')")
            conn.commit()
            conn.close()
        if auth:
            self.db(lambda db: db.mark_account_authenticated(acc.id))
        if settings:
            from youtube_bot import apply_account_settings

            self.db(lambda db: apply_account_settings(db, [acc], **settings))
        return acc.id

    def account(self, name: str):
        return self.db(lambda db: db.get_account(name))

    def shell(self) -> Controller:
        return Controller([build_tab(self.ctx)])

    @staticmethod
    def screen(c: Controller) -> str:
        return frags_text(render(c))

    @staticmethod
    def type(c: Controller, text: str) -> None:
        for ch in text:
            c.press(ch)


class TestRows(AccountsTabCase):
    def test_empty_tab_invites_to_add(self):
        with patch("modules.ui.tab_accounts.load", return_value=[]):
            c = self.shell()
            self.assertIn("добавить аккаунт", self.screen(c))
            c.press("enter")
            self.assertIn("почта пароль 2FA", self.screen(c))

    def test_states_have_their_icons_and_hint(self):
        self.add("work", lifecycle_mode="mature")
        self.add("sleep", is_active=False)
        self.add("nologin", logged=False, auth=False)
        self.add("stale", logged=False)
        stopped = self.add("stop", lifecycle_mode="mature")
        for _ in range(3):
            self.db(lambda db: db.record_account_error(stopped))
        screen = self.screen(c := self.shell())
        for line in ("●  work", "‖  sleep", "✕  nologin", "!  stale", "■  stop", "⌂ свой IP"):
            self.assertIn(line, screen)
        self.assertIn("Работает · Уже прогрет", screen)  # hint of the selected first row
        self.assertIsNotNone(c.current_row())

    def test_space_shows_details(self):
        self.add("work", lifecycle_mode="mature")
        c = self.shell()
        c.press("space")
        self.assertIn("следующий ответ", self.screen(c))

    def test_warn_mark_for_accounts_without_proxy(self):
        self.add("work")
        self.assertTrue(build_tab(self.ctx).warn())


class TestMenu(AccountsTabCase):
    def test_mode_changes_with_arrows(self):
        self.add("work")
        c = self.shell()
        c.press("enter")
        self.assertIn("прокси", self.screen(c))
        c.press("down")
        c.press("right")
        self.assertEqual(self.account("work").lifecycle_mode, "mature")
        self.assertIn("уже прогрет", self.screen(c))

    def test_pause_from_menu(self):
        self.add("work")
        c = self.shell()
        c.press("enter")
        c.press("down")
        c.press("down")
        c.press("right")
        self.assertFalse(self.account("work").is_active)

    def test_assign_library_proxy(self):
        self.add("work")
        self.db(lambda db: db.add_proxy_url("http://u:p@1.2.3.4:8080", country="PL"))
        c = self.shell()
        c.press("enter")
        c.press("enter")  # «прокси» → list
        self.assertIn("PL 1.2.3.4:8080", self.screen(c))
        c.press("enter")
        self.assertIsNotNone(self.account("work").proxy_id)
        self.assertIn("PL 1.2.3.4", self.screen(c))

    def test_rename(self):
        self.add("work")
        c = self.shell()
        c.press("enter")
        for _ in range(7):
            c.press("down")
        c.press("enter")
        for _ in range(4):
            c.press("backspace")
        self.type(c, "neo")
        c.press("enter")
        self.assertIsNotNone(self.account("neo"))
        self.assertIn("Переименован", self.screen(c))

    def test_profile_draft_is_marked(self):
        self.add("work")
        c = self.shell()
        c.press("enter")
        for _ in range(5):
            c.press("down")
        c.press("enter")
        self.assertIn("профиль канала", self.screen(c))
        c.press("down")  # никнейм
        c.press("enter")
        self.type(c, "Vonji Reviews")
        c.press("enter")
        self.assertEqual(self.account("work").profile_name, "Vonji Reviews")
        screen = self.screen(c)
        self.assertIn("Vonji Reviews", screen)
        self.assertIn("применить на YouTube · 1 изменение", screen)
        c.press("escape")
        self.assertNotIn("профиль канала", self.screen(c))


class TestAddAndBulk(AccountsTabCase):
    def test_add_pasted_lines(self):
        self.add("old@x.com")
        c = self.shell()
        c.press("+")
        c.press("paste:new1@x.com pass111\nold@x.com pass222\nbroken-line\n")
        screen = self.screen(c)
        self.assertIn("new1@x.com · пароль", screen)
        self.assertIn("уже есть", screen)
        self.assertIn("строка 3", screen)
        c.press("enter")  # empty line: done → mode question
        self.assertIn("Режим для новых", self.screen(c))
        c.press("right")  # уже прогрет
        c.press("enter")
        self.assertIn("куки-робот", self.screen(c))
        c.press("enter")  # «Нет»
        self.assertIn("Войти и прогреть", self.screen(c))
        c.press("enter")  # «Позже»
        self.assertIn("Добавлено 1", self.screen(c))
        self.assertEqual(self.account("new1@x.com").lifecycle_mode, "mature")
        self.assertIsNone(c.pending_run)

    def add_one(self) -> Controller:
        c = self.shell()
        c.press("+")
        c.press("paste:new1@x.com pass111\n")
        c.press("enter")
        c.press("right")
        c.press("enter")
        self.assertIn("Нагулять профиль перед входом", self.screen(c))
        return c

    def test_cookie_robot_question_yes_runs_the_robot_then_asks_to_sign_in(self):
        from modules.cookie_robot import CookieRobotResult
        from modules.ui.shell import Confirm

        c = self.add_one()
        c.press("right")  # «Да»
        c.press("enter")
        run = c.pending_run
        self.assertIsNotNone(run)
        self.assertIn("Куки-робот", run.title)
        seen = []

        async def fake_robot(config_path, names, on_progress=None, headless=None, report=print):
            on_progress(names[0], 1, 4, "google.com")
            on_progress(names[0], 4, 4, "wikipedia.org")
            seen.append(list(names))
            return [CookieRobotResult(names[0], True, ("google.com",), (), 1.0, "4 из 4 сайтов")]

        with patch("youtube_bot.run_cookie_robot", fake_robot), patch("modules.wizard.safe_print") as printed:
            step = run.work()
        self.assertEqual(seen, [["new1@x.com"]])
        self.assertIsInstance(step, Confirm)  # the usual «Войти и прогреть сейчас?» follows
        self.assertIn("Войти и прогреть", step.question[0].text)
        drawn = "".join(str(call.args[0]) for call in printed.call_args_list if call.args)
        self.assertIn("[=>        ] 1/4 google.com", drawn)
        self.assertIn("[=========>] 4/4 wikipedia.org", drawn)

    def test_cookie_robot_failure_still_asks_to_sign_in(self):
        from modules.ui.shell import Confirm

        c = self.add_one()
        c.press("right")
        c.press("enter")

        async def broken(*_a, **_k):
            raise RuntimeError("boom")

        with patch("youtube_bot.run_cookie_robot", broken), patch("modules.wizard.safe_print"):
            step = c.pending_run.work()
        self.assertIsInstance(step, Confirm)

    def test_manual_cookie_robot_from_the_login_menu(self):
        from modules.cookie_robot import CookieRobotResult
        from modules.ui.shell import Notice
        from modules.ui.tab_accounts import login_menu

        account_id = self.add("work")
        menu = login_menu(self.ctx, account_id)
        ids = [row.id for row in menu.rows]
        self.assertLess(ids.index("l-manual"), ids.index("l-cookies"))
        self.assertLess(ids.index("l-cookies"), ids.index("l-check"))
        self.assertIn("l-auto", ids)

        confirm = menu.rows[ids.index("l-cookies")].enter()
        self.assertEqual(confirm.choices, ["Нет", "Нагулять"])
        self.assertIsNone(confirm.pick(0))
        run = confirm.pick(1)
        self.assertIn("Куки-робот · work", run.title)

        async def fake_robot(config_path, names, on_progress=None, headless=None, report=print):
            return [CookieRobotResult(names[0], True, ("google.com",), ("reddit.com",), 1.0, "3 из 4 сайтов (не открылись: reddit.com)")]

        with patch("youtube_bot.run_cookie_robot", fake_robot), patch("modules.wizard.safe_print"):
            notice = run.work()
        self.assertIsInstance(notice, Notice)
        self.assertEqual(notice.kind, "ok")
        self.assertIn("3 из 4", notice.text)

    def test_bulk_mode(self):
        self.add("a")
        self.add("b")
        c = self.shell()
        c.press("x")
        c.press("space")
        c.press("down")
        c.press("space")
        c.press("enter")
        self.assertIn("режим…", self.screen(c))
        c.press("enter")
        c.press("down")
        c.press("enter")
        self.assertIn("Изменено 2 из 2", self.screen(c))
        self.assertEqual({self.account("a").lifecycle_mode, self.account("b").lifecycle_mode}, {"mature"})

    def test_warm_now_is_a_run(self):
        self.add("work")
        c = self.shell()
        c.press("enter")
        for _ in range(4):
            c.press("down")
        c.press("enter")
        c.press("right")
        c.press("enter")
        self.assertIsNotNone(c.pending_run)


if __name__ == "__main__":
    unittest.main()
