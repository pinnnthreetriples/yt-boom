"""Tab «Цель» of the tabbed interface (the current project): topics, freshness, views, videos per topic, own links,
reference links, whom to answer, replies per video, preview, the project switch."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from config import load_config
from modules.ui import icons
from modules.ui.app import AppContext, _ensure_schema
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
        _ensure_schema(self.ctx)
        self.tab = build_tab(self.ctx)
        self.c = Controller([self.tab])

    def tearDown(self):
        self.temp_dir.cleanup()

    def project(self):
        return self.ctx.project()

    def set(self, **fields) -> None:
        self.ctx.update_project(self.project().id, **fields)
        self.c.refresh()

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

    def paste(self) -> None:
        self.go("pool")
        self.keys("enter", "enter")  # menu → «вставить ссылки»
        self.c.press("paste:" + VIDEOS)
        self.keys("enter", "enter")


class TopicsTest(TargetTabCase):
    def test_nothing_set_warns_and_one_project_has_no_switch_row(self):
        text = self.screen()
        self.assertIn("тем нет", text)
        self.assertIn("0 ждут", text)
        self.assertNotIn("1/1", text)
        self.assertNotIn("видео на тему", text)  # no topics, no references: no filters
        self.assertTrue(self.tab.warn())

    def test_topics_are_added_edited_and_deleted_in_their_screen(self):
        self.go("topics")
        self.keys("enter")
        self.assertIn("тем нет · + добавить", self.screen())
        self.keys("+")
        self.type("iphone 17 обзор")
        self.keys("enter")
        self.assertEqual(self.project().search_topics, ["iphone 17 обзор"])
        self.keys("+")
        self.type("IPHONE 17  обзор")
        self.keys("enter")
        self.assertIn("Такая тема уже есть", self.screen())
        self.keys("+", "enter")
        self.assertIn("Пустая тема не добавлена", self.screen())
        self.keys("+")
        self.type("эфир")
        self.keys("enter")
        self.go("t1")
        self.keys("enter")
        self.type(" новости")
        self.keys("enter")
        self.assertEqual(self.project().search_topics, ["iphone 17 обзор", "эфир новости"])
        self.go("t0")
        self.keys("D", "right", "enter")
        self.assertEqual(self.project().search_topics, ["эфир новости"])
        self.keys("escape")
        self.assertIn("темы 1 · «эфир новости»", self.screen())
        self.assertIn("5 видео на тему за запуск", self.screen())
        self.assertFalse(self.tab.warn())
        self.assertIsNone(load_config(self.config_path).collector.search_query)  # config.yaml stays as it was

    def test_freshness_and_views(self):
        self.set(search_topics=["котики"])
        self.go("age")
        self.keys("right")
        self.assertEqual(self.project().max_age_hours, 12)
        self.go("views")
        self.keys("enter")
        for _ in range(5):
            self.keys("backspace")
        self.type("10к 500к")
        self.keys("enter")
        self.assertEqual((self.project().min_views, self.project().max_views), (10000, 500000))
        self.assertIn("10 тыс – 500 тыс", self.screen())

    def test_bad_views_keep_the_old_range(self):
        self.set(search_topics=["котики"], min_views=5)
        self.go("views")
        self.keys("enter")
        self.type(" много")
        self.keys("enter")
        self.assertEqual(self.project().min_views, 5)
        self.assertIn("Не понял диапазон", self.screen())

    def test_videos_per_topic_by_arrows_and_by_number(self):
        self.set(search_topics=["котики"])
        self.go("videos")
        self.keys("right")
        self.assertEqual(self.project().max_videos, 6)
        self.keys("enter", "backspace")
        self.type("12")
        self.keys("enter")
        self.assertEqual(self.project().max_videos, 12)
        self.assertIn("12 видео на тему за запуск", self.screen())
        self.keys("enter", "backspace", "backspace")
        self.type("0")
        self.keys("enter")
        self.assertIn("от 1 до 50", self.screen())
        self.assertEqual(self.project().max_videos, 12)


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
        self.paste()
        self.assertIn("+2 добавлено", self.screen())
        self.assertIn("2 ждут", self.screen())
        self.assertFalse(self.tab.warn())
        self.assertEqual(self.project().links_total, 2)

        self.keys("enter", "down", "enter")  # menu → «показать список»
        self.assertIn("abcdefghijk", self.screen())
        self.assertIn("0/2", self.screen())
        self.keys("escape")

        self.keys("enter", "down", "down", "enter")  # menu → «очистить» → Confirm (Нет is first)
        self.assertIn("Очистить список видео проекта «Основной»?", self.screen())
        self.keys("right", "enter")
        self.assertIn("Удалено видео: 2", self.screen())
        self.assertIn("0 ждут", self.screen())

    def test_clear_defaults_to_no(self):
        self.paste()
        self.keys("enter", "down", "down", "enter", "enter")
        self.assertIn("2 ждут", self.screen())

    def test_links_and_clearing_stay_in_their_project(self):
        other = self.ctx.db(lambda db: db.create_project("Другой"))
        self.ctx.db(lambda db: db.add_video_to_pool("https://www.youtube.com/watch?v=zzzzzzzzzzz", 2, project_id=other.id))
        self.c.refresh()
        self.paste()
        self.go("pool")
        self.keys("enter", "down", "down", "enter", "right", "enter")
        self.assertIn("Удалено видео: 2", self.screen())
        self.assertEqual(self.ctx.db(lambda db: db.count_pool_videos(project_id=other.id)), 1)

    def test_search_finds_are_not_shown_as_own_links(self):
        self.ctx.db(lambda db: db.queue_search_video("https://www.youtube.com/watch?v=found_vid01", 2,
                                                     project_id=self.project().id))
        self.c.refresh()
        self.assertIn("0 ждут", self.screen())
        self.assertTrue(self.tab.warn())


class ReferencesTest(TargetTabCase):
    def test_inactive_without_own_links(self):
        self.go("refs")
        self.keys("right", "space")
        self.assertEqual((self.project().use_similar, self.project().use_title_words), (False, False))
        self.assertIn("ориентир: похожие ▢ · слова из названия ▢", self.screen())

    def test_arrows_and_space_go_through_the_kinds(self):
        self.paste()
        self.go("refs")
        self.keys("right")
        self.assertEqual((self.project().use_similar, self.project().use_title_words), (True, False))
        self.keys("space")
        self.assertEqual((self.project().use_similar, self.project().use_title_words), (False, True))
        self.keys("right")
        self.assertIn("ориентир: похожие ▣ · слова из названия ▣", self.screen())
        self.assertIn("5 видео на тему за запуск", self.screen())  # the filters apply to the found videos


class ModeAndLimitTest(TargetTabCase):
    def test_mode_changes_and_space_shows_the_example(self):
        self.go("mode")
        self.keys("right")
        self.assertEqual(self.project().target_mode, "popular_parent")
        self.keys("space")
        self.assertIn("отвечаем сюда", self.screen())

    def test_limit_by_arrows_and_by_number_updates_the_list(self):
        self.paste()
        self.go("limit")
        self.keys("right")
        self.assertEqual(self.project().comments_per_video, 3)
        self.assertIn("в списке обновлено: 2", self.screen())
        self.keys("enter", "backspace")
        self.type("7")
        self.keys("enter")
        self.assertEqual(self.project().comments_per_video, 7)
        self.keys("enter", "backspace")
        self.type("99")
        self.keys("enter")
        self.assertIn("от 1 до 50", self.screen())
        self.assertEqual(self.project().comments_per_video, 7)
        self.assertEqual(load_config(self.config_path).limits.max_iterations_per_session, 2)


class PreviewTest(TargetTabCase):
    def test_no_videos(self):
        self.go("preview")
        self.keys("enter")
        self.assertIsNone(self.c.pending_run)
        self.assertIn("нет ни своих ссылок, ни тем", self.screen())

    def test_comments_of_the_first_own_link(self):
        self.paste()
        self.go("preview")
        self.keys("enter")
        found = [SimpleNamespace(text="а батарея как?", like_count=15, parent_text="Камера огонь")]
        with patch("modules.ui.tab_target._fetch_preview", return_value=("https://www.youtube.com/watch?v=abcdefghijk", found)) as fetch:
            self.run_pending()
        self.assertEqual(fetch.call_args.args[2], "https://www.youtube.com/watch?v=abcdefghijk")
        text = self.screen()
        self.assertIn("Комментариев: 1", text)
        self.assertIn("«а батарея как?» ♥15", text)
        self.assertIn("под «Камера огонь»", text)

    def test_without_links_the_first_topic_is_searched(self):
        self.set(search_topics=["котики"])
        self.go("preview")
        self.keys("enter")
        with patch("modules.ui.tab_target._fetch_preview", return_value=(None, [])) as fetch:
            self.run_pending()
        self.assertIsNone(fetch.call_args.args[2])
        self.assertIn("По теме «котики» видео не найдено", self.screen())

    def test_fetch_error_is_a_notice(self):
        self.paste()
        self.go("preview")
        self.keys("enter")
        with patch("modules.ui.tab_target._fetch_preview", side_effect=RuntimeError("offline")):
            self.run_pending()
        self.assertIn("Комментарии не загрузились: offline", self.screen())


class SwitchTest(TargetTabCase):
    def test_arrows_switch_the_current_project_and_writes_go_there(self):
        self.ctx.db(lambda db: db.create_project("Крипта", search_topics=["биткоин"]))
        self.c.refresh()
        self.assertIn("Основной", self.screen())
        self.assertIn("1/2", self.screen())
        self.go("project")
        self.keys("right")
        self.assertEqual(self.project().name, "Крипта")
        self.assertIn("«биткоин»", self.screen())
        self.go("mode")
        self.keys("right")
        projects = {p.name: p for p in self.ctx.projects()}
        self.assertEqual((projects["Крипта"].target_mode, projects["Основной"].target_mode), ("popular_parent", "first_reply"))

    def test_the_warn_follows_the_projects_in_the_launch(self):
        self.set(search_topics=["котики"])
        crypto = self.ctx.db(lambda db: db.create_project("Крипта"))
        self.assertTrue(self.tab.warn())
        self.ctx.update_project(crypto.id, enabled=False)
        self.assertFalse(self.tab.warn())


if __name__ == "__main__":
    unittest.main()
