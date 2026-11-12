"""Console logging while a menu is on screen: held back, then printed once the menu is closed."""
from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from loguru import logger

from modules import logger_config


class ConsoleHeldCase(unittest.TestCase):
    def setUp(self):
        self.err = io.StringIO()
        patcher = patch("sys.stderr", self.err)
        patcher.start()
        self.addCleanup(patcher.stop)
        logger_config.setup_bot_logging("INFO")
        self.addCleanup(logger.remove)
        self.addCleanup(setattr, logger_config, "_console_sink", None)

    def test_line_logged_inside_is_not_written_until_exit(self):
        with logger_config.console_held():
            logger.info("движок chrome")
            self.assertEqual(self.err.getvalue(), "")
        self.assertIn("движок chrome", self.err.getvalue())

    def test_lines_outside_still_go_to_the_console(self):
        with logger_config.console_held():
            pass
        logger.info("после меню")
        self.assertIn("после меню", self.err.getvalue())

    def test_level_is_kept(self):
        with logger_config.console_held():
            logger.debug("тихо")
        self.assertEqual(self.err.getvalue(), "")

    def test_console_comes_back_after_an_error(self):
        with self.assertRaises(RuntimeError):
            with logger_config.console_held():
                raise RuntimeError("boom")
        logger.warning("жив")
        self.assertIn("жив", self.err.getvalue())

    def test_nested_hold_does_not_lose_lines(self):
        with logger_config.console_held():
            with logger_config.console_held():
                logger.info("внутри")
            self.assertEqual(self.err.getvalue(), "")
        self.assertEqual(self.err.getvalue().count("внутри"), 1)

    def test_without_setup_it_does_nothing(self):
        logger_config._console_sink = None
        with logger_config.console_held():
            logger.info("никуда")


if __name__ == "__main__":
    unittest.main()
