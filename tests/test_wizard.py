from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from config import load_config
from modules.wizard import (
    check_deepseek_api,
    check_youtube_session,
    clear_youtube_session,
    mask_secret,
    step_account,
    step_deepseek,
    step_launch,
    step_proxy,
    step_video,
    update_config_file,
)


class TestWizardHelpers(unittest.TestCase):
    def test_mask_secret(self):
        self.assertEqual(mask_secret(None), "[не задан]")
        self.assertEqual(mask_secret(""), "[не задан]")
        self.assertEqual(mask_secret("short"), "****")
        masked = mask_secret("sk-abcdefghijklmnopqrstuvwxyz123456")
        self.assertTrue(masked.startswith("sk-abcd..."))
        self.assertTrue(masked.endswith("3456"))

class TestWizardSessionAndApi(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_check_youtube_session_missing_dir(self):
        ok, msg = check_youtube_session(self.root)
        self.assertFalse(ok)
        self.assertIn("отсутствует", msg)

    def test_check_youtube_session_empty_profile(self):
        yt_dir = self.root / "youtube"
        yt_dir.mkdir(parents=True)
        ok, msg = check_youtube_session(self.root)
        self.assertFalse(ok)
        self.assertIn("отсутствуют", msg)

    def test_check_youtube_session_guest_mode(self):
        yt_dir = self.root / "youtube"
        yt_dir.mkdir(parents=True)
        db_path = yt_dir / "cookies.sqlite"
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT)")
        c.execute("INSERT INTO moz_cookies VALUES ('.youtube.com', 'VISITOR_INFO1_LIVE', 'anon123')")
        conn.commit()
        conn.close()

        ok, msg = check_youtube_session(self.root)
        self.assertFalse(ok)
        self.assertIn("Гостевой режим", msg)

    def test_check_youtube_session_authenticated(self):
        yt_dir = self.root / "youtube"
        yt_dir.mkdir(parents=True)
        db_path = yt_dir / "cookies.sqlite"
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT)")
        c.execute("INSERT INTO moz_cookies VALUES ('.youtube.com', 'LOGIN_INFO', 'auth_token_xyz')")
        c.execute("INSERT INTO moz_cookies VALUES ('.youtube.com', 'SID', 'sid_val')")
        conn.commit()
        conn.close()

        ok, msg = check_youtube_session(self.root)
        self.assertTrue(ok)
        self.assertIn("Авторизован", msg)

    def test_clear_youtube_session(self):
        yt_dir = self.root / "youtube"
        yt_dir.mkdir(parents=True)
        (yt_dir / "test.txt").write_text("data")
        self.assertTrue(yt_dir.exists())

        ok, msg = clear_youtube_session(self.root)
        self.assertTrue(ok)
        self.assertFalse(yt_dir.exists())

    def test_check_deepseek_api_empty_key(self):
        ok, msg = asyncio.run(check_deepseek_api(""))
        self.assertFalse(ok)
        self.assertIn("пуст", msg)

    def test_check_deepseek_api_success(self):
        with patch("openai.AsyncOpenAI.models") as mock_models:
            mock_list = AsyncMock()
            m1 = MagicMock()
            m1.id = "deepseek-chat"
            mock_list.return_value = MagicMock(data=[m1])
            mock_models.list = mock_list

            ok, msg = asyncio.run(check_deepseek_api("sk-valid-key-12345"))
            self.assertTrue(ok)
            self.assertIn("Ключ активен", msg)

    def test_check_deepseek_api_401_error(self):
        with patch("openai.AsyncOpenAI.models") as mock_models:
            mock_models.list = AsyncMock(side_effect=RuntimeError("Error code: 401 - Authentication Fails"))

            ok, msg = asyncio.run(check_deepseek_api("sk-wrong-key"))
            self.assertFalse(ok)
            self.assertIn("401", msg)


class TestWizardConfigUpdateAndSteps(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "config.yaml"

        with open("config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cfg["app"]["db_path"] = str(self.root / "test.db")
        cfg["app"]["log_file"] = str(self.root / "test.log")
        cfg["browser"]["session_root"] = str(self.root / "sessions")
        cfg["browser"]["accounts"] = []  # the real config.yaml may already hold the user's own accounts
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_update_config_file_proxy_and_endpoint(self):
        update_config_file(
            self.config_path,
            {
                "browser.proxy": "http://127.0.0.1:8080",
                "collector.endpoints[0].url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            },
        )
        loaded = load_config(self.config_path)
        self.assertEqual(loaded.browser.proxy, "http://127.0.0.1:8080")
        self.assertEqual(loaded.collector.endpoints[0].url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_step_proxy_continue_warns_once_then_leaves(self):
        with patch("modules.wizard.prompt_user", return_value="1") as prompt:
            self.assertIsNone(step_proxy(self.config_path))
        self.assertEqual(prompt.call_count, 2)  # «default» has no proxy: the first «Продолжить» only warns

    def test_step_proxy_plain_mode_picks_search_direct(self):
        from database import Database

        async def seed():
            async with Database(load_config(self.config_path).app.db_path) as db:
                record, _ = await db.add_proxy_url("http://1.2.3.4:8080")
                await db.set_search_proxy(record.id)

        from modules.wizard import load_accounts
        load_accounts(self.config_path)
        asyncio.run(seed())
        with patch("modules.wizard.prompt_user", side_effect=["S", "D", "1", "1"]):
            step_proxy(self.config_path)

        async def search():
            async with Database(load_config(self.config_path).app.db_path) as db:
                return await db.get_search_proxy()

        self.assertIsNone(asyncio.run(search()))

    def test_step_deepseek_leave_as_is(self):
        with patch("modules.wizard.prompt_user", return_value="1"):
            result = step_deepseek(self.config_path, "sk-initial-key", "https://api.deepseek.com")
            self.assertEqual(result, "sk-initial-key")

    def test_step_video_change_limit(self):
        with patch("modules.wizard.prompt_user", side_effect=["2", "5", "4"]):  # section stays open until «Готово»
            lim = step_video(self.config_path, 2)
            self.assertEqual(lim, 5)

    def test_step_launch_dry_run(self):
        update_config_file(self.config_path, {"collector.search_query": "python"})
        with patch("modules.wizard.prompt_user", return_value="1"), \
             patch("youtube_bot.run_multi_video_pipeline", new_callable=AsyncMock) as mock_reply:
            result = step_launch(
                self.config_path,
                self.root / "sessions",
                "sk-dummy-key",
                2,
            )
            self.assertTrue(result)
            mock_reply.assert_called_once()
            _, kwargs = mock_reply.call_args
            self.assertTrue(kwargs.get("dry_run"))

    def test_step_video_persists_limit_to_config_yaml(self):
        with patch("modules.wizard.prompt_user", side_effect=["2", "7", "4"]):
            lim = step_video(self.config_path, 2)
            self.assertEqual(lim, 7)
            loaded = load_config(self.config_path)
            self.assertEqual(loaded.limits.max_iterations_per_session, 7)
            self.assertEqual(loaded.limits.total_iterations, 7)

    def test_step_proxy_back_raises_wizard_back(self):
        from modules.wizard import WizardBack
        with patch("modules.wizard.prompt_user", return_value="0"):
            with self.assertRaises(WizardBack):
                step_proxy(self.config_path)

    def test_step_account_check_status_then_continue(self):
        with patch("modules.wizard.prompt_user", side_effect=["A1", "1", "4"]):
            res = step_account(self.config_path, self.root / "sessions")
            self.assertFalse(res)

    def test_step_deepseek_check_api_then_continue(self):
        with patch("modules.wizard.prompt_user", side_effect=["3", "1"]), \
             patch("modules.wizard.check_deepseek_api", new_callable=AsyncMock, return_value=(True, "ok")):
            res = step_deepseek(self.config_path, "sk-test-key", "https://api.deepseek.com")
            self.assertEqual(res, "sk-test-key")

    def test_step_launch_back_raises_wizard_back(self):
        from modules.wizard import WizardBack
        with patch("modules.wizard.prompt_user", return_value="0"):
            with self.assertRaises(WizardBack):
                step_launch(
                    self.config_path,
                    self.root / "sessions",
                    "sk-dummy-key",
                    2,
                )

    def test_run_step_by_step_wizard_aborts_on_wizard_back(self):
        from modules.wizard import run_step_by_step_wizard
        with patch("modules.wizard.step_account", side_effect=__import__("modules.wizard", fromlist=["WizardBack"]).WizardBack), \
             patch("modules.wizard.step_proxy") as mock_proxy:
            run_step_by_step_wizard(self.config_path)
            mock_proxy.assert_not_called()

    def test_step_video_configure_search(self):
        # Configure search with query, min_views, max_views
        inputs = ["1", "5", "ai agents", "10000", "500000", "4"]  # «Видео» -> «Автопоиск»
        with patch("modules.wizard.prompt_user", side_effect=inputs):
            step_video(self.config_path, 2)
            cfg = load_config(self.config_path)
            self.assertEqual(cfg.collector.search_query, "ai agents")
            self.assertEqual(cfg.collector.min_views, 10000)
            self.assertEqual(cfg.collector.max_views, 500000)

        # Reset search with '0'
        reset_inputs = ["1", "5", "0", "4"]
        with patch("modules.wizard.prompt_user", side_effect=reset_inputs):
            step_video(self.config_path, 2)
            cfg = load_config(self.config_path)
            self.assertIsNone(cfg.collector.search_query)

    def test_step_video_configure_target_mode(self):
        # Mode 1 -> first_reply
        with patch("modules.wizard.prompt_user", side_effect=["6", "1", "4"]):
            step_video(self.config_path, 2)
            cfg = load_config(self.config_path)
            self.assertEqual(cfg.collector.target_mode, "first_reply")

        # Mode 3 -> sub_comment
        with patch("modules.wizard.prompt_user", side_effect=["6", "3", "4"]):
            step_video(self.config_path, 2)
            cfg = load_config(self.config_path)
            self.assertEqual(cfg.collector.target_mode, "sub_comment")

        # Mode 4 -> mixed
        with patch("modules.wizard.prompt_user", side_effect=["6", "4", "4"]):
            step_video(self.config_path, 2)
            cfg = load_config(self.config_path)
            self.assertEqual(cfg.collector.target_mode, "mixed")

    def test_view_record_detail(self):
        from database import Database, ActivityLogCreate
        from modules.wizard import view_record_detail

        db_file = self.root / "test.db"
        db = Database(db_file)

        async def _seed():
            await db.connect()
            await db.init_schema()
            await db.log_activity(
                ActivityLogCreate(
                    video_id="v_rec_detail",
                    video_title="Detail Test Video",
                    video_url="https://youtube.com/watch?v=v_rec_detail",
                    target_id="cid_100",
                    target_author="@author_rec",
                    target_text="Target comment to inspect",
                    reply_text="Bot reply text here",
                    mode="live",
                    status="success",
                    duration_sec=3.14,
                )
            )
            await db.close()

        asyncio.run(_seed())

        with patch("modules.wizard.prompt_user", return_value=""):
            # Test existing record
            view_record_detail(str(db_file), 1)

            # Test non-existent record
            view_record_detail(str(db_file), 999)

    def test_activity_log_viewer_flow(self):
        from database import Database, ActivityLogCreate
        from modules.wizard import activity_log_viewer

        db_file = self.root / "test.db"
        db = Database(db_file)

        async def _seed_multiple():
            await db.connect()
            await db.init_schema()
            for i in range(1, 11):
                st = "success" if i % 2 == 0 else "failed"
                await db.log_activity(
                    ActivityLogCreate(
                        video_id=f"v_pg_{i}",
                        target_id=f"cid_{i}",
                        target_author=f"@user_{i}",
                        target_text=f"Target {i}",
                        reply_text=f"Reply {i}",
                        mode="live",
                        status=st,
                    )
                )
            await db.close()

        asyncio.run(_seed_multiple())

        # Sequence of viewer commands:
        # 1. 'N' (next page)
        # 2. 'P' (previous page)
        # 3. 'V 1' (view record 1) -> prompt in detail modal returns ''
        # 4. 'F' -> '2' (filter by success)
        # 5. 'C' -> 'y' (confirm clear all logs)
        # 6. '0' (exit viewer)
        inputs = ["N", "P", "V 1", "", "F", "2", "C", "y", "0"]
        with patch("modules.wizard.prompt_user", side_effect=inputs):
            activity_log_viewer(self.config_path)

        async def _verify_empty():
            await db.connect()
            try:
                cnt = await db.count_activity_logs()
                self.assertEqual(cnt, 0)
            finally:
                await db.close()

        asyncio.run(_verify_empty())

    def test_main_cli_help_and_config(self):
        from main import main as main_cli
        # Test --help returns 0
        ret_help = main_cli(["--help"])
        self.assertEqual(ret_help, 0)

        # Test --wizard launches wizard app
        with patch("modules.wizard.run_wizard_app") as mock_wizard:
            ret_wiz = main_cli(["--wizard", "--config", str(self.config_path)])
            self.assertEqual(ret_wiz, 0)
            mock_wizard.assert_called_once_with(str(self.config_path))


if __name__ == "__main__":
    unittest.main()


