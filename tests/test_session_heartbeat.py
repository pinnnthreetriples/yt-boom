from __future__ import annotations

import hashlib
import json
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
    check_session_offline,
    fingerprint_headers,
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

    def test_cookie_hosts_are_matched_strictly(self) -> None:
        future_ts = int(time.time() + 86400 * 30)
        self._create_mock_cookies_db([
            (".youtube.com", "A", "dot", future_ts),
            ("youtube.com", "B", "bare", future_ts),
            ("evilyoutube.com", "C", "lookalike", future_ts),
            (".notyoutube.com", "D", "lookalike", future_ts),
        ])
        self.assertEqual(read_youtube_cookies(self.session_path), {"A": "dot", "B": "bare"})

    def test_login_info_lookup_ignores_lookalike_hosts(self) -> None:
        self._create_mock_cookies_db([("evilyoutube.com", "LOGIN_INFO", "x", int(time.time() + 86400))])
        self.assertEqual(read_login_info_metadata(self.session_path), (False, None, None))

    def _write_fingerprint(self, navigator: dict) -> None:
        payload = {"format": 1, "os": "windows", "fingerprint": {"navigator": navigator}}
        (self.session_path / "fingerprint.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_fingerprint_headers_follow_the_pinned_fingerprint(self) -> None:
        self._write_fingerprint({
            "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0",
            "languages": ["es-ES", "en-US", "en"],
        })
        headers = fingerprint_headers(self.session_path)
        self.assertIn("Firefox/146.0", headers["User-Agent"])
        self.assertEqual(headers["Accept-Language"], "es-ES,en-US;q=0.7,en;q=0.3")

    def test_fingerprint_headers_two_languages_match_firefox_default(self) -> None:
        self._write_fingerprint({"userAgent": "UA", "languages": ["en-US", "en"]})
        self.assertEqual(fingerprint_headers(self.session_path)["Accept-Language"], "en-US,en;q=0.5")

    def test_fingerprint_headers_empty_without_fingerprint(self) -> None:
        self.assertEqual(fingerprint_headers(self.session_path), {})
        (self.session_path / "fingerprint.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(fingerprint_headers(self.session_path), {})

    async def test_request_looks_like_the_profile_firefox(self) -> None:
        future_ts = int(time.time() + 86400 * 30)
        self._create_mock_cookies_db([(".youtube.com", "LOGIN_INFO", "token123", future_ts)])
        self._write_fingerprint({"userAgent": "Mozilla/5.0 Firefox/146.0", "languages": ["en-US", "en"]})
        mock_resp = MagicMock(status_code=200, text='{"LOGGED_IN": true}')
        mock_session = AsyncMock()
        mock_session.get.return_value = mock_resp
        mock_session.__aenter__.return_value = mock_session

        with patch("modules.session_heartbeat.AsyncSession", return_value=mock_session) as session_cls:
            await check_session_heartbeat(self.session_path, account_name="acc", proxy="http://1.2.3.4:80")

        kwargs = session_cls.call_args.kwargs
        self.assertEqual(kwargs["impersonate"], "firefox133")
        self.assertEqual(kwargs["proxy"], "http://1.2.3.4:80")
        self.assertEqual(kwargs["headers"], {"User-Agent": "Mozilla/5.0 Firefox/146.0", "Accept-Language": "en-US,en;q=0.5"})
        self.assertNotIn("Chrome", json.dumps(kwargs["headers"]))
        mock_session.get.assert_awaited_once()
        self.assertEqual(mock_session.get.await_args.args[0], "https://www.youtube.com/")

    async def test_heartbeat_never_writes_to_the_cookie_db(self) -> None:
        future_ts = int(time.time() + 86400 * 30)
        self._create_mock_cookies_db([(".youtube.com", "LOGIN_INFO", "token123", future_ts)])
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        mock_resp = MagicMock(status_code=200, text='{"LOGGED_IN": true}')
        mock_resp.cookies = {"__Secure-1PSIDTS": "rotated"}
        mock_session = AsyncMock()
        mock_session.get.return_value = mock_resp
        mock_session.__aenter__.return_value = mock_session

        with patch("modules.session_heartbeat.AsyncSession", return_value=mock_session):
            await check_session_heartbeat(self.session_path, account_name="acc")
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), before)

    def test_offline_status_alive_with_valid_login_info(self) -> None:
        self._create_mock_cookies_db([(".youtube.com", "LOGIN_INFO", "t", int(time.time() + 86400 * 20))])
        with patch("modules.session_heartbeat.AsyncSession", side_effect=AssertionError("network used")):
            res = check_session_offline(self.session_path, "acc")
        self.assertTrue(res.is_alive)
        self.assertTrue(res.login_info_present)
        self.assertGreater(res.days_remaining or 0, 19.0)
        self.assertIn("offline", res.details)

    def test_offline_status_expired_login_info(self) -> None:
        self._create_mock_cookies_db([(".youtube.com", "LOGIN_INFO", "t", int(time.time() - 3600))])
        res = check_session_offline(self.session_path, "acc")
        self.assertFalse(res.is_alive)
        self.assertTrue(res.login_info_present)
        self.assertEqual(res.details, "LOGIN_INFO expired")

    def test_offline_status_without_cookie_db(self) -> None:
        res = check_session_offline(self.session_path, "acc")
        self.assertFalse(res.is_alive)
        self.assertFalse(res.login_info_present)

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
