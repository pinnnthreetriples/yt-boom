"""Screens of the journal / accounts / threads group driven with scripted arrow keys."""
from __future__ import annotations

import asyncio
import io
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from database import Database
from modules.wizard import (
    _confirm_country_mismatch,
    _confirm_country_mismatch_async,
)
from tests.tui_keys import DOWN, ENTER, ESC, scripted_keys

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


class TestAccounts(ScreenCase):

    def test_country_mismatch_question_defaults_to_cancel(self):
        msg = "Аккаунт «bob»: ожидаемая страна PL, а прокси выходит из страны US.\nРиск."
        for keys, expected in (((ENTER,), False), ((ESC,), False), ((DOWN, ENTER), True)):
            with self.subTest(keys=keys):
                result, screen, _ = self.run_keys(lambda cfg: _confirm_country_mismatch(msg), keys)
                self.assertIs(result, expected)
        self.assertIn("Отменить вход", screen)
        self.assertIn("Продолжить вход", screen)
        self.assertIn("ожидаемая страна PL", screen)

    def test_country_mismatch_question_does_not_block_the_event_loop(self):
        with patch("modules.wizard._confirm_country_mismatch", return_value=True) as ask:
            self.assertTrue(asyncio.run(_confirm_country_mismatch_async("msg")))
        ask.assert_called_once_with("msg")


class TestPasteAndParsing(ScreenCase):

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
