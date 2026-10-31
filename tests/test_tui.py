from __future__ import annotations

import io
import unittest
from unittest.mock import MagicMock, patch

from modules import tui
from tests.tui_keys import DOWN, ENTER, ESC, LEFT, RIGHT, SPACE, UP, scripted_keys

ROWS = [
    ("1", "Первый", "статус", "ok"),
    ("2", "Второй", "", ""),
    None,
    ("D", "Действие", "", ""),
]


def menu(*keys: str, **kwargs):
    with scripted_keys(*keys) as screen:
        result = tui.select_menu(kwargs.pop("rows", ROWS), ["HDR"], interactive=True, **kwargs)
    return result, screen.getvalue()


class TestSelectMenu(unittest.TestCase):
    def test_arrows_skip_blank_rows_and_enter_picks(self):
        result, screen = menu(DOWN, DOWN, ENTER)
        self.assertEqual(result, ("D", 3))
        self.assertIn("Действие", screen)
        self.assertIn("HDR", screen)

    def test_up_wraps_and_space_picks(self):
        self.assertEqual(menu(UP, SPACE)[0], ("D", 3))

    def test_hotkeys_hidden_keys_and_unknown(self):
        self.assertEqual(menu("x", "2")[0], ("2", 1))
        self.assertEqual(menu("q", hidden_keys={"Q"})[0], ("Q", 0))

    def test_russian_layout_hotkeys(self):
        self.assertEqual(menu("в")[0], ("D", 3))  # "в" sits on the D key
        self.assertEqual(menu("й", hidden_keys={"Q"})[0], ("Q", 0))

    def test_escape(self):
        self.assertEqual(menu(ESC, esc_key="0")[0], ("0", 0))
        self.assertEqual(menu(ESC, ENTER)[0], ("1", 0))  # ignored without esc_key

    def test_actions_run_in_place_and_rebuild(self):
        action = MagicMock()
        build = MagicMock(return_value=ROWS)
        result, _ = menu("2", "1", rows=build, actions={"2": action})
        action.assert_called_once()
        self.assertEqual(build.call_count, 2)
        self.assertEqual(result, ("1", 0))

    def test_side_actions_only_on_selected_row(self):
        side = MagicMock()
        result, _ = menu(DOWN, LEFT, UP, RIGHT, LEFT, ENTER, side_actions={"1": side})
        self.assertEqual(side.call_count, 2)
        self.assertEqual(result, ("1", 0))

    def test_picked_menu_is_erased_so_menus_never_stack(self):
        with scripted_keys(ENTER, erase=True) as screen:
            tui.select_menu(ROWS, ["HDR"], interactive=True)
        drawn = screen.getvalue()
        self.assertIn("Первый", drawn)  # it was drawn...
        self.assertNotIn("Первый", drawn.rsplit("\x1b[J", 1)[1])  # ...and wiped from the screen at the end

    def test_question_mark_toggles_help(self):
        result, screen = menu("?", ENTER, help_lines=[("D / R", "запуск")])
        self.assertIn("запуск", screen)
        result, _ = menu("?", ESC, ENTER, esc_key="0")
        self.assertEqual(result, ("1", 0))  # first Esc only closes the help

    def test_ctrl_c_raises_keyboard_interrupt(self):
        with self.assertRaises(KeyboardInterrupt):
            menu("\x03")

    def test_fallback_reads_answer(self):
        ask_fn = MagicMock(side_effect=["?", "в"])
        with patch("sys.stdout", io.StringIO()) as out:
            result = tui.select_menu(ROWS, ["HDR"], interactive=False, ask_fn=ask_fn, help_lines=[("D", "действие")])
        self.assertEqual(result, ("D", 3))
        self.assertIn("[1]", out.getvalue())
        self.assertIn("действие", out.getvalue())

    def test_fallback_empty_and_actions(self):
        action = MagicMock()
        with patch("sys.stdout", io.StringIO()):
            self.assertEqual(tui.select_menu(ROWS, interactive=False, ask_fn=lambda _: "", selected=1), ("", 1))
            answers = iter(["b", "1"])
            tui.select_menu(ROWS, interactive=False, ask_fn=lambda _: next(answers), actions={"B": action})
        action.assert_called_once()


class TestOutputHelpers(unittest.TestCase):
    def test_helpers_render_compact_text(self):
        with patch("sys.stdout", io.StringIO()) as out:
            tui.section_header("Прокси", "этап 1/5")
            tui.panel("Статус", ["[ok]готово[/]"])
            tui.kv_table([("Ключ", "sk-…"), ("Вход", "нет", "warn")])
            tui.status_line("err", "ошибка")
        text = out.getvalue()
        for part in ("Прокси", "этап 1/5", "Статус", "готово", "Ключ", "sk-…", "× ошибка"):
            self.assertIn(part, text)
        self.assertLessEqual(max(len(line) for line in text.splitlines()), tui.MAX_WIDTH)

    def test_ask_confirm_pause(self):
        with patch("builtins.input", side_effect=["", "да", ""]), patch("sys.stdout", io.StringIO()):
            self.assertEqual(tui.ask("Вопрос", "def"), "def")
            self.assertTrue(tui.confirm("Точно?"))
            tui.pause()

    def test_ask_ctrl_c_raises_back(self):
        with patch("builtins.input", side_effect=KeyboardInterrupt), patch("sys.stdout", io.StringIO()):
            with self.assertRaises(tui.WizardBack):
                tui.ask("Вопрос")

    def test_fmt_and_layout_mapping(self):
        self.assertEqual(tui.normalize_key("ы"), "S")
        self.assertEqual(tui.normalize_key("d"), "D")
        self.assertIn("текст", tui.fmt("[warn]текст[/]"))


if __name__ == "__main__":
    unittest.main()
