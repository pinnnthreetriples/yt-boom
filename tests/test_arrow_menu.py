from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from modules.wizard import (
    _readiness,
    account_menu,
    arrow_menu,
    more_menu,
    run_dashboard,
)
from tests.tui_keys import DOWN, ENTER, ESC, LEFT, RIGHT, SPACE, scripted_keys

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


def stub_cfg(reply_mode: str = "ai", api_key: str = "sk-x", replies: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        processor=SimpleNamespace(reply_mode=reply_mode, custom_replies=replies or []),
        deepseek=SimpleNamespace(api_key=api_key),
        browser=SimpleNamespace(session_root=Path("unused")),
        app=SimpleNamespace(db_path=Path("unused.db")),  # no such DB: the main profile decides the login
    )


class TestReadiness(unittest.TestCase):
    def test_states(self):
        with patch("modules.wizard.check_youtube_session", return_value=(True, "")):
            self.assertEqual(_readiness(stub_cfg())[0], "● готов к запуску")
            self.assertEqual(_readiness(stub_cfg(api_key=""))[0], "! ключ DeepSeek не задан")
            self.assertEqual(_readiness(stub_cfg("custom"))[0], "! нет шаблонов ответа")
            self.assertEqual(_readiness(stub_cfg("custom", replies=["hi"]))[0], "● готов к запуску")
        with patch("modules.wizard.check_youtube_session", return_value=(False, "")):
            self.assertEqual(_readiness(stub_cfg())[0], "! нет входа в YouTube")


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "cfg.yaml"
        with open("config.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["app"]["db_path"] = str(self.root / "t.db")
        data["app"]["log_file"] = str(self.root / "t.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["browser"]["headless"] = False
        data["processor"]["reply_mode"] = "ai"
        data["reply_mode"] = "ai"  # the top-level copy overrides processor.reply_mode when it is "custom"
        data["deepseek"]["api_key"] = "sk-test-key-123456"
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def headless(self) -> bool:
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8"))["browser"]["headless"]

    def run_screen(self, func, *keys: str) -> str:
        """Run a wizard screen with scripted keys; returns menu rendering + printed output."""
        out = io.StringIO()
        with scripted_keys(*keys) as screen, \
             patch("modules.wizard.prompt_user", return_value=""), \
             patch("sys.stdout", out):
            func(self.config_path)
        return screen.getvalue() + out.getvalue()


class TestDashboard(ConfigCase):
    def test_layout_and_quit(self):
        output = self.run_screen(run_dashboard, ESC, "q")  # Esc on the main panel is ignored
        for label in ("Запуск", "Цель", "Ответы", "Аккаунт", "История", "Ещё…", "! нет видео"):
            self.assertIn(label, output)
        self.assertNotIn("sk-test", output)  # key mask lives inside the section only
        self.assertIn("До свидания", output)

    def test_left_right_switches_launch_mode_and_enter_launches(self):
        with patch("modules.wizard._launch") as launch:
            output = self.run_screen(run_dashboard, RIGHT, ENTER, LEFT, ENTER, "q")
        self.assertIn("БОЕВОЙ", output)
        self.assertEqual([c.kwargs["dry_run"] for c in launch.call_args_list], [False, True])

    def test_accelerators_including_russian_layout(self):
        with patch("modules.wizard._launch") as launch, \
             patch("modules.wizard.step_reply_mode") as reply_mode, \
             patch("modules.wizard.step_account") as proxy, \
             patch("modules.wizard.activity_log_viewer") as log, \
             patch("modules.wizard._show_stats") as stats, \
             patch("modules.wizard.step_deepseek") as deepseek:
            self.run_screen(run_dashboard, "d", "к", "m", "1", "р", "t", "3", "й")  # к=R, р=H, й=Q
        self.assertEqual([c.kwargs["dry_run"] for c in launch.call_args_list], [True, False])
        for m in (reply_mode, proxy, log, stats, deepseek):
            m.assert_called_once()

    def test_b_toggles_browser_without_leaving_menu(self):
        output = self.run_screen(run_dashboard, "b", "q")
        self.assertTrue(self.headless())
        self.assertIn("без окна", output)

    def test_question_mark_shows_hotkeys(self):
        output = self.run_screen(run_dashboard, "?", "q")
        self.assertIn("сразу пробный / боевой запуск", output)

    def test_live_keeps_login_warning_and_confirmation(self):
        from modules.wizard import _launch
        with patch("modules.wizard.prompt_user", side_effect=["y", "n"]) as prompt, \
             patch("modules.wizard._target_run") as bot, \
             patch("sys.stdout", io.StringIO()) as out:
            _launch(self.config_path, dry_run=False)
        bot.assert_not_called()
        self.assertIn("Вход в YouTube не выполнен", out.getvalue())
        self.assertEqual(prompt.call_count, 2)

    def test_fallback_accepts_old_keys(self):
        with patch("modules.wizard._use_arrow_keys", return_value=False), \
             patch("modules.wizard.prompt_user", side_effect=["", "browser", "x", "quit"]), \
             patch("sys.stdout", io.StringIO()) as out:
            run_dashboard(self.config_path)
        self.assertTrue(self.headless())
        self.assertIn("Неизвестная команда: X", out.getvalue())


class TestSubScreens(ConfigCase):
    def test_account_menu(self):
        with patch("modules.wizard.step_proxy") as proxy, patch("modules.wizard.step_account") as acc:
            output = self.run_screen(account_menu, ENTER, DOWN, ENTER, DOWN, DOWN, SPACE, ESC)
        proxy.assert_called_once()
        acc.assert_called_once()
        self.assertIn("нет прокси · все с вашего IP", output)
        self.assertIn("⚠ с вашего IP", output)
        self.assertTrue(self.headless())

    def test_dashboard_opens_account_section(self):
        with patch("modules.wizard.account_menu") as acc:
            self.run_screen(run_dashboard, DOWN, DOWN, DOWN, ENTER, "q")
        acc.assert_called_once()

    def test_more_menu(self):
        with patch("modules.wizard._preview_comments") as preview, \
             patch("modules.wizard.run_step_by_step_wizard") as wizard:
            output = self.run_screen(more_menu, ENTER, DOWN, ENTER, ESC)
        for m in (preview, wizard):
            m.assert_called_once()
        self.assertNotIn("пул", output.lower())


if __name__ == "__main__":
    unittest.main()
