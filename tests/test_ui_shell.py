"""The tabbed shell: keys, steps (menu, field, confirm, screen, run, go-to-tab), marks and the rendered screen."""
from __future__ import annotations

import unittest

from modules.ui import icons
from modules.ui.shell import (
    Back, Cell, Confirm, Controller, Field, GoTab, Hotkey, Menu, Notice, Row, Run, Screen, Tab, View, fit, frags_text,
    render, width,
)


def screen(c: Controller) -> str:
    return frags_text(render(c))


class ShellCase(unittest.TestCase):
    def setUp(self):
        icons.use_emoji(False)
        self.log: list[str] = []
        self.value = 0
        self.texts: list[str] = []

        def first() -> View:
            rows = [
                Row("a", [Cell("альфа")], hint="Строка А", detail=lambda: [[Cell("подробно про А")]],
                    enter=lambda: Menu([
                        Row("m1", [Cell("пункт меню")], enter=lambda: Notice("ok", "Пункт выбран")),
                        Row("m2", [Cell("ввод")], enter=lambda: Field("Имя", lambda t: Notice("ok", f"Имя: {t}"))),
                        Row("m3", [Cell("удалить")], enter=lambda: Confirm(
                            [Cell("Удалить?")], ["Нет", "Удалить"], lambda i: Notice("ok", "Удалено") if i else None)),
                    ])),
                Row("b", [Cell("бета"), Cell(["один", "два", "три"][self.value])], hint="Строка Б",
                    values=["один", "два", "три"], value_index=self.value, on_value=self.set_value),
                Row("c", [Cell("вглубь")], enter=lambda: Screen(lambda: View(
                    rows=[Row("deep", [Cell("внутри")], enter=lambda: Back(Notice("ok", "Вернулись")))],
                    title=[Cell("путь")], keys="Enter назад"))),
                Row("d", [Cell("строки")], enter=lambda: Field(
                    "Вставьте строки", lambda t: self.got(t), multiline=True,
                    preview=lambda lines: [[Cell(f"разобрано {len(lines)}")]])),
                Row("e", [Cell("работа")], enter=lambda: Run(lambda: Notice("ok", "Готово"), "Работа")),
                Row("f", [Cell("к вкладке")], enter=lambda: GoTab(1)),
            ]
            return View(rows=rows, keys="Enter действия",
                        hotkeys=[Hotkey("+", lambda row: Notice("info", f"плюс на {row.id if row else '-'}"))],
                        marks=lambda ids: [Row("bulk", [Cell("всем")], enter=lambda: Notice("ok", f"отмечено {','.join(ids)}"))])

        self.tabs = [
            Tab("Первая", "1", first),
            Tab("Вторая", "2", lambda: View(rows=[Row("x", [Cell("икс")])]), warn=lambda: True),
        ]
        self.c = Controller(self.tabs, status=lambda: [Cell("● готов", "ok")])

    def set_value(self, i: int) -> None:
        self.value = i

    def got(self, text: str):
        self.texts.append(text)
        return Notice("ok", f"строк {len(text.splitlines())}")

    def keys(self, *keys: str) -> None:
        for key in keys:
            self.c.press(key)

    def test_first_frame_shows_tabs_status_rows_and_hint(self):
        out = screen(self.c)
        self.assertIn(" 1 Первая ", out)
        self.assertIn(" 2 •", out)  # the inactive tab shows only its icon, its warning dot stays
        self.assertNotIn("Вторая", out)
        self.assertIn("● готов", out)
        self.assertIn(" ▸ альфа", out)
        self.assertIn("Строка А", out)
        self.assertIn("? клавиши", out)

    def test_arrows_move_and_wrap(self):
        self.keys("down")
        self.assertEqual(self.c.current_id(), "b")
        self.assertIn("Строка Б", screen(self.c))
        self.keys("up", "up")
        self.assertEqual(self.c.current_id(), "f")

    def test_left_right_change_the_value(self):
        self.keys("down", "right")
        self.assertEqual(self.value, 1)
        self.assertIn("два ‹›", screen(self.c))
        self.keys("left", "left")
        self.assertEqual(self.value, 2)  # wraps around

    def test_space_shows_and_hides_details(self):
        self.keys("space")
        self.assertIn("подробно про А", screen(self.c))
        self.keys("space")
        self.assertNotIn("подробно про А", screen(self.c))

    def test_enter_opens_an_inline_menu_and_escape_closes_it(self):
        self.keys("enter")
        out = screen(self.c)
        self.assertIn("    ▸ пункт меню", out)
        self.keys("escape")
        self.assertNotIn("пункт меню", screen(self.c))

    def test_menu_item_gives_a_notice(self):
        self.keys("enter", "enter")
        self.assertEqual(self.c.notice, Notice("ok", "Пункт выбран"))
        self.assertIsNone(self.c.menu)
        self.assertIn("Пункт выбран", screen(self.c))
        self.keys("down")  # the next key clears the notice
        self.assertIsNone(self.c.notice)

    def test_field_reads_text_and_backspace(self):
        self.keys("enter", "down", "enter")
        for ch in "Иваx":
            self.keys(ch)
        self.keys("backspace", "н")
        self.assertIn("› Иван▏", screen(self.c))
        self.keys("enter")
        self.assertEqual(self.c.notice.text, "Имя: Иван")

    def test_field_escape_cancels(self):
        self.keys("enter", "down", "enter", "a", "escape")
        self.assertIsNone(self.c.edit)
        self.assertIsNone(self.c.notice)

    def test_confirm_starts_on_the_safe_answer(self):
        self.keys("enter", "down", "down", "enter")
        self.assertIn("▸ Нет", screen(self.c))
        self.keys("enter")
        self.assertIsNone(self.c.notice)
        self.keys("enter", "down", "down", "enter", "right", "enter")
        self.assertEqual(self.c.notice.text, "Удалено")

    def test_nested_screen_and_back(self):
        self.keys("down", "down", "enter")
        out = screen(self.c)
        self.assertIn("путь", out)
        self.assertIn("Esc назад", out)
        self.keys("escape")
        self.assertEqual(self.c.current_id(), "c")
        self.keys("enter", "enter")
        self.assertEqual(self.c.notice.text, "Вернулись")
        self.assertEqual(self.c.stacks[0], [])

    def test_multiline_field_with_paste_and_preview(self):
        self.keys("down", "down", "down", "enter", "paste:one 1\ntwo 2\nthr")
        out = screen(self.c)
        self.assertIn("› one 1", out)
        self.assertIn("› thr▏", out)
        self.assertIn("разобрано 3", out)
        self.keys("e", "e", "enter", "enter")
        self.assertEqual(self.texts, ["one 1\ntwo 2\nthree"])

    def test_run_waits_for_the_outer_loop(self):
        self.keys("up", "up", "enter")  # «работа»
        self.assertIsNotNone(self.c.pending_run)
        notice = self.c.pending_run.work()
        self.c.finish_run(notice)
        self.assertIsNone(self.c.pending_run)
        self.assertEqual(self.c.notice.text, "Готово")

    def test_go_to_tab_and_tab_keys(self):
        self.keys("up", "enter")
        self.assertEqual(self.c.tab, 1)
        self.assertIn(" 2 Вторая ", screen(self.c))
        self.keys("tab")
        self.assertEqual(self.c.tab, 0)
        self.keys("2")
        self.assertEqual(self.c.tab, 1)
        self.keys("s-tab")
        self.assertEqual(self.c.tab, 0)

    def test_hotkey_gets_the_current_row(self):
        self.keys("down", "+")
        self.assertEqual(self.c.notice.text, "плюс на b")

    def test_marks_offer_actions_for_the_ticked_rows(self):
        self.keys("X", "space", "down", "space")
        out = screen(self.c)
        self.assertIn("▣ альфа", out)
        self.assertIn("отмечено 2", out)
        self.keys("enter", "enter")
        self.assertEqual(self.c.notice.text, "отмечено a,b")
        self.keys("X", "escape")
        self.assertFalse(self.c.marking)

    def test_help_and_quit(self):
        self.keys("?")
        self.assertIn("следующая вкладка", screen(self.c))
        self.keys("down")  # any key closes the help, nothing else happens
        self.assertFalse(self.c.help)
        self.assertEqual(self.c.current_id(), "a")
        self.keys("Q")
        self.assertTrue(self.c.quit)

    def test_quit_is_ignored_inside_a_nested_screen(self):
        self.keys("down", "down", "enter", "Q")
        self.assertFalse(self.c.quit)

    def test_a_broken_tab_shows_the_error_instead_of_closing(self):
        def broken() -> View:
            raise RuntimeError("нет базы")

        c = Controller([Tab("Сломана", "!", broken)])
        self.assertIn("Ошибка экрана: RuntimeError: нет базы", screen(c))


class TextHelpers(unittest.TestCase):
    def test_emoji_take_two_columns_and_fit_keeps_the_width(self):
        self.assertEqual(width("🌳"), 2)
        cut = fit("🌳🌳🌳🌳", 5)
        self.assertLessEqual(width(cut), 5)
        self.assertTrue(cut.endswith("…"))

    def test_quota_pie(self):
        self.assertEqual(icons.quota(0, 5), "○")
        self.assertEqual(icons.quota(5, 5), "●")
        self.assertEqual(icons.quota(1, 5), "◔")
        self.assertEqual(icons.quota(0, 0), "·")

    def test_icon_sets(self):
        icons.use_emoji(True)
        try:
            self.assertEqual(icons.icon("mature"), "🌳")
        finally:
            icons.use_emoji(False)
        self.assertEqual(icons.icon("mature"), "▇")


if __name__ == "__main__":
    unittest.main()
