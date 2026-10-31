from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from config import (
    AccountSettings,
    AppConfig,
    AppSettings,
    BrowserSettings,
    CollectorEndpoint,
    CollectorSettings,
    DeepSeekSettings,
    LimitsSettings,
    ProcessorSettings,
    TargetSettings,
    ValidatorSettings,
)
from database import Database, VideoPoolItem
from modules.collector import CollectedItem
from modules.multi_account_orchestrator import MultiAccountOrchestrator
from modules.pool_manager import PoolManager
from modules.ui_test_runner import SubmissionResult


def _make_test_config(temp_dir: Path) -> AppConfig:
    return AppConfig(
        app=AppSettings(
            name="test-app",
            db_path=str(temp_dir / "test.db"),
            log_level="DEBUG",
            log_file=str(temp_dir / "test.log"),
        ),
        deepseek=DeepSeekSettings(
            api_key="test-key-12345",
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
        ),
        collector=CollectorSettings(
            languages=["en", "ru"],
            min_text_length=5,
            max_text_length=500,
            endpoints=[
                CollectorEndpoint(
                    name="yt-ep",
                    url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                )
            ],
        ),
        processor=ProcessorSettings(
            system_prompt="Test system prompt",
            user_prompt_template="Test {text}",
            reply_mode="custom",
            custom_replies=["Nice point!", "Interesting thought!"],
            paraphrase_custom=False,  # no DeepSeek client: tests stay offline
        ),
        browser=BrowserSettings(
            headless=True,
            session_root=temp_dir / "sessions",
            render_min_seconds=0.1,
            render_max_seconds=0.2,
            targets=[
                TargetSettings(
                    name="youtube",
                    base_url="https://www.youtube.com",
                    item_url_template="https://www.youtube.com/watch?v={external_id}",
                    submit_input_selector="#contenteditable-root",
                    submit_button_selector="#submit-button",
                    confirmation_selector="ytd-comment-view-model",
                )
            ],
            accounts=[
                AccountSettings(
                    name="account_alpha",
                    session_path=str(temp_dir / "sessions" / "alpha"),
                    proxy_url=None,
                    is_active=True,
                ),
                AccountSettings(
                    name="account_beta",
                    session_path=str(temp_dir / "sessions" / "beta"),
                    proxy_url="http://user:pass@proxy.example.com:8080",
                    is_active=True,
                ),
            ],
        ),
        limits=LimitsSettings(
            total_iterations=2,
            max_concurrent_sessions=2,
        ),
        validator=ValidatorSettings(
            endpoint_template="https://www.youtube.com/watch?v={remote_id}",
        ),
    )


class TestMultiAccountOrchestrator(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.config = _make_test_config(self.base_dir)
        self.db = Database(self.base_dir / "test.db")
        await self.db.connect()
        await self.db.init_schema()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    # ---------------------------------------------------- Video Pool Concurrency
    async def test_video_pool_phase_concurrency_control(self) -> None:
        """Video pool phase execution across multiple accounts using concurrency control (semaphore)."""
        await self.db.upsert_account("acc_1", str(self.base_dir / "acc1"))
        await self.db.upsert_account("acc_2", str(self.base_dir / "acc2"))

        # Add 3 videos
        v1 = await self.db.add_video_to_pool("https://www.youtube.com/watch?v=vid11111111", comments_target=1)
        v2 = await self.db.add_video_to_pool("https://www.youtube.com/watch?v=vid22222222", comments_target=1)
        v3 = await self.db.add_video_to_pool("https://www.youtube.com/watch?v=vid33333333", comments_target=1)

        active_concurrent = 0
        max_concurrent_seen = 0
        lock = asyncio.Lock()

        def mock_runner_factory(acc):
            runner = MagicMock()
            async def mock_run(text, external_id=None, payload_id=None, dry_run=False):
                nonlocal active_concurrent, max_concurrent_seen
                async with lock:
                    active_concurrent += 1
                    if active_concurrent > max_concurrent_seen:
                        max_concurrent_seen = active_concurrent
                await asyncio.sleep(0.02)
                async with lock:
                    active_concurrent -= 1
                return SubmissionResult(success=True, remote_id=f"rem_{external_id}", duration_seconds=0.1)
            runner.run = AsyncMock(side_effect=mock_run)
            runner.close = AsyncMock()
            return runner

        mock_collector = MagicMock()
        async def mock_fetch_popular(video_url, limit=4, target_mode="first_reply"):
            vid = video_url.split("v=")[-1]
            return [
                CollectedItem(
                    endpoint=f"youtube:{vid}",
                    external_id=f"{vid}#root#c_{vid}",
                    text=f"Cool video {vid}!",
                    language="en",
                    confidence=0.95,
                    source_url=f"https://www.youtube.com/watch?v={vid}",
                    author=f"user_{vid}",
                    video_id=vid,
                    like_count=5,
                )
            ]
        mock_collector.fetch_popular_comments = AsyncMock(side_effect=mock_fetch_popular)
        mock_collector.close = AsyncMock()

        # Concurrency = 2
        concurrency = 2
        orchestrator = MultiAccountOrchestrator(
            self.config,
            db=self.db,
            concurrency=concurrency,
            account_mode="share",
            collector=mock_collector,
            runner_factory=mock_runner_factory,
            rng=random.Random(42),
        )

        total_done = await orchestrator.execute_video_pool(max_comments_per_video=1)
        self.assertEqual(total_done, 3)

        # Concurrency must not exceed configured limit
        self.assertLessEqual(max_concurrent_seen, concurrency)

        # Verify pool progress updated in DB
        pending = await self.db.list_pool_videos(status="pending")
        self.assertEqual(len(pending), 0)

        # Verify activity logged for all 3
        logs = await self.db.list_activity_logs()
        self.assertEqual(len(logs), 3)
        self.assertTrue(all(log.status == "success" for log in logs))

    # ---------------------------------------------------------- Error Isolation
    async def test_error_isolation_video_pool(self) -> None:
        """If Account 1 fails (e.g. browser crash or captcha), Account 2 must continue and succeed."""
        await self.db.upsert_account("acc_faulty", str(self.base_dir / "faulty"))
        await self.db.upsert_account("acc_healthy", str(self.base_dir / "healthy"))

        v1 = await self.db.add_video_to_pool("https://www.youtube.com/watch?v=errvid11111", comments_target=1)
        v2 = await self.db.add_video_to_pool("https://www.youtube.com/watch?v=errvid22222", comments_target=1)

        def mock_runner_pool(acc):
            runner = MagicMock()
            if acc.name == "acc_faulty":
                runner.run = AsyncMock(return_value=SubmissionResult(success=False, error="Simulated captcha detected", duration_seconds=0.2))
            else:
                runner.run = AsyncMock(return_value=SubmissionResult(success=True, remote_id="rem_ok", duration_seconds=0.2))
            runner.close = AsyncMock()
            return runner

        mock_collector = MagicMock()
        mock_collector.fetch_popular_comments = AsyncMock(return_value=[
            CollectedItem(
                endpoint="youtube:errvid",
                external_id="errvid#root#c_err",
                text="Comment to reply to",
                language="en",
                confidence=0.95,
                source_url="https://www.youtube.com/watch?v=errvid",
                author="user_err",
                video_id="errvid",
                like_count=1,
            )
        ])
        mock_collector.close = AsyncMock()

        orch_pool = MultiAccountOrchestrator(
            self.config,
            db=self.db,
            concurrency=1,
            account_mode="share",
            collector=mock_collector,
            runner_factory=mock_runner_pool,
        )

        comments_done = await orch_pool.execute_video_pool(max_comments_per_video=1)
        # Pipeline didn't crash, healthy account posted at least one reply
        self.assertGreaterEqual(comments_done, 1)

        # Activity log captures both failed and success records
        logs = await self.db.list_activity_logs()
        statuses = [l.status for l in logs]
        self.assertIn("failed", statuses)
        self.assertIn("success", statuses)

    # ------------------------------------------------ End-to-End Video Pool
    async def test_run_video_pool_e2e(self) -> None:
        """run_video_pool loads the videos file and replies with the config accounts."""
        videos_file = self.base_dir / "videos.txt"
        videos_file.write_text(
            "https://www.youtube.com/watch?v=e2evid11111\nhttps://www.youtube.com/watch?v=e2evid22222\n",
            encoding="utf-8",
        )

        mock_collector = MagicMock()
        mock_collector.fetch_popular_comments = AsyncMock(return_value=[
            CollectedItem(
                endpoint="youtube:e2evid",
                external_id="e2evid#root#c_e2e",
                text="Great guide!",
                language="en",
                confidence=0.95,
                source_url="https://www.youtube.com/watch?v=e2evid",
                author="viewer",
                video_id="e2evid",
                like_count=10,
            )
        ])
        mock_collector.close = AsyncMock()

        def mock_runner_factory(acc):
            runner = MagicMock()
            runner.run = AsyncMock(return_value=SubmissionResult(success=True, remote_id="rem_e2e", duration_seconds=0.1))
            runner.close = AsyncMock()
            return runner

        orchestrator = MultiAccountOrchestrator(
            self.config,
            db=self.db,
            concurrency=2,
            collector=mock_collector,
            runner_factory=mock_runner_factory,
            rng=random.Random(42),
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            comments_done = await orchestrator.run_video_pool(videos_file=videos_file)

        self.assertGreater(comments_done, 0)
        videos = await self.db.list_pool_videos(status="all")
        self.assertEqual(len(videos), 2)
        a1 = await self.db.get_account("account_alpha")
        a2 = await self.db.get_account("account_beta")
        self.assertEqual(a1.total_replies + a2.total_replies, comments_done)

    # --------------------------------------------------------------- Dry Run
    async def test_dry_run_mode(self) -> None:
        """Dry-run mode simulates comments without live database submission."""
        await self.db.upsert_account("dry_acc", str(self.base_dir / "dry"))
        await self.db.add_video_to_pool("https://www.youtube.com/watch?v=dry_vid1234", comments_target=1)

        dry_run_flag_seen = []

        def mock_runner_factory(acc):
            runner = MagicMock()
            async def mock_run(text, external_id=None, payload_id=None, dry_run=False):
                dry_run_flag_seen.append(("run", dry_run))
                return SubmissionResult(success=True, remote_id="sim_123", duration_seconds=0.05)
            runner.run = AsyncMock(side_effect=mock_run)
            runner.close = AsyncMock()
            return runner

        mock_collector = MagicMock()
        mock_collector.fetch_popular_comments = AsyncMock(return_value=[
            CollectedItem(
                endpoint="youtube:dry_vid1234",
                external_id="dry_vid1234#root#c_dry",
                text="Dry run test comment",
                language="en",
                confidence=0.95,
                source_url="https://www.youtube.com/watch?v=dry_vid1234",
                author="dry_user",
                video_id="dry_vid1234",
                like_count=0,
            )
        ])
        mock_collector.close = AsyncMock()

        orchestrator = MultiAccountOrchestrator(
            self.config,
            db=self.db,
            dry_run=True,
            collector=mock_collector,
            runner_factory=mock_runner_factory,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            comments = await orchestrator.execute_video_pool(max_comments_per_video=1)

        self.assertEqual(comments, 1)
        self.assertTrue(all(flag is True for _, flag in dry_run_flag_seen))

        # Activity log has 'simulated' status and mode 'dry_run'
        logs = await self.db.list_activity_logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].mode, "dry_run")
        self.assertEqual(logs[0].status, "simulated")

    # ----------------------------------------------------------- Empty Pools
    async def test_empty_pool_returns_zero(self) -> None:
        """When the video pool is empty, execute_video_pool returns 0."""
        await self.db.upsert_account("empty_user", str(self.base_dir / "empty"))
        orchestrator = MultiAccountOrchestrator(
            self.config,
            db=self.db,
        )
        comments = await orchestrator.execute_video_pool()
        self.assertEqual(comments, 0)

    # ----------------------------------------------------------- Account modes
    def _mode_collector(self, videos: list[str]) -> MagicMock:
        """Collector with two comments under each of `videos` (ids <video>#c0 / #c1)."""
        def items(video_url, limit=4, target_mode="first_reply"):
            vid = video_url.split("v=")[-1]
            return [
                CollectedItem(
                    endpoint=f"youtube:{vid}",
                    external_id=f"{vid}#c{i}",
                    text=f"comment {i} under {vid}",
                    language="en",
                    confidence=0.95,
                    source_url=video_url,
                    author="viewer",
                    video_id=vid,
                    like_count=1,
                )
                for i in range(2)
            ]
        collector = MagicMock()
        collector.fetch_popular_comments = AsyncMock(side_effect=items)
        collector.close = AsyncMock()
        return collector

    async def _run_mode(self, mode: str, concurrency: int, meet: bool = False) -> tuple[list[tuple[str, str, str]], int]:
        """2 accounts, 2 videos, 1 reply per video; returns (event, account, comment) events and replies.

        meet=True: a reply waits until both accounts have started one (fails with a timeout otherwise).
        """
        await self.db.upsert_account("acc_a", str(self.base_dir / "a"))
        await self.db.upsert_account("acc_b", str(self.base_dir / "b"))
        for vid in ("modevid1111", "modevid2222"):
            await self.db.add_video_to_pool(f"https://www.youtube.com/watch?v={vid}", comments_target=1)
        events: list[tuple[str, str, str]] = []
        both_started = asyncio.Event()

        def factory(acc):
            async def run(text, external_id=None, payload_id=None, dry_run=False):
                events.append(("start", acc.name, external_id))
                if {name for kind, name, _ in events if kind == "start"} == {"acc_a", "acc_b"}:
                    both_started.set()
                if meet:
                    await asyncio.wait_for(both_started.wait(), timeout=5)
                await asyncio.sleep(0.01)
                events.append(("end", acc.name, external_id))
                return SubmissionResult(success=True, remote_id="r", duration_seconds=0.01)
            runner = MagicMock()
            runner.run = AsyncMock(side_effect=run)
            runner.close = AsyncMock()
            return runner

        orchestrator = MultiAccountOrchestrator(
            self.config, db=self.db, concurrency=concurrency, account_mode=mode, dry_run=True,
            collector=self._mode_collector(["modevid1111", "modevid2222"]), runner_factory=factory,
        )
        done = await orchestrator.execute_video_pool()
        return events, done

    async def test_each_mode_one_thread_runs_accounts_one_after_another(self) -> None:
        """«each», 1 thread: account A passes the whole list, then B; nobody replies to the same comment."""
        events, done = await self._run_mode("each", concurrency=1)
        self.assertEqual(done, 4)  # 2 accounts x 2 videos x 1 reply
        order = [name for kind, name, _ in events if kind == "start"]
        self.assertEqual(order, ["acc_a", "acc_a", "acc_b", "acc_b"])
        comments = [cid for kind, _, cid in events if kind == "start"]
        self.assertEqual(len(comments), len(set(comments)))
        self.assertEqual(await self.db.list_pool_videos(status="pending"), [])

    async def test_each_mode_two_threads_runs_accounts_together(self) -> None:
        """«each», 2 threads: both accounts work at the same time, still on different comments."""
        events, done = await self._run_mode("each", concurrency=2, meet=True)
        self.assertEqual(done, 4)  # with one account at a time the first reply would time out
        comments = [cid for kind, _, cid in events if kind == "start"]
        self.assertEqual(len(comments), len(set(comments)))

    async def test_share_mode_splits_replies_between_accounts(self) -> None:
        """«share»: one reply per video in total, the accounts take turns."""
        events, done = await self._run_mode("share", concurrency=1)
        self.assertEqual(done, 2)
        self.assertEqual({name for kind, name, _ in events if kind == "start"}, {"acc_a", "acc_b"})

    def test_account_mode_defaults_to_config(self) -> None:
        self.assertEqual(MultiAccountOrchestrator(self.config, db=self.db).account_mode, "each")
        self.config.limits.account_mode = "share"
        self.assertEqual(MultiAccountOrchestrator(self.config, db=self.db).account_mode, "share")

    # ------------------------------------------------- One browser per account
    async def test_one_browser_per_account_stays_open_between_comments(self) -> None:
        """Several comments by one account reuse a single runner; it is closed once at the end."""
        await self.db.upsert_account("solo_acc", str(self.base_dir / "solo"))
        await self.db.add_video_to_pool("https://www.youtube.com/watch?v=solo_vid1111", comments_target=3)

        items = [
            CollectedItem(
                endpoint="youtube:solo_vid1111",
                external_id=f"solo_vid1111#root#c{i}",
                text=f"comment {i}",
                language="en",
                confidence=0.95,
                source_url="https://www.youtube.com/watch?v=solo_vid1111",
                author="viewer",
                video_id="solo_vid1111",
                like_count=1,
            )
            for i in range(3)
        ]
        mock_collector = MagicMock()
        mock_collector.fetch_popular_comments = AsyncMock(return_value=items)
        mock_collector.close = AsyncMock()

        created: list[MagicMock] = []

        def factory(acc):
            runner = MagicMock()
            runner.run = AsyncMock(return_value=SubmissionResult(success=True, remote_id="r", duration_seconds=0.1))
            runner.close = AsyncMock()
            created.append(runner)
            return runner

        orchestrator = MultiAccountOrchestrator(
            self.config, db=self.db, collector=mock_collector, runner_factory=factory, dry_run=True,
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            comments = await orchestrator.execute_video_pool()

        self.assertEqual(comments, 3)
        self.assertEqual(len(created), 1)  # one browser, not one per comment
        self.assertEqual(created[0].run.await_count, 3)
        created[0].close.assert_awaited_once()

    # ------------------------------------------------------------- main.py CLI
    def test_main_cli_multi_account_pipeline_dispatch(self) -> None:
        """main.py with --videos-file dispatches to MultiAccountOrchestrator.run_video_pool."""
        from main import main
        import yaml

        cfg_file = self.base_dir / "test_cli_config.yaml"
        # Dump valid config
        cfg_data = {
            "app": {"name": "cli-test", "db_path": str(self.base_dir / "cli.db")},
            "deepseek": {"api_key": "dummy", "base_url": "https://api.deepseek.com", "model": "m"},
            "collector": {
                "languages": ["en"],
                "min_text_length": 5,
                "max_text_length": 500,
                "endpoints": [{"name": "ep", "url": "https://youtube.com"}],
            },
            "processor": {
                "system_prompt": "s",
                "user_prompt_template": "t {text}",
                "reply_mode": "custom",
                "custom_replies": ["hi"],
            },
            "browser": {
                "headless": True,
                "session_root": str(self.base_dir / "sessions"),
                "render_min_seconds": 1.0,
                "render_max_seconds": 2.0,
                "targets": [{
                    "name": "youtube",
                    "base_url": "https://www.youtube.com",
                    "submit_input_selector": "#in",
                    "submit_button_selector": "#btn",
                    "confirmation_selector": "ytd-comment-view-model",
                }],
            },
            "limits": {"total_iterations": 1},
            "validator": {"endpoint_template": "https://youtube.com/{remote_id}"},
        }
        cfg_file.write_text(yaml.safe_dump(cfg_data), encoding="utf-8")

        vid_file = self.base_dir / "cli_videos.txt"
        vid_file.write_text("https://www.youtube.com/watch?v=clivid11111\n", encoding="utf-8")

        with patch("modules.multi_account_orchestrator.MultiAccountOrchestrator.run_video_pool", new_callable=AsyncMock) as mock_pipe, \
             patch("modules.multi_account_orchestrator.MultiAccountOrchestrator.close", new_callable=AsyncMock):
            mock_pipe.return_value = 1

            exit_code = main([
                "--config", str(cfg_file),
                "--videos-file", str(vid_file),
                "--concurrency", "2",
                "--dry-run",
                "--headless",
            ])

            self.assertEqual(exit_code, 0)
            mock_pipe.assert_awaited_once_with(videos_file=str(vid_file))


class TestLifecyclePacing(unittest.IsolatedAsyncioTestCase):
    """Lifecycle gate, error counting and pauses inside `_process_video` (mode "each", live)."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.config = _make_test_config(self.base_dir)
        self.db = Database(self.base_dir / "test.db")
        await self.db.connect()
        await self.db.init_schema()
        self.video = await self.db.add_video_to_pool("https://www.youtube.com/watch?v=lifevid1111", comments_target=3)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    async def _account(self, name: str, hours_since_sign_in: float | None):
        account = await self.db.upsert_account(name, str(self.base_dir / name))
        if hours_since_sign_in is not None:
            await self.db.mark_account_authenticated(account.id)
            stamped = (datetime.now(timezone.utc) - timedelta(hours=hours_since_sign_in)).isoformat()
            async with self.db._transaction() as conn:
                await conn.execute("UPDATE accounts SET authenticated_at = ? WHERE id = ?", (stamped, account.id))
        return await self.db.get_account(account.id)

    def _orchestrator(self, run) -> tuple[MultiAccountOrchestrator, MagicMock]:
        runner = MagicMock(run=AsyncMock(side_effect=run), close=AsyncMock())
        collector = MagicMock(close=AsyncMock())
        items = [
            CollectedItem(
                endpoint="youtube:lifevid1111",
                external_id=f"lifevid1111#root#c_{n}",
                text=f"Comment {n}",
                language="en",
                confidence=0.9,
                source_url="https://www.youtube.com/watch?v=lifevid1111",
                author=f"user_{n}",
                video_id="lifevid1111",
                like_count=1,
            )
            for n in range(6)
        ]
        collector.fetch_popular_comments = AsyncMock(return_value=items)
        orchestrator = MultiAccountOrchestrator(
            self.config,
            db=self.db,
            account_mode="each",
            collector=collector,
            runner_factory=lambda acc: runner,
            rng=random.Random(7),
        )
        return orchestrator, runner

    async def test_probation_account_posts_one_comment_then_stops(self) -> None:
        account = await self._account("probation_acc", 50)
        orchestrator, runner = self._orchestrator(
            lambda *a, **k: SubmissionResult(success=True, remote_id="r", duration_seconds=0.1)
        )
        with patch("modules.multi_account_orchestrator.asyncio.sleep", AsyncMock()):
            done = await orchestrator._process_video(self.video, 3, account)
        self.assertEqual(done, 1)
        stored = await self.db.get_account(account.id)
        self.assertEqual((stored.daily_comments_done, stored.total_replies), (1, 1))
        self.assertIsNotNone(stored.last_comment_at)
        self.assertEqual(stored.lifecycle_stage, "probation")

    async def test_failed_submissions_trip_the_circuit_breaker(self) -> None:
        account = await self._account("mature_acc", 100)
        orchestrator, runner = self._orchestrator(
            lambda *a, **k: SubmissionResult(success=False, error="submit failed", duration_seconds=0.1)
        )
        done = await orchestrator._process_video(self.video, 5, account)
        self.assertEqual(done, 0)
        self.assertEqual(runner.run.await_count, 3)
        stored = await self.db.get_account(account.id)
        self.assertEqual(stored.consecutive_errors, 3)
        self.assertEqual(stored.lifecycle_stage, "restricted")

    async def test_pause_between_comments_comes_from_the_limits_config(self) -> None:
        account = await self._account("manual_acc", None)  # never signed in by the bot: no lifecycle gate
        orchestrator, _ = self._orchestrator(
            lambda *a, **k: SubmissionResult(success=True, remote_id="r", duration_seconds=0.1)
        )
        sleep = AsyncMock()
        with patch("modules.multi_account_orchestrator.asyncio.sleep", sleep):
            done = await orchestrator._process_video(self.video, 3, account)
        self.assertEqual(done, 3)
        pauses = [call.args[0] for call in sleep.await_args_list]
        self.assertEqual(len(pauses), 2)  # between the comments, not after the last one
        limits = self.config.limits
        self.assertTrue(all(limits.pause_min_seconds <= p <= limits.pause_max_seconds for p in pauses))


if __name__ == "__main__":
    unittest.main()
