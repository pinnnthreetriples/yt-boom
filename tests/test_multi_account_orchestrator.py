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

    async def test_target_without_reply_button_is_skipped_not_blamed_on_the_account(self) -> None:
        """A comment YouTube gives no Reply button: next candidate is tried, the account gets no error, the target is remembered."""
        acc = await self.db.upsert_account("acc_birut", str(self.base_dir / "birut"))
        await self.db.add_video_to_pool("https://www.youtube.com/watch?v=noreply1111", comments_target=1)

        def make_item(cid: str) -> CollectedItem:
            return CollectedItem(
                endpoint="youtube:noreply", external_id=f"noreply#root#{cid}", text=f"text {cid}", language="en",
                confidence=0.95, source_url="https://www.youtube.com/watch?v=noreply", author="u", video_id="noreply", like_count=1,
            )

        results = {
            "noreply#root#c_hidden": SubmissionResult(
                success=False, error="ReplyUnavailableError: hidden", duration_seconds=0.1, reply_unavailable=True
            ),
            "noreply#root#c_ok": SubmissionResult(success=True, remote_id="rem_ok", duration_seconds=0.1),
        }

        def runner_factory(_acc):
            runner = MagicMock()
            runner.run = AsyncMock(side_effect=lambda text, external_id=None, **kw: results[external_id])
            runner.close = AsyncMock()
            return runner

        collector = MagicMock()
        collector.fetch_popular_comments = AsyncMock(return_value=[make_item("c_hidden"), make_item("c_ok")])
        collector.close = AsyncMock()
        orchestrator = MultiAccountOrchestrator(
            self.config, db=self.db, concurrency=1, account_mode="share", collector=collector,
            runner_factory=runner_factory, rng=random.Random(1),
        )
        self.assertEqual(await orchestrator.execute_video_pool(max_comments_per_video=1), 1)

        statuses = sorted(log.status for log in await self.db.list_activity_logs())
        self.assertEqual(statuses, ["failed", "success"])
        self.assertEqual((await self.db.get_account(acc.id)).consecutive_errors, 0)  # the target was at fault
        self.assertTrue(await self.db.is_reply_unavailable("noreply#root#c_hidden"))
        self.assertFalse(await self.db.is_reply_unavailable("noreply#root#c_ok"))

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
        # a dry run leaves the pool as it was: a live run takes the same videos
        self.assertEqual([(v.status, v.comments_done) for v in await self.db.list_pool_videos(status="all")],
                         [("pending", 0), ("pending", 0)])

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

    async def test_parallel_workers_cannot_exceed_a_probation_accounts_daily_limit(self) -> None:
        account = await self._account("probation_acc", 50)
        for n in (2, 3):
            await self.db.add_video_to_pool(f"https://www.youtube.com/watch?v=lifevid{n}{n}{n}{n}", comments_target=1)
        orchestrator, runner = self._orchestrator(
            lambda *a, **k: SubmissionResult(success=True, remote_id="r", duration_seconds=0.1)
        )
        orchestrator._account_mode, orchestrator._concurrency = "share", 2
        orchestrator._semaphore = asyncio.Semaphore(2)
        with patch("modules.multi_account_orchestrator.asyncio.sleep", AsyncMock()):
            await orchestrator.execute_video_pool(max_comments_per_video=1)
        self.assertEqual(runner.run.await_count, 1)
        self.assertEqual((await self.db.get_account(account.id)).daily_comments_done, 1)

    async def test_gated_account_leaves_the_videos_pending_in_each_mode(self) -> None:
        await self._account("probation_acc", 50)
        orchestrator, runner = self._orchestrator(
            lambda *a, **k: SubmissionResult(success=True, remote_id="r", duration_seconds=0.1)
        )
        with patch("modules.multi_account_orchestrator.asyncio.sleep", AsyncMock()):
            done = await orchestrator.execute_video_pool(max_comments_per_video=3)
        self.assertEqual(done, 1)
        self.assertEqual(len(await self.db.list_pool_videos(status="pending")), 1)

    async def _gated_run(self, hours: float, extra_videos: int = 2):
        await self._account("mature_acc", hours)
        for n in range(extra_videos):
            await self.db.add_video_to_pool(f"https://www.youtube.com/watch?v=lifevid{n}{n}{n}x{n}", comments_target=3)
        orchestrator, runner = self._orchestrator(
            lambda *a, **k: SubmissionResult(success=True, remote_id="r", duration_seconds=0.1)
        )
        sleep = AsyncMock()
        with patch("modules.multi_account_orchestrator.asyncio.sleep", sleep):
            done = await orchestrator.execute_video_pool(max_comments_per_video=2)
        return orchestrator, runner, sleep, done

    async def test_gated_account_does_not_fetch_comments_for_the_remaining_videos(self) -> None:
        orchestrator, runner, _, done = await self._gated_run(100)
        self.assertEqual(done, 1)
        self.assertEqual(runner.run.await_count, 1)
        self.assertEqual(orchestrator._collector.fetch_popular_comments.await_count, 1)

    async def test_gated_account_leaves_every_video_unfinished_and_skips_the_useless_pause(self) -> None:
        orchestrator, _, sleep, _ = await self._gated_run(100)
        videos = await self.db.list_pool_videos(status="all")
        self.assertEqual(len(videos), 3)
        self.assertTrue(all(v.status != "completed" for v in videos))
        self.assertEqual(len(await self.db.list_pool_videos(status="pending")), 3)
        sleep.assert_not_awaited()  # the cooldown would refuse the next comment anyway
        self.assertEqual(len(orchestrator._gated_accounts), 1)

    async def test_one_gated_account_does_not_stop_the_others(self) -> None:
        gated = await self._account("mature_acc", 100)
        free = await self._account("manual_acc", None)  # no lifecycle clock: never gated
        for n in range(2):
            await self.db.add_video_to_pool(f"https://www.youtube.com/watch?v=lifevid{n}{n}{n}y{n}", comments_target=2)
        orchestrator, runner = self._orchestrator(
            lambda *a, **k: SubmissionResult(success=True, remote_id="r", duration_seconds=0.1)
        )
        with patch("modules.multi_account_orchestrator.asyncio.sleep", AsyncMock()):
            done = await orchestrator.execute_video_pool(max_comments_per_video=2)
        self.assertEqual(orchestrator._gated_accounts, {gated.id})
        # the free account went on past the first video (the shared fixture comments run out after 6 ids)
        self.assertGreater((await self.db.get_account(free.id)).total_replies, 2)
        self.assertEqual((await self.db.get_account(gated.id)).total_replies, 1)
        self.assertEqual(done, 1 + (await self.db.get_account(free.id)).total_replies)


class TestVisibilityHook(TestLifecyclePacing):
    """Post-submit visibility check: background, observability only (inherits the fixtures, mode "each")."""

    def _watched(self, run, checker):
        orchestrator, _ = self._orchestrator(run)
        orchestrator._visibility = checker
        return orchestrator

    @staticmethod
    def _checker(release: asyncio.Event | None = None, seen: bool | None = False):
        async def watch(video_id, comment_id, account_name):
            if release is not None:
                await release.wait()
            return seen

        return MagicMock(watch=AsyncMock(side_effect=watch))

    async def test_live_reply_is_checked_in_the_background_without_blocking_or_hurting_the_account(self) -> None:
        account = await self._account("manual_acc", None)
        release = asyncio.Event()
        checker = self._checker(release, seen=False)
        orchestrator = self._watched(
            lambda *a, **k: SubmissionResult(success=True, remote_id="Ugx.r1", duration_seconds=0.1), checker
        )
        with patch("modules.multi_account_orchestrator.asyncio.sleep", AsyncMock()):
            done = await orchestrator._process_video(self.video, 3, account)  # returns while the checks still wait
        self.assertEqual(done, 3)
        self.assertEqual(len(orchestrator._visibility_tasks), 3)
        release.set()
        await orchestrator._finish_visibility_checks()
        self.assertEqual(checker.watch.await_count, 3)
        self.assertEqual(checker.watch.await_args_list[0].args, ("lifevid1111", "Ugx.r1", "manual_acc"))
        self.assertEqual((await self.db.get_account(account.id)).consecutive_errors, 0)  # a miss is not an error

    async def test_the_result_is_written_to_the_journal_record_of_that_reply(self) -> None:
        account = await self._account("manual_acc", None)
        results = iter([True, False, None])

        async def watch(video_id, comment_id, account_name):
            return next(results)

        checker = MagicMock(watch=AsyncMock(side_effect=watch))
        orchestrator = self._watched(
            lambda *a, **k: SubmissionResult(success=True, remote_id="Ugx.r1", duration_seconds=0.1), checker
        )
        with patch("modules.multi_account_orchestrator.asyncio.sleep", AsyncMock()):
            await orchestrator._process_video(self.video, 3, account)
        await orchestrator._finish_visibility_checks()
        rows = sorted(await self.db.list_activity_logs(), key=lambda r: r.id)
        self.assertEqual([(r.status, r.visibility) for r in rows],
                         [("success", "seen"), ("success", "unseen"), ("success", "unknown")])
        self.assertEqual((await self.db.get_account(account.id)).consecutive_errors, 0)

    async def test_dry_run_failed_submit_and_missing_remote_id_are_not_checked(self) -> None:
        account = await self._account("manual_acc", None)
        cases = (
            (True, SubmissionResult(success=True, remote_id="Ugx.r1", duration_seconds=0.1)),
            (False, SubmissionResult(success=False, error="x", duration_seconds=0.1)),
            (False, SubmissionResult(success=True, remote_id=None, duration_seconds=0.1)),
        )
        for dry_run, result in cases:
            checker = self._checker()
            orchestrator = self._watched(lambda *a, result=result, **k: result, checker)
            orchestrator._dry_run = dry_run
            with patch("modules.multi_account_orchestrator.asyncio.sleep", AsyncMock()):
                await orchestrator._process_video(self.video, 1, account)
            await orchestrator._finish_visibility_checks()
            checker.watch.assert_not_awaited()

    async def test_checker_exists_only_for_real_runners_with_a_positive_delay(self) -> None:
        orchestrator, _ = self._orchestrator(lambda *a, **k: None)
        self.assertIsNone(orchestrator._visibility)  # tests inject a runner_factory
        real = MultiAccountOrchestrator(self.config, db=self.db, collector=MagicMock())
        self.assertIsNotNone(real._visibility)
        self.config.limits.visibility_check_delay_seconds = 0
        self.assertIsNone(MultiAccountOrchestrator(self.config, db=self.db, collector=MagicMock())._visibility)
        self.config.limits.visibility_check_delay_seconds = 45
        self.config.limits.visibility_check_enabled = False  # the switch of the «Запуск» tab
        self.assertIsNone(MultiAccountOrchestrator(self.config, db=self.db, collector=MagicMock())._visibility)

    async def test_pool_run_waits_for_pending_checks_before_returning(self) -> None:
        await self._account("manual_acc", None)
        checker = self._checker()
        orchestrator = self._watched(
            lambda *a, **k: SubmissionResult(success=True, remote_id="Ugx.r1", duration_seconds=0.1), checker
        )
        with patch("modules.multi_account_orchestrator.asyncio.sleep", AsyncMock()):
            await orchestrator.execute_video_pool(max_comments_per_video=1)
        self.assertEqual(checker.watch.await_count, 1)
        self.assertFalse(orchestrator._visibility_tasks)

    async def test_leaving_on_an_error_cancels_pending_checks_instead_of_waiting(self) -> None:
        await self._account("manual_acc", None)
        checker = self._checker(asyncio.Event())  # never released: a normal exit would wait forever
        orchestrator = self._watched(
            lambda *a, **k: SubmissionResult(success=True, remote_id="Ugx.r1", duration_seconds=0.1), checker
        )
        original = orchestrator._execute_video_pool

        async def fail_after_posting(*args, **kwargs):
            await original(*args, **kwargs)
            raise RuntimeError("pool failed")

        orchestrator._execute_video_pool = fail_after_posting
        with patch("modules.multi_account_orchestrator.asyncio.sleep", AsyncMock()):
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(orchestrator.execute_video_pool(max_comments_per_video=1), timeout=10)
        self.assertFalse(orchestrator._visibility_tasks)

    async def test_gate_message_is_logged_only_in_each_mode(self) -> None:
        account = await self._account("mature_acc", 100)
        for mode, expected in (("each", 1), ("share", 0)):
            orchestrator, _ = self._orchestrator(lambda *a, **k: None)
            orchestrator._account_mode = mode
            with patch("modules.multi_account_orchestrator.logger") as log:
                orchestrator._gate_account(account)
            self.assertEqual(log.info.call_count, expected)
            self.assertIn(account.id, orchestrator._gated_accounts)

    async def test_default_collector_uses_the_search_proxy(self) -> None:
        """Without an injected collector the pool collects through the library's search proxy (set in `ensure_db`);
        a leftover `browser.proxy` is ignored and no search proxy means direct."""
        record, _ = await self.db.add_proxy_url("socks5://1.2.3.4:1080")
        for search_id, expected in ((record.id, "socks5://1.2.3.4:1080"), (None, None)):
            with self.subTest(search=expected):
                await self.db.set_search_proxy(search_id)
                self.config.browser.proxy = "http://9.9.9.9:9"
                collector = MagicMock(set_proxy=AsyncMock(), close=AsyncMock())
                with patch("modules.multi_account_orchestrator.YouTubeCollector", return_value=collector) as collector_cls:
                    orchestrator = MultiAccountOrchestrator(self.config, db=self.db, processor=MagicMock())
                collector_cls.assert_called_once_with(self.config.collector, None)
                await orchestrator.ensure_db()
                collector.set_proxy.assert_awaited_once_with(expected)


# ------------------------------------------------------------ topic search through the accounts


def _search_item(vid: str, cid: str) -> CollectedItem:
    return CollectedItem(
        endpoint=f"youtube:{vid}",
        external_id=f"{vid}#root#{cid}",
        text=f"Comment {cid} about the video",
        language="en",
        confidence=0.95,
        source_url=f"https://www.youtube.com/watch?v={vid}",
        author=f"user_{cid}",
        video_id=vid,
        like_count=10,
    )


def _found(*vids: str) -> list[dict]:
    return [{"video_id": v, "url": f"https://www.youtube.com/watch?v={v}", "title": v, "view_count": 20_000} for v in vids]


class TestSearchThroughAccounts(unittest.IsolatedAsyncioTestCase):
    """run_search: search → found videos into the pool (source 'search') → the accounts, as the own links."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.config = _make_test_config(self.base_dir)
        self.config.browser.accounts = []
        self.db = Database(self.base_dir / "test.db")
        await self.db.connect()
        await self.db.init_schema()
        self.posted: list[tuple[str, str]] = []  # (account, external_id)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp_dir.cleanup()

    def collector(self, found: list[dict], comments: dict[str, list[CollectedItem]]) -> MagicMock:
        col = MagicMock()
        col.search_videos = AsyncMock(return_value=found)

        async def fetch(url, limit=10, target_mode="first_reply"):
            return list(comments.get(url.split("v=")[-1], []))

        col.fetch_popular_comments = AsyncMock(side_effect=fetch)
        col.close = AsyncMock()
        return col

    def orchestrator(self, col, *, dry_run=True, account_mode="each", concurrency=2) -> MultiAccountOrchestrator:
        def factory(acc):
            runner = MagicMock()

            async def run(text, external_id=None, payload_id=None, dry_run=False):
                self.posted.append((acc.name, external_id))
                return SubmissionResult(success=True, remote_id=f"r_{len(self.posted)}", duration_seconds=0.01)

            runner.run = AsyncMock(side_effect=run)
            runner.close = AsyncMock()
            return runner

        return MultiAccountOrchestrator(
            self.config, db=self.db, concurrency=concurrency, dry_run=dry_run, account_mode=account_mode,
            collector=col, runner_factory=factory, rng=random.Random(1),
        )

    async def test_found_videos_go_through_every_account_without_taking_a_comment_twice(self) -> None:
        await self.db.upsert_account("acc_a", str(self.base_dir / "a"))
        await self.db.upsert_account("acc_b", str(self.base_dir / "b"))
        comments = {v: [_search_item(v, f"{v}_c{n}") for n in range(4)] for v in ("srch_vid_01", "srch_vid_02")}
        col = self.collector(_found("srch_vid_01", "srch_vid_02"), comments)
        orch = self.orchestrator(col)

        result = await orch.run_search("iphone 17", min_views=10_000, max_views=500_000, max_videos=2, comments_per_video=1)

        self.assertEqual(tuple(result), (2, 2, 4))  # "each": both accounts write 1 reply under both videos
        col.search_videos.assert_awaited_once_with(
            query="iphone 17", min_views=10_000, max_views=500_000, limit=2, max_age_hours=None,
        )
        self.assertEqual({name for name, _ in self.posted}, {"acc_a", "acc_b"})
        ids = [ext for _, ext in self.posted]
        self.assertEqual(len(ids), len(set(ids)))  # a dry run never takes the same comment twice
        self.assertEqual(len(await self.db.list_pool_videos(status="all", source="search")), 2)
        self.assertEqual(await self.db.list_pool_videos(status="all", source="links"), [])

    async def test_share_mode_splits_the_found_videos(self) -> None:
        await self.db.upsert_account("acc_a", str(self.base_dir / "a"))
        await self.db.upsert_account("acc_b", str(self.base_dir / "b"))
        comments = {v: [_search_item(v, f"{v}_c{n}") for n in range(4)] for v in ("srch_vid_01", "srch_vid_02")}
        orch = self.orchestrator(self.collector(_found("srch_vid_01", "srch_vid_02"), comments), account_mode="share")

        result = await orch.run_search("iphone 17", max_videos=2, comments_per_video=1)

        self.assertEqual(tuple(result), (2, 2, 2))  # "share": one reply per video for all accounts together

    async def test_a_video_found_again_is_taken_again_and_answered_comments_are_skipped(self) -> None:
        await self.db.upsert_account("acc_a", str(self.base_dir / "a"))
        vid = "again_vid01"
        comments = {vid: [_search_item(vid, "c1"), _search_item(vid, "c2")]}

        for _ in range(2):
            orch = self.orchestrator(self.collector(_found(vid), comments), dry_run=False, account_mode="share")
            result = await orch.run_search("topic", max_videos=1, comments_per_video=1)
            self.assertEqual(tuple(result), (1, 1, 1))

        self.assertEqual([ext for _, ext in self.posted], [f"{vid}#root#c1", f"{vid}#root#c2"])
        self.assertEqual(len(await self.db.list_pool_videos(status="all")), 1)  # UNIQUE video_url: one record

    async def test_an_own_link_found_by_the_search_keeps_its_record(self) -> None:
        url = "https://www.youtube.com/watch?v=own_link_01"
        own = await self.db.add_video_to_pool(url, comments_target=3)
        await self.db.update_pool_video_progress(own.id, comments_added=3)

        item = await self.db.queue_search_video(url, comments_target=1)

        self.assertEqual((item.status, item.comments_done, item.comments_target), ("pending", 0, 1))  # for this run
        stored = (await self.db.list_pool_videos(status="all"))[0]
        self.assertEqual((stored.source, stored.status, stored.comments_done), ("links", "completed", 3))

    async def test_search_finds_are_not_part_of_the_own_links_run(self) -> None:
        await self.db.queue_search_video("https://www.youtube.com/watch?v=found_vid01", comments_target=1)
        await self.db.add_video_to_pool("https://www.youtube.com/watch?v=own_link_02", comments_target=1)
        pending = await self.db.list_pool_videos(status="pending", source="links")
        self.assertEqual([v.video_id for v in pending], ["own_link_02"])

    async def test_a_pasted_link_found_by_the_search_before_becomes_an_own_link(self) -> None:
        from modules.pool_manager import PoolManager

        url = "https://www.youtube.com/watch?v=found_vid02"
        found = await self.db.queue_search_video(url, comments_target=1)
        await self.db.update_pool_video_progress(found.id, comments_added=1)

        added = await PoolManager(self.db).add_videos_from_text(url, comments_target=2)

        self.assertEqual((added.added, added.existing), (1, 0))  # new to the user's own list
        stored = (await self.db.list_pool_videos(status="all"))[0]
        self.assertEqual((stored.source, stored.status, stored.comments_done, stored.comments_target), ("links", "pending", 0, 2))
        self.assertEqual((await self.db.get_next_pool_video()).video_id, "found_vid02")

    async def test_nothing_found_opens_no_browser(self) -> None:
        await self.db.upsert_account("acc_a", str(self.base_dir / "a"))
        orch = self.orchestrator(self.collector([], {}))
        result = await orch.run_search("nothing", max_views=100, max_videos=3)
        self.assertEqual(tuple(result), (1, 0, 0))
        self.assertEqual(self.posted, [])

    async def test_without_accounts_able_to_post_there_is_no_search(self) -> None:
        col = self.collector(_found("srch_vid_01"), {})
        result = await self.orchestrator(col).run_search("topic")
        self.assertEqual(tuple(result), (0, 0, 0))
        col.search_videos.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
