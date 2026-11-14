"""Tab «Ответы» of the tabbed interface: who writes, rewording, templates (of the current project), the DeepSeek key
(common to every project)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from config import load_config
from modules.ui import icons
from modules.ui.app import AppContext, _ensure_schema
from modules.ui.shell import Controller, Run, frags_text, render
from modules.ui.tab_replies import build_tab


class RepliesTabCase(unittest.TestCase):
    def setUp(self):
        icons.use_emoji(False)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "cfg.yaml"
        data = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
        data["app"]["db_path"] = str(self.root / "t.db")
        data["app"]["log_file"] = str(self.root / "t.log")
        data["processor"]["reply_mode"] = "ai"
        data["processor"]["custom_replies"] = []
        data["processor"]["paraphrase_custom"] = True
        data.pop("reply_mode", None)
        data.pop("custom_replies", None)
        data["deepseek"]["api_key"] = "sk-test-key-123456"
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.ctx = AppContext(self.config_path)
        _ensure_schema(self.ctx)  # as run_app does: «Основной» is seeded from this config
        self.tab = build_tab(self.ctx)
        self.c = Controller([self.tab])

    def tearDown(self):
        self.temp_dir.cleanup()

    def cfg(self):
        return load_config(self.config_path)

    def project(self):
        return self.ctx.project()

    def screen(self) -> str:
        return frags_text(render(self.c))

    def keys(self, *keys: str) -> None:
        for key in keys:
            self.c.press(key)

    def type(self, text: str) -> None:
        for ch in text:
            self.c.press("space" if ch == " " else ch)

    def run_pending(self):
        run = self.c.pending_run
        self.assertIsInstance(run, Run)
        self.c.finish_run(run.work())


class WriterTest(RepliesTabCase):
    def test_ai_with_a_key(self):
        text = self.screen()
        self.assertIn("ИИ DeepSeek", text)
        self.assertIn("sk-te…3456", text)
        self.assertFalse(self.tab.warn())

    def test_templates_without_templates_warn(self):
        self.keys("right")
        self.assertEqual(self.project().reply_mode, "custom")
        self.assertEqual(self.cfg().processor.reply_mode, "ai")  # config.yaml is not the project's
        text = self.screen()
        self.assertIn("Шаблонов нет", text)
        self.assertIn("менять слова", text)
        self.assertTrue(self.tab.warn())
        self.keys("left")
        self.assertEqual(self.project().reply_mode, "ai")

    def test_ai_without_a_key_warns(self):
        self.ctx.update({"deepseek.api_key": "sk-your-deepseek-key"})
        self.c.refresh()
        self.assertTrue(self.tab.warn())
        self.assertIn("не задан", self.screen())

    def test_rewording_toggle(self):
        self.keys("right")
        self.c.select("shuffle")
        self.keys("right")
        self.assertFalse(self.project().paraphrase_custom)
        self.assertIn("как есть", self.screen())


class TextStyleTest(RepliesTabCase):
    """Reply language and Latin letter mixing: common to every project, saved into config.yaml, both writers."""

    def test_language_is_picked_with_the_arrows(self):
        self.assertEqual(self.cfg().processor.reply_language, "auto")
        self.assertIn("язык: авто", self.screen())
        self.c.select("language")
        self.keys("right")
        self.assertEqual(self.cfg().processor.reply_language, "ru")
        self.assertIn("Язык ответов: русский", self.screen())
        self.assertIn("язык: русский", self.screen())
        self.keys("left")
        self.assertEqual(self.cfg().processor.reply_language, "auto")

    def test_language_warns_when_templates_cannot_be_translated(self):
        self.keys("right")  # templates
        self.ctx.update_project(self.project().id, custom_replies=["Привет"], paraphrase_custom=False)
        self.c.select("language")
        self.keys("right")
        self.assertIn("Шаблоны переводятся только при включённой рандомизации", self.screen())

    def test_mix_level_is_picked_in_both_writers(self):
        for writer in ("ai", "custom"):
            self.ctx.update_project(self.project().id, reply_mode=writer, custom_replies=["Привет"])
            self.ctx.update({"processor.latin_mix_probability": 0.0})
            self.c.refresh()
            self.assertIn("смешивание букв: выкл", self.screen())
            self.c.select("mix")
            self.keys("right", "right")
            self.assertEqual(self.cfg().processor.latin_mix_probability, 0.3)
            self.assertIn("Смешивание букв: иногда · 30%", self.screen())
            self.assertIn("смешивание букв: иногда · 30%", self.screen())
            self.keys("left", "left")
            self.assertEqual(self.cfg().processor.latin_mix_probability, 0.0)
            self.assertIn("Смешивание букв выключено", self.screen())

    def test_unusual_values_from_the_config_are_shown_as_they_are(self):
        self.ctx.update({"processor.latin_mix_probability": 0.25, "processor.reply_language": "it"})
        self.c.refresh()
        text = self.screen()
        self.assertIn("смешивание букв: 25%", text)
        self.assertIn("язык: it", text)

    def test_rows_are_under_the_common_header_with_several_projects(self):
        self.ctx.db(lambda db: db.create_project("Фитнес", reply_mode="custom", custom_replies=["Класс"]))
        self.c.refresh()
        text = self.screen()
        self.assertIn("── общее для всех проектов", text)
        self.assertLess(text.index("── общее для всех проектов"), text.index("язык:"))


class KeyTest(RepliesTabCase):
    def enter_key(self, key: str) -> None:
        self.c.select("key")
        self.keys("enter")
        self.type(key)
        self.assertNotIn(key, self.screen())  # secret: shown as dots
        self.keys("enter")

    def test_good_key_is_checked_then_saved(self):
        self.enter_key("sk-good-key-0000000")
        with patch("modules.wizard.check_deepseek_api", AsyncMock(return_value=(True, "Ключ активен"))):
            self.run_pending()
        self.assertEqual(self.cfg().deepseek.api_key, "sk-good-key-0000000")
        self.assertIn("Ключ сохранён", self.screen())
        self.assertIn("✓", self.screen())

    def test_rejected_key_is_kept_until_confirmed(self):
        self.enter_key("sk-bad-key-11111111")
        with patch("modules.wizard.check_deepseek_api", AsyncMock(return_value=(False, "401"))):
            self.run_pending()
        self.assertEqual(self.cfg().deepseek.api_key, "sk-test-key-123456")
        self.assertIn("Ключ не принят", self.screen())
        self.keys("enter")
        self.assertIn("не прошёл проверку", self.screen())
        self.keys("right", "enter")
        self.assertEqual(self.cfg().deepseek.api_key, "sk-bad-key-11111111")

    def test_rejected_key_no_keeps_the_old_one(self):
        self.enter_key("sk-bad-key-11111111")
        with patch("modules.wizard.check_deepseek_api", AsyncMock(return_value=(False, "401"))):
            self.run_pending()
        self.keys("enter", "enter")
        self.assertEqual(self.cfg().deepseek.api_key, "sk-test-key-123456")

    def test_empty_key_changes_nothing(self):
        self.c.select("key")
        self.keys("enter", "enter")
        self.assertIsNone(self.c.pending_run)
        self.assertIn("Ключ не введён", self.screen())

    def test_space_says_where_to_get_a_key(self):
        self.c.select("key")
        self.keys("space")
        self.assertIn("platform.deepseek.com", self.screen())


class TemplatesTest(RepliesTabCase):
    def open(self):
        self.c.select("templates")
        self.keys("enter")

    def test_add_edit_delete(self):
        self.open()
        self.assertIn("шаблонов нет", self.screen())
        self.keys("+")
        self.type("Отличный обзор")
        self.keys("enter")
        self.assertEqual(self.project().custom_replies, ["Отличный обзор"])
        self.keys("+")
        self.type("Согласен")
        self.keys("enter")
        self.assertIn("Согласен", self.screen())

        self.keys("enter")
        self.type(", спасибо")
        self.keys("enter")
        self.assertEqual(self.project().custom_replies[0], "Отличный обзор, спасибо")

        self.keys("D")
        self.assertIn("Удалить шаблон?", self.screen())
        self.keys("right", "enter")
        self.assertEqual(self.project().custom_replies, ["Согласен"])

        self.keys("escape")
        self.assertIn("шаблоны 1", self.screen())

    def test_edit_a_template_in_the_middle(self):
        self.ctx.update_project(self.project().id, custom_replies=["Секрет в нике"])
        self.open()
        self.keys("enter")  # the template opens with the cursor at its end
        self.keys("home", "c-right")
        self.type(" 🔥")
        self.keys("c-right", "c-right", "backspace", "backspace", "backspace", "backspace")
        self.type("моём нике")
        self.keys("enter")
        self.assertEqual(self.project().custom_replies, ["Секрет 🔥 в моём нике"])

    def test_delete_defaults_to_no(self):
        self.ctx.update_project(self.project().id, custom_replies=["a", "b"])
        self.open()
        self.keys("delete", "enter")
        self.assertEqual(self.project().custom_replies, ["a", "b"])



class SeveralProjectsTest(RepliesTabCase):
    def setUp(self):
        super().setUp()
        self.other = self.ctx.db(lambda db: db.create_project("Фитнес", reply_mode="custom", custom_replies=["Класс"]))
        self.c.refresh()

    def test_the_switch_row_and_each_project_keeps_its_replies(self):
        text = self.screen()
        self.assertIn("Основной", text)
        self.assertIn("1/2", text)
        self.assertIn("── общее для всех проектов", text)
        self.keys("right")  # the switch row is the first one
        self.assertEqual(self.project().name, "Фитнес")
        self.assertIn("мои шаблоны", self.screen())
        self.c.select("writer")
        self.keys("left")
        self.assertEqual(self.project().reply_mode, "ai")
        main = next(p for p in self.ctx.projects() if p.name == "Основной")
        self.assertEqual(main.reply_mode, "ai")
        self.assertEqual(self.ctx.projects()[1].custom_replies, ["Класс"])

    def test_templates_are_the_projects(self):
        self.ctx.set_project(self.other.id)
        self.c.select("templates")
        self.keys("enter")
        self.assertIn("Фитнес › шаблоны · 1", self.screen())
        self.keys("+")
        self.type("Огонь")
        self.keys("enter")
        self.assertEqual(self.project().custom_replies, ["Класс", "Огонь"])
        self.assertEqual(next(p for p in self.ctx.projects() if p.name == "Основной").custom_replies, [])

    def test_warn_follows_the_projects_in_the_launch(self):
        self.assertFalse(self.tab.warn())
        self.ctx.update_project(self.other.id, custom_replies=[])
        self.assertTrue(self.tab.warn())
        self.ctx.update_project(self.other.id, enabled=False)
        self.assertFalse(self.tab.warn())


if __name__ == "__main__":
    unittest.main()
