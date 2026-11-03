"""Несовпадение страны прокси перед входом: флаг, вопрос «отменить или продолжить», отмена без консоли."""
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from loguru import logger

import youtube_bot
from database import Database
from tests import test_account_geo as geo  # module import: a class imported by name would be collected twice
from tests.test_account_geo import SECRET, _cfg


class TestCountryDecision(geo._DbCase):
    _run = geo.TestAuthenticateGuard._run  # same sign-in harness: runner, authenticator and exit lookup are mocked

    async def _fresh(self, **kwargs):
        (self.root / "metrics.db").unlink(missing_ok=True)  # an earlier sign-in in the same test must not leave the account authenticated
        return await self._run(**kwargs)

    def _capture(self, level: str) -> list[str]:
        messages: list[str] = []
        sink = logger.add(lambda m: messages.append(str(m)), level=level, format="{message}")
        self.addCleanup(logger.remove, sink)
        return messages

    async def test_flag_continues_with_a_warning_and_never_asks(self):
        messages = self._capture("WARNING")
        confirm = MagicMock(return_value=False)
        for exit_country in ("US", None):
            result, stored, _, _, _ = await self._fresh(
                expected="PL", exit_country=exit_country, allow_country_mismatch=True, confirm_country=confirm
            )
            self.assertTrue(result)
            self.assertEqual(stored.auth_status, "authenticated")
        confirm.assert_not_called()
        self.assertTrue(any("--allow-country-mismatch" in m and "PL" in m for m in messages))

    async def test_confirm_yes_continues_and_no_cancels_for_mismatch_and_unknown(self):
        for exit_country in ("US", None):
            for answer in (True, False):
                with self.subTest(exit_country=exit_country, answer=answer):
                    confirm = MagicMock(return_value=answer)
                    result, stored, runner_cls, _, _ = await self._fresh(
                        expected="PL", exit_country=exit_country, confirm_country=confirm
                    )
                    self.assertEqual(result, answer)
                    self.assertEqual(runner_cls.called, answer)
                    self.assertEqual(stored.auth_status, "authenticated" if answer else "pending")
                    confirm.assert_called_once()

    async def test_async_confirm_is_awaited_and_message_explains_the_risk(self):
        confirm = AsyncMock(return_value=True)
        result, *_ = await self._fresh(expected="PL", exit_country="US", confirm_country=confirm)
        self.assertTrue(result)
        text = confirm.await_args.args[0]
        for part in ("bob", "PL", "US", "блокировк"):
            self.assertIn(part, text)
        unknown = AsyncMock(return_value=False)
        await self._fresh(expected="PL", exit_country=None, confirm_country=unknown)
        self.assertIn("гео-база Camoufox", unknown.await_args.args[0])

    async def test_console_question_defaults_to_cancel(self):
        for typed, expected in (("", False), ("о", False), ("что-то", False), ("п", True), ("Да", True), ("y", True)):
            with self.subTest(typed=typed), patch("builtins.input", return_value=typed):
                result, _, runner_cls, _, _ = await self._fresh(expected="PL", exit_country="US", interactive=True)
                self.assertEqual((result, runner_cls.called), (expected, expected))

    async def test_closed_console_cancels(self):
        with patch("builtins.input", side_effect=EOFError):
            result, _, runner_cls, _, _ = await self._fresh(expected="PL", exit_country="US", interactive=True)
        self.assertFalse(result)
        runner_cls.assert_not_called()

    async def test_no_console_and_no_flag_cancels_with_a_hint_about_the_flag(self):
        messages = self._capture("ERROR")
        result, _, runner_cls, _, _ = await self._fresh(expected="PL", exit_country="US", interactive=False)
        self.assertFalse(result)
        runner_cls.assert_not_called()
        self.assertTrue(any("--allow-country-mismatch" in m for m in messages))

    async def test_ok_and_skipped_never_ask(self):
        confirm = MagicMock(return_value=False)
        for expected, exit_country in (("PL", "PL"), (None, "US")):
            result, *_ = await self._fresh(expected=expected, exit_country=exit_country, confirm_country=confirm)
            self.assertTrue(result)
        confirm.assert_not_called()

    async def test_decision_is_logged(self):
        messages = self._capture("WARNING")
        await self._fresh(expected="PL", exit_country="US", confirm_country=MagicMock(return_value=True))
        await self._fresh(expected="PL", exit_country="US", confirm_country=MagicMock(return_value=False))
        joined = "\n".join(messages)
        self.assertIn("вход продолжен", joined)
        self.assertIn("вход отменён", joined)

    async def test_auth_account_cli_asks_once_per_login(self):
        cfg = _cfg(self.root)
        async with Database(cfg.app.db_path) as db:
            await db.upsert_account(
                "bob", str(self.root / "s"), proxy_url="socks5://u:p@1.2.3.4:1080", email="bob@gmail.com",
                password="pw", expected_country="PL",
            )
        confirm = MagicMock(return_value=False)
        with patch("youtube_bot.load_config", return_value=cfg), \
             patch("youtube_bot.YouTubeRunner") as runner_cls, \
             patch("modules.geo_guard.resolve_exit_country", AsyncMock(return_value="US")):
            self.assertFalse(await youtube_bot.auth_account_cli("bob", confirm_country=confirm))
        runner_cls.assert_not_called()
        confirm.assert_called_once()

    async def test_import_passes_the_flag_to_every_account(self):
        file = self.root / "accounts.txt"
        file.write_text(f"a@gmail.com:pw:{SECRET}\nb@gmail.com:pw:{SECRET}\n", encoding="utf-8")
        auth = AsyncMock(return_value=True)
        with patch("youtube_bot.load_config", return_value=_cfg(self.root)), \
             patch("youtube_bot._authenticate_account", auth), \
             patch("youtube_bot._pause_between_accounts", return_value=0):
            await youtube_bot.import_accounts_file_cli(str(file), allow_country_mismatch=True)
        self.assertEqual(auth.await_count, 2)
        self.assertTrue(all(call.args[3] is True for call in auth.await_args_list))


class TestCountryFlagArgparse(unittest.TestCase):
    def _main(self, argv: list[str], target: str) -> MagicMock:
        with patch.object(sys, "argv", ["youtube_bot.py", *argv]), \
             patch(f"youtube_bot.{target}", new_callable=AsyncMock, return_value=True) as handler, \
             patch("youtube_bot.bind_to_kill_on_close_job"), patch("youtube_bot.setup_cli_logging"):
            try:
                youtube_bot.main()
            except SystemExit:
                pass
        return handler

    def test_flag_reaches_every_sign_in_command(self):
        for argv, target in (
            (["--auth-account", "bob"], "auth_account_cli"),
            (["--import-accounts-file", "f.txt"], "import_accounts_file_cli"),
            (["--login", "--account", "bob"], "interactive_login"),
        ):
            with self.subTest(target=target):
                self.assertFalse(self._main(argv, target).await_args.kwargs["allow_country_mismatch"])
                self.assertTrue(self._main([*argv, "--allow-country-mismatch"], target).await_args.kwargs["allow_country_mismatch"])


if __name__ == "__main__":
    unittest.main()
