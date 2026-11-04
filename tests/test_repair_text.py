"""Emoji typed in the Windows console arrive as UTF-16 surrogate halves: they are joined before they are stored."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from config import load_config, repair_text

HIGH, LOW = chr(0xD83D), chr(0xDE33)  # 😳 as two halves


class RepairText(unittest.TestCase):
    def test_halves_become_one_emoji(self):
        self.assertEqual(repair_text("знал?" + HIGH + LOW), "знал?😳")

    def test_a_lone_half_is_dropped_and_plain_text_is_kept(self):
        self.assertEqual(repair_text("a" + HIGH + "b"), "ab")
        self.assertEqual(repair_text("обычный текст"), "обычный текст")

    def test_nested_values(self):
        self.assertEqual(repair_text({"x": [HIGH + LOW, 3]}), {"x": ["😳", 3]})

    def test_config_with_escaped_halves_loads_whole_emoji(self):
        with open("config.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.yaml"
            text = yaml.safe_dump(repair_text(data), allow_unicode=True)
            text += 'custom_replies:\n  - "ЧЕЛ ТЫ СЕРЬЕЗНО НЕ ЗНАЛ???\uD83D\uDE33"\n'
            path.write_text(text, encoding="utf-8")
            cfg = load_config(path)
        self.assertIn("ЧЕЛ ТЫ СЕРЬЕЗНО НЕ ЗНАЛ???😳", (cfg.custom_replies or []) + list(cfg.processor.custom_replies or []))

    def test_update_config_file_writes_whole_emoji(self):
        from modules.wizard import update_config_file

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfg.yaml"
            path.write_text("a: 1\n", encoding="utf-8")
            update_config_file(path, {"processor.custom_replies": ["ok" + HIGH + LOW]})
            self.assertIn("ok😳", path.read_text(encoding="utf-8"))


class ShellPaste(unittest.TestCase):
    def test_pasted_halves_are_joined_in_a_field(self):
        from modules.ui.shell import Cell, Controller, Field, Row, Tab, View

        got: list[str] = []
        view = View(rows=[Row("a", [Cell("x")], enter=lambda: Field("текст", lambda v: got.append(v)))])
        c = Controller([Tab("Т", "т", lambda: view)])
        c.press("enter")
        c.press("paste:привет " + HIGH + LOW)
        c.press("enter")
        self.assertEqual(got, ["привет 😳"])


if __name__ == "__main__":
    unittest.main()
