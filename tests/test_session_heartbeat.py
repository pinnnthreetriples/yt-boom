from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from database import AccountRecord
from modules.session_heartbeat import (
    HeartbeatStatus,
    check_account_heartbeat,
    check_session_heartbeat,
    get_cookies_db_path,
    read_login_info_metadata,
    read_youtube_cookies,
)


class TestSessionHeartbeat(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.session_path = Path(self.temp_dir.name)
        self.db_path = self.session_path / "cookies.sqlite"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_mock_cookies_db(self, cookies: list[tuple[str, str, str, int]]) -> None:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE moz_cookies ("
            "id INTEGER PRIMARY KEY, host TEXT, name TEXT, value TEXT, expiry INTEGER, isSecure INTEGER, isHttpOnly INTEGER"
            ")"
        )
        for host, name, val, exp in cookies:
            cur.execute(
                "INSERT INTO moz_cookies (host, name, value, expiry, isSecure, isHttpOnly) VALUES (?, ?, ?, ?, 1, 1)",
                (host, name, val, exp),
            )
        conn.commit()
        conn.close()

    def test_get_cookies_db_path(self) -> None:
        self.assertIsNone(get_cookies_db_path(self.session_path))
        self.db_path.touch()
        self.assertEqual(get_cookies_db_path(self.session_path), self.db_path)

    def test_read_youtube_cookies_filtering(self) -> None:
        future_ts = int(time.time() + 86400 * 30)
        self._create_mock_cookies_db([
            (".youtube.com", "LOGIN_INFO", "secret_login_info", future_ts),
            (".youtube.com", "SID", "youtube_sid", future_ts),
            (".google.com", "SID", "google_sid", future_ts),
            ("example.com", "FOO", "bar", future_ts),
        ])
        cookies = read_youtube_cookies(self.session_path)
        self.assertEqual(cookies.get("LOGIN_INFO"), "secret_login_info")
        self.assertEqual(cookies.get("SID"), "youtube_sid")
        self.assertNotIn("FOO", cookies)

    def test_read_login_info_metadata_formatting(self) -> None:
        future_ms = int((time.time() + 86400 * 10) * 1000)
        self._create_mock_cookies_db([
            (".youtube.com", "LOGIN_INFO", "secret_login_info", future_ms),
        ])
        has_cookie, expires_str, days_left = read_login_info_metadata(self.session_path)
        self.assertTrue(has_cookie)
        self.assertIsNotNone(expires_str)
        self.assertGreater(days_left or 0, 9.0)

    async def test_check_session_heartbeat_missing_login_info(self) -> None:
        res = await check_session_heartbeat(self.session_path, account_name="test_acc")
        self.assertFalse(res.is_alive)
        self.assertFalse(res.login_info_present)

    async def test_check_session_heartbeat_success(self) -> None:
        future_ts = int(time.time() + 86400 * 30)
        self._create_mock_cookies_db([
            (".youtube.com", "LOGIN_INFO", "token123", future_ts),
        ])
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = 'var ytcfg = {"LOGGED_IN": true}; "avatar":{"thumbnails":[{"url":"https://yt.com/avatar.jpg"}]}'

        mock_session = AsyncMock()
        mock_session.get.return_value = mock_resp
        mock_session.__aenter__.return_value = mock_session

        with patch("modules.session_heartbeat.AsyncSession", return_value=mock_session):
            res = await check_session_heartbeat(self.session_path, account_name="test_acc")
            self.assertTrue(res.is_alive)
            self.assertTrue(res.login_info_present)
            self.assertEqual(res.avatar_url, "https://yt.com/avatar.jpg")

    async def test_check_session_heartbeat_logged_out(self) -> None:
        future_ts = int(time.time() + 86400 * 30)
        self._create_mock_cookies_db([
            (".youtube.com", "LOGIN_INFO", "token123", future_ts),
        ])
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = 'var ytcfg = {"LOGGED_IN": false};'

        mock_session = AsyncMock()
        mock_session.get.return_value = mock_resp
        mock_session.__aenter__.return_value = mock_session

        with patch("modules.session_heartbeat.AsyncSession", return_value=mock_session):
            res = await check_session_heartbeat(self.session_path, account_name="test_acc")
            self.assertFalse(res.is_alive)
            self.assertTrue(res.login_info_present)
            self.assertIn("LOGGED_IN: false", res.details)

    async def test_check_account_heartbeat_delegation(self) -> None:
        acc = AccountRecord(
            id=1,
            name="acc1",
            session_path=str(self.session_path),
            proxy_url="http://user:pass@1.2.3.4:8080",
            created_at="2026-01-01T00:00:00Z",
        )
        with patch("modules.session_heartbeat.check_session_heartbeat") as mock_check:
            mock_check.return_value = HeartbeatStatus(
                account_name="acc1",
                is_alive=True,
                login_info_present=True,
            )
            res = await check_account_heartbeat(acc)
            self.assertTrue(res.is_alive)
            mock_check.assert_called_once_with(
                session_dir=str(self.session_path),
                account_name="acc1",
                proxy="http://user:pass@1.2.3.4:8080",
                timeout=15.0,
            )


if __name__ == "__main__":
    unittest.main()
