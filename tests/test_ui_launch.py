"""Tab «Запуск»: first-run list, start (dry / live with a question), threads, window, journal and one record."""
from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

from database import Database
from modules.ui import icons
from modules.ui.app import AppContext, status_cells
from modules.ui.shell import Confirm, Controller, Run, Tab, frags_text, render
from modules.ui.tab_launch import build_tab, run_target


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
        db = Database(str(self.db_path))
        await db.connect()
        await db.init_schema()
        await db.close()

    def tearDown(self):
        self.temp_dir.cleanup()

    def raw(self) -> dict:
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8"))

    def add_accounts(self, *names: str) -> None:
        async def go():
            db = Database(str(self.db_path))
            await db.connect()
            await db.init_schema()
            for name in names:
                await db.upsert_account(name, session_path=str(self.root / "sessions" / name))
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
        self.assertIn("Публикуем ответы с 1 акк.?", self.screen(c))
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

    def test_run_target_without_videos_tells_where_to_go(self):
        data = self.raw()
        data["collector"]["search_query"] = None
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        notice = run_target(self.ctx, dry_run=True)
        self.assertEqual(notice.kind, "warn")
        self.assertIn("«Цель»", notice.text)

    def test_run_target_search_reports_errors_as_a_notice(self):
        with patch("youtube_bot.run_multi_video_pipeline", side_effect=RuntimeError("сеть")):
            notice = run_target(self.ctx, dry_run=True)
        self.assertEqual(notice.kind, "err")
        self.assertIn("сеть", notice.text)

    def test_setup_row_leads_to_its_tab(self):
        other = Tab("Аккаунты", "A", lambda: build_tab(self.ctx).build())
        c = Controller([build_tab(self.ctx), other])
        c.press("enter")
        self.assertEqual(c.tab, 1)


if __name__ == "__main__":
    unittest.main()
