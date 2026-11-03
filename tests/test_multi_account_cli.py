from __future__ import annotations

import asyncio
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import yaml

from config import load_config
from database import Database
from youtube_bot import (
    interactive_login,
    main,
    run_video_pool_cli,
    run_reply_bot,
)


class TestMultiAccountCLI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "test.db"
        self.config_path = self.root / "test_config.yaml"

        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg_data = yaml.safe_load(f)
        cfg_data["app"]["db_path"] = str(self.db_path)
        cfg_data["app"]["log_file"] = str(self.root / "test.log")
        cfg_data["browser"]["session_root"] = str(self.root / "sessions")
        cfg_data["collector"]["target_mode"] = "raw"
        cfg_data["reply_mode"] = "ai"
        cfg_data["processor"]["reply_mode"] = "ai"
        cfg_data["custom_replies"] = []
        cfg_data["processor"]["custom_replies"] = []
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg_data, f)

        self.db = Database(self.db_path)
        await self.db.connect()
        await self.db.init_schema()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp_dir.cleanup()

    def test_cli_argument_parsing_account_single_video(self):
        test_args = [
            "youtube_bot.py",
            "--video",
            "https://www.youtube.com/watch?v=vid123",
            "--account",
            "bot_alpha",
            "--config",
            str(self.config_path),
            "--dry-run",
        ]
        with patch("sys.argv", test_args), patch("youtube_bot.run_reply_bot", new_callable=AsyncMock) as mock_bot:
            main()
            mock_bot.assert_called_once()
            _, kwargs = mock_bot.call_args
            self.assertEqual(kwargs.get("account_name"), "bot_alpha")
            self.assertTrue(kwargs.get("dry_run"))

    def test_cli_argument_parsing_account_search(self):
        test_args = [
            "youtube_bot.py",
            "--search",
            "python programming",
            "--account",
            "search_worker",
            "--config",
            str(self.config_path),
            "--dry-run",
        ]
        with patch("sys.argv", test_args), patch("youtube_bot.run_multi_video_pipeline", new_callable=AsyncMock) as mock_multi:
            main()
            mock_multi.assert_called_once()
            args, kwargs = mock_multi.call_args
            self.assertEqual(args[0], "python programming")
            self.assertEqual(kwargs.get("account_name"), "search_worker")

    def test_cli_login_with_account_name(self):
        test_args = [
            "youtube_bot.py",
            "--login",
            "--account",
            "login_bot_acc",
            "--config",
            str(self.config_path),
        ]
        with patch("sys.argv", test_args), patch("youtube_bot.interactive_login", new_callable=AsyncMock) as mock_login:
            main()
            mock_login.assert_called_once_with(str(self.config_path), account_name="login_bot_acc", allow_country_mismatch=False)

    async def test_interactive_login_passes_correct_session_and_proxy(self):
        # Insert configured account with proxy
        sess_dir = (self.root / "sessions" / "proxy_acc").as_posix()
        await self.db.upsert_account(
            name="proxy_acc",
            session_path=sess_dir,
            proxy_url="http://user:pass@185.22.33.44:8080",
            is_active=True,
        )

        mock_context = AsyncMock()
        mock_context.pages = []
        mock_page = AsyncMock()
        mock_context.new_page.return_value = mock_page

        mock_camoufox = MagicMock()
        mock_camoufox.return_value.__aenter__.return_value = mock_context
        mock_camoufox.return_value.__aexit__.return_value = None

        with patch("camoufox.async_api.AsyncCamoufox", mock_camoufox):
            await interactive_login(str(self.config_path), account_name="proxy_acc")

            self.assertTrue(mock_camoufox.called)
            _, kwargs = mock_camoufox.call_args
            self.assertEqual(Path(kwargs.get("user_data_dir")), Path(sess_dir))
            proxy_opt = kwargs.get("proxy")
            self.assertIsNotNone(proxy_opt)
            self.assertEqual(proxy_opt.get("server"), "http://185.22.33.44:8080")
            self.assertEqual(proxy_opt.get("username"), "user")
            self.assertEqual(proxy_opt.get("password"), "pass")

    def test_cli_add_account(self):
        target_session = (self.root / "sessions" / "cli_created_acc").as_posix()
        test_args = [
            "youtube_bot.py",
            "--add-account",
            "cli_created_acc",
            "--proxy",
            "http://proxy.host:3128",
            "--session",
            target_session,
            "--config",
            str(self.config_path),
        ]
        with patch("sys.argv", test_args):
            main()

        # Check DB directly
        async def _check():
            acc = await self.db.get_account("cli_created_acc")
            self.assertIsNotNone(acc)
            self.assertEqual(acc.name, "cli_created_acc")
            self.assertEqual(acc.proxy_url, "http://proxy.host:3128")
            self.assertEqual(acc.session_path, target_session)

        asyncio.run(_check())

    def test_cli_list_accounts(self):
        async def _seed():
            await self.db.upsert_account("acc_one", "./sess1", proxy_url=None, is_active=True)
            await self.db.upsert_account("acc_two", "./sess2", proxy_url="http://p:80", is_active=False)

        asyncio.run(_seed())

        test_args = ["youtube_bot.py", "--list-accounts", "--config", str(self.config_path)]
        output = io.StringIO()
        with patch("sys.argv", test_args), patch("sys.stdout", output):
            main()

        printed = output.getvalue()
        self.assertIn("YOUTUBE ACCOUNTS OVERVIEW", printed)
        self.assertIn("acc_one", printed)
        self.assertIn("acc_two", printed)
        self.assertIn("ACTIVE", printed)
        self.assertIn("DISABLED", printed)

    def test_cli_video_pool_arguments(self):
        vids_path = self.root / "videos.txt"
        vids_path.write_text("https://youtube.com/watch?v=v1\n", encoding="utf-8")

        test_args = [
            "youtube_bot.py",
            "--videos-file",
            str(vids_path),
            "--concurrency",
            "3",
            "--account-mode",
            "share",
            "--dry-run",
            "--config",
            str(self.config_path),
        ]
        with patch("sys.argv", test_args), patch(
            "youtube_bot.run_video_pool_cli", new_callable=AsyncMock
        ) as mock_pool_run:
            mock_pool_run.return_value = 4
            main()

            mock_pool_run.assert_called_once_with(
                config_path=str(self.config_path),
                videos_file=str(vids_path),
                concurrency=3,
                account_mode="share",
                dry_run=True,
                headless=None,
            )

    async def test_run_video_pool_cli_uses_config_threads_and_mode(self):
        with patch("youtube_bot.Database", return_value=AsyncMock()), \
             patch("modules.multi_account_orchestrator.MultiAccountOrchestrator") as orch_cls:
            orch_cls.return_value.run_video_pool = AsyncMock(return_value=5)
            orch_cls.return_value.close = AsyncMock()
            res = await run_video_pool_cli(config_path=str(self.config_path), videos_file="vids.txt", dry_run=True)
        self.assertEqual(res, 5)
        orch_cls.return_value.run_video_pool.assert_awaited_once_with(videos_file="vids.txt")
        orch_cls.return_value.close.assert_awaited_once()
        kwargs = orch_cls.call_args.kwargs
        cfg = load_config(self.config_path)
        self.assertEqual(kwargs["concurrency"], cfg.limits.max_concurrent_sessions)
        self.assertIsNone(kwargs["account_mode"])  # the orchestrator falls back to limits.account_mode
        self.assertTrue(kwargs["dry_run"])

    async def test_run_reply_bot_with_account_resolves_and_updates_stats(self):
        acc = await self.db.upsert_account(
            name="runner_stats_acc",
            session_path=str(self.root / "sessions" / "stats_acc"),
            proxy_url=None,
            is_active=True,
        )
        self.assertEqual(acc.total_replies, 0)

        from modules.collector import CollectedItem
        from modules.ui_test_runner import SubmissionResult

        fake_item = CollectedItem(
            endpoint="youtube:feed",
            external_id="vid_test_xyz#comm1",
            text="Nice tutorial!",
            language="en",
            confidence=0.95,
            author="@viewer1",
            is_reply=False,
        )

        with patch("modules.youtube_collector.YouTubeCollector.collect_from_videos", new_callable=AsyncMock) as mock_col, \
             patch("modules.youtube_collector.YouTubeCollector.fetch_popular_comments", new_callable=AsyncMock) as mock_fetch, \
             patch("modules.processor.DeepSeekProcessor.generate", new_callable=AsyncMock) as mock_gen, \
             patch("modules.youtube_runner.YouTubeRunner.run", new_callable=AsyncMock) as mock_run:

            mock_col.return_value = [fake_item]
            mock_fetch.return_value = [fake_item]
            mock_gen.return_value = "Glad you enjoyed it!"
            mock_run.return_value = SubmissionResult(
                success=True,
                remote_id="yt_remote_101",
                duration_seconds=1.2,
            )

            await run_reply_bot(
                "https://www.youtube.com/watch?v=vid_test_xyz",
                config_path=str(self.config_path),
                limit=1,
                account_name="runner_stats_acc",
                dry_run=False,
            )

            updated = await self.db.get_account("runner_stats_acc")
            self.assertIsNotNone(updated)
            self.assertEqual(updated.total_replies, 1)


class TestMultiAccountWizard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "wizard_test.db"
        self.config_path = self.root / "wizard_config.yaml"

        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg_data = yaml.safe_load(f)
        cfg_data["app"]["db_path"] = str(self.db_path)
        cfg_data["app"]["log_file"] = str(self.root / "wizard.log")
        cfg_data["browser"]["session_root"] = str(self.root / "sessions")
        cfg_data["browser"]["accounts"] = []
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg_data, f)

    def tearDown(self):
        self.temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
