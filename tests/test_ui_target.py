"""Tab «Цель» of the tabbed interface: source, topic, freshness, views, links, whom to answer, limit, preview."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from config import load_config
from modules.ui import icons
from modules.ui.app import AppContext
from modules.ui.shell import Controller, Run, frags_text, render
from modules.ui.tab_target import build_tab, parse_views, views_label

VIDEOS = "https://www.youtube.com/watch?v=abcdefghijk\nhttps://www.youtube.com/watch?v=12345678901"


class TargetTabCase(unittest.TestCase):
    def setUp(self):
        icons.use_emoji(False)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "cfg.yaml"
        data = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
        data["app"]["db_path"] = str(self.root / "t.db")
        data["app"]["log_file"] = str(self.root / "t.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["collector"]["search_query"] = None
        data["collector"]["target_mode"] = "first_reply"
        data["collector"]["max_age_hours"] = None
        data["collector"]["min_views"] = 0
        data["collector"]["max_views"] = None
        data["limits"]["max_iterations_per_session"] = 2
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.ctx = AppContext(self.config_path)
        self.tab = build_tab(self.ctx)
        self.c = Controller([self.tab])

    def tearDown(self):
        self.temp_dir.cleanup()

    def cfg(self):
        return load_config(self.config_path)

    def screen(self) -> str:
        return frags_text(render(self.c))

    def keys(self, *keys: str) -> None:
        for key in keys:
            self.c.press(key)

    def type(self, text: str) -> None:
        for ch in text:
            self.c.press("space" if ch == " " else ch)

    def go(self, row_id: str) -> None:
        self.c.select(row_id)

    def run_pending(self):
        run = self.c.pending_run
        self.assertIsInstance(run, Run)
        self.c.finish_run(run.work())


class SourceTest(TargetTabCase):
    def test_links_by_default_and_the_tab_warns_without_videos(self):
        text = self.screen()
        self.assertIn("свои ссылки", text)
        self.assertIn("0 ждут", text)
        self.assertTrue(self.tab.warn())

    def test_switching_to_search_asks_for_a_topic_then_saves_it(self):
        self.keys("left")
        self.assertIn("тема не задана", self.screen())
        self.go("topic")
        self.keys("enter")
        self.type("iphone 17 обзор")
        self.keys("enter")
        self.assertEqual(self.cfg().collector.search_query, "iphone 17 обзор")
        self.assertIn("«iphone 17 обзор»", self.screen())
        self.assertFalse(self.tab.warn())

    def test_links_clear_the_query_and_search_brings_it_back(self):
        self.ctx.update({"collector.search_query": "котики"})
        self.c.refresh()
        self.keys("right")
        self.assertIsNone(self.cfg().collector.search_query)
        self.assertIn("свои ссылки", self.screen())
        self.keys("left")
        self.assertEqual(self.cfg().collector.search_query, "котики")

    def test_freshness_and_views(self):
        self.ctx.update({"collector.search_query": "котики"})
        self.c.refresh()
        self.go("age")
        self.keys("right")
        self.assertEqual(self.cfg().collector.max_age_hours, 12)
        self.go("views")
        self.keys("enter")
        for _ in range(5):
            self.keys("backspace")
        self.type("10к 500к")
        self.keys("enter")
        self.assertEqual((self.cfg().collector.min_views, self.cfg().collector.max_views), (10000, 500000))
        self.assertIn("10 тыс – 500 тыс", self.screen())

    def test_bad_views_keep_the_old_range(self):
        self.ctx.update({"collector.search_query": "котики", "collector.min_views": 5})
        self.c.refresh()
        self.go("views")
        self.keys("enter")
        self.type(" много")
        self.keys("enter")
        self.assertEqual(self.cfg().collector.min_views, 5)
        self.assertIn("Не понял диапазон", self.screen())


class ViewsParseTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_views("10к 500к"), (10000, 500000))
        self.assertEqual(parse_views("10000-500000"), (10000, 500000))
        self.assertEqual(parse_views("от 10 тыс до 1,5 млн"), (10000, 1500000))
        self.assertEqual(parse_views("5000"), (5000, None))
        self.assertEqual(parse_views("0 1млн"), (0, 1000000))

    def test_nonsense(self):
        for text in ("много", "500 10", "1 2 3", ""):
            with self.assertRaises(ValueError):
                parse_views(text)

    def test_label(self):
        self.assertEqual(views_label(0, None), "любые")
        self.assertEqual(views_label(10000, None), "от 10 тыс")
        self.assertEqual(views_label(0, 1500000), "до 1,5 млн")


class LinksTest(TargetTabCase):
    def test_paste_show_and_clear(self):
        self.go("pool")
        self.keys("enter", "enter")  # menu → «вставить ссылки»
        self.c.press("paste:" + VIDEOS)
        self.keys("enter", "enter")
        self.assertIn("+2 добавлено", self.screen())
        self.assertIn("2 ждут", self.screen())
        self.assertFalse(self.tab.warn())

        self.keys("enter", "down", "enter")  # menu → «показать список»
        self.assertIn("abcdefghijk", self.screen())
        self.assertIn("0/2", self.screen())
        self.keys("escape")

        self.keys("enter", "down", "down", "enter")  # menu → «очистить» → Confirm (Нет is first)
        self.assertIn("Очистить список видео?", self.screen())
        self.keys("right", "enter")
        self.assertIn("Удалено видео: 2", self.screen())
        self.assertIn("0 ждут", self.screen())

    def test_clear_defaults_to_no(self):
        self.go("pool")
        self.keys("enter", "enter")
        self.c.press("paste:" + VIDEOS)
        self.keys("enter", "enter", "enter", "down", "down", "enter", "enter")
        self.assertIn("2 ждут", self.screen())


class ModeAndLimitTest(TargetTabCase):
    def test_mode_changes_and_space_shows_the_example(self):
        self.go("mode")
        self.keys("right")
        self.assertEqual(self.cfg().collector.target_mode, "popular_parent")
        self.keys("space")
        self.assertIn("отвечаем сюда", self.screen())

    def test_limit_by_arrows_and_by_number_updates_the_list(self):
        self.go("pool")
        self.keys("enter", "enter")
        self.c.press("paste:" + VIDEOS)
        self.keys("enter", "enter")
        self.go("limit")
        self.keys("right")
        self.assertEqual(self.cfg().limits.max_iterations_per_session, 3)
        self.assertIn("в списке обновлено: 2", self.screen())
        self.keys("enter", "backspace")
        self.type("7")
        self.keys("enter")
        self.assertEqual(self.cfg().limits.max_iterations_per_session, 7)
        self.assertEqual(self.cfg().collector.comments_per_video, 7)
        self.keys("enter", "backspace")
        self.type("99")
        self.keys("enter")
        self.assertIn("от 1 до 50", self.screen())
        self.assertEqual(self.cfg().limits.max_iterations_per_session, 7)


class PreviewTest(TargetTabCase):
    def test_no_videos(self):
        self.go("preview")
        self.keys("enter")
        self.assertIsNone(self.c.pending_run)
        self.assertIn("список пуст", self.screen())

    def test_comments_show_under_the_tab(self):
        self.go("pool")
        self.keys("enter", "enter")
        self.c.press("paste:" + VIDEOS)
        self.keys("enter", "enter")
        self.go("preview")
        self.keys("enter")
        found = [SimpleNamespace(text="а батарея как?", like_count=15, parent_text="Камера огонь")]
        with patch("modules.ui.tab_target._fetch_preview", return_value=found):
            self.run_pending()
        text = self.screen()
        self.assertIn("Комментариев: 1", text)
        self.assertIn("«а батарея как?» ♥15", text)
        self.assertIn("под «Камера огонь»", text)

    def test_fetch_error_is_a_notice(self):
        self.go("pool")
        self.keys("enter", "enter")
        self.c.press("paste:" + VIDEOS)
        self.keys("enter", "enter")
        self.go("preview")
        self.keys("enter")
        with patch("modules.ui.tab_target._fetch_preview", side_effect=RuntimeError("offline")):
            self.run_pending()
        self.assertIn("Комментарии не загрузились: offline", self.screen())


class VideosPerRunTest(TargetTabCase):
    def test_videos_per_run_by_arrows_and_by_number(self):
        self.ctx.update({"collector.search_query": "котики"})
        self.c.refresh()
        self.assertIn("5 видео за запуск", self.screen())
        self.go("videos")
        self.keys("right")
        self.assertEqual(self.cfg().collector.max_videos, 6)
        self.keys("enter", "backspace")
        self.type("12")
        self.keys("enter")
        self.assertEqual(self.cfg().collector.max_videos, 12)
        self.assertIn("12 видео за запуск", self.screen())
        self.keys("enter", "backspace", "backspace")
        self.type("0")
        self.keys("enter")
        self.assertIn("от 1 до 50", self.screen())
        self.assertEqual(self.cfg().collector.max_videos, 12)

    def test_own_links_have_no_videos_per_run_row(self):
        self.assertNotIn("видео за запуск", self.screen())

    def test_search_finds_are_not_shown_as_own_links(self):
        self.ctx.db(lambda db: db.queue_search_video("https://www.youtube.com/watch?v=found_vid01", comments_target=2))
        self.c.refresh()
        self.assertIn("0 ждут", self.screen())
        self.assertTrue(self.tab.warn())


if __name__ == "__main__":
    unittest.main()
