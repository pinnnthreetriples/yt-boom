import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from config import BrowserSettings, TargetSettings
from modules.interaction_simulator import InteractionError, InteractionSimulator
from modules.ui_test_runner import SubmissionError, SubmissionResult
from modules.youtube_runner import YouTubeRunner


class TestYouTubeRunner(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.session_root = Path(self.temp_dir.name)
        self.browser_cfg = BrowserSettings(
            headless=True,
            session_root=self.session_root,
            render_min_seconds=0.1,
            render_max_seconds=0.2,
            targets=[
                TargetSettings(
                    name="youtube",
                    base_url="https://www.youtube.com",
                    item_url_template="https://www.youtube.com/watch?v={external_id}",
                    submit_input_selector="#reply-dialog #contenteditable-root",
                    submit_button_selector="#reply-dialog #submit-button button",
                    confirmation_selector="ytd-comment-view-model",
                )
            ],
        )
        fingerprint_patch = patch("modules.youtube_runner.get_or_create_fingerprint", return_value={"pinned": True})
        fingerprint_patch.start()  # _launch_options must not generate a real fingerprint (0.4 s, needs the fpgen data)
        self.addCleanup(fingerprint_patch.stop)
        self.target = self.browser_cfg.targets[0]
        self.runner = YouTubeRunner(self.browser_cfg, self.target)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_empty_text_raises_value_error(self):
        with self.assertRaises(ValueError):
            asyncio.run(self.runner.run(""))
        with self.assertRaises(ValueError):
            asyncio.run(self.runner.run("   \n\t "))

    def test_launch_options(self):
        opts = self.runner._launch_options()
        self.assertTrue(opts["persistent_context"])
        self.assertEqual(opts["os"], "windows")
        self.assertIn("user_data_dir", opts)
        self.assertTrue(opts["user_data_dir"].endswith("youtube"))

    async def test_navigate_youtube_url_formatting(self):
        page = MagicMock()
        page.goto = AsyncMock(return_value=MagicMock(status=200))
        page.is_closed = MagicMock(return_value=False)

        # Composite ID: video_id#parent_id#comment_id
        await self.runner._navigate_youtube(page, external_id="dQw4w9WgXcQ#p123#c456", query=None)
        page.goto.assert_called_with(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&lc=c456",
            wait_until="domcontentloaded",
            timeout=self.browser_cfg.navigation_timeout_ms,
        )

        # Plain ID
        await self.runner._navigate_youtube(page, external_id="dQw4w9WgXcQ", query=None)
        page.goto.assert_called_with(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            wait_until="domcontentloaded",
            timeout=self.browser_cfg.navigation_timeout_ms,
        )

    async def test_dismiss_consent_clicks_if_visible(self):
        page = MagicMock()
        btn_mock = MagicMock()
        btn_mock.count = AsyncMock(return_value=1)
        btn_mock.is_visible = AsyncMock(return_value=True)
        btn_mock.click = AsyncMock()

        locator_mock = MagicMock()
        locator_mock.first = btn_mock
        page.locator = MagicMock(return_value=locator_mock)

        with patch.object(self.runner._simulator, "click_locator", AsyncMock()) as click:
            await self.runner._dismiss_consent(page)
        click.assert_awaited_once_with(page, btn_mock)  # through the simulator, never locator.click()
        btn_mock.click.assert_not_called()

    async def test_dismiss_consent_survives_a_banner_without_a_clickable_box(self):
        page = MagicMock()
        btn_mock = MagicMock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True))
        page.locator = MagicMock(return_value=MagicMock(first=btn_mock))
        with patch.object(self.runner._simulator, "click_locator", AsyncMock(side_effect=InteractionError("no box"))) as click:
            await self.runner._dismiss_consent(page)  # must not raise
        click.assert_awaited()  # the click was attempted and its InteractionError swallowed

    async def test_key_hold_is_between_40_and_110_ms(self):
        for _ in range(50):
            self.assertTrue(40 <= self.runner._key_hold_ms() <= 110)

    async def test_linked_comment_is_scrolled_with_the_wheel_before_the_correction(self):
        page = MagicMock(evaluate=AsyncMock(return_value=800))
        locator = MagicMock(
            bounding_box=AsyncMock(return_value={"x": 0, "y": 2000, "width": 100, "height": 50}),
            scroll_into_view_if_needed=AsyncMock(),
        )
        with patch.object(self.runner._simulator, "scroll", AsyncMock()) as scroll:
            await self.runner._scroll_target_into_view(page, locator)
        scroll.assert_awaited_once_with(page, 2000 - 320)
        locator.scroll_into_view_if_needed.assert_awaited_once()

    async def test_a_comment_already_in_view_is_not_scrolled(self):
        page = MagicMock(evaluate=AsyncMock(return_value=800))
        locator = MagicMock(
            bounding_box=AsyncMock(return_value={"x": 0, "y": 100, "width": 100, "height": 50}),
            scroll_into_view_if_needed=AsyncMock(),
        )
        with patch.object(self.runner._simulator, "scroll", AsyncMock()) as scroll:
            await self.runner._scroll_target_into_view(page, locator)
        scroll.assert_not_awaited()

    async def test_submit_reply_dry_run_with_mocked_elements(self):
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)

        # Mock reply button
        reply_btn = MagicMock()
        reply_btn.count = AsyncMock(return_value=1)
        reply_btn.is_visible = AsyncMock(return_value=True)
        reply_btn.scroll_into_view_if_needed = AsyncMock()
        reply_btn.bounding_box = AsyncMock(return_value={"x": 100, "y": 200, "width": 50, "height": 30})

        # Mock reply input
        reply_input = MagicMock()
        reply_input.count = AsyncMock(return_value=1)
        reply_input.is_visible = AsyncMock(return_value=True)
        reply_input.scroll_into_view_if_needed = AsyncMock()
        reply_input.bounding_box = AsyncMock(return_value={"x": 100, "y": 250, "width": 200, "height": 40})

        def locator_side_effect(selector):
            mock = MagicMock()
            if "reply-button" in selector or "Reply" in selector or "Ответить" in selector:
                mock.first = reply_btn
                mock.nth = MagicMock(return_value=reply_btn)
                mock.count = AsyncMock(return_value=1)
            elif "contenteditable" in selector:
                mock.first = reply_input
                mock.nth = MagicMock(return_value=reply_input)
                mock.count = AsyncMock(return_value=1)
            else:
                empty = MagicMock()
                empty.count = AsyncMock(return_value=0)
                empty.is_visible = AsyncMock(return_value=False)
                mock.first = empty
                mock.count = AsyncMock(return_value=0)
            return mock

        page.locator = MagicMock(side_effect=locator_side_effect)
        page.mouse = MagicMock()
        page.mouse.move = AsyncMock()
        page.mouse.down = AsyncMock()
        page.mouse.up = AsyncMock()
        page.keyboard = MagicMock()
        page.keyboard.type = AsyncMock()
        page.keyboard.press = AsyncMock()

        remote_id = await self.runner._submit_reply(page, "Test reply text", comment_id="sub_999", dry_run=True)
        self.assertIsNotNone(remote_id)
        self.assertTrue(remote_id.startswith("dry_run_sub_reply_"))
        # Keyboard typing was invoked
        page.keyboard.type.assert_called()

    async def test_find_sub_comment_reply_button_targets_specific_comment_id(self):
        page = MagicMock()
        queried_selectors = []

        sub_btn = MagicMock()
        sub_btn.count = AsyncMock(return_value=1)
        sub_btn.is_visible = AsyncMock(return_value=True)

        def locator_func(selector):
            queried_selectors.append(selector)
            mock = MagicMock()
            if "lc=target_cid_123" in selector:
                mock.first = sub_btn
                mock.nth = MagicMock(return_value=sub_btn)
                mock.count = AsyncMock(return_value=1)
                mock.is_visible = AsyncMock(return_value=True)
            else:
                mock.first = MagicMock(count=AsyncMock(return_value=0), is_visible=AsyncMock(return_value=False))
                mock.count = AsyncMock(return_value=0)
            return mock

        page.locator = MagicMock(side_effect=locator_func)
        btn = await self.runner._find_sub_comment_reply_button(page, comment_id="target_cid_123")
        self.assertEqual(btn, sub_btn)
        # Ensure targeted selector with lc= was queried first
        self.assertTrue(any("lc=target_cid_123" in s for s in queried_selectors))

    async def test_first_visible_skips_hidden_duplicate_of_the_comment(self):
        """YouTube keeps a zero-size duplicate of the linked reply ahead of the real one: pick the visible one."""
        hidden = MagicMock(is_visible=AsyncMock(return_value=False))
        real = MagicMock(is_visible=AsyncMock(return_value=True), scroll_into_view_if_needed=AsyncMock())
        matches = MagicMock(count=AsyncMock(return_value=2), nth=MagicMock(side_effect=[hidden, real]))
        page = MagicMock(locator=MagicMock(return_value=matches))
        btn = await self.runner._find_sub_comment_reply_button(page, comment_id="dup_reply", timeout_seconds=1.0)
        self.assertIs(btn, real)

    async def test_known_target_never_falls_back_to_parent_reply_button(self):
        """Replies load lazily: until the target shows up, the parent's generic Reply button must not be used."""
        page = MagicMock()
        parent_btn = MagicMock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True))
        queried = []

        def locator_func(selector):
            queried.append(selector)
            mock = MagicMock()
            if "lc=" in selector:  # the target reply is not in the DOM yet
                mock.first = MagicMock(count=AsyncMock(return_value=0), is_visible=AsyncMock(return_value=False))
            else:  # any generic selector would find the parent comment's button
                mock.first = parent_btn
                mock.nth = MagicMock(return_value=parent_btn)
                mock.count = AsyncMock(return_value=1)
            return mock

        page.locator = MagicMock(side_effect=locator_func)
        btn = await self.runner._find_sub_comment_reply_button(page, comment_id="late_reply", timeout_seconds=0.5)
        self.assertIsNone(btn)
        self.assertTrue(all("lc=late_reply" in s for s in queried))

    async def test_expand_replies_does_not_click_reply_action_button(self):
        page = MagicMock()
        clicked_selectors = []

        def locator_func(selector):
            mock_btn = MagicMock()
            mock_btn.count = AsyncMock(return_value=1)
            mock_btn.is_visible = AsyncMock(return_value=True)
            mock_locator = MagicMock()
            mock_locator.count = AsyncMock(return_value=1)
            mock_locator.nth = MagicMock(return_value=mock_btn)
            return mock_locator

        page.locator = MagicMock(side_effect=locator_func)
        with patch.object(self.runner._simulator, "click_locator", new_callable=AsyncMock) as mock_click, \
             patch.object(self.runner._simulator, "human_pause", new_callable=AsyncMock):
            await self.runner._expand_replies(page)
            # Should have clicked the first valid expander
            mock_click.assert_called_once()
            called_sel = page.locator.call_args_list[0][0][0]
            # Must NOT be generic reply button
            self.assertNotEqual(called_sel, "button[aria-label*='reply' i]")
            self.assertNotEqual(called_sel, "button[aria-label*='ответ' i]")

    async def test_russian_keyboard_neighbors_and_typo_injection(self):
        from modules.interaction_simulator import _NEIGHBORS, InteractionConfig
        # Check all 33 Russian letters are mapped in _NEIGHBORS
        russian_alphabet = "йцукенгшщзхъфывапролджэячсмитьбюё"
        for ch in russian_alphabet:
            self.assertIn(ch, _NEIGHBORS, f"Russian character {ch!r} must be in _NEIGHBORS")
            self.assertTrue(len(_NEIGHBORS[ch]) >= 1)

        # Test typo simulation with Russian text
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.keyboard = MagicMock()
        page.keyboard.type = AsyncMock()
        page.keyboard.press = AsyncMock()

        # Config with typo_probability = 0.5 (maximum allowed by schema)
        cfg = InteractionConfig(
            typo_probability=0.5,
            key_delay_ms_min=5,
            key_delay_ms_max=5,
            backspace_delay_ms_min=10,
            backspace_delay_ms_max=10,
        )
        mock_rng = MagicMock()
        mock_rng.random.return_value = 0.0  # guaranteed to trigger typo
        mock_rng.choice.side_effect = lambda choices: choices[0]
        mock_rng.uniform.return_value = 5.0
        mock_rng.lognormvariate.return_value = 5.0

        sim = InteractionSimulator(config=cfg, rng=mock_rng)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await sim.type_text(page, "Привет")
        # Should have pressed Backspace for injected typos
        self.assertTrue(page.keyboard.press.called)
        backspace_calls = [c for c in page.keyboard.press.call_args_list if c[0][0] == "Backspace"]
        self.assertTrue(len(backspace_calls) > 0)

    @staticmethod
    def _click_page_and_locator(hits):
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.mouse = MagicMock(move=AsyncMock(), down=AsyncMock(), up=AsyncMock())
        locator = MagicMock(
            scroll_into_view_if_needed=AsyncMock(),
            bounding_box=AsyncMock(return_value={"x": 100, "y": 200, "width": 60, "height": 30}),
            evaluate=AsyncMock(side_effect=hits),
        )
        return page, locator

    async def test_verified_click_retargets_when_neighbour_is_under_cursor(self):
        """The first aim lands on 'Cancel' (layout shifted): no press, aim again, press only on a real hit."""
        page, locator = self._click_page_and_locator([False, True])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await InteractionSimulator().click_locator(page, locator, verify_hit=True)
        self.assertEqual(locator.evaluate.await_count, 2)
        page.mouse.down.assert_awaited_once()
        page.mouse.up.assert_awaited_once()

    async def test_verified_click_never_presses_when_target_keeps_moving(self):
        from modules.interaction_simulator import InteractionError

        page, locator = self._click_page_and_locator([False, False, False])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaises(InteractionError):
                await InteractionSimulator().click_locator(page, locator, verify_hit=True)
        page.mouse.down.assert_not_awaited()

    async def test_submit_searches_inside_reply_dialog_and_requires_create_request(self):
        """Submit button comes from the dialog of the typed text, and a click without a request is a failure."""
        submit = MagicMock(is_visible=AsyncMock(return_value=True))
        dialog = MagicMock()
        dialog.locator = MagicMock(
            return_value=MagicMock(count=AsyncMock(return_value=1), nth=MagicMock(return_value=submit))
        )
        reply_input = MagicMock(focus=AsyncMock(), locator=MagicMock(return_value=dialog))
        page = MagicMock()

        class _NoRequest:
            async def __aenter__(self):
                return MagicMock(value=self._never())

            async def __aexit__(self, *exc):
                return False

            @staticmethod
            async def _never():
                from playwright.async_api import Error as PlaywrightError

                raise PlaywrightError("Timeout 15000ms exceeded")

        page.expect_response = MagicMock(return_value=_NoRequest())
        sim = self.runner._simulator
        with patch.object(self.runner, "_find_sub_comment_reply_button", AsyncMock(return_value=MagicMock())), \
             patch.object(self.runner, "_first_visible", AsyncMock(side_effect=[reply_input, submit])), \
             patch.object(sim, "click_locator", new_callable=AsyncMock) as mock_click, \
             patch.object(sim, "human_pause", new_callable=AsyncMock), \
             patch.object(sim, "thinking_pause", new_callable=AsyncMock), \
             patch.object(sim, "type_text", new_callable=AsyncMock):
            with self.assertRaises(SubmissionError):
                await self.runner._submit_reply(page, "text", comment_id="c1")
        self.assertEqual(mock_click.await_args_list[-1].kwargs, {"verify_hit": True})
        self.assertIs(mock_click.await_args_list[-1].args[1], submit)


class TestPlayback(unittest.IsolatedAsyncioTestCase):
    """_start_playback: a real, muted, verified click on the video so Firefox's autoplay block does not leave it paused."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        fingerprint_patch = patch("modules.youtube_runner.get_or_create_fingerprint", return_value={"pinned": True})
        fingerprint_patch.start()
        self.addCleanup(fingerprint_patch.stop)
        self.calls = []

    def _runner(self, **settings):
        cfg = BrowserSettings(
            headless=True,
            session_root=Path(self.temp_dir.name),
            render_min_seconds=0.1,
            render_max_seconds=0.2,
            targets=[TargetSettings(name="youtube", base_url="https://www.youtube.com", item_url_template="https://www.youtube.com/watch?v={external_id}",
                                    submit_input_selector="#i", submit_button_selector="#b", confirmation_selector="#c")],
            **settings,
        )
        runner = YouTubeRunner(cfg, cfg.targets[0])
        runner._simulator = MagicMock()
        runner._simulator.click_locator = AsyncMock(side_effect=lambda *a, **k: self.calls.append("click"))
        runner._simulator.human_pause = AsyncMock()
        return runner

    def _page(self, *states, mute_result=True):
        """A page whose player state reads come from `states` in order (the last one repeats); mute and key calls are recorded."""
        states = list(states)

        async def evaluate(script, *args):
            if "mute()" in script:
                self.calls.append("mute")
                return mute_result
            return states.pop(0) if len(states) > 1 else states[0]

        page = MagicMock()
        page.evaluate = AsyncMock(side_effect=evaluate)
        page.locator = MagicMock(return_value=MagicMock(first="video-locator"))
        page.keyboard.press = AsyncMock(side_effect=lambda key, **k: self.calls.append(f"key {key}"))
        return page

    PAUSED = {"video": True, "paused": True, "ended": False, "ad": False, "muted": False, "shown": True}

    async def test_paused_video_is_muted_then_clicked_with_a_verified_click(self):
        runner, page = self._runner(), self._page(self.PAUSED, {**self.PAUSED, "paused": False, "muted": True})
        await runner._start_playback(page)
        self.assertEqual(self.calls, ["mute", "click"])  # muted BEFORE the click: no audible start
        page.locator.assert_called_with("#movie_player video")
        args, kwargs = runner._simulator.click_locator.await_args
        self.assertEqual((args[1], kwargs.get("verify_hit")), ("video-locator", True))

    async def test_a_video_that_never_started_is_started_through_its_big_play_button(self):
        runner = self._runner()
        page = self._page({**self.PAUSED, "bigPlay": True}, {**self.PAUSED, "paused": False, "muted": True})
        await runner._start_playback(page)
        page.locator.assert_called_with("#movie_player .ytp-large-play-button")
        self.assertEqual(self.calls, ["mute", "click"])

    async def test_nothing_is_clicked_when_playing_ended_ad_or_no_player(self):
        for state in ({**self.PAUSED, "paused": False}, {**self.PAUSED, "ended": True}, {**self.PAUSED, "ad": True}, {**self.PAUSED, "video": False}, None):
            self.calls.clear()
            runner = self._runner()
            await runner._start_playback(self._page(state))
            self.assertEqual(self.calls, [], msg=str(state))

    async def test_player_that_never_settles_is_not_clicked(self):
        runner = self._runner()
        await runner._start_playback(self._page({**self.PAUSED, "shown": False}))  # video off-screen, no big play button
        self.assertEqual(self.calls, [])

    async def test_flag_off_touches_nothing(self):
        runner, page = self._runner(render_play_video=False), self._page(self.PAUSED)
        await runner._start_playback(page)
        page.evaluate.assert_not_awaited()
        self.assertEqual(self.calls, [])

    async def test_player_that_appears_late_is_waited_for_briefly(self):
        runner = self._runner()
        page = self._page({**self.PAUSED, "video": False}, {**self.PAUSED, "shown": False}, self.PAUSED, {**self.PAUSED, "paused": False, "muted": True})
        await runner._start_playback(page)
        self.assertEqual(self.calls, ["mute", "click"])

    async def test_m_key_is_the_fallback_only_when_the_player_is_still_unmuted(self):
        runner = self._runner()
        await runner._start_playback(self._page(self.PAUSED, {**self.PAUSED, "paused": False, "muted": False}, mute_result=False))
        self.assertEqual(self.calls, ["mute", "click", "key m"])
        self.calls.clear()
        await runner._start_playback(self._page(self.PAUSED, {**self.PAUSED, "paused": False, "muted": True}))
        self.assertEqual(self.calls, ["mute", "click"])

    async def test_failures_are_swallowed(self):
        from modules.interaction_simulator import InteractionError

        runner = self._runner()
        runner._simulator.click_locator = AsyncMock(side_effect=InteractionError("layout kept shifting"))
        await runner._start_playback(self._page(self.PAUSED))
        page = MagicMock(evaluate=AsyncMock(side_effect=RuntimeError("page closed")))
        await runner._start_playback(page)

    async def test_cancellation_is_not_swallowed(self):
        runner = self._runner()
        runner._simulator.click_locator = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await runner._start_playback(self._page(self.PAUSED))

    async def test_render_starts_playback_before_the_idle_wait(self):
        runner = self._runner()
        runner._simulator.idle = AsyncMock(side_effect=lambda *a, **k: self.calls.append("idle"))
        runner._simulator.scroll = AsyncMock()
        with patch.object(runner, "_start_playback", AsyncMock(side_effect=lambda page: self.calls.append("play"))):
            await runner._render(MagicMock())
        self.assertEqual(self.calls[:2], ["play", "idle"])


if __name__ == "__main__":
    unittest.main()
