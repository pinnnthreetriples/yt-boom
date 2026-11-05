"""Visibility of published replies: config switch, `activity_log.visibility`, background checks in every run mode,
the «Запуск» tab (switch, journal markers, record, filter, counters) and the `--history` column."""
from __future__ import annotations

import asyncio
import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from config import AppConfig, LimitsSettings, load_config
from database import ActivityLogCreate, Database
from modules.collector import CollectedItem
from modules.comment_visibility import (
    CommentVisibilityChecker,
    VisibilityWatcher,
    make_visibility_checker,
    visibility_enabled,
    watch_and_record,
)
from modules.ui import icons
from modules.ui.app import AppContext, status_cells
from modules.ui.shell import Controller, frags_text, render
from modules.ui.tab_launch import build_tab
from modules.ui_test_runner import SubmissionResult
from youtube_bot import history_visibility_cell, print_activity_history, run_multi_video_pipeline, run_reply_bot


def _log(**extra) -> ActivityLogCreate:
    base = dict(video_id="vid", target_id="c1", mode="live", status="success", reply_text="Ответ")
    base.update(extra)
    return ActivityLogCreate(**base)


class TestConfigSwitch(unittest.TestCase):
    def test_defaults_to_on_so_old_configs_keep_working(self) -> None:
        limits = LimitsSettings()
        self.assertTrue(limits.visibility_check_enabled)
        self.assertEqual(limits.visibility_check_delay_seconds, 45.0)
        self.assertTrue(visibility_enabled(SimpleNamespace(limits=limits)))

    def test_switch_or_zero_delay_turns_the_check_off(self) -> None:
        self.assertFalse(visibility_enabled(SimpleNamespace(limits=LimitsSettings(visibility_check_enabled=False))))
        self.assertFalse(visibility_enabled(SimpleNamespace(limits=LimitsSettings(visibility_check_delay_seconds=0))))

    def test_factory_builds_a_checker_only_when_enabled(self) -> None:
        collector = object()
        on = make_visibility_checker(SimpleNamespace(limits=LimitsSettings(visibility_check_delay_seconds=7)), collector)
        self.assertIsInstance(on, CommentVisibilityChecker)
        self.assertEqual(on._delay, 7)
        self.assertIs(on._collector, collector)
        self.assertIsNone(make_visibility_checker(SimpleNamespace(limits=LimitsSettings(visibility_check_enabled=False)), collector))

    def test_example_config_has_both_keys(self) -> None:
        raw = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
        self.assertIs(raw["limits"]["visibility_check_enabled"], True)
        self.assertEqual(raw["limits"]["visibility_check_delay_seconds"], 45)
        self.assertIsInstance(load_config("config.example.yaml"), AppConfig)


class TestWatchLogging(unittest.IsolatedAsyncioTestCase):
    async def _watch(self, result):
        checker = CommentVisibilityChecker(MagicMock(), delay_seconds=0)
        checker.is_visible = AsyncMock(return_value=result)
        with patch("modules.comment_visibility.logger") as log:
            self.assertEqual(await checker.watch("vid", "Ugx.r", "acc"), result)
        return log

    async def test_seen_is_a_success_line(self) -> None:
        log = await self._watch(True)
        message = log.success.call_args.args[0]
        self.assertIn("[ВИДИМОСТЬ]", message)
        self.assertIn("виден всем", message)
        self.assertEqual(log.success.call_args.args[1:4], ("Ugx.r", "acc", "vid"))  # then looks made and seconds waited
        log.warning.assert_not_called()

    async def test_unseen_and_unknown_are_warnings_with_different_words(self) -> None:
        unseen = (await self._watch(False)).warning.call_args.args[0]
        unknown = (await self._watch(None)).warning.call_args.args[0]
        self.assertIn("не найден в анонимной выдаче", unseen)
        self.assertIn("не удалось проверить", unknown)
        self.assertNotIn("не найден", unknown)


class TestDatabaseColumn(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "v.db"

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def _open(self) -> Database:
        db = Database(self.path)
        await db.connect()
        await db.init_schema()
        return db

    async def test_old_database_without_the_column_is_migrated(self) -> None:
        db = await self._open()
        await db.log_activity(_log(target_id="old"))
        await db.close()
        conn = sqlite3.connect(self.path)
        conn.execute("ALTER TABLE activity_log DROP COLUMN visibility")
        conn.commit()
        self.assertNotIn("visibility", [r[1] for r in conn.execute("PRAGMA table_info(activity_log)")])
        conn.close()

        db = await self._open()  # init_schema adds it again, the old record is kept with NULL
        try:
            old = (await db.list_activity_logs())[0]
            self.assertEqual(old.target_id, "old")
            self.assertIsNone(old.visibility)
            await db.set_activity_visibility(old.id, "seen")
            self.assertEqual((await db.list_activity_logs())[0].visibility, "seen")
        finally:
            await db.close()
        db = await self._open()  # a second start is a no-op
        await db.close()

    async def test_set_validates_and_never_touches_status_or_counters(self) -> None:
        db = await self._open()
        try:
            entry = await db.log_activity(_log())
            self.assertIsNone(entry.visibility)
            before = await db.stats()
            for state in ("seen", "unseen", "unknown"):
                await db.set_activity_visibility(entry.id, state)
                row = (await db.list_activity_logs())[0]
                self.assertEqual((row.visibility, row.status), (state, "success"))
            for bad in ("hidden", "", "SEEN", "none"):
                with self.assertRaises(ValueError):
                    await db.set_activity_visibility(entry.id, bad)
            self.assertEqual((await db.list_activity_logs())[0].visibility, "unknown")
            self.assertEqual(await db.stats(), before)
        finally:
            await db.close()

    async def test_missing_record_is_a_silent_no_op(self) -> None:
        db = await self._open()
        try:
            await db.set_activity_visibility(9999, "seen")
            self.assertEqual(await db.list_activity_logs(), [])
        finally:
            await db.close()


class TestWatchAndRecord(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "w.db")
        await self.db.connect()
        await self.db.init_schema()
        self.entry = await self.db.log_activity(_log())

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp.cleanup()

    async def _state(self) -> str | None:
        return (await self.db.list_activity_logs())[0].visibility

    async def test_results_become_states(self) -> None:
        for result, state in ((True, "seen"), (False, "unseen"), (None, "unknown")):
            checker = SimpleNamespace(watch=AsyncMock(return_value=result))
            self.assertEqual(await watch_and_record(checker, self.db, self.entry.id, "vid", "Ugx.r", "acc"), state)
            self.assertEqual(await self._state(), state)
            checker.watch.assert_awaited_once_with("vid", "Ugx.r", "acc")

    async def test_a_crashing_checker_never_escapes_and_writes_nothing(self) -> None:
        checker = SimpleNamespace(watch=AsyncMock(side_effect=RuntimeError("boom")))
        self.assertIsNone(await watch_and_record(checker, self.db, self.entry.id, "vid", "Ugx.r", "acc"))
        self.assertIsNone(await self._state())

    async def test_a_failing_database_write_is_swallowed(self) -> None:
        db = SimpleNamespace(set_activity_visibility=AsyncMock(side_effect=sqlite3.OperationalError("locked")))
        checker = SimpleNamespace(watch=AsyncMock(return_value=True))
        self.assertIsNone(await watch_and_record(checker, db, 1, "vid", "Ugx.r", "acc"))

    async def test_cancellation_is_not_swallowed(self) -> None:
        checker = SimpleNamespace(watch=AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await watch_and_record(checker, self.db, self.entry.id, "vid", "Ugx.r", "acc")


class TestVisibilityWatcher(unittest.IsolatedAsyncioTestCase):
    def _checker(self, result=True, delay=0.0, gate: asyncio.Event | None = None):
        async def watch(*_args):
            if gate is not None:
                await gate.wait()
            return result

        return SimpleNamespace(_delay=delay, watch=AsyncMock(side_effect=watch))

    async def test_skips_with_a_reason_and_leaves_no_tasks(self) -> None:
        db = SimpleNamespace(set_activity_visibility=AsyncMock())
        checker = self._checker()
        cases = (
            (VisibilityWatcher(None, db), dict(log_id=1, remote_id="Ugx.r")),  # switched off
            (VisibilityWatcher(checker, db), dict(log_id=1, remote_id="Ugx.r", dry_run=True)),
            (VisibilityWatcher(checker, db), dict(log_id=1, remote_id=None)),
            (VisibilityWatcher(checker, db), dict(log_id=1, remote_id="")),
            (VisibilityWatcher(checker, db), dict(log_id=None, remote_id="Ugx.r")),
        )
        for watcher, kwargs in cases:
            self.assertIsNone(watcher.schedule(kwargs.pop("log_id"), "vid", kwargs.pop("remote_id"), "acc", **kwargs))
            self.assertFalse(watcher.tasks)
            await watcher.finish()
        checker.watch.assert_not_awaited()
        db.set_activity_visibility.assert_not_awaited()

    async def test_finish_waits_for_the_pending_check_and_the_result_is_stored(self) -> None:
        db = SimpleNamespace(set_activity_visibility=AsyncMock())
        gate = asyncio.Event()
        watcher = VisibilityWatcher(self._checker(False, gate=gate), db)
        task = watcher.schedule(5, "vid", "Ugx.r", "acc")
        self.assertIsNotNone(task)
        asyncio.get_running_loop().call_later(0.05, gate.set)
        await watcher.finish()
        self.assertTrue(task.done())
        db.set_activity_visibility.assert_awaited_once_with(5, "unseen")
        self.assertFalse(watcher.tasks)

    async def test_cancel_drops_the_wait_without_writing(self) -> None:
        db = SimpleNamespace(set_activity_visibility=AsyncMock())
        watcher = VisibilityWatcher(self._checker(gate=asyncio.Event()), db)  # never released
        task = watcher.schedule(5, "vid", "Ugx.r", "acc")
        await asyncio.wait_for(watcher.cancel(), timeout=2)
        self.assertTrue(task.cancelled())
        db.set_activity_visibility.assert_not_awaited()

    async def test_finish_never_waits_forever(self) -> None:
        db = SimpleNamespace(set_activity_visibility=AsyncMock())
        watcher = VisibilityWatcher(self._checker(gate=asyncio.Event()), db)
        task = watcher.schedule(5, "vid", "Ugx.r", "acc")
        real_wait_for = asyncio.wait_for
        limits: list[float] = []

        async def short(awaitable, timeout):
            limits.append(timeout)
            return await real_wait_for(awaitable, 0.05)

        with patch("modules.comment_visibility.asyncio.wait_for", short):
            await watcher.finish()
        self.assertEqual(limits, [60.0])  # delay (0) + 60 s
        self.assertTrue(task.cancelled())

    async def test_finish_gives_the_retries_time_but_still_has_a_bound(self) -> None:
        db = SimpleNamespace(set_activity_visibility=AsyncMock())
        checker = self._checker(gate=asyncio.Event())
        checker._delay, checker._retry_delays = 45.0, (120.0, 300.0)
        watcher = VisibilityWatcher(checker, db)
        task = watcher.schedule(5, "vid", "Ugx.r", "acc")
        real_wait_for = asyncio.wait_for
        limits: list[float] = []

        async def short(awaitable, timeout):
            limits.append(timeout)
            return await real_wait_for(awaitable, 0.05)

        with patch("modules.comment_visibility.asyncio.wait_for", short):
            await watcher.finish()
        self.assertEqual(limits, [45 + 120 + 300 + 60.0])
        self.assertTrue(task.cancelled())


class LoopCase(unittest.IsolatedAsyncioTestCase):
    """The two CLI loops of youtube_bot.py with a fake checker (no network, no waiting)."""

    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "t.db"
        self.config_path = self.root / "cfg.yaml"
        data = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
        data["app"]["db_path"] = str(self.db_path)
        data["app"]["log_file"] = str(self.root / "t.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["collector"]["target_mode"] = "raw"
        data["reply_mode"] = "ai"
        data["processor"]["reply_mode"] = "ai"
        data["limits"]["visibility_check_delay_seconds"] = 0.0
        self.data = data
        self.write_config()
        self.events: list[str] = []

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def write_config(self) -> None:
        self.config_path.write_text(yaml.safe_dump(self.data, allow_unicode=True), encoding="utf-8")

    def item(self, ident: str) -> CollectedItem:
        return CollectedItem(endpoint="youtube:test", external_id=ident, text="Комментарий", language="en",
                             confidence=0.99, author="@a", video_id="vid", is_reply=False)

    def checker(self, result=True, release: asyncio.Event | None = None):
        async def watch(video_id, remote_id, name):
            self.events.append("watch-start")
            if release is not None:
                await release.wait()
            await asyncio.sleep(0.05)  # a check still running when the last reply is posted
            self.events.append("watch-end")
            return result

        return SimpleNamespace(_delay=0.0, watch=AsyncMock(side_effect=watch))

    async def logs(self) -> list:
        db = Database(self.db_path)
        await db.connect()
        try:
            return await db.list_activity_logs()
        finally:
            await db.close()

    def run_single(self, checker, *, dry_run=False, remote_id="Ugx.reply", result=None, limit=1, items=("c1",)):
        async def close(*_a, **_k):
            self.events.append("collector-close")

        patches = [
            patch("youtube_bot.make_visibility_checker", return_value=checker),
            patch("modules.youtube_collector.YouTubeCollector.collect_from_videos",
                  AsyncMock(return_value=[self.item(i) for i in items])),
            patch("modules.processor.DeepSeekProcessor.generate", AsyncMock(return_value="Ответ бота")),
            patch("modules.youtube_runner.YouTubeRunner.run",
                  AsyncMock(side_effect=result) if isinstance(result, list)
                  else AsyncMock(return_value=SubmissionResult(success=True, remote_id=remote_id, duration_seconds=1.0))),
            patch("modules.youtube_collector.YouTubeCollector.close", AsyncMock(side_effect=close)),
            patch("youtube_bot.asyncio.sleep", AsyncMock()),
        ]
        return patches, dict(config_path=str(self.config_path), target_mode="raw", limit=limit, dry_run=dry_run)


class TestRunReplyBotVisibility(LoopCase):
    async def _run(self, checker, **kwargs):
        patches, call = self.run_single(checker, **kwargs)
        for p in patches:
            p.start()
        try:
            await run_reply_bot("https://www.youtube.com/watch?v=dQw4w9WgXcQ", **call)
        finally:
            for p in reversed(patches):
                p.stop()

    async def test_live_reply_is_checked_and_awaited_before_collector_and_db_close(self) -> None:
        checker = self.checker(False)
        await self._run(checker)
        checker.watch.assert_awaited_once_with("dQw4w9WgXcQ", "Ugx.reply", "default")
        self.assertEqual(self.events, ["watch-start", "watch-end", "collector-close"])  # waited, then closed
        rows = await self.logs()
        self.assertEqual((rows[0].status, rows[0].visibility), ("success", "unseen"))  # written before db.close()

    async def test_seen_and_unknown_results_are_recorded(self) -> None:
        for result, state in ((True, "seen"), (None, "unknown")):
            await self._run(self.checker(result))
            self.assertEqual((await self.logs())[0].visibility, state)
            Path(self.db_path).unlink()

    async def test_dry_run_is_not_checked(self) -> None:
        checker = self.checker()
        await self._run(checker, dry_run=True)
        checker.watch.assert_not_awaited()
        row = (await self.logs())[0]
        self.assertEqual((row.status, row.visibility), ("simulated", None))

    async def test_a_reply_without_an_id_is_not_checked(self) -> None:
        checker = self.checker()
        await self._run(checker, remote_id=None)
        checker.watch.assert_not_awaited()
        row = (await self.logs())[0]
        self.assertEqual((row.status, row.visibility), ("success", None))

    async def test_a_failed_submit_is_not_checked(self) -> None:
        checker = self.checker()
        await self._run(checker, result=[SubmissionResult(success=False, error="x", duration_seconds=1.0)])
        checker.watch.assert_not_awaited()
        self.assertEqual((await self.logs())[0].status, "failed")

    async def test_switched_off_in_the_config_means_no_check_at_all(self) -> None:
        self.data["limits"]["visibility_check_enabled"] = False
        self.write_config()
        with patch.object(CommentVisibilityChecker, "watch", AsyncMock()) as watch, \
             patch("modules.comment_visibility.CommentVisibilityChecker.is_visible", AsyncMock()) as probe:
            patches, call = self.run_single(None)
            patches = [p for p in patches if "make_visibility_checker" not in str(p.attribute)]  # the real factory
            for p in patches:
                p.start()
            try:
                await run_reply_bot("https://www.youtube.com/watch?v=dQw4w9WgXcQ", **call)
            finally:
                for p in reversed(patches):
                    p.stop()
        watch.assert_not_awaited()
        probe.assert_not_awaited()
        self.assertIsNone((await self.logs())[0].visibility)

    async def test_ctrl_c_cancels_the_waiting_checks_instead_of_waiting_them_out(self) -> None:
        checker = self.checker(release=asyncio.Event())  # never released
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(
                self._run(checker, limit=2, items=("c1", "c2"),
                          result=[SubmissionResult(success=True, remote_id="Ugx.a", duration_seconds=1.0),
                                  asyncio.CancelledError()]),
                timeout=10,
            )
        self.assertEqual(self.events.count("watch-end"), 0)
        self.assertIn("collector-close", self.events)  # still closed on the way out
        self.assertIsNone((await self.logs())[0].visibility)


class TestMultiVideoVisibility(LoopCase):
    async def _run(self, checker, dry_run=False):
        videos = [{"video_id": f"vm{i}", "title": f"T{i}", "url": f"https://www.youtube.com/watch?v=vm{i}",
                   "view_count": 5000} for i in (1, 2)]

        async def close(*_a, **_k):
            self.events.append("collector-close")

        with patch("youtube_bot.make_visibility_checker", return_value=checker), \
             patch("modules.youtube_collector.YouTubeCollector.search_videos", AsyncMock(return_value=videos)), \
             patch("modules.youtube_collector.YouTubeCollector.fetch_popular_comments",
                   AsyncMock(side_effect=[[self.item("vm1#c1")], [self.item("vm2#c2")]])), \
             patch("modules.processor.DeepSeekProcessor.generate", AsyncMock(return_value="Ответ бота")), \
             patch("modules.youtube_runner.YouTubeRunner.run",
                   AsyncMock(side_effect=lambda *a, **k: SubmissionResult(
                       success=True, remote_id=f"Ugx.{len(self.events)}", duration_seconds=1.0))), \
             patch("modules.youtube_collector.YouTubeCollector.close", AsyncMock(side_effect=close)), \
             patch("youtube_bot.asyncio.sleep", AsyncMock()):
            await run_multi_video_pipeline("q", config_path=str(self.config_path), max_videos=2, comments_per_video=1,
                                           target_mode="popular_parent", dry_run=dry_run)

    async def test_every_live_reply_is_checked_and_awaited_before_closing(self) -> None:
        checker = self.checker(True)
        await self._run(checker)
        self.assertEqual(checker.watch.await_count, 2)
        self.assertEqual(self.events[-1], "collector-close")
        self.assertEqual(self.events.count("watch-end"), 2)
        self.assertEqual([r.visibility for r in await self.logs()], ["seen", "seen"])

    async def test_dry_run_is_not_checked(self) -> None:
        checker = self.checker()
        await self._run(checker, dry_run=True)
        checker.watch.assert_not_awaited()
        self.assertEqual({r.visibility for r in await self.logs()}, {None})


# --------------------------------------------------------------------------- UI


class LaunchUiCase(unittest.TestCase):
    def setUp(self) -> None:
        icons.use_emoji(False)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "cfg.yaml"
        data = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
        data["app"]["db_path"] = str(self.root / "t.db")
        data["app"]["log_file"] = str(self.root / "t.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["browser"]["accounts"] = []
        data["browser"]["headless"] = False
        data["deepseek"]["api_key"] = "sk-test-key-123456"
        data["limits"]["max_concurrent_sessions"] = 1
        data["collector"]["search_query"] = "iphone 17 обзор"
        # the example file has no switch in the older config.yaml copies: make sure the key is absent like in them
        data["limits"].pop("visibility_check_enabled", None)
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        asyncio.run(self._init())
        self.ctx = AppContext(self.config_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def _init(self) -> None:
        from modules.projects import project_seed

        db = Database(str(self.root / "t.db"), project_seed=project_seed(load_config(self.config_path)))
        await db.connect()
        await db.init_schema()
        acc = await db.upsert_account("one", session_path=str(self.root / "sessions" / "one"))
        await db.add_project_accounts((await db.list_projects())[0].id, [acc.id])
        await db.close()

    def add(self, status: str, text: str, visibility: str | None = None, mode: str | None = None) -> None:
        conn = sqlite3.connect(self.root / "t.db")
        conn.execute(
            "INSERT INTO activity_log (created_at, video_id, target_id, mode, status, reply_text, error, target_author,"
            " video_title, visibility) VALUES (?, 'vid1', 'c1', ?, ?, ?, ?, '@a', 'Обзор', ?)",
            (datetime.now(timezone.utc).isoformat(), mode or ("dry_run" if status == "simulated" else "live"), status,
             None if status == "failed" else text, text if status == "failed" else None, visibility),
        )
        conn.commit()
        conn.close()

    def raw(self) -> dict:
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8"))

    def controller(self) -> Controller:
        return Controller([build_tab(self.ctx)], status=lambda: status_cells(self.ctx))

    def screen(self, c: Controller) -> str:
        return frags_text(render(c))

    def styles(self, c: Controller, needle: str) -> set[str]:
        """Styles of the rendered fragments that contain `needle`."""
        return {token.removeprefix("class:") for style, text, *_ in render(c) if needle in text for token in style.split()}

    def open_journal(self, c: Controller) -> None:
        while c.current_id() != "journal":
            c.press("down")
        c.press("enter")


class TestLaunchSwitch(LaunchUiCase):
    def test_switch_row_is_on_by_default_and_writes_the_config(self) -> None:
        c = self.controller()
        out = self.screen(c)
        self.assertIn("Проверка видимости: включена", out)
        c.press("down")  # start -> window
        c.press("down")  # window -> visibility
        self.assertEqual(c.current_id(), "visibility")
        c.press("right")
        self.assertIs(self.raw()["limits"]["visibility_check_enabled"], False)
        self.assertIn("Проверка видимости: выключена", self.screen(c))
        c.press("left")
        self.assertIs(self.raw()["limits"]["visibility_check_enabled"], True)
        self.assertIn("включена", self.screen(c))

    def test_hint_and_colours(self) -> None:
        c = self.controller()
        c.press("down")
        c.press("down")
        self.assertIn("После ответа бот через ~45 с смотрит, виден ли он другим зрителям", self.screen(c))
        self.assertIn("ok", self.styles(c, "включена"))
        c.press("right")
        self.assertIn("dim", self.styles(c, "выключена"))

    def test_window_and_threads_rows_keep_their_order(self) -> None:
        c = self.controller()
        c.press("down")
        self.assertEqual(c.current_id(), "window")

    def test_a_zero_delay_is_told_in_the_hint(self) -> None:
        self.ctx.update({"limits.visibility_check_delay_seconds": 0})
        c = self.controller()
        c.press("down")
        c.press("down")
        self.assertIn("задержкой", self.screen(c))


class TestJournalMarkers(LaunchUiCase):
    def test_each_state_has_its_marker_and_style(self) -> None:
        for visibility in ("seen", "unseen", "unknown", None):
            self.add("success", f"ответ-{visibility}", visibility)
        self.add("failed", "ошибка-X")
        self.add("simulated", "пробный-Y")
        c = self.controller()
        self.open_journal(c)
        out = self.screen(c)
        for line in out.splitlines():
            if "ответ-seen" in line:
                self.assertIn("●", line)
            if "ответ-unseen" in line or "ответ-unknown" in line:
                self.assertIn("!", line)
            if "ответ-None" in line:
                self.assertIn("—", line)
            if "ошибка-X" in line or "пробный-Y" in line:
                self.assertNotIn("—", line.split("ошибка-X")[0].split("пробный-Y")[0][3:])
        self.assertIn("ok", self.styles(c, "●"))
        self.assertIn("warn", self.styles(c, "!"))

    def test_journal_fits_80_columns(self) -> None:
        self.add("success", "Очень длинный ответ " * 8, "seen")
        c = self.controller()
        self.open_journal(c)
        for line in self.screen(c).splitlines():
            self.assertLessEqual(len(line), 80, line)

    def test_counters_count_confirmed_and_unconfirmed(self) -> None:
        for visibility in ("seen", "seen", "unseen", "unknown", None):
            self.add("success", "t", visibility)
        c = self.controller()
        self.assertIn("● 2", self.screen(c))  # «сегодня» line of the tab
        self.assertIn("! 2", self.screen(c))
        self.open_journal(c)
        out = self.screen(c)
        self.assertIn("● 2", out)
        self.assertIn("! 2", out)
        self.assertIn("ok", self.styles(c, "● 2"))
        self.assertIn("warn", self.styles(c, "! 2"))

    def test_filter_not_visible_shows_only_unconfirmed(self) -> None:
        self.add("success", "виден-1", "seen")
        self.add("success", "скрыт-2", "unseen")
        self.add("success", "неясно-3", "unknown")
        self.add("success", "не-проверен-4", None)
        self.add("failed", "ошибка-5")
        c = self.controller()
        self.open_journal(c)
        for _ in range(4):
            c.press("right")
        out = self.screen(c)
        self.assertIn("не видны", out)
        self.assertIn("скрыт-2", out)
        self.assertIn("неясно-3", out)
        for gone in ("виден-1", "не-проверен-4", "ошибка-5"):
            self.assertNotIn(gone, out)
        self.assertIn("стр. 1/1", out)
        c.press("left")  # errors only
        self.assertIn("ошибка-5", self.screen(c))
        self.assertNotIn("скрыт-2", self.screen(c))


class TestRecordCard(LaunchUiCase):
    def card(self, status: str, visibility: str | None, mode: str | None = None) -> tuple[str, Controller]:
        self.add(status, "текст", visibility, mode)
        c = self.controller()
        self.open_journal(c)
        c.press("down")  # filter -> first record
        c.press("enter")
        return self.screen(c), c

    def test_seen(self) -> None:
        out, c = self.card("success", "seen")
        self.assertIn("видимость: видна всем зрителям", out)
        self.assertIn("ok", self.styles(c, "видна всем зрителям"))

    def test_unseen(self) -> None:
        out, c = self.card("success", "unseen")
        self.assertIn("не найдена в анонимной выдаче", out)
        self.assertIn("скрыта YouTube", out)
        self.assertIn("виден только автору", out)
        self.assertIn("warn", self.styles(c, "не найдена в анонимной выдаче"))
        for line in out.splitlines():
            self.assertLessEqual(len(line), 80, line)

    def test_unknown(self) -> None:
        out, c = self.card("success", "unknown")
        self.assertIn("не удалось проверить", out)
        self.assertIn("warn", self.styles(c, "не удалось проверить"))

    def test_not_checked(self) -> None:
        out, c = self.card("success", None)
        self.assertIn("видимость: не проверялась", out)
        self.assertIn("dim", self.styles(c, "не проверялась"))
        out, _ = self.card("simulated", None)
        self.assertIn("не проверялась (пробный режим", out)


# --------------------------------------------------------------------------- --history


class TestHistoryColumn(unittest.IsolatedAsyncioTestCase):
    async def test_words_and_colour_rules(self) -> None:
        self.assertEqual(history_visibility_cell("seen", color=False), "виден   ")
        self.assertEqual(history_visibility_cell("unseen", color=False), "НЕ ВИДЕН")
        self.assertEqual(history_visibility_cell("unknown", color=False), "?       ")
        self.assertEqual(history_visibility_cell(None, color=False), "—       ")
        self.assertEqual(history_visibility_cell("seen", color=True), "\033[32mвиден   \033[0m")
        self.assertEqual(history_visibility_cell("unseen", color=True), "\033[33mНЕ ВИДЕН\033[0m")
        self.assertEqual(history_visibility_cell("unknown", color=True), "\033[33m?       \033[0m")
        self.assertEqual(history_visibility_cell(None, color=True), "—       ")  # no colour for «not checked»

    async def test_colour_only_on_a_tty_without_no_color(self) -> None:
        with patch("youtube_bot.sys.stdout", SimpleNamespace(isatty=lambda: True)), patch.dict("os.environ", {}, clear=False):
            with patch.dict("os.environ", {"NO_COLOR": ""}):
                self.assertIn("\033[", history_visibility_cell("seen"))
            with patch.dict("os.environ", {"NO_COLOR": "1"}):
                self.assertNotIn("\033[", history_visibility_cell("seen"))
        with patch("youtube_bot.sys.stdout", SimpleNamespace(isatty=lambda: False)):
            self.assertNotIn("\033[", history_visibility_cell("seen"))

    async def test_history_table_has_the_column(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = yaml.safe_load(Path("config.example.yaml").read_text(encoding="utf-8"))
            data["app"]["db_path"] = str(root / "h.db")
            data["app"]["log_file"] = str(root / "h.log")
            (root / "c.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            db = Database(root / "h.db")
            await db.connect()
            await db.init_schema()
            for i, state in enumerate(("seen", "unseen", "unknown", None)):
                entry = await db.log_activity(_log(target_id=f"c{i}", target_author=f"@auth{i}"))
                if state:
                    await db.set_activity_visibility(entry.id, state)
            await db.log_activity(_log(target_id="cs", target_author="@dry", mode="dry_run", status="simulated"))
            await db.close()
            buf = io.StringIO()
            with redirect_stdout(buf):  # stdout is not a tty here: plain words
                await print_activity_history(str(root / "c.yaml"))
            out = buf.getvalue()
        lines = {line.split("|")[6].strip(): line for line in out.splitlines() if "@" in line and "|" in line}
        self.assertIn("Видим.", out)
        for author, word in (("@auth0", "виден"), ("@auth1", "НЕ ВИДЕН"), ("@auth2", "?"), ("@auth3", "—"), ("@dry", "—")):
            self.assertEqual(lines[author].split("|")[3].strip(), word, author)
        self.assertNotIn("\033[", out)


if __name__ == "__main__":
    unittest.main()
