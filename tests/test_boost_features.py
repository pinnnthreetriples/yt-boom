import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from config import AppConfig, ProcessorSettings, load_config
from database import Database
from modules.collector import CollectedItem
from modules.interaction_simulator import InteractionConfig, InteractionSimulator
from modules.processor import DEFAULT_STYLES


class TestBoostFeatures(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "test_config.yaml"
        self.db_path = self.root / "test_boost.db"

        cfg = {
            "app": {
                "name": "boost-test",
                "db_path": str(self.db_path),
                "log_level": "INFO",
                "log_file": str(self.root / "test.log"),
            },
            "deepseek": {
                "api_key": "sk-dummy-key",
                "base_url": "https://api.deepseek.com",
            },
            "collector": {
                "endpoints": [
                    {
                        "name": "test-feed",
                        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    }
                ],
                "comments_per_video": 2,
            },
            "processor": {
                "system_prompt": "You are a friendly YouTube commenter.",
                "user_prompt_template": "Sub-Comment: {text}\nTone: {style}",
                "reply_mode": "ai",
                "custom_replies": [
                    "Great point, totally agree!",
                    "Thanks for the comment!",
                ],
            },
            "browser": {
                "headless": True,
                "session_root": str(self.root / "sessions"),
                "targets": [
                    {
                        "name": "youtube",
                        "base_url": "https://www.youtube.com",
                        "item_url_template": "https://www.youtube.com/watch?v={external_id}",
                        "submit_input_selector": "#reply-dialog #contenteditable-root",
                        "submit_button_selector": "#reply-dialog #submit-button button",
                        "confirmation_selector": "ytd-comment-view-model",
                    }
                ],
            },
            "limits": {
                "total_iterations": 2,
                "session_cooldown_seconds": 120,
                "video_cooldown_seconds": 180,
                "pause_min_seconds": 15,
                "pause_max_seconds": 45,
            },
            "validator": {
                "endpoint_template": "https://www.youtube.com/watch?v={remote_id}",
            },
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f)

    def tearDown(self):
        self.temp_dir.cleanup()

    # -------------------------------------------------------------
    # 1. Point 1: Keystroke Hold Time (60-100ms)
    # -------------------------------------------------------------
    def test_keystroke_hold_time_defaults_and_validation(self):
        cfg = InteractionConfig()
        self.assertEqual(cfg.key_hold_ms_min, 60)
        self.assertEqual(cfg.key_hold_ms_max, 100)

        # Valid custom hold time
        custom = InteractionConfig(key_hold_ms_min=70, key_hold_ms_max=90)
        self.assertEqual(custom.key_hold_ms_min, 70)
        self.assertEqual(custom.key_hold_ms_max, 90)

        # Invalid: min > max raises ValueError
        with self.assertRaises(ValueError):
            InteractionConfig(key_hold_ms_min=110, key_hold_ms_max=60)

    async def test_type_character_uses_down_up_with_hold_time(self):
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.keyboard = MagicMock()
        page.keyboard.down = AsyncMock()
        page.keyboard.up = AsyncMock()
        page.keyboard.type = AsyncMock()

        cfg = InteractionConfig(key_hold_ms_min=65, key_hold_ms_max=65)
        sim = InteractionSimulator(config=cfg)

        slept_intervals = []
        async def mock_sleep(sec):
            slept_intervals.append(sec)

        with patch("asyncio.sleep", side_effect=mock_sleep):
            await sim._press_character(page, "a")

        page.keyboard.down.assert_called_once_with("a")
        page.keyboard.up.assert_called_once_with("a")
        self.assertFalse(page.keyboard.type.called)
        # Hold time should be 65ms = 0.065s
        self.assertTrue(any(abs(interval - 0.065) < 1e-4 for interval in slept_intervals))

    # -------------------------------------------------------------
    # 2. Point 2: Cognitive Thinking Pause (1.0-2.5s)
    # -------------------------------------------------------------
    def test_thinking_pause_defaults_and_validation(self):
        cfg = InteractionConfig()
        self.assertEqual(cfg.thinking_pause_min, 1.0)
        self.assertEqual(cfg.thinking_pause_max, 2.5)

        with self.assertRaises(ValueError):
            InteractionConfig(thinking_pause_min=3.0, thinking_pause_max=1.5)

    async def test_thinking_pause_execution(self):
        cfg = InteractionConfig(thinking_pause_min=1.2, thinking_pause_max=1.2)
        sim = InteractionSimulator(config=cfg)

        slept = []
        with patch("asyncio.sleep", side_effect=lambda s: slept.append(s)):
            await sim.thinking_pause()

        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 1.2)

    # -------------------------------------------------------------
    # 3. Point 3: Safe Timing & Cooldown Defaults
    # -------------------------------------------------------------
    def test_safe_timing_and_cooldown_configuration(self):
        loaded = load_config(self.config_path)
        self.assertEqual(loaded.limits.video_cooldown_seconds, 180.0)
        self.assertEqual(loaded.collector.comments_per_video, 2)
        self.assertEqual(loaded.limits.pause_min_seconds, 15.0)
        self.assertEqual(loaded.limits.pause_max_seconds, 45.0)

    # -------------------------------------------------------------
    # 4. Point 4: Variety of AI Reply Styles
    # -------------------------------------------------------------
    def test_ai_reply_styles_list_and_defaults(self):
        self.assertTrue(len(DEFAULT_STYLES) >= 5)
        proc_cfg = ProcessorSettings(
            system_prompt="Test sys",
            user_prompt_template="Comment: {text}\nTone: {style}",
        )
        self.assertTrue(len(proc_cfg.styles) >= 5)
        self.assertNotIn("friendly agreement and encouragement", proc_cfg.styles)  # stock agreement invites cliches
        self.assertIn("thoughtful and insightful follow-up question", proc_cfg.styles)

    # -------------------------------------------------------------
    # 5. Critical User Requirement: Custom Reply Text
    # -------------------------------------------------------------
    def test_custom_reply_config_properties(self):
        loaded = load_config(self.config_path)
        self.assertEqual(loaded.processor.reply_mode, "ai")
        self.assertEqual(loaded.reply_mode, "ai")
        self.assertEqual(len(loaded.processor.custom_replies), 2)
        self.assertEqual(len(loaded.custom_replies), 2)

    async def test_run_reply_bot_custom_reply_mode(self):
        from youtube_bot import run_reply_bot

        # Sub-comment to answer
        sub = CollectedItem(
            endpoint="youtube:dQw4w9WgXcQ",
            external_id="vid_boost#p1#c1",
            text="Nice tutorial!",
            language="en",
            confidence=0.95,
            author="viewer_one",
        )

        with patch("modules.youtube_collector.YouTubeCollector.fetch_popular_comments", new_callable=AsyncMock) as mock_pop, \
             patch("modules.processor.DeepSeekProcessor.generate", new_callable=AsyncMock) as mock_llm_gen, \
             patch("modules.youtube_runner.YouTubeRunner.run", new_callable=AsyncMock) as mock_runner_run, \
             patch("asyncio.sleep", new_callable=AsyncMock):

            mock_pop.return_value = [sub]
            mock_runner_run.return_value = MagicMock(success=True, remote_id="custom_res_123", duration_seconds=1.2)

            await run_reply_bot(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                config_path=str(self.config_path),
                limit=1,
                target_mode="popular_parent",
                dry_run=False,
                reply_mode="custom",
                custom_reply="Custom thanks!",
            )

            # DeepSeek generate must NOT be called in custom mode!
            mock_llm_gen.assert_not_called()

            # Runner must have been called with formatted custom text
            mock_runner_run.assert_called_once()
            called_text = mock_runner_run.call_args[0][0]
            self.assertEqual(called_text, "Custom thanks!")

            # Database activity log must have recorded custom reply
            db = Database(self.db_path)
            await db.connect()
            try:
                logs = await db.list_activity_logs(page=1, page_size=1)
                self.assertEqual(len(logs), 1)
                self.assertEqual(logs[0].reply_text, "Custom thanks!")
                self.assertEqual(logs[0].status, "success")
            finally:
                await db.close()

    async def test_run_multi_video_pipeline_custom_reply_mode(self):
        from youtube_bot import run_multi_video_pipeline

        mock_vids = [{"video_id": "vid_multi_c", "title": "Custom Test", "view_count": 5000}]
        sub = CollectedItem(
            endpoint="youtube:vid_multi_c",
            external_id="vid_multi_c#c1",
            text="Great breakdown!",
            language="en",
            confidence=0.9,
            author="dev_fan",
        )

        with patch("modules.youtube_collector.YouTubeCollector.search_videos", new_callable=AsyncMock) as mock_search, \
             patch("modules.youtube_collector.YouTubeCollector.fetch_popular_comments", new_callable=AsyncMock) as mock_pop, \
             patch("modules.processor.DeepSeekProcessor.generate", new_callable=AsyncMock) as mock_llm_gen, \
             patch("modules.youtube_runner.YouTubeRunner.run", new_callable=AsyncMock) as mock_runner_run, \
             patch("asyncio.sleep", new_callable=AsyncMock):

            mock_search.return_value = mock_vids
            mock_pop.return_value = [sub]
            mock_runner_run.return_value = MagicMock(success=True, remote_id="yt_custom_777", duration_seconds=1.5)

            await run_multi_video_pipeline(
                "python",
                config_path=str(self.config_path),
                max_videos=1,
                comments_per_video=1,
                dry_run=False,
                reply_mode="custom",
                custom_reply="Always happy to help!",
            )

            mock_llm_gen.assert_not_called()
            mock_runner_run.assert_called_once()
            called_text = mock_runner_run.call_args[0][0]
            self.assertEqual(called_text, "Always happy to help!")

    def test_cli_argument_parsing_reply_mode_and_custom_reply(self):
        import sys
        from youtube_bot import main

        test_args = [
            "youtube_bot.py",
            "--video", "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "--reply-mode", "custom",
            "--custom-reply", "Hello from CLI!",
            "--dry-run",
            "--config", str(self.config_path),
        ]

        with patch.object(sys, "argv", test_args), \
             patch("youtube_bot.run_reply_bot", new_callable=AsyncMock) as mock_bot:
            main()
            mock_bot.assert_called_once()
            _, kwargs = mock_bot.call_args
            self.assertEqual(kwargs.get("reply_mode"), "custom")
            self.assertEqual(kwargs.get("custom_reply"), "Hello from CLI!")


    def test_resolve_reply_text_edge_cases(self):
        from modules.processor import ProcessorError, resolve_reply_text

        item = CollectedItem(
            endpoint="youtube:vid1",
            external_id="vid1#c1",
            text="Awesome guide!",
            language="en",
            confidence=0.9,
            author="sam_dev",
            parent_author="@lead_dev",
        )

        # 1. Author placeholders are not supported: the text goes out as written
        res1 = resolve_reply_text(item, custom_reply="Hello {author}!")
        self.assertEqual(res1, "Hello {author}!")

        # 2. Unmatched / extra braces e.g. {100%} or {xyz} should not prevent {text} expansion
        res2 = resolve_reply_text(item, custom_reply="Re: {text} {100%}")
        self.assertEqual(res2, "Re: Awesome guide! {100%}")

        # 4. Whitespace-only custom_replies list raises ProcessorError
        with self.assertRaises(ProcessorError):
            resolve_reply_text(item, custom_reply=None, custom_replies=["   ", ""])

    async def test_main_orchestrator_custom_reply_mode(self):
        from main import Orchestrator

        loaded = load_config(self.config_path)
        loaded.processor.reply_mode = "custom"
        loaded.processor.custom_replies = ["Custom orchestrator answer"]
        loaded.reply_mode = "custom"

        orch = Orchestrator(loaded)
        sub_item = CollectedItem(
            endpoint="youtube:vid_orch",
            external_id="vid_orch#c1",
            text="Need help with python",
            language="en",
            confidence=0.9,
            author="junior_dev",
        )

        with patch.object(orch._collector, "collect", new_callable=AsyncMock) as mock_col, \
             patch.object(orch._processor, "generate", new_callable=AsyncMock) as mock_gen, \
             patch.object(orch, "_submit_payload", new_callable=AsyncMock) as mock_sub:

            mock_col.return_value = [sub_item]
            mock_sub.return_value = True

            await orch._db.connect()
            await orch._db.init_schema()
            await orch._seed_nodes()
            try:
                res = await orch._run_iteration(1)
                self.assertTrue(res)
                # DeepSeek processor.generate must NOT be called in custom mode
                mock_gen.assert_not_called()
                # _submit_payload must be called with resolved custom text
                mock_sub.assert_called_once()
                payload_arg = mock_sub.call_args[0][2]
                self.assertEqual(payload_arg.text, "Custom orchestrator answer")
            finally:
                await orch._close()

    async def test_press_character_non_ascii_uses_type_directly(self):
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.keyboard = MagicMock()
        page.keyboard.down = AsyncMock()
        page.keyboard.up = AsyncMock()
        page.keyboard.type = AsyncMock()

        cfg = InteractionConfig(key_hold_ms_min=50, key_hold_ms_max=50)
        sim = InteractionSimulator(config=cfg)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            # Cyrillic character should directly call type and sleep, without calling down/up
            await sim._press_character(page, "п")

        page.keyboard.down.assert_not_called()
        page.keyboard.up.assert_not_called()
        page.keyboard.type.assert_called_once_with("п")

    def test_config_sync_bidirectional(self):
        # Even if reply_mode was default 'ai', setting processor.reply_mode='custom' syncs both
        cfg_dict = {
            "app": {"name": "test", "db_path": str(self.db_path)},
            "deepseek": {"api_key": "sk-dummy"},
            "collector": {"endpoints": [{"name": "yt", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}]},
            "processor": {
                "system_prompt": "sys",
                "user_prompt_template": "Sub: {text}",
                "reply_mode": "custom",
                "custom_replies": ["Hello"],
            },
            "browser": {"targets": [{"name": "youtube", "base_url": "https://www.youtube.com", "submit_input_selector": "i", "submit_button_selector": "b", "confirmation_selector": "c"}]},
            "limits": {},
            "validator": {"endpoint_template": "https://www.youtube.com/watch?v={remote_id}"},
            "reply_mode": "ai",  # root level was 'ai'
        }
        app_cfg = AppConfig.model_validate(cfg_dict)
        self.assertEqual(app_cfg.processor.reply_mode, "custom")
        self.assertEqual(app_cfg.reply_mode, "custom")
        self.assertEqual(app_cfg.custom_replies, ["Hello"])

    async def test_youtube_runner_uses_configured_thinking_pause(self):
        from modules.youtube_runner import YouTubeRunner
        from config import BrowserSettings, TargetSettings

        b_cfg = BrowserSettings(
            headless=True,
            session_root=self.root / "runner_test",
            targets=[TargetSettings(name="youtube", base_url="https://www.youtube.com", submit_input_selector="i", submit_button_selector="b", confirmation_selector="c")],
        )
        sim_mock = MagicMock(spec=InteractionSimulator)
        sim_mock.thinking_pause = AsyncMock()
        sim_mock.click_locator = AsyncMock()
        sim_mock.type_text = AsyncMock()
        sim_mock.human_pause = AsyncMock()

        runner = YouTubeRunner(b_cfg, b_cfg.targets[0], simulator=sim_mock)

        page = MagicMock()
        reply_btn = MagicMock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True), scroll_into_view_if_needed=AsyncMock(), bounding_box=AsyncMock(return_value={"x": 10, "y": 10, "width": 10, "height": 10}))
        reply_input = MagicMock(count=AsyncMock(return_value=1), is_visible=AsyncMock(return_value=True), scroll_into_view_if_needed=AsyncMock(), bounding_box=AsyncMock(return_value={"x": 10, "y": 10, "width": 10, "height": 10}))

        def loc(sel):
            mock = MagicMock()
            if "reply" in sel:
                mock.first = reply_btn
                mock.nth = MagicMock(return_value=reply_btn)
                mock.count = AsyncMock(return_value=1)
            elif "contenteditable" in sel:
                mock.first = reply_input
                mock.nth = MagicMock(return_value=reply_input)
                mock.count = AsyncMock(return_value=1)
            else:
                mock.first = MagicMock(count=AsyncMock(return_value=0), is_visible=AsyncMock(return_value=False))
                mock.count = AsyncMock(return_value=0)
            return mock

        page.locator = MagicMock(side_effect=loc)
        page.is_closed = MagicMock(return_value=False)

        await runner._submit_reply(page, "Some text", dry_run=True)

        # thinking_pause must have been called without overriding arguments (args=())
        sim_mock.thinking_pause.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
