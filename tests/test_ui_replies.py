"""Tab «Ответы» of the tabbed interface: who writes, the DeepSeek key, rewording, templates."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from config import load_config
from modules.ui import icons
from modules.ui.app import AppContext
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
        cfg = self.cfg()
        self.assertEqual(cfg.processor.reply_mode, "custom")
        text = self.screen()
        self.assertIn("Шаблонов нет", text)
        self.assertIn("менять слова", text)
        self.assertTrue(self.tab.warn())
        self.keys("left")
        self.assertEqual(self.cfg().processor.reply_mode, "ai")

    def test_ai_without_a_key_warns(self):
        self.ctx.update({"deepseek.api_key": "sk-your-deepseek-key"})
        self.c.refresh()
        self.assertTrue(self.tab.warn())
        self.assertIn("не задан", self.screen())

    def test_rewording_toggle(self):
        self.keys("right")
        self.c.select("shuffle")
        self.keys("right")
        self.assertFalse(self.cfg().processor.paraphrase_custom)
        self.assertIn("как есть", self.screen())


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
        self.assertEqual(self.cfg().processor.custom_replies, ["Отличный обзор"])
        self.keys("+")
        self.type("Согласен")
        self.keys("enter")
        self.assertIn("Согласен", self.screen())

        self.keys("enter")
        self.type(", спасибо")
        self.keys("enter")
        self.assertEqual(self.cfg().processor.custom_replies[0], "Отличный обзор, спасибо")

        self.keys("D")
        self.assertIn("Удалить шаблон?", self.screen())
        self.keys("right", "enter")
        self.assertEqual(self.cfg().processor.custom_replies, ["Согласен"])

        self.keys("escape")
        self.assertIn("шаблоны 1", self.screen())

    def test_delete_defaults_to_no(self):
        self.ctx.update({"processor.custom_replies": ["a", "b"], "custom_replies": ["a", "b"]})
        self.open()
        self.keys("delete", "enter")
        self.assertEqual(self.cfg().processor.custom_replies, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
