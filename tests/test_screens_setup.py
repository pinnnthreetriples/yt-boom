"""Setup screens (proxy / YouTube / DeepSeek / reply mode / account / step-by-step wizard) driven by arrow keys."""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from modules.wizard import (
    WizardBack,
    account_menu,
    print_step_header,
    prompt_proxy_details,
    run_step_by_step_wizard,
    step_account,
    step_deepseek,
    step_proxy,
    step_reply_mode,
)
from tests.tui_keys import DOWN, ENTER, ESC, UP, scripted_keys


class SetupScreenCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "cfg.yaml"
        with open("config.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["app"]["db_path"] = str(self.root / "t.db")
        data["app"]["log_file"] = str(self.root / "t.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["browser"]["proxy"] = "http://1.2.3.4:8080"
        data["browser"]["accounts"] = []  # the real config.yaml may already hold the user's own accounts
        data["deepseek"]["api_key"] = "sk-test-key-123456"
        data["processor"]["reply_mode"] = "ai"
        data["reply_mode"] = "ai"  # the top-level copy wins over processor.reply_mode when it is "custom"
        data["processor"]["custom_replies"] = ["first", "second"]
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def raw(self) -> dict:
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8"))

    def run_keys(self, func, *keys: str, inputs: list[str] | None = None):
        """Run func() with scripted menu keys and prompt_user answers; returns (result, output)."""
        out = io.StringIO()
        prompt = {"side_effect": inputs} if inputs is not None else {"return_value": ""}
        with scripted_keys(*keys) as screen, patch("modules.wizard.prompt_user", **prompt), \
             patch("sys.stdout", out):
            result = func()
        return result, screen.getvalue() + out.getvalue()


class TestHeader(unittest.TestCase):
    def test_stage_title_and_long_hint_on_own_line(self):
        with patch("sys.stdout", io.StringIO()) as out:
            print_step_header(2, 5, "Вход", "коротко")
            print_step_header(4, 5, "Цель", "очень длинная подсказка " * 4)
        lines = out.getvalue().splitlines()
        self.assertIn("Этап 2/5 · Вход  коротко", lines[0])
        self.assertTrue(all(len(line) <= 70 for line in lines))
        self.assertTrue(lines[-1].startswith(" очень длинная"))


class TestProxyScreen(SetupScreenCase):
    def accounts(self):
        from modules.wizard import load_accounts
        return load_accounts(self.config_path)

    def test_enter_continues_when_every_account_has_proxy(self):
        result, output = self.run_keys(lambda: step_proxy(self.config_path), ENTER)
        self.assertIsNone(result)
        self.assertIn("у 1 из 1 аккаунтов", output)
        self.assertRegex(output, r"default +http://1\.2\.3\.4:8080")  # the seeded account inherits browser.proxy
        self.assertNotIn("Этап", output)  # stage numbers only inside the wizard

    def test_remove_proxy_of_single_account(self):
        self.accounts()
        self.run_keys(lambda: step_proxy(self.config_path), DOWN, DOWN, DOWN, ENTER, UP, UP, UP, ENTER)
        self.assertIsNone(self.accounts()[0].proxy_url)
        self.assertIsNone(self.raw()["browser"]["accounts"][0]["proxy_url"])

    def test_check_result_stays_under_menu_then_esc_goes_back(self):
        with patch("modules.wizard.check_proxy_connectivity", return_value=(True, "Внешний IP: 9.9.9.9")):
            with self.assertRaises(WizardBack):
                self.run_keys(lambda: step_proxy(self.config_path), DOWN, DOWN, DOWN, DOWN, ENTER, ESC)

    def test_assign_proxy_step_by_step_with_type_by_arrows(self):
        with patch("modules.wizard.check_proxy_connectivity", return_value=(True, "ok")):
            _, output = self.run_keys(
                lambda: step_proxy(self.config_path), DOWN, DOWN, ENTER, DOWN, DOWN, ENTER, UP, UP, ENTER,
                inputs=["5.6.7.8:1080", "user", "pw"],
            )
        self.assertEqual(self.accounts()[0].proxy_url, "socks5://user:pw@5.6.7.8:1080")
        self.assertEqual(self.raw()["browser"]["accounts"][0]["proxy_url"], "socks5://user:pw@5.6.7.8:1080")
        self.assertIn("«default»: прокси сохранён", output)

    def test_several_accounts_pick_one_and_cursor_starts_on_missing_proxy(self):
        from modules.wizard import _save_account
        self.accounts()  # seeds «default» (with the inherited proxy)
        _save_account(self.config_path, "bot_2", str(self.root / "sessions" / "bot_2"), None)
        with patch("modules.wizard.check_proxy_connectivity", return_value=(True, "ok")):
            _, output = self.run_keys(
                lambda: step_proxy(self.config_path), ENTER, DOWN, ENTER, DOWN, DOWN, ENTER, UP, UP, ENTER,
                inputs=["9.9.9.9:3128", ""],
            )
        by_name = {a.name: a.proxy_url for a in self.accounts()}
        self.assertEqual(by_name, {"default": "http://1.2.3.4:8080", "bot_2": "socks5://9.9.9.9:3128"})
        self.assertIn("у 1 из 2 аккаунтов", output)
        self.assertIn("«bot_2»: прокси сохранён", output)

    def test_ctrl_c_in_input_stays_in_section(self):
        self.run_keys(lambda: step_proxy(self.config_path), DOWN, DOWN, ENTER, UP, UP, ENTER, inputs=[WizardBack()])
        self.assertEqual(self.accounts()[0].proxy_url, "http://1.2.3.4:8080")

    def test_no_accounts_asks_to_add_them_first(self):
        with patch("modules.wizard.load_accounts", return_value=[]):
            result, output = self.run_keys(lambda: step_proxy(self.config_path), DOWN, DOWN, ENTER, UP, UP, ENTER)
        self.assertIsNone(result)
        self.assertIn("сначала добавьте их на этапе «Аккаунты»", output)

    def test_prompt_proxy_details_esc_on_type_cancels(self):
        result, _ = self.run_keys(prompt_proxy_details, ESC, inputs=["5.6.7.8:1080", ""])
        self.assertIsNone(result)

    def test_prompt_proxy_details_plain_fallback_keeps_number_answers(self):
        with patch("modules.wizard.prompt_user", side_effect=["5.6.7.8:1080", "", "2"]), \
             patch("sys.stdout", io.StringIO()):
            self.assertEqual(prompt_proxy_details(), "https://5.6.7.8:1080")


class TestAccountScreens(SetupScreenCase):
    def test_fresh_config_lists_default_account_and_continue_is_default(self):
        result, output = self.run_keys(lambda: step_account(self.config_path, self.root / "sessions"), ENTER)
        self.assertFalse(result)
        self.assertIn("Аккаунты", output)
        self.assertIn("Аккаунтов  1 · вошли 0 из 1", output)
        self.assertRegex(output, r"Аккаунт +Вход")
        self.assertRegex(output, r"default +○ нет входа")
        self.assertRegex(output, r"Аккаунт +Вход +Прокси")
        self.assertRegex(output, r"default +○ нет входа +http://1\.2\.3\.4:8080")
        self.assertIn("(по умолчанию — можно переименовать)", output)

    def test_add_account_refreshes_list(self):
        def add(config_path):
            from modules.wizard import _save_account
            _save_account(config_path, "bot_2", str(self.root / "sessions" / "bot_2"), None)
            return True

        with patch("modules.wizard.add_account_stepwise", side_effect=add) as adder:
            _, output = self.run_keys(lambda: step_account(self.config_path, self.root / "sessions"), DOWN, DOWN, ENTER, UP, ENTER, ENTER)
        adder.assert_called_once_with(self.config_path)
        self.assertIn("Аккаунт добавлен · всего 2", output)
        self.assertIn("bot_2", output)

    def test_list_is_capped_at_eight(self):
        from modules.wizard import _save_account
        for i in range(10):
            _save_account(self.config_path, f"acc_{i}", str(self.root / "s" / str(i)), None)
        _, output = self.run_keys(lambda: step_account(self.config_path, self.root / "sessions"), ENTER)
        self.assertIn("Аккаунтов  10 · вошли 0 из 10", output)
        self.assertIn("acc_7", output)
        self.assertNotIn("acc_8", output)
        self.assertIn("… ещё 2", output)

    def test_no_accounts_defaults_to_add_and_continue_warns_once(self):
        with patch("modules.wizard.load_accounts", return_value=[]), \
             patch("modules.wizard.add_account_stepwise", return_value=False) as adder:
            result, output = self.run_keys(
                lambda: step_account(self.config_path, self.root / "sessions"), ENTER, UP, UP, ENTER, ENTER,
            )
        adder.assert_called_once()
        self.assertFalse(result)
        self.assertIn("пока нет — добавьте первый", output)
        self.assertIn("публиковать будет нечем", output)

    def test_logout_main_profile_asks_confirmation(self):
        session = self.root / "sessions" / "youtube"
        session.mkdir(parents=True)
        self.run_keys(
            lambda: step_account(self.config_path, self.root / "sessions"),
            *[DOWN] * 5, ENTER, ENTER, *[UP] * 5, ENTER, inputs=["n", "y"],
        )  # «Выйти» → no, «Выйти» → yes, back up to «Продолжить»
        self.assertFalse(session.exists())

    def test_logout_picks_account_when_several(self):
        from modules.wizard import _save_account, load_accounts
        load_accounts(self.config_path)  # seeds «default» into the empty DB first
        other = self.root / "sessions" / "bot_2"
        other.mkdir(parents=True)
        _save_account(self.config_path, "bot_2", str(other), None)
        main = self.root / "sessions" / "youtube"
        main.mkdir(parents=True)
        _, output = self.run_keys(
            lambda: step_account(self.config_path, self.root / "sessions"),
            *[DOWN] * 5, ENTER, DOWN, ENTER, *[UP] * 5, ENTER, inputs=["y"],
        )  # «Выйти» → second account → yes → «Продолжить»
        self.assertFalse(other.exists())
        self.assertTrue(main.exists())
        self.assertIn("Сессия «bot_2» удалена", output)

    def rename(self, *keys: str, inputs: list[str]) -> str:
        """«Переименовать» (5th row) → keys → name input(s) → back up to «Продолжить»."""
        _, output = self.run_keys(
            lambda: step_account(self.config_path, self.root / "sessions"),
            *[DOWN] * 4, ENTER, *keys, *[UP] * 4, ENTER, inputs=inputs,
        )
        return output

    def test_rename_default_survives_restart(self):
        from config import load_config
        from modules.wizard import load_accounts
        output = self.rename(inputs=["  main@mail.ru "])
        self.assertIn("Переименован: default → main@mail.ru", output)
        self.assertRegex(output, r"main@mail\.ru +○ нет входа")
        self.assertEqual([a.name for a in load_accounts(self.config_path)], ["main@mail.ru"])
        cfg = load_config(self.config_path)  # what sync_accounts_from_config upserts on the next run
        self.assertEqual([a.name for a in cfg.browser.accounts], ["main@mail.ru"])
        self.assertEqual(Path(cfg.browser.accounts[0].session_path), (self.root / "sessions" / "youtube").resolve())
        self.assertFalse((self.root / "sessions" / "main@mail.ru").exists())  # session folder is not renamed

    def test_rename_picks_account_and_updates_config_entry(self):
        from modules.wizard import _save_account, load_accounts
        load_accounts(self.config_path)
        _save_account(self.config_path, "bot_2", str(self.root / "sessions" / "bot_2"), None)
        output = self.rename(DOWN, ENTER, inputs=["shop_2"])
        self.assertIn("Переименован: bot_2 → shop_2", output)
        self.assertEqual([a.name for a in load_accounts(self.config_path)], ["default", "shop_2"])
        self.assertEqual([a["name"] for a in self.raw()["browser"]["accounts"]], ["shop_2"])

    def test_rename_rejects_empty_and_duplicate(self):
        from modules.wizard import _save_account, load_accounts
        load_accounts(self.config_path)
        _save_account(self.config_path, "bot_2", str(self.root / "sessions" / "bot_2"), None)
        self.assertIn("Имя «default» уже занято", self.rename(DOWN, ENTER, inputs=["default"]))
        self.assertIn("Имя не может быть пустым", self.rename(ENTER, inputs=["  "]))
        self.assertEqual([a.name for a in load_accounts(self.config_path)], ["default", "bot_2"])

    def test_login_uses_account_picker(self):
        with patch("modules.wizard.login_account_wizard", return_value=False) as login:
            _, output = self.run_keys(
                lambda: step_account(self.config_path, self.root / "sessions"), DOWN, DOWN, ENTER, UP, UP, ENTER,
            )
        login.assert_called_once()
        self.assertIn("Вход не выполнен", output)

    def test_account_menu_header(self):
        with patch("modules.wizard.step_proxy") as proxy:
            _, output = self.run_keys(lambda: account_menu(self.config_path), DOWN, ENTER, ESC)
        proxy.assert_called_once()
        self.assertIn("Аккаунт", output)
        self.assertIn("нет входа", output)


class TestDeepseekAndReplyMode(SetupScreenCase):
    def test_new_key_checked_and_saved(self):
        with patch("modules.wizard.check_deepseek_api", new_callable=AsyncMock, return_value=(True, "Ключ активен")):
            result, output = self.run_keys(
                lambda: step_deepseek(self.config_path, "sk-old", "https://api.deepseek.com"), DOWN, DOWN, ENTER,
                inputs=["sk-new-key-0000000"],
            )
        self.assertEqual(result, "sk-new-key-0000000")
        self.assertEqual(self.raw()["deepseek"]["api_key"], result)
        self.assertIn("Ключ активен", output)

    def test_reply_mode_switch_and_delete_template_by_arrows(self):
        # «Свой текст», then «Удалить шаблон» → second template, then Esc back
        (mode, replies), output = self.run_keys(
            lambda: step_reply_mode(self.config_path), DOWN, ENTER, DOWN, DOWN, ENTER, DOWN, ENTER, ESC,
        )
        self.assertEqual(mode, "custom")
        self.assertEqual(replies, ["first"])
        self.assertEqual(self.raw()["processor"]["custom_replies"], ["first"])
        self.assertIn("Удалён: second", output)

    def test_randomization_is_picked_explicitly_not_flipped(self):
        from modules.wizard import update_config_file
        update_config_file(self.config_path, {"processor.paraphrase_custom": False})
        # ↓×4 «Рандомизация текста» → picker opens on «Выключена»: ↑ «Включена»;
        # Enter on the row again opens the picker on «Включена»: Enter keeps it on (no flip back)
        _, output = self.run_keys(
            lambda: step_reply_mode(self.config_path), DOWN, DOWN, DOWN, DOWN, ENTER, UP, ENTER, ENTER, ENTER, ESC,
        )
        self.assertIs(self.raw()["processor"]["paraphrase_custom"], True)
        self.assertIn("Рандомизация текста включена", output)
        # then off: the notice is not a green «ok»
        self.run_keys(lambda: step_reply_mode(self.config_path), DOWN, DOWN, DOWN, DOWN, ENTER, DOWN, ENTER, ESC)
        self.assertIs(self.raw()["processor"]["paraphrase_custom"], False)

    def test_reply_mode_notice_does_not_reappear_on_deepseek(self):
        from modules.wizard import update_config_file
        update_config_file(self.config_path, {"processor.paraphrase_custom": False})
        keys = (
            DOWN, DOWN, DOWN, DOWN, ENTER,             # «Режим ответов»
            DOWN, DOWN, DOWN, DOWN, ENTER, UP, ENTER,  # «Рандомизация» → «Включена»
            ESC, ESC,                                  # back to DeepSeek, then leave
        )
        out = io.StringIO()
        with scripted_keys(*keys), patch("modules.wizard.prompt_user", return_value=""), \
             patch("sys.stdout", out), self.assertRaises(WizardBack):
            step_deepseek(self.config_path, "sk-old", "https://api.deepseek.com")
        self.assertIs(self.raw()["processor"]["paraphrase_custom"], True)
        deepseek_again = out.getvalue().rsplit("DeepSeek", 1)[-1]
        self.assertNotIn("Рандомизация текста включена", deepseek_again)

    def test_reply_mode_from_deepseek_returns_to_deepseek(self):
        # «Режим ответов» → Esc back to DeepSeek → Esc leaves the section
        with self.assertRaises(WizardBack):
            self.run_keys(
                lambda: step_deepseek(self.config_path, "sk-old", "https://api.deepseek.com"),
                DOWN, DOWN, DOWN, DOWN, ENTER, ESC, ESC,
            )


class TestLaunchSummary(SetupScreenCase):
    def test_own_text_mode_shows_the_template_text(self):
        from modules.wizard import _reply_templates_row
        self.assertEqual(_reply_templates_row(["привет"]), ("свой текст: «привет»", ""))
        text, style = _reply_templates_row(["первый", "второй", "третий"])
        self.assertEqual(text, "свой текст: «первый» · и ещё 2")
        self.assertEqual(_reply_templates_row([])[1], "warn")

    def test_logged_in_accounts_are_named_and_proxy_row_warns_without_proxy(self):
        from modules.wizard import _logged_in_names, _proxy_row, _save_account
        from config import load_config
        self.assertEqual(_logged_in_names(load_config(self.config_path)), [])  # no DB yet, no main session
        _save_account(self.config_path, "kiri", str(self.root / "s" / "kiri"), None)
        _save_account(self.config_path, "mim", str(self.root / "s" / "mim"), "http://1.1.1.1:80")
        cfg = load_config(self.config_path)
        with patch("modules.wizard.check_youtube_session", side_effect=lambda p: (Path(p).name == "kiri", "")):
            self.assertEqual(_logged_in_names(cfg), ["kiri"])
        self.assertEqual(_proxy_row(cfg), ("Прокси", "прокси: 1 из 2", "warn"))
        _save_account(self.config_path, "mim", str(self.root / "s" / "mim"), None)
        self.assertEqual(_proxy_row(load_config(self.config_path))[2:], ("warn",))


class TestStepByStepWizard(SetupScreenCase):
    def test_forward_through_all_stages(self):
        with patch("modules.wizard.step_video") as video, patch("modules.wizard.step_launch") as launch:
            _, output = self.run_keys(lambda: run_step_by_step_wizard(self.config_path), ENTER, ENTER, ENTER)
        video.assert_called_once()
        launch.assert_called_once()
        for title in ("Этап 1/5 · Аккаунты", "Этап 2/5 · Прокси", "Этап 3/5 · DeepSeek"):
            self.assertIn(title, output)

    def test_back_returns_to_previous_stage_and_exits_from_first(self):
        with patch("modules.wizard.step_video") as video:
            _, output = self.run_keys(lambda: run_step_by_step_wizard(self.config_path), ENTER, ESC, ESC)
        video.assert_not_called()
        self.assertEqual(output.count("Этап 1/5 · Аккаунты"), 2)
        self.assertEqual(output.count("Этап 2/5"), 1)

    def test_stage_back_from_video_reopens_deepseek(self):
        with patch("modules.wizard.step_video", side_effect=[WizardBack(), ("u", 2)]), \
             patch("modules.wizard.step_launch") as launch:
            _, output = self.run_keys(lambda: run_step_by_step_wizard(self.config_path), ENTER, ENTER, ENTER, ENTER)
        launch.assert_called_once()
        self.assertEqual(output.count("Этап 3/5"), 2)


if __name__ == "__main__":
    unittest.main()
