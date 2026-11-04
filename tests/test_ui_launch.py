"""Tab «Запуск»: first-run list, start (dry / live with a question), threads, window, journal and one record; a launch
runs the projects «в запуске» (`run_projects`)."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from config import load_config
from database import Database
from modules.multi_account_orchestrator import ProjectRunResult
from modules.projects import project_seed
from modules.ui import icons
from modules.ui.app import AppContext, status_cells
from modules.ui.shell import Confirm, Controller, Run, Tab, frags_text, render
from modules.ui.tab_launch import build_tab, run_target
from youtube_bot import ProjectsRunSummary


class LaunchCase(unittest.TestCase):
    def setUp(self):
        icons.use_emoji(False)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "cfg.yaml"
        with open("config.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["app"]["db_path"] = str(self.root / "t.db")
        data["app"]["log_file"] = str(self.root / "t.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["browser"]["accounts"] = []
        data["browser"]["headless"] = False
        data["deepseek"]["api_key"] = "sk-test-key-123456"
        data["processor"]["reply_mode"] = "ai"
        data["reply_mode"] = "ai"
        data["collector"]["search_query"] = "iphone 17 обзор"
        data["limits"]["max_concurrent_sessions"] = 1
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.db_path = self.root / "t.db"
        asyncio.run(self._init_db())
        self.ctx = AppContext(self.config_path)

    async def _init_db(self):
        db = Database(str(self.db_path), project_seed=project_seed(load_config(self.config_path)))
        await db.connect()
        await db.init_schema()
        await db.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def raw(self) -> dict:
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8"))

    def add_accounts(self, *names: str, member: bool = True) -> None:
        """New accounts; `member`: they join «Основной» (a new account belongs to no project by itself)."""
        async def go():
            db = Database(str(self.db_path))
            await db.connect()
            await db.init_schema()
            for name in names:
                acc = await db.upsert_account(name, session_path=str(self.root / "sessions" / name))
                if member:
                    await db.add_project_accounts((await db.list_projects())[0].id, [acc.id])
            await db.close()

        asyncio.run(go())

    def add_records(self, *records: tuple[str, str, str]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        for status, text, author in records:
            conn.execute(
                "INSERT INTO activity_log (created_at, video_id, target_id, mode, status, reply_text, error, target_author,"
                " video_title) VALUES (?, 'vid1', 'c1', ?, ?, ?, ?, ?, 'Обзор')",
                (now, "dry_run" if status == "simulated" else "live", status,
                 None if status == "failed" else text, text if status == "failed" else None, author),
            )
        conn.commit()
        conn.close()

    def controller(self) -> Controller:
        return Controller([build_tab(self.ctx)], status=lambda: status_cells(self.ctx))

    def screen(self, c: Controller) -> str:
        return frags_text(render(c))

    def test_first_run_lists_what_is_missing(self):
        c = self.controller()
        out = self.screen(c)
        self.assertIn("Чтобы начать:", out)
        self.assertIn("добавить аккаунт", out)
        self.assertNotIn("тема поиска или ссылки", out)  # the search topic is set
        self.assertIn("нет аккаунтов", out)
        self.assertEqual(c.current_id(), "setup-accounts")

    def test_ready_screen_has_start_window_journal_and_today(self):
        self.add_accounts("one")
        self.add_records(("success", "Согласен, камера лучше", "@a"), ("failed", "кнопка не найдена", "@b"))
        c = self.controller()
        out = self.screen(c)
        self.assertNotIn("Чтобы начать", out)
        self.assertIn(" ▸ ◇  пробный ‹›", out)
        self.assertIn("показывать", out)
        self.assertIn("журнал 2", out)
        self.assertIn("сегодня", out)
        self.assertIn("✓ 1", out)
        self.assertIn("кнопка не найдена", out)
        self.assertNotIn("≡  1", out)  # one active account: no threads row

    def test_dry_start_runs_without_a_question(self):
        self.add_accounts("one")
        c = self.controller()
        c.press("enter")
        self.assertIsInstance(c.pending_run, Run)

    def test_live_start_asks_first(self):
        self.add_accounts("one")
        c = self.controller()
        c.press("right")
        self.assertTrue(self.ctx.live)
        self.assertIn("БОЕВОЙ", self.screen(c))
        c.press("enter")
        self.assertIsInstance(c.confirm, Confirm)
        self.assertIn("Публикуем ответы · до 1 акк. (лимиты этапов учитываются)?", self.screen(c))
        c.press("enter")  # «Нет» is selected
        self.assertIsNone(c.pending_run)
        c.press("enter")
        c.press("right")
        c.press("enter")
        self.assertIsInstance(c.pending_run, Run)

    def test_threads_and_split_rows_with_two_accounts(self):
        self.add_accounts("one", "two")
        c = self.controller()
        c.press("down")
        self.assertEqual(c.current_id(), "threads")
        c.press("right")
        self.assertEqual(self.raw()["limits"]["max_concurrent_sessions"], 2)
        c.press("down")
        c.press("right")
        self.assertEqual(self.raw()["limits"]["account_mode"], "share")
        c.press("space")
        self.assertIn("2 ответа на всех", self.screen(c))

    def test_window_toggle(self):
        self.add_accounts("one")
        c = self.controller()
        c.press("down")
        c.press("right")
        self.assertTrue(self.raw()["browser"]["headless"])
        self.assertIn("скрыто", self.screen(c))

    def test_journal_filter_pages_record_and_clear(self):
        self.add_accounts("one")
        self.add_records(("success", "Ответ один", "@a"), ("simulated", "Пробный ответ", "@b"), ("failed", "ошибка X", "@c"))
        c = self.controller()
        c.press("down")
        c.press("down")
        c.press("enter")
        out = self.screen(c)
        self.assertIn("журнал", out)
        self.assertIn("Ответ один", out)
        self.assertIn("стр. 1/1", out)
        c.press("right")
        c.press("right")
        c.press("right")  # filter: errors
        out = self.screen(c)
        self.assertIn("ошибки", out)
        self.assertNotIn("Ответ один", out)
        self.assertIn("ошибка X", out)
        c.press("down")
        c.press("enter")
        self.assertIn("запись #3", self.screen(c))
        c.press("escape")
        c.press("C")
        c.press("right")
        c.press("enter")
        self.assertIn("удалено 3", c.notice.text)
        self.assertIn("записей нет", self.screen(c))

    def launch(self, result, **kwargs):
        run = AsyncMock(return_value=result) if not isinstance(result, BaseException) else AsyncMock(side_effect=result)
        with patch("youtube_bot.run_projects", run):
            notice = run_target(self.ctx, dry_run=kwargs.pop("dry_run", True), **kwargs)
        return notice, run

    def test_run_target_runs_the_projects_and_says_what_happened(self):
        notice, run = self.launch(ProjectsRunSummary([ProjectRunResult("Основной", 2, 7, 5)]))
        args, kwargs = run.call_args
        self.assertEqual(args, (str(self.config_path), True, None))
        self.assertIn("progress", kwargs)
        self.assertEqual(notice.kind, "ok")
        self.assertIn("видео: 7 · ответов: 5", notice.text)

    def test_run_target_one_project(self):
        notice, run = self.launch(ProjectsRunSummary([ProjectRunResult("Основной", 1, 1, 1)]), project_ids=[7])
        self.assertEqual(run.call_args.args, (str(self.config_path), True, [7]))

    def test_run_target_nothing_found_says_what_to_loosen(self):
        main = self.ctx.projects()[0]
        self.ctx.update_project(main.id, min_views=10000, max_views=500000, max_age_hours=24)
        notice, _ = self.launch(ProjectsRunSummary([ProjectRunResult("Основной", 1, 0, 0, "видео не найдено")]))
        self.assertEqual(notice.kind, "warn")
        self.assertIn("Видео не найдено", notice.text)
        self.assertIn("шире свежесть (до 24 ч), шире просмотры (10 тыс – 500 тыс)", notice.text)

    def test_run_target_skips_say_where_to_go(self):
        for reason, words in (("нет аккаунтов, которые могут отвечать сейчас", "«Аккаунты»"),
                              ("нет тем и ссылок", "«Цель»"), ("нет аккаунтов", "«Проекты»"),
                              ("нет шаблонов", "«Ответы»")):
            notice, _ = self.launch(ProjectsRunSummary([ProjectRunResult("Основной", 0, 0, 0, reason)]))
            self.assertEqual(notice.kind, "warn")
            self.assertIn(words, notice.text)
        notice, _ = self.launch(ProjectsRunSummary([]))
        self.assertIn("Нет проектов в запуске", notice.text)

    def test_run_target_several_projects_in_one_line(self):
        notice, _ = self.launch(ProjectsRunSummary([ProjectRunResult("Крипта", 2, 4, 5),
                                                    ProjectRunResult("Фитнес", 0, 0, 0, "нет аккаунтов")]))
        self.assertEqual(notice.kind, "ok")
        self.assertIn("ответов: 5 · Крипта: 5 · Фитнес: пропущен (нет аккаунтов)", notice.text)

    def test_run_target_errors_and_stops_are_notices(self):
        notice, _ = self.launch(RuntimeError("сеть"))
        self.assertEqual(notice.kind, "err")
        self.assertIn("сеть", notice.text)
        notice, _ = self.launch(ProjectsRunSummary([ProjectRunResult("Крипта", 1, 1, 1)], "Фитнес", "RuntimeError: браузер"))
        self.assertEqual(notice.kind, "err")
        self.assertIn("остановлен на проекте «Фитнес»: RuntimeError: браузер", notice.text)

    def test_ctrl_c_says_on_which_project(self):
        async def interrupted(*args, progress=None, **kwargs):
            progress["project"] = "Крипта"
            raise KeyboardInterrupt

        with patch("youtube_bot.run_projects", interrupted):
            notice = run_target(self.ctx, dry_run=False)
        self.assertEqual((notice.kind, notice.text), ("warn", "Боевой запуск прерван на проекте «Крипта»"))

    def test_live_start_without_accounts_able_to_post_says_so(self):
        self.add_accounts("one")
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE accounts SET auth_status = 'failed'")
        conn.commit()
        conn.close()
        c = self.controller()
        c.press("right")
        c.press("enter")
        self.assertIsNone(c.confirm)
        self.assertIsNone(c.pending_run)
        self.assertIn("Нет аккаунтов в работе", c.notice.text)

    def test_icon_choice_is_remembered(self):
        from modules.ui.app import _load_icon_choice, _switch_icons, build_tabs

        c = Controller(build_tabs(self.ctx))
        notice = _switch_icons(self.ctx, c)  # plain → colour
        self.assertTrue(icons.EMOJI)
        self.assertEqual(notice.text, "Значки: цветные")
        self.assertIn("🚀", frags_text(render(c)))
        icons.use_emoji(False)
        with patch.dict("os.environ", {"YTBOT_ICONS": ""}):
            _load_icon_choice(self.ctx)
        self.assertTrue(icons.EMOJI)
        icons.use_emoji(False)

    def test_setup_row_leads_to_its_tab(self):
        other = Tab("Аккаунты", "A", lambda: build_tab(self.ctx).build())
        c = Controller([build_tab(self.ctx), other])
        c.press("enter")
        self.assertEqual(c.tab, 1)


if __name__ == "__main__":
    unittest.main()
