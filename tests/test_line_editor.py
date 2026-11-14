"""The text field's editor: cursor moves over whole characters (emoji sequences included), word keys, wrapping."""
from __future__ import annotations

import unittest

from modules.ui.line_editor import LineEditor, boundaries, char_width, chars, typeable

FAMILY = "👨‍👩‍👧"
HEART = "❤️"
THUMB_DARK = "👍🏽"
FLAG_RU = "🇷🇺"


class BoundaryCase(unittest.TestCase):
    def test_emoji_sequences_are_one_character(self):
        self.assertEqual(chars(f"a{FAMILY}b"), ["a", FAMILY, "b"])
        self.assertEqual(chars(f"{HEART}{THUMB_DARK}"), [HEART, THUMB_DARK])
        self.assertEqual(chars(f"{FLAG_RU}🇺🇸"), [FLAG_RU, "🇺🇸"])
        self.assertEqual(chars("е́ж"), ["е́", "ж"])  # a combining accent stays with its letter
        self.assertEqual(boundaries(""), [0])

    def test_widths(self):
        self.assertEqual(char_width("a"), 1)
        self.assertEqual(char_width("ж"), 1)
        for emoji in ("😳", FAMILY, HEART, THUMB_DARK, FLAG_RU):
            self.assertEqual(char_width(emoji), 2, emoji)

    def test_typeable_takes_the_joiner_but_not_control_codes(self):
        self.assertTrue(typeable("‍"))
        self.assertTrue(typeable("️"))
        self.assertFalse(typeable("\x17"))
        self.assertFalse(typeable("\ud83d"))


class EditCase(unittest.TestCase):
    def test_starts_at_the_end_and_inserts_at_the_cursor(self):
        e = LineEditor("Привет мир")
        self.assertEqual(e.cursor, len(e.text))
        for _ in range(3):
            e.press("left")
        e.insert("👋 ")
        self.assertEqual(e.text, "Привет 👋 мир")
        e.press("home")
        e.insert("— ")
        self.assertEqual(e.text, "— Привет 👋 мир")
        self.assertEqual(e.cursor, 2)

    def test_arrows_and_deletes_never_split_an_emoji(self):
        e = LineEditor(f"a{FAMILY}b")
        e.press("left")
        e.press("left")  # over the whole family at once
        self.assertEqual(e.cursor, 1)
        e.press("delete")
        self.assertEqual(e.text, "ab")
        e = LineEditor(f"x{THUMB_DARK}")
        e.press("backspace")
        self.assertEqual(e.text, "x")

    def test_typing_an_emoji_sequence_key_by_key(self):
        e = LineEditor("ok")
        e.press("home")
        for ch in FAMILY:
            e.insert(ch)
        self.assertEqual(e.text, f"{FAMILY}ok")
        self.assertEqual(e.cursor, len(FAMILY))
        e.press("left")
        self.assertEqual(e.cursor, 0)

    def test_a_selector_typed_after_a_heart_joins_it(self):
        e = LineEditor("❤")
        e.insert("️")
        self.assertEqual(chars(e.text), [HEART])
        self.assertEqual(e.cursor, len(HEART))

    def test_word_keys(self):
        e = LineEditor("один, два три")
        e.press("c-left")
        self.assertEqual(e.text[e.cursor:], "три")
        e.press("c-left")
        self.assertEqual(e.text[e.cursor:], "два три")
        e.press("c-left")
        self.assertEqual(e.cursor, 0)
        e.press("c-right")
        self.assertEqual(e.text[: e.cursor], "один")
        e.press("c-right")
        self.assertEqual(e.text[: e.cursor], "один, два")
        e.press("c-w")
        self.assertEqual(e.text, "один,  три")
        e.press("c-delete")
        self.assertEqual(e.text, "один, ")

    def test_word_keys_stay_quick_on_a_long_text(self):
        """Ctrl+arrows split the text into characters once: on 5000 characters the old per-character rescan took
        many seconds per key."""
        import time

        e = LineEditor("слово " * 1000)
        started = time.monotonic()
        e.press("c-left")
        e.press("home")
        e.press("c-right")
        e.press("c-delete")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(e.cursor, len("слово"))
        self.assertTrue(e.text.startswith("слово слово"))

    def test_kill_to_start_and_end(self):
        e = LineEditor("начало|конец")
        e.cursor = 6
        e.press("c-k")
        self.assertEqual(e.text, "начало")
        e.cursor = 3
        e.press("c-u")
        self.assertEqual((e.text, e.cursor), ("ало", 0))

    def test_unknown_key_is_left_to_the_caller(self):
        self.assertFalse(LineEditor("x").press("enter"))
        self.assertFalse(LineEditor("x").press("tab"))

    def test_edges_are_safe(self):
        e = LineEditor("")
        for key in ("left", "right", "backspace", "delete", "c-left", "c-right", "c-w", "up", "down"):
            self.assertTrue(e.press(key, 10))
        self.assertEqual((e.text, e.cursor), ("", 0))


class WrapCase(unittest.TestCase):
    def test_rows_cut_by_columns_and_keep_emoji_whole(self):
        e = LineEditor("abcd😳ef")
        rows = e.rows(5)
        self.assertEqual([e.text[a:b] for a, b in rows], ["abcd", "😳ef"])
        e = LineEditor("abcde")  # full row: the cursor at the end gets a row of its own
        self.assertEqual([e.text[a:b] for a, b in e.rows(5)], ["abcde", ""])
        self.assertEqual(e.cursor_row(5), 1)

    def test_up_and_down_keep_the_column(self):
        e = LineEditor("0123456789abcdefghij")  # rows of 10: «0123456789», «abcdefghij», «»
        e.cursor = 13  # «d»
        e.press("up", 10)
        self.assertEqual(e.cursor, 3)
        e.press("down", 10)
        self.assertEqual(e.cursor, 13)
        e.press("up", 10)
        e.press("up", 10)  # above the first row: the start
        self.assertEqual(e.cursor, 0)
        e.press("down", 10)
        e.press("down", 10)
        e.press("down", 10)  # past the last row: the end
        self.assertEqual(e.cursor, len(e.text))

    def test_up_on_a_single_row_goes_home(self):
        e = LineEditor("короткий")
        e.press("up", 40)
        self.assertEqual(e.cursor, 0)
        e.press("down", 40)
        self.assertEqual(e.cursor, len(e.text))


if __name__ == "__main__":
    unittest.main()
