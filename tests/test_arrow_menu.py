from __future__ import annotations

import io
import unittest
from unittest.mock import MagicMock, patch

from modules.wizard import arrow_menu
from tests.tui_keys import DOWN, RIGHT, scripted_keys

ROWS = [
    ("1", "Первый", "статус", "\033[92m"),
    ("2", "Второй", "", ""),
    None,
    ("D", "Действие", "", ""),
]


class TestArrowMenuWrapper(unittest.TestCase):
    def test_interactive_passthrough(self):
        side = MagicMock()
        with scripted_keys(DOWN, DOWN, RIGHT, "q") as screen:
            result = arrow_menu(lambda: ROWS, ["HDR"], extra_keys=frozenset({"Q"}), side_actions={"D": side})
        self.assertEqual(result, ("Q", 3))
        side.assert_called_once()
        self.assertIn("\x1b[0;32mстатус", screen.getvalue())  # CLR_GREEN mapped to the "ok" theme style

    def test_fallback_uses_prompt_user(self):
        with patch("modules.wizard._use_arrow_keys", return_value=False), \
             patch("modules.wizard.prompt_user", side_effect=["d"]), \
             patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(arrow_menu(lambda: ROWS, [], selected=1), ("D", 3))
        self.assertIn("[1]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
