"""Screens of the journal / accounts / threads group driven with scripted arrow keys."""
from __future__ import annotations

import asyncio
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from database import ActivityLogCreate, Database
from modules.wizard import (
    _threads_summary,
    activity_log_viewer,
    add_account_stepwise,
    list_accounts_wizard,
    load_accounts,
    login_account_wizard,
    step_threads,
)
from tests.tui_keys import DOWN, ENTER, ESC, SPACE, scripted_keys

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class ScreenCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "t.db"
        self.config_path = self.root / "cfg.yaml"
        with open("config.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["app"]["db_path"] = str(self.db_path)
        data["app"]["log_file"] = str(self.root / "t.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["browser"]["accounts"] = []
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def db(self, fn):
        async def _run():
            db = Database(self.db_path)
            await db.connect()
            await db.init_schema()
            try:
                return await fn(db)
            finally:
                await db.close()
        return asyncio.run(_run())

    def seed_accounts(self, *names: str) -> None:
        async def _seed(db):
            for name in names:
                await db.upsert_account(name, str(self.root / "sessions" / name), None, True)
        self.db(_seed)

    def run_keys(self, func, keys: tuple[str, ...], answers=None):
        """Run func(config_path) with scripted keys; prompt_user answers come from `answers`."""
        out = io.StringIO()
        with scripted_keys(*keys) as screen, \
             patch("modules.wizard.prompt_user", side_effect=answers or (lambda *a, **k: "")), \
             patch("sys.stdout", out):
            result = func(self.config_path)
        return result, ANSI.sub("", screen.getvalue()), out.getvalue()


class TestJournal(ScreenCase):
    def seed_log(self, n: int = 10) -> None:
        async def _seed(db):
            for i in range(1, n + 1):
                await db.log_activity(ActivityLogCreate(
                    video_id=f"v{i}", target_id=f"c{i}", target_author=f"@user_{i}",
                    target_text=f"Target {i}", reply_text=f"Reply number {i}", mode="live",
                    status="success" if i % 2 == 0 else "failed",
                ))
        self.db(_seed)

    def test_arrows_detail_pages_filter_clear(self):
        self.seed_log()

        def answers(text, default=None):
            return "y" if "Очистить" in text else ""

        _, screen, out = self.run_keys(
            activity_log_viewer,
            (ENTER, "n", "f", DOWN, ENTER, "c", ESC),
            answers,
        )
        self.assertIn("Reply number 10", screen)     # newest record first, one row per record
        self.assertIn("Запись #10", out)             # Enter opened its card
        self.assertIn("стр. 2/2", out)               # N went to the next page
        self.assertIn("отправленные", out)           # filter picked with arrows
        self.assertIn("Журнал очищен, удалено записей: 10", screen)  # clearing ignores the filter
        self.assertEqual(self.db(lambda db: db.count_activity_logs()), 0)
        for line in out.splitlines():
            self.assertLessEqual(len(line), 70, line)

    def test_escape_leaves_and_empty_journal(self):
        _, screen, out = self.run_keys(activity_log_viewer, (ESC,))
        self.assertIn("Записей нет", screen)
        self.assertIn("Журнал отработок", out)


class TestAccounts(ScreenCase):
    def test_table_fits_70_columns(self):
        async def _seed(db):
            await db.upsert_account("a_very_long_account_name_that_overflows", "s1",
                                    "http://user:password@very-long-proxy-hostname.example.com:8080", True)
            await db.upsert_account("off", "s2", None, False)
        self.db(_seed)
        out = io.StringIO()
        with patch("sys.stdout", out):
            results = list_accounts_wizard(self.config_path)
        self.assertEqual(len(results), 2)
        lines = out.getvalue().splitlines()
        self.assertIn("активных: 1", lines[-1])
        for line in lines:
            self.assertLessEqual(len(line), 70, line)

    def test_login_picks_account_with_arrows(self):
        self.seed_accounts("first", "second")
        with patch("youtube_bot.interactive_login", new_callable=AsyncMock) as login:
            ok, screen, _ = self.run_keys(login_account_wizard, (DOWN, ENTER))
        self.assertTrue(ok)
        login.assert_called_once_with(str(self.config_path), account_name="second")
        self.assertIn("○ нет входа", screen)

    def test_login_escape_cancels(self):
        self.seed_accounts("first")
        with patch("youtube_bot.interactive_login", new_callable=AsyncMock) as login:
            ok, _, _ = self.run_keys(login_account_wizard, (ESC,))
        self.assertFalse(ok)
        login.assert_not_called()

    def test_stepwise_asks_name_and_login_only(self):
        ok, screen, out = self.run_keys(add_account_stepwise, (), ["wiz_acc", "n"])
        self.assertTrue(ok)
        self.assertNotIn("Без прокси", screen)
        acc = next(a for a in load_accounts(self.config_path) if a.name == "wiz_acc")
        self.assertIsNone(acc.proxy_url)
        self.assertEqual(Path(acc.session_path).as_posix(), "test_sessions/wiz_acc")  # folder is chosen automatically

    def test_stepwise_empty_name_cancels(self):
        before = [a.name for a in load_accounts(self.config_path)]
        ok, _, _ = self.run_keys(add_account_stepwise, (), [""])
        self.assertFalse(ok)
        self.assertEqual([a.name for a in load_accounts(self.config_path)], before)


class TestThreads(ScreenCase):
    def cfg(self):
        from config import load_config
        return load_config(self.config_path)

    def test_threads_appear_only_with_two_accounts(self):
        self.seed_accounts("solo")
        self.assertEqual(_threads_summary(self.cfg()), "")
        self.seed_accounts("second")
        self.assertIn("каждый — весь список", _threads_summary(self.cfg()))

    def test_set_threads_and_mode_with_space_hint(self):
        self.seed_accounts("first", "second")
        # Enter «Потоки» → 2; ↓↓ Space shows the «share» example, Enter picks «Список делится»; Esc
        _, screen, _ = self.run_keys(step_threads, (ENTER, DOWN, DOWN, SPACE, ENTER, ESC), ["2"])
        cfg = self.cfg()
        self.assertEqual(cfg.limits.max_concurrent_sessions, 2)
        self.assertEqual(cfg.limits.account_mode, "share")
        self.assertIn("Под каждым видео всего 2 ответа, их пишут A и B по очереди.", screen)
        self.assertIn("● ответы под видео делятся", screen)

    def test_threads_above_accounts_rejected(self):
        self.seed_accounts("first", "second")
        before = self.cfg().limits.max_concurrent_sessions
        _, _, out = self.run_keys(step_threads, (ENTER, ESC), ["5"])
        self.assertEqual(self.cfg().limits.max_concurrent_sessions, before)
        self.assertIn("Нужно число от 1 до 2", out)


class TestPasteAndParsing(ScreenCase):
    def test_paste_fallback_reads_until_empty_line(self):
        from modules.wizard import _pools_read_paste
        with patch("modules.wizard.prompt_user", side_effect=["a", "b", "", "never"]):
            self.assertEqual(_pools_read_paste(), "a\nb")
        with patch("modules.wizard.prompt_user", side_effect=[""]):
            self.assertEqual(_pools_read_paste(), "")  # empty first line = cancel

    def test_parse_video_text(self):
        from modules.pool_manager import parse_video_text
        urls, invalid = parse_video_text(
            "# comment\nhttps://youtu.be/dQw4w9WgXcQ\nhttps://www.youtube.com/watch?v=dQw4w9WgXcQ\n"
            "https://youtube.com/shorts/12345678901\nabcdefghijk  # inline\n\nnot a video\n"
        )
        self.assertEqual(urls, [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=12345678901",
            "https://www.youtube.com/watch?v=abcdefghijk",
        ])
        self.assertEqual(invalid, 1)
    def test_add_from_text_counts_and_db_clear(self):
        from modules.pool_manager import PoolManager

        async def fn(db):
            manager = PoolManager(db)
            first = await manager.add_videos_from_text("dQw4w9WgXcQ\nbad")
            second = await manager.add_videos_from_text("https://youtu.be/dQw4w9WgXcQ\nabcdefghijk")
            await db.update_pool_video_progress(second.items[1].id, comments_added=5)  # completed
            counts = (await db.count_pool_videos(), await db.count_pool_videos("pending"),
                      await db.count_pool_videos("completed"))
            cleared = await db.clear_video_pool()
            return first, second, counts, cleared, await db.count_pool_videos()

        first, second, counts, cleared, after = self.db(fn)
        self.assertEqual((first.added, first.existing, first.invalid), (1, 0, 1))
        self.assertEqual((second.added, second.existing, second.invalid), (1, 1, 0))
        self.assertEqual(counts, (2, 1, 1))
        self.assertEqual(cleared, 2)
        self.assertEqual(after, 0)


if __name__ == "__main__":
    unittest.main()
