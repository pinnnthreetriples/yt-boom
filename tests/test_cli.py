import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import yaml
from types import SimpleNamespace

from config import load_config
from database import Database
from youtube_bot import EXTRA_CANDIDATES, format_author, list_sub_comments, print_status, run_reply_bot


class TestYouTubeBotCLI(unittest.IsolatedAsyncioTestCase):
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
        cfg_data["limits"]["visibility_check_enabled"] = False  # no anonymous look-up (45 s wait, network) in unit tests
        cfg_data["reply_mode"] = "ai"
        cfg_data["processor"]["reply_mode"] = "ai"
        cfg_data["custom_replies"] = []
        cfg_data["processor"]["custom_replies"] = []
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg_data, f)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    def test_format_author(self):
        self.assertEqual(format_author("@user123"), "@user123")
        self.assertEqual(format_author("user456"), "@user456")
        self.assertEqual(format_author(None), "@unknown")
        self.assertEqual(format_author(""), "@unknown")

    async def test_print_status(self):
        # Should execute without errors on valid isolated config, even with emojis
        db = Database(self.db_path)
        await db.connect()
        await db.init_schema()
        node = await db.upsert_test_node("youtube", "https://www.youtube.com")
        p = await db.create_payload(
            node_id=node.id,
            text="Comment with emoji \U0001f604 and \U0001f44c",
            language="en",
            source_ref="vid#c1#sub1",
        )
        await db.mark_payload_submitted(p.id, "sub_123")
        await db.close()

        with patch("builtins.print") as mock_print:
            await print_status(str(self.config_path))
            self.assertTrue(mock_print.called)

    async def test_list_sub_comments_mocked(self):
        with patch("modules.youtube_collector.YouTubeCollector.fetch_video_sub_comments", new_callable=AsyncMock) as mock_fetch:
            from modules.youtube_collector import YouTubeSubComment
            mock_fetch.return_value = [
                YouTubeSubComment(
                    video_id="dQw4w9WgXcQ",
                    comment_id="c1.s1",
                    parent_comment_id="c1",
                    author="@tester",
                    text="Hello under comment with \U0001f44c",
                    parent_author="@original",
                    parent_text="Top comment",
                    permalink="https://www.youtube.com/watch?v=dQw4w9WgXcQ&lc=c1.s1",
                )
            ]
            with patch("builtins.print") as mock_print:
                await list_sub_comments("https://www.youtube.com/watch?v=dQw4w9WgXcQ", str(self.config_path), limit=1)
                self.assertTrue(mock_print.called)

    async def test_run_reply_bot_dry_run_does_not_poison_database(self):
        unique_id = "test_vid_abc#c1#c1.s1"
        with patch("modules.youtube_collector.YouTubeCollector.collect_from_videos", new_callable=AsyncMock) as mock_col, \
             patch("modules.processor.DeepSeekProcessor.generate", new_callable=AsyncMock) as mock_gen, \
             patch("modules.youtube_runner.YouTubeRunner.run", new_callable=AsyncMock) as mock_run:

            from modules.collector import CollectedItem
            from modules.ui_test_runner import SubmissionResult

            mock_col.return_value = [
                CollectedItem(
                    endpoint="youtube:test",
                    external_id=unique_id,
                    text="Sub-comment to reply to",
                    language="en",
                    confidence=0.99,
                    parent_id="c1",
                    parent_text="Parent context",
                    parent_author="@parent_user",
                    author="@sub_user",
                    is_reply=True,
                )
            ]
            mock_gen.return_value = "Great point, I totally agree \U0001f604"
            mock_run.return_value = SubmissionResult(
                success=True,
                remote_id="dry_run_sub_123",
                duration_seconds=1.5,
            )

            await run_reply_bot(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                config_path=str(self.config_path),
                target_mode="raw",  # the target of a run without a project comes from «Основной»: name the mode
                limit=1,
                dry_run=True,
            )
            mock_col.assert_called_once()
            mock_gen.assert_called_once()
            mock_run.assert_called_once()

            # Verify that dry run did NOT record a 'submitted' status in database
            db = Database(self.db_path)
            await db.connect()
            try:
                has_replied = await db.has_replied_to(unique_id)
                self.assertFalse(has_replied, "Dry run must NOT mark sub-comment as replied in database")
            finally:
                await db.close()

    async def test_list_sub_comments_collector_error(self):
        from modules.collector import CollectorError
        with patch("modules.youtube_collector.YouTubeCollector.fetch_video_sub_comments", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.side_effect = CollectorError("invalid YouTube video URL or ID: 'bad_url'")
            # Must catch CollectorError gracefully without raising
            await list_sub_comments("bad_url", str(self.config_path), limit=1)

    async def test_run_reply_bot_live_atomic_reservation_and_submit(self):
        unique_id = "test_vid_xyz#c2#c2.s1"
        with patch("modules.youtube_collector.YouTubeCollector.collect_from_videos", new_callable=AsyncMock) as mock_col, \
             patch("modules.processor.DeepSeekProcessor.generate", new_callable=AsyncMock) as mock_gen, \
             patch("modules.youtube_runner.YouTubeRunner.run", new_callable=AsyncMock) as mock_run:

            from modules.collector import CollectedItem
            from modules.ui_test_runner import SubmissionResult

            mock_col.return_value = [
                CollectedItem(
                    endpoint="youtube:test",
                    external_id=unique_id,
                    text="Sub-comment to reply to live",
                    language="en",
                    confidence=0.99,
                    parent_id="c2",
                    parent_text="Parent context",
                    parent_author="@parent_user",
                    author="@sub_user",
                    is_reply=True,
                )
            ]
            mock_gen.return_value = "Excellent insight!"
            mock_run.return_value = SubmissionResult(
                success=True,
                remote_id="live_reply_777",
                duration_seconds=1.2,
            )

            await run_reply_bot(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                config_path=str(self.config_path),
                target_mode="raw",  # the target of a run without a project comes from «Основной»: name the mode
                limit=1,
                dry_run=False,
            )

            db = Database(self.db_path)
            await db.connect()
            try:
                self.assertTrue(await db.has_replied_to(unique_id))
                payload = await db.get_payload_by_source_ref(unique_id)
                self.assertIsNotNone(payload)
                self.assertEqual(payload.status, "submitted")
                self.assertEqual(payload.text, "Excellent insight!")
                self.assertEqual(payload.remote_id, "live_reply_777")
            finally:
                await db.close()

    async def test_run_reply_bot_live_llm_failure_marks_payload_failed(self):
        unique_id = "test_vid_fail#c3#c3.s1"
        with patch("modules.youtube_collector.YouTubeCollector.collect_from_videos", new_callable=AsyncMock) as mock_col, \
             patch("modules.processor.DeepSeekProcessor.generate", new_callable=AsyncMock) as mock_gen:

            from modules.collector import CollectedItem

            mock_col.return_value = [
                CollectedItem(
                    endpoint="youtube:test",
                    external_id=unique_id,
                    text="Sub-comment that fails LLM",
                    language="en",
                    confidence=0.99,
                    parent_id="c3",
                    parent_text="Parent context",
                    parent_author="@parent_user",
                    author="@sub_user",
                    is_reply=True,
                )
            ]
            mock_gen.side_effect = RuntimeError("DeepSeek rate limited: 429")

            await run_reply_bot(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                config_path=str(self.config_path),
                target_mode="raw",  # the target of a run without a project comes from «Основной»: name the mode
                limit=1,
                dry_run=False,
            )

            db = Database(self.db_path)
            await db.connect()
            try:
                # Payload marked failed should not block retry (has_replied_to == False)
                self.assertFalse(await db.has_replied_to(unique_id))
                payload = await db.get_payload_by_source_ref(unique_id)
                self.assertIsNotNone(payload)
                self.assertEqual(payload.status, "failed")
                self.assertIn("LLM generation failed", payload.error or "")
            finally:
                await db.close()

    async def test_run_reply_bot_live_runner_crash_marks_payload_failed(self):
        unique_id = "test_vid_runner_fail#c4#c4.s1"
        with patch("modules.youtube_collector.YouTubeCollector.collect_from_videos", new_callable=AsyncMock) as mock_col, \
             patch("modules.processor.DeepSeekProcessor.generate", new_callable=AsyncMock) as mock_gen, \
             patch("modules.youtube_runner.YouTubeRunner.run", new_callable=AsyncMock) as mock_run:

            from modules.collector import CollectedItem

            mock_col.return_value = [
                CollectedItem(
                    endpoint="youtube:test",
                    external_id=unique_id,
                    text="Sub-comment where browser crashes",
                    language="en",
                    confidence=0.99,
                    parent_id="c4",
                    parent_text="Parent context",
                    parent_author="@parent_user",
                    author="@sub_user",
                    is_reply=True,
                )
            ]
            mock_gen.return_value = "Generated text"
            mock_run.side_effect = RuntimeError("Camoufox browser process died unexpectedly")

            await run_reply_bot(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                config_path=str(self.config_path),
                target_mode="raw",  # the target of a run without a project comes from «Основной»: name the mode
                limit=1,
                dry_run=False,
            )

            db = Database(self.db_path)
            await db.connect()
            try:
                # Payload marked failed should not block retry (has_replied_to == False)
                self.assertFalse(await db.has_replied_to(unique_id))
                payload = await db.get_payload_by_source_ref(unique_id)
                self.assertIsNotNone(payload)
                self.assertEqual(payload.status, "failed")
                self.assertIn("Execution failed", payload.error or "")
            finally:
                await db.close()

    async def test_run_reply_bot_sub_comment_uses_fetch_popular_comments(self):
        from modules.collector import CollectedItem
        from modules.ui_test_runner import SubmissionResult

        unique_id = "test_sub_mode#p1#s1"
        with patch("modules.youtube_collector.YouTubeCollector.fetch_popular_comments", new_callable=AsyncMock) as mock_pop, \
             patch("modules.processor.DeepSeekProcessor.generate", new_callable=AsyncMock) as mock_gen, \
             patch("modules.youtube_runner.YouTubeRunner.run", new_callable=AsyncMock) as mock_run:

            mock_pop.return_value = [
                CollectedItem(
                    endpoint="youtube:test",
                    external_id=unique_id,
                    text="Popular sub comment",
                    language="en",
                    confidence=0.95,
                    like_count=350,
                    parent_id="p1",
                    author="@sub_user",
                    is_reply=True,
                )
            ]
            mock_gen.return_value = "Great sub-comment reply"
            mock_run.return_value = SubmissionResult(
                success=True,
                remote_id="sub_resp_999",
                duration_seconds=1.2,
            )

            await run_reply_bot(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                config_path=str(self.config_path),
                limit=1,
                target_mode="sub_comment",
                dry_run=True,
            )

            mock_pop.assert_called_once_with(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                limit=2 + EXTRA_CANDIDATES,  # two per reply wanted, plus spares for targets without a Reply button
                target_mode="sub_comment",
            )
            mock_gen.assert_called_once()
            mock_run.assert_called_once()

    def test_cli_argument_parsing_search_and_history(self):
        from youtube_bot import main as bot_main
        import sys

        # Test --search multi-video dispatch
        test_args = [
            "youtube_bot.py",
            "--search", "python automated testing",
            "--min-views", "5000",
            "--max-views", "100000",
            "--max-videos", "3",
            "--comments-per-video", "4",
            "--target-mode", "mixed",
            "--max-age-hours", "48",
            "--config", str(self.config_path),
            "--dry-run",
        ]
        run_defaults = dict(headless=None, concurrency=None, account_mode=None, reply_mode=None, custom_reply=None)
        with patch.object(sys, "argv", test_args), \
             patch("youtube_bot.run_search_cli", new_callable=AsyncMock) as mock_multi:
            bot_main()
            mock_multi.assert_called_once_with(
                "python automated testing",
                str(self.config_path),
                min_views=5000,
                max_views=100000,
                max_age_hours=48,
                max_videos=3,
                comments_per_video=4,
                target_mode="mixed",
                dry_run=True,
                **run_defaults,
            )

        # Test --history / --log flag
        with patch.object(sys, "argv", ["youtube_bot.py", "--history", "--config", str(self.config_path)]), \
             patch("youtube_bot.print_activity_history", new_callable=AsyncMock) as mock_hist:
            bot_main()
            mock_hist.assert_called_once_with(str(self.config_path))

        # Test config-driven search query dispatch when --search is omitted
        with open(self.config_path, "r", encoding="utf-8") as f:
            cfg_mod = yaml.safe_load(f)
        cfg_mod["collector"]["search_query"] = "topic from config"
        cfg_mod["collector"]["target_mode"] = "sub_comment"
        cfg_mod["collector"]["comments_per_video"] = 7
        cfg_mod["collector"]["min_views"] = 0
        cfg_mod["collector"]["max_views"] = None
        cfg_mod["collector"]["max_age_hours"] = None
        cfg_mod["collector"]["max_videos"] = 5
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg_mod, f)

        # Without --search / --video / --account a run goes through the projects «в запуске» (the topic of
        # config.yaml lives in the project «Основной» since the migration); the global search is not used.
        with patch.object(sys, "argv", ["youtube_bot.py", "--config", str(self.config_path), "--dry-run"]), \
             patch("youtube_bot.run_search_cli", new_callable=AsyncMock) as mock_cfg_multi, \
             patch("youtube_bot.run_projects_cli", new_callable=AsyncMock) as mock_projects:
            mock_projects.return_value = SimpleNamespace(stopped_at=None)
            bot_main()
            mock_cfg_multi.assert_not_called()
            mock_projects.assert_called_once_with(
                str(self.config_path), None, dry_run=True, headless=None, concurrency=None, account_mode=None,
                topics=None,
            )

        # Test --headless flag passes headless=True
        with patch.object(sys, "argv", ["youtube_bot.py", "--headless", "--video", "https://youtube.com/watch?v=xyz", "--config", str(self.config_path), "--dry-run"]), \
             patch("youtube_bot.run_reply_bot", new_callable=AsyncMock) as mock_reply:
            bot_main()
            self.assertTrue(mock_reply.call_args[1].get("headless") is True)

    def search_call(self, *flags: str, **collector):
        import sys
        from youtube_bot import main as bot_main

        with open(self.config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["collector"].update(collector)
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f)
        argv = ["youtube_bot.py", "--search", "iphone 17", "--config", str(self.config_path), "--dry-run", *flags]
        with patch.object(sys, "argv", argv), patch("youtube_bot.run_search_cli", new_callable=AsyncMock) as run:
            bot_main()
        run.assert_called_once()
        return run.call_args.kwargs

    def test_search_takes_the_filters_from_config_without_flags(self):
        kwargs = self.search_call(min_views=10000, max_views=500000, max_age_hours=24, max_videos=8,
                                  comments_per_video=3, target_mode="first_reply")
        self.assertEqual(
            {k: kwargs[k] for k in ("min_views", "max_views", "max_age_hours", "max_videos", "comments_per_video", "target_mode")},
            {"min_views": 10000, "max_views": 500000, "max_age_hours": 24, "max_videos": 8,
             "comments_per_video": 3, "target_mode": "first_reply"},
        )

    def test_search_zero_turns_the_view_cap_and_freshness_off(self):
        kwargs = self.search_call("--max-views", "0", "--max-age-hours", "0", "--target-mode", "first_reply",
                                  "--concurrency", "3", "--account-mode", "share",
                                  max_views=500000, max_age_hours=24)
        self.assertIsNone(kwargs["max_views"])
        self.assertIsNone(kwargs["max_age_hours"])
        self.assertEqual((kwargs["target_mode"], kwargs["concurrency"], kwargs["account_mode"]), ("first_reply", 3, "share"))

    def test_search_with_account_keeps_the_single_account_pipeline(self):
        import sys
        from youtube_bot import main as bot_main

        argv = ["youtube_bot.py", "--search", "iphone 17", "--account", "solo", "--max-age-hours", "0",
                "--config", str(self.config_path), "--dry-run"]
        with patch.object(sys, "argv", argv), \
             patch("youtube_bot.run_multi_video_pipeline", new_callable=AsyncMock) as single, \
             patch("youtube_bot.run_search_cli", new_callable=AsyncMock) as through_accounts:
            bot_main()
        through_accounts.assert_not_called()
        self.assertEqual(single.call_args.kwargs["account_name"], "solo")
        self.assertEqual(single.call_args.kwargs["max_age_hours"], 0)  # 0 = any age in the pipeline

    async def test_run_search_cli_hands_the_run_to_the_orchestrator(self):
        from modules.multi_account_orchestrator import SearchRunResult
        from youtube_bot import run_search_cli

        with patch("modules.multi_account_orchestrator.MultiAccountOrchestrator.run_search",
                   new_callable=AsyncMock, return_value=SearchRunResult(1, 2, 3)) as run_search:
            result = await run_search_cli("topic", str(self.config_path), min_views=5, max_views=None,
                                          max_age_hours=12, max_videos=4, comments_per_video=2, dry_run=True)
        self.assertEqual(tuple(result), (1, 2, 3))
        run_search.assert_awaited_once_with("topic", min_views=5, max_views=None, max_age_hours=12,
                                            max_videos=4, comments_per_video=2)

    async def test_print_activity_history(self):
        from database import ActivityLogCreate
        from youtube_bot import print_activity_history
        import io
        from contextlib import redirect_stdout

        db = Database(self.db_path)
        await db.connect()
        await db.init_schema()

        # 1. Test empty database message
        buf = io.StringIO()
        with redirect_stdout(buf):
            await print_activity_history(str(self.config_path))
        output_empty = buf.getvalue()
        self.assertIn("Журнал отработок пуст", output_empty)

        # 2. Seed records and test formatted output
        await db.log_activity(
            ActivityLogCreate(
                video_id="vid_seed_1",
                video_title="Seed Video 1",
                video_url="https://youtube.com/watch?v=vid_seed_1",
                target_id="cid_1",
                target_author="@author1",
                target_text="First target comment",
                reply_text="Hello from bot 1",
                mode="live",
                status="success",
                duration_sec=2.5,
            )
        )
        await db.log_activity(
            ActivityLogCreate(
                video_id="vid_seed_2",
                video_title="Seed Video 2",
                video_url="https://youtube.com/watch?v=vid_seed_2",
                target_id="cid_2",
                target_author="@author2",
                target_text="Second target comment",
                reply_text="Simulated reply 2",
                mode="dry_run",
                status="simulated",
                duration_sec=1.1,
            )
        )
        await db.close()

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            await print_activity_history(str(self.config_path))
        output_seeded = buf2.getvalue()
        self.assertIn("ЖУРНАЛ ОТРАБОТОК", output_seeded)
        self.assertIn("@author1", output_seeded)
        self.assertIn("@author2", output_seeded)
        self.assertIn("success", output_seeded)
        self.assertIn("simulated", output_seeded)

    async def test_run_multi_video_pipeline(self):
        from youtube_bot import run_multi_video_pipeline
        from modules.collector import CollectedItem
        from modules.ui_test_runner import SubmissionResult

        mock_search_results = [
            {
                "video_id": "vid_multi_1",
                "title": "Tutorial Video 1",
                "url": "https://www.youtube.com/watch?v=vid_multi_1",
                "view_count": 15000,
                "channel": "Code Channel",
            },
            {
                "video_id": "vid_multi_2",
                "title": "Tutorial Video 2",
                "url": "https://www.youtube.com/watch?v=vid_multi_2",
                "view_count": 30000,
                "channel": "Tech Channel",
            },
        ]

        items_v1 = [
            CollectedItem(
                endpoint="youtube:vid_multi_1",
                external_id="vid_multi_1#c1",
                text="Top comment on video 1",
                language="en",
                confidence=0.98,
                author="@dev_user_1",
                like_count=500,
                video_id="vid_multi_1",
                is_reply=False,
            )
        ]
        items_v2 = [
            CollectedItem(
                endpoint="youtube:vid_multi_2",
                external_id="vid_multi_2#c2",
                text="Top comment on video 2",
                language="en",
                confidence=0.99,
                author="@dev_user_2",
                like_count=1200,
                video_id="vid_multi_2",
                is_reply=False,
            )
        ]

        with patch("modules.youtube_collector.YouTubeCollector.search_videos", new_callable=AsyncMock) as mock_search, \
             patch("modules.youtube_collector.YouTubeCollector.fetch_popular_comments", new_callable=AsyncMock) as mock_pop, \
             patch("modules.processor.DeepSeekProcessor.generate", new_callable=AsyncMock) as mock_gen, \
             patch("modules.youtube_runner.YouTubeRunner.run", new_callable=AsyncMock) as mock_run, \
             patch("asyncio.sleep", new_callable=AsyncMock):

            mock_search.return_value = mock_search_results
            mock_pop.side_effect = [items_v1, items_v2]
            mock_gen.side_effect = ["AI Reply to video 1", "AI Reply to video 2"]
            mock_run.return_value = SubmissionResult(
                success=True,
                remote_id="yt_resp_123",
                duration_seconds=1.8,
            )

            await run_multi_video_pipeline(
                "python async",
                config_path=str(self.config_path),
                min_views=1000,
                max_views=50000,
                max_videos=2,
                comments_per_video=1,
                target_mode="popular_parent",
                dry_run=False,
            )

            mock_search.assert_called_once_with(
                query="python async",
                min_views=1000,
                max_views=50000,
                limit=2,
                max_age_hours=None,
            )
            self.assertEqual(mock_pop.call_count, 2)
            self.assertEqual(mock_gen.call_count, 2)
            self.assertEqual(mock_run.call_count, 2)

            # Verify activity log entries and database deduplication
            db = Database(self.db_path)
            await db.connect()
            try:
                self.assertTrue(await db.has_replied_to("vid_multi_1#c1"))
                self.assertTrue(await db.has_replied_to("vid_multi_2#c2"))

                logs = await db.list_activity_logs(page=1, page_size=10)
                self.assertEqual(len(logs), 2)
                self.assertEqual(logs[0].status, "success")
                self.assertEqual(logs[0].mode, "live")
                self.assertIn("AI Reply to video", logs[0].reply_text)
            finally:
                await db.close()

    async def test_run_multi_video_pipeline_empty_search(self):
        from youtube_bot import run_multi_video_pipeline

        with patch("modules.youtube_collector.YouTubeCollector.search_videos", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = []
            # Should exit gracefully without running processor or runner
            await run_multi_video_pipeline(
                "nonexistent topic",
                config_path=str(self.config_path),
                max_videos=2,
                comments_per_video=1,
            )
            mock_search.assert_called_once()


class TestProjectsCLI(unittest.TestCase):
    """Projects from the command line: list, add, members, the columns of --list-accounts / --history, the run flags."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "p.yaml"
        data = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
        data["app"]["db_path"] = str(self.root / "p.db")
        data["app"]["log_file"] = str(self.root / "p.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["browser"]["accounts"] = []
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def db(self, fn):
        async def run():
            async with Database(self.root / "p.db") as db:
                return await fn(db)

        return asyncio.run(run())

    def main(self, *argv: str) -> tuple[str, int]:
        import io
        import sys

        from youtube_bot import main as bot_main

        out = io.StringIO()
        code = 0
        with patch.object(sys, "argv", ["youtube_bot.py", "--config", str(self.config_path), *argv]), \
             patch("sys.stdout", out), patch("sys.stderr", out):
            try:
                bot_main()
            except SystemExit as exc:
                code = int(exc.code or 0)
        return out.getvalue(), code

    def test_add_list_and_members(self):
        self.db(lambda db: db.upsert_account("alpha", "s/alpha", email="alpha@x.com"))
        self.db(lambda db: db.upsert_account("beta", "s/beta"))
        out, code = self.main("--project-add", "Крипта")
        self.assertEqual(code, 0)
        self.assertIn("Проект «Крипта» создан", out)
        out, code = self.main("--project-add", "крипта")
        self.assertEqual(code, 1)
        self.assertIn("уже есть", out)
        out, code = self.main("--project-accounts", "КРИПТА", "alpha@x.com, beta")
        self.assertEqual(code, 0)
        self.assertIn("добавлено 2 из 2", out)
        out, code = self.main("--project-accounts", "Крипта", "alpha,nobody")
        self.assertEqual(code, 1)
        self.assertIn("'nobody' не найден", out)
        out, _ = self.main("--list-projects")
        self.assertIn("Основной", out)
        self.assertIn("Крипта", out)
        self.assertRegex(out, r"Крипта\s+\| да\s+\| 2 ")
        out, code = self.main("--project-remove-account", "Крипта", "beta")
        self.assertEqual(code, 0)
        self.assertIn("убрано 1 из 1", out)
        out, code = self.main("--project-remove-account", "Нет такого", "beta")
        self.assertEqual(code, 1)
        out, _ = self.main("--list-accounts")
        self.assertIn("Projects", out)
        self.assertIn("Крипта", out)
        self.assertIn("— без проекта", out)

    def test_a_new_account_is_told_it_has_no_project(self):
        out, _ = self.main("--add-account", "newbie")
        self.assertIn("Без проекта", out)
        self.assertIn("newbie", out)

    def test_history_shows_the_project(self):
        from database import ActivityLogCreate

        project = self.db(lambda db: db.create_project("Крипта"))
        self.db(lambda db: db.log_activity(ActivityLogCreate(video_id="v", target_id="t", project_id=project.id)))
        self.db(lambda db: db.log_activity(ActivityLogCreate(video_id="v", target_id="t2", project_id=999)))
        out, _ = self.main("--history")
        self.assertIn("Проект", out)
        self.assertIn("Крипта", out)
        self.assertIn("удалён", out)

    def test_run_flags(self):
        summary = SimpleNamespace(stopped_at=None)
        with patch("youtube_bot.run_projects_cli", AsyncMock(return_value=summary)) as run:
            self.main("--dry-run", "--project", "Крипта", "--search", "  биткоин   прогноз ", "--min-views", "5")
        self.assertEqual(run.call_args.args, (str(self.config_path), "Крипта"))
        self.assertEqual(run.call_args.kwargs["topics"], ["биткоин прогноз"])
        with patch("youtube_bot.run_projects_cli", AsyncMock(return_value=None)):
            _, code = self.main("--dry-run", "--project", "Нет такого")
        self.assertEqual(code, 1)
        with patch("youtube_bot.run_projects_cli", AsyncMock(return_value=SimpleNamespace(stopped_at="Крипта"))):
            _, code = self.main("--dry-run")
        self.assertEqual(code, 1)
        with patch("youtube_bot.run_video_pool_cli", AsyncMock(return_value=0)) as pool:
            self.main("--videos-file", "v.txt", "--project", "Крипта")
        self.assertEqual(pool.call_args.kwargs["project"], "Крипта")

    def test_unknown_project_of_run_projects_cli(self):
        from youtube_bot import run_projects_cli, run_video_pool_cli

        self.assertIsNone(asyncio.run(run_projects_cli(str(self.config_path), "Нет такого", dry_run=True)))
        self.db(lambda db: db.rename_project(1, "Другое имя"))
        self.assertEqual(asyncio.run(run_video_pool_cli(str(self.config_path), "v.txt", dry_run=True)), 0)

    def test_videos_file_goes_into_the_named_project(self):
        from youtube_bot import run_video_pool_cli

        crypto = self.db(lambda db: db.create_project("Крипта"))
        videos = self.root / "v.txt"
        videos.write_text("https://www.youtube.com/watch?v=aaaaaaaaaaa\n", encoding="utf-8")
        asyncio.run(run_video_pool_cli(str(self.config_path), str(videos), dry_run=True, project="крипта"))
        self.assertEqual(self.db(lambda db: db.count_pool_videos(project_id=crypto.id)), 1)
        self.assertEqual(self.db(lambda db: db.count_pool_videos()), 1)

if __name__ == "__main__":
    unittest.main()
