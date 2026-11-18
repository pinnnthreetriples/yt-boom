"""Мини-флаги стран (три ▀ с цветами) и их место в строках интерфейса."""
import unittest
from types import SimpleNamespace

from modules.ui import icons
from modules.ui.flags import FLAG_COLUMNS, flag, flag_prefix
from modules.ui.shell import Cell, cells_frags, frags_text, width


class FlagCase(unittest.TestCase):
    def setUp(self):
        self.addCleanup(icons.use_emoji, icons.EMOJI)
        icons.use_emoji(True)


class TestFlag(FlagCase):
    def test_stripes_and_drawings_are_three_coloured_half_blocks(self):
        for code in ("DE", "FR", "US", "SG", "PL", "KZ", "GB", "JP", "AE"):
            with self.subTest(code=code):
                parts = flag(code)
                self.assertEqual("".join(text for _, text in parts), "▀▀▀")
                for style, _ in parts:
                    self.assertRegex(style, r"^fg:#[0-9a-f]{6} bg:#[0-9a-f]{6}$")

    def test_vertical_tricolour_keeps_its_order(self):
        self.assertEqual([s for s, _ in flag("FR")], ["fg:#012169 bg:#012169", "fg:#ffffff bg:#ffffff", "fg:#d80027 bg:#d80027"])

    def test_two_horizontal_stripes_are_top_and_bottom(self):
        self.assertEqual(set(s for s, _ in flag("PL")), {"fg:#ffffff bg:#d80027"})

    def test_lower_case_and_unknown_codes(self):
        self.assertEqual(flag("de"), flag("DE"))
        self.assertEqual(flag("ZZ"), ())
        self.assertEqual(flag(None), ())
        self.assertEqual(flag(""), ())

    def test_plain_icons_draw_no_flag(self):
        icons.use_emoji(False)
        self.assertEqual(flag("DE"), ())
        self.assertEqual(flag_prefix("DE"), ())

    def test_prefix_keeps_rows_aligned_for_a_country_without_a_flag(self):
        for code in ("DE", "ZZ", None):
            with self.subTest(code=code):
                self.assertEqual(sum(width(text) for _, text in flag_prefix(code)), FLAG_COLUMNS)


class TestCellPrefix(FlagCase):
    def test_a_prefix_takes_columns_from_the_cell_not_from_the_row(self):
        plain = cells_frags([Cell("PL 1.2.3.4:8080", "", 20), Cell("next")])
        flagged = cells_frags([Cell("PL 1.2.3.4:8080", "", 20, prefix=flag_prefix("PL")), Cell("next")])
        self.assertEqual(width(frags_text(plain)), width(frags_text(flagged)))
        self.assertTrue(frags_text(flagged).startswith("▀▀▀ PL 1.2.3.4:8080"))

    def test_long_text_after_a_prefix_is_cut_inside_the_cell(self):
        frags = cells_frags([Cell("PL 123.123.123.123:65000", "", 12, prefix=flag_prefix("PL")), Cell("x")])
        self.assertEqual(width(frags_text(frags)), 12 + 1)
        self.assertIn("…", frags_text(frags))


class TestProxyRows(FlagCase):
    def test_proxy_tab_row_draws_the_checked_country_flag(self):
        from modules.ui.tab_proxies import _proxy_cells

        record = SimpleNamespace(country="US", last_exit_country="DE", address="1.2.3.4:80", is_search=False,
                                 accounts_count=2, last_ok=True, last_error=None, password_unavailable=False,
                                 last_latency_ms=100)
        cells = _proxy_cells(record)
        country = cells[1]
        self.assertEqual(country.text, "DE")  # the checked exit country, not the seller's
        self.assertEqual(country.prefix[:3], flag("DE"))


if __name__ == "__main__":
    unittest.main()
