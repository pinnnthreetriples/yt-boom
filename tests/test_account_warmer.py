from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from config import BrowserSettings, TargetSettings
from database import AccountRecord, Database
from modules.account_warmer import AccountWarmer, WarmUpResult
from modules.interaction_simulator import InteractionConfig, InteractionSimulator


class TestAccountWarmer(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.session_path = Path(self.temp_dir.name)
        self.browser_cfg = BrowserSettings(
            headless=True,
            targets=[
                TargetSettings(
                    name="youtube",
                    base_url="https://www.youtube.com",
                    item_url_template="https://www.youtube.com/watch?v={external_id}",
                    submit_input_selector="#contenteditable-root",
                    submit_button_selector="#submit-button button",
                    confirmation_selector="ytd-comment-view-model",
                )
            ],
        )
        self.mock_sim = MagicMock()
        self.mock_sim.human_pause = AsyncMock()
        self.mock_sim.scroll = AsyncMock()
        self.mock_sim.move_mouse = AsyncMock()
        self.account = AccountRecord(
            id=1,
            name="warmer_acc",
            session_path=str(self.session_path),
            created_at="2026-01-01T00:00:00Z",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_warm_up_with_explicit_video(self) -> None:
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.query_selector = AsyncMock(return_value=None)

        mock_context = MagicMock()
        mock_context.pages = [mock_page]

        mock_runner = AsyncMock()
        mock_runner.context = mock_context
        mock_runner._dismiss_consent = AsyncMock()

        warmer = AccountWarmer(self.browser_cfg, simulator=self.mock_sim)
        with patch("modules.account_warmer.YouTubeRunner", return_value=mock_runner):
            res = await warmer.warm_up(
                self.account,
                duration_seconds=0.1,
                target_video_url="https://www.youtube.com/watch?v=custom123",
            )
            self.assertTrue(res.success)
            self.assertEqual(res.account_name, "warmer_acc")
            self.assertEqual(res.video_url, "https://www.youtube.com/watch?v=custom123")
            mock_runner.start.assert_called_once()
            mock_runner.close.assert_called_once()
            mock_page.goto.assert_called_with("https://www.youtube.com/watch?v=custom123", wait_until="domcontentloaded")

    async def test_warm_up_homepage_fallback_and_db_stats(self) -> None:
        mock_page = AsyncMock()
        mock_page.goto = AsyncMock()
        mock_page.eval_on_selector_all = AsyncMock(return_value=["https://www.youtube.com/watch?v=home_vid_1"])
        mock_page.query_selector = AsyncMock(return_value=None)

        mock_context = MagicMock()
        mock_context.pages = [mock_page]

        mock_runner = AsyncMock()
        mock_runner.context = mock_context
        mock_runner._dismiss_consent = AsyncMock()

        mock_db = AsyncMock(spec=Database)

        warmer = AccountWarmer(self.browser_cfg, simulator=self.mock_sim)
        with patch("modules.account_warmer.YouTubeRunner", return_value=mock_runner):
            res = await warmer.warm_up(
                self.account,
                duration_seconds=0.1,
                db=mock_db,
            )
            self.assertTrue(res.success)
            self.assertEqual(res.video_url, "https://www.youtube.com/watch?v=home_vid_1")
            mock_db.increment_account_stats.assert_called_once_with(1, replies=0)

    async def test_warm_up_failure_handled_gracefully(self) -> None:
        mock_runner = AsyncMock()
        mock_runner.start.side_effect = RuntimeError("Browser crashed")

        warmer = AccountWarmer(self.browser_cfg, simulator=self.mock_sim)
        with patch("modules.account_warmer.YouTubeRunner", return_value=mock_runner):
            res = await warmer.warm_up(
                self.account,
                duration_seconds=0.1,
            )
            self.assertFalse(res.success)
            self.assertIn("Browser crashed", res.details)
            mock_runner.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
