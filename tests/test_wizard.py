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
    update_config_file,
)


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


