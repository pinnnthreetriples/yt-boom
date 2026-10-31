from __future__ import annotations

import random
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

    @staticmethod
    def _element(**attrs):
        element = AsyncMock()
        element.get_attribute = AsyncMock(side_effect=lambda name: attrs.get(name))
        element.is_visible = AsyncMock(return_value=True)
        element.bounding_box = AsyncMock(return_value={"x": 10, "y": 10, "width": 400, "height": 300})
        return element

    def _page(self, cards, url="https://www.youtube.com/watch?v=home_vid_1"):
        page = AsyncMock()
        page.url = url
        by_selector = {
            "ytd-rich-grid-media a#video-title-link": cards,
            "#replies #more-replies button": [self._element()],
        }
        page.query_selector_all = AsyncMock(side_effect=lambda sel: by_selector.get(sel, []))
        page.query_selector = AsyncMock(side_effect=lambda sel: self._element(**{"aria-pressed": "false"}))
        return page

    async def _warm(self, page, rng, db=None):
        mock_runner = AsyncMock()
        mock_runner.context = MagicMock(pages=[page])
        mock_runner._dismiss_consent = AsyncMock()
        self.mock_sim.click_locator = AsyncMock()
        warmer = AccountWarmer(self.browser_cfg, simulator=self.mock_sim, rng=rng)
        with patch("modules.account_warmer.YouTubeRunner", return_value=mock_runner):
            return await warmer.warm_up(self.account, duration_seconds=0.1, db=db)

    async def test_warm_up_discovers_a_video_on_the_home_feed_and_records_the_session(self) -> None:
        card = self._element(href="/watch?v=home_vid_1")
        page = self._page([card])
        mock_db = AsyncMock(spec=Database)

        res = await self._warm(page, random.Random(1), db=mock_db)

        self.assertTrue(res.success, res.details)
        self.assertEqual(res.video_url, "https://www.youtube.com/watch?v=home_vid_1")
        self.assertEqual(page.goto.await_args_list[0].args[0], "https://www.youtube.com/?hl=en")
        self.mock_sim.click_locator.assert_any_await(page, card)
        mock_db.record_account_warmup_success.assert_awaited_once_with(1)
        mock_db.increment_account_stats.assert_not_called()

    async def test_warm_up_without_video_cards_fails_instead_of_using_a_hardcoded_link(self) -> None:
        page = self._page([])
        mock_db = AsyncMock(spec=Database)

        res = await self._warm(page, random.Random(1), db=mock_db)

        self.assertFalse(res.success)
        self.assertIn("no video cards", res.details)
        mock_db.record_account_warmup_success.assert_not_called()

    async def test_player_events_fire_when_every_chance_hits(self) -> None:
        rng = MagicMock(spec=random.Random)
        rng.random.return_value = 0.0  # theater, pause, skip, replies and like all roll "yes"
        rng.uniform.side_effect = lambda a, b: a
        rng.randint.side_effect = lambda a, b: a
        rng.choice.side_effect = lambda seq: seq[0]
        page = self._page([self._element(href="/watch?v=home_vid_1")])

        res = await self._warm(page, rng)

        self.assertTrue(res.success, res.details)
        keys = [call.args[0] for call in page.keyboard.press.await_args_list]
        self.assertEqual(keys.count("t"), 1)
        self.assertEqual(keys.count("k"), 2)  # pause and unpause
        self.assertEqual(keys.count("ArrowRight"), 1)

    async def test_no_player_events_when_every_chance_misses(self) -> None:
        rng = MagicMock(spec=random.Random)
        rng.random.return_value = 0.99
        rng.uniform.side_effect = lambda a, b: a
        rng.randint.side_effect = lambda a, b: a
        rng.choice.side_effect = lambda seq: seq[0]
        page = self._page([self._element(href="/watch?v=home_vid_1")])

        res = await self._warm(page, rng)

        self.assertTrue(res.success, res.details)
        page.keyboard.press.assert_not_awaited()

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
