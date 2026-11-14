"""Projects, launch: one project's run (members, pool, settings, references), the queue of projects and its stop,
one reply per comment across projects, the project's daily ceiling on top of the account's limits, and the
reference links (similar videos, title words) on a saved InnerTube response."""
from __future__ import annotations

import json
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from config import load_config
from database import Database
from modules.collector import CollectedItem
from modules.multi_account_orchestrator import MultiAccountOrchestrator, ProjectRunResult
from modules.processor import DeepSeekProcessor
from modules.ui_test_runner import SubmissionResult
from modules.youtube_collector import YouTubeCollector, title_keywords
from tests.test_multi_account_orchestrator import _make_test_config

FIXTURE = Path(__file__).parent / "fixtures" / "innertube_next_watch.json"


def watch(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def comments(video_id: str, count: int = 3) -> list[CollectedItem]:
    return [
        CollectedItem(
            endpoint=f"youtube:{video_id}", external_id=f"{video_id}#root#c{n}", text=f"Comment {n} under {video_id}",
            language="en", confidence=0.9, source_url=watch(video_id), author=f"user{n}", video_id=video_id,
            like_count=1,
        )
        for n in range(count)
    ]


def found(video_id: str, views: int = 50_000) -> dict:
    return {"video_id": video_id, "url": watch(video_id), "title": f"Video {video_id}", "view_count": views}


class FakeCollector:
    """Search / recommendations / titles from dicts; three comments under every video."""

    def __init__(self, searches=None, related=None, titles=None):
        self.searches = searches or {}
        self.related = related or {}
        self.titles = titles or {}
        self.search_calls: list[dict] = []
        self.related_calls: list[dict] = []

    async def search_videos(self, query, **kwargs):
        self.search_calls.append({"query": query, **kwargs})
        return list(self.searches.get(query, []))[: kwargs.get("limit", 99)]

    async def related_videos(self, video_id, **kwargs):
        self.related_calls.append({"video_id": video_id, **kwargs})
        return list(self.related.get(video_id, []))[: kwargs.get("limit", 99)]

    async def video_title(self, video_id):
        return self.titles.get(video_id, "")

    async def fetch_popular_comments(self, video_url, limit=4, target_mode="first_reply", skip=()):
        return comments(video_url.split("v=")[-1])

    async def close(self):
        pass


class RunCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config = _make_test_config(self.root)
        self.config.limits.pause_min_seconds = 0
        self.config.limits.pause_max_seconds = 0
        self.db = Database(self.root / "test.db")
        await self.db.connect()
        await self.db.init_schema()
        self.posted: list[tuple[str, str, str]] = []  # (account, comment id, text)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp_dir.cleanup()

    def runner_factory(self, acc):
        async def run(text, external_id=None, payload_id=None, dry_run=False):
            self.posted.append((acc.name, external_id, text))
            return SubmissionResult(success=True, remote_id=f"r_{external_id}", duration_seconds=0.01)

        return MagicMock(run=AsyncMock(side_effect=run), close=AsyncMock())

    def orchestrator(self, project, collector=None, dry_run=True, claimed=None, account_mode="each"):
        return MultiAccountOrchestrator(
            self.config, db=self.db, dry_run=dry_run, account_mode=account_mode, collector=collector or FakeCollector(),
            runner_factory=self.runner_factory, rng=random.Random(1), project=project, claimed=claimed,
        )

    async def account(self, name: str, mode: str | None = None, hours: float | None = None) -> int:
        acc = await self.db.upsert_account(name, str(self.root / name))
        if hours is not None:
            await self.db.mark_account_authenticated(acc.id)
            stamped = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
            async with self.db._transaction() as conn:
                await conn.execute("UPDATE accounts SET authenticated_at = ? WHERE id = ?", (stamped, acc.id))
        if mode:
            await self.db.set_account_lifecycle_mode(acc.id, mode)
        return acc.id

    async def project(self, name: str, members: list[int], links: list[str] = (), **fields):
        fields.setdefault("reply_mode", "custom")
        fields.setdefault("custom_replies", [f"ответ {name}"])
        project = await self.db.create_project(name, **fields)
        await self.db.add_project_accounts(project.id, members)
        for video_id in links:
            await self.db.add_video_to_pool(watch(video_id), project.comments_per_video, project_id=project.id)
        return await self.db.get_project(project.id)


class ProjectRunTest(RunCase):
    async def test_only_members_and_the_projects_own_pool(self):
        a, b, c = await self.account("a"), await self.account("b"), await self.account("c")
        await self.db.add_video_to_pool(watch("mainvideo01"), 2)  # «Основной»: not this project's
        project = await self.project("Крипта", [a, b], ["cryptovid01"], comments_per_video=1)
        result = await self.orchestrator(project).run_project()
        self.assertEqual(result, ProjectRunResult("Крипта", 2, 1, 2))
        self.assertEqual({name for name, _, _ in self.posted}, {"a", "b"})
        self.assertTrue(all(cid.startswith("cryptovid01") for _, cid, _ in self.posted))
        log = await self.db.list_activity_logs()
        self.assertEqual({entry.project_id for entry in log}, {project.id})
        self.assertEqual({entry.account_id for entry in log}, {a, b})
        self.assertNotIn(c, {entry.account_id for entry in log})

    async def test_each_project_answers_with_its_own_templates(self):
        a = await self.account("a")
        p1 = await self.project("P1", [a], ["videoaaaaa1"], custom_replies=["шаблон первого"], comments_per_video=1)
        p2 = await self.project("P2", [a], ["videobbbbb1"], custom_replies=["шаблон второго"], comments_per_video=1)
        for project in (p1, p2):
            orchestrator = self.orchestrator(project)
            self.assertIsNone(orchestrator._processor)  # templates as they are: no DeepSeek client at all
            await orchestrator.run_project()
        self.assertEqual([text for _, _, text in self.posted], ["шаблон первого", "шаблон второго"])
        self.assertEqual(self.config.processor.custom_replies, ["Nice point!", "Interesting thought!"])

    async def test_an_ai_project_gets_the_deepseek_processor(self):
        a = await self.account("a")
        project = await self.project("AI", [a], reply_mode="ai", custom_replies=[])
        orchestrator = self.orchestrator(project)
        try:
            self.assertIsInstance(orchestrator._processor, DeepSeekProcessor)
            self.assertEqual(orchestrator.config.processor.reply_mode, "ai")
            self.assertEqual(orchestrator.config.collector.max_videos, project.max_videos)
        finally:
            await orchestrator._processor.close()

    async def test_without_usable_members_nothing_runs(self):
        a = await self.account("a")
        await self.db.set_account_active(a, False)
        project = await self.project("P", [a], ["videoaaaaa1"])
        result = await self.orchestrator(project).run_project()
        self.assertEqual((result.accounts, result.skipped), (0, "нет аккаунтов, которые могут отвечать сейчас"))
        self.assertEqual(self.posted, [])

    async def test_topics_references_and_links_in_order_without_duplicates(self):
        a = await self.account("a")
        project = await self.project(
            "P", [a], ["ownlink0001", "ownlink0002"], search_topics=["биткоин", "эфир"], use_similar=True,
            use_title_words=True, max_videos=2, min_views=1000, max_age_hours=48, comments_per_video=1,
        )
        collector = FakeCollector(
            searches={
                "биткоин": [found("topicvid001"), found("bothvideo01")],
                "эфир": [found("bothvideo01"), found("topicvid002")],
                "Ethereum CRASH": [found("titlevid001"), found("ownlink0001")],
            },
            related={"ownlink0002": [found("ownlink0001"), found("similarvi01"), found("topicvid001"), found("similarvi02"),
                                     found("similarvi03")]},
            titles={"ownlink0002": "🔥 Ethereum CRASH today?! #crypto (4K)"},
        )
        orchestrator = self.orchestrator(project, collector)
        videos = await orchestrator._collect_project_videos(project.search_topics)
        self.assertEqual(
            [v.video_id for v in videos],
            ["topicvid001", "bothvideo01", "topicvid002", "similarvi01", "similarvi02", "titlevid001",
             "ownlink0001", "ownlink0002"],
        )
        self.assertEqual({v.source for v in videos[:6]}, {"search"})
        self.assertEqual({v.project_id for v in videos}, {project.id})
        self.assertEqual(collector.search_calls[0],
                         {"query": "биткоин", "limit": 2, "min_views": 1000, "max_views": None, "max_age_hours": 48})
        self.assertEqual(collector.related_calls[0]["video_id"], "ownlink0002")  # the newest own link first
        self.assertEqual([c["query"] for c in collector.search_calls], ["биткоин", "эфир", "Ethereum CRASH"])

    async def test_references_need_own_links_and_skip_a_title_without_words(self):
        a = await self.account("a")
        project = await self.project("P", [a], search_topics=["x"], use_similar=True, use_title_words=True)
        collector = FakeCollector(searches={"x": [found("xvideo00001")]})
        await self.orchestrator(project, collector)._collect_project_videos(project.search_topics)
        self.assertEqual(collector.related_calls, [])
        project = await self.project("Q", [a], ["ownlink0001"], use_title_words=True)
        collector = FakeCollector(titles={"ownlink0001": "😂😂😂 #shorts"})
        videos = await self.orchestrator(project, collector)._collect_project_videos([])
        self.assertEqual([v.video_id for v in videos], ["ownlink0001"])
        self.assertEqual(collector.search_calls, [])

    async def test_one_comment_gets_one_reply_across_projects(self):
        a, b = await self.account("a"), await self.account("b")
        p1 = await self.project("P1", [a], ["sharedvid01"], comments_per_video=3)
        p2 = await self.project("P2", [b], ["sharedvid01"], comments_per_video=3)
        claimed: set[str] = set()
        first = await self.orchestrator(p1, claimed=claimed).run_project()
        second = await self.orchestrator(p2, claimed=claimed).run_project()
        self.assertEqual((first.replies, second.replies), (3, 0))  # a dry run writes no payloads: the launch's set
        self.assertEqual(len({cid for _, cid, _ in self.posted}), 3)

    async def test_live_replies_are_global_by_comment(self):
        a, b = await self.account("a", "unlimited", 100), await self.account("b", "unlimited", 100)
        p1 = await self.project("P1", [a], ["sharedvid01"], comments_per_video=2)
        p2 = await self.project("P2", [b], ["sharedvid01"], comments_per_video=3)
        await self.orchestrator(p1, dry_run=False).run_project()
        await self.orchestrator(p2, dry_run=False).run_project()  # a new launch: only the payloads remember
        ids = [cid for _, cid, _ in self.posted]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 3)


class CeilingTest(RunCase):
    async def test_the_project_ceiling_stops_an_unlimited_account(self):
        a = await self.account("a", "unlimited", 100)
        project = await self.project("P", [a], ["videoaaaaa1", "videobbbbb1"], comments_per_video=3, daily_per_account=2)
        result = await self.orchestrator(project, dry_run=False).run_project()
        self.assertEqual(result.replies, 2)
        self.assertEqual(await self.db.project_daily_gate(project.id, a), (2, 2, False))
        account = await self.db.get_account(a)
        self.assertEqual(account.daily_comments_done, 2)  # the account's own counter counts the same replies

    async def test_the_accounts_own_limit_holds_without_a_project_ceiling(self):
        a = await self.account("a", "mature", 100)
        async with self.db._transaction() as conn:  # five replies today already: the mature stage limit
            reset = Database.next_daily_reset(datetime.now(timezone.utc)).isoformat()
            await conn.execute("UPDATE accounts SET daily_comments_done = 5, daily_limit_reset_at = ? WHERE id = ?",
                               (reset, a))
        project = await self.project("P", [a], ["videoaaaaa1"], comments_per_video=3)
        result = await self.orchestrator(project, dry_run=False).run_project()
        self.assertEqual((result.accounts, result.replies), (0, 0))

    async def test_the_ceiling_is_per_project(self):
        a = await self.account("a", "unlimited", 100)
        p1 = await self.project("P1", [a], ["videoaaaaa1"], comments_per_video=3, daily_per_account=1)
        p2 = await self.project("P2", [a], ["videobbbbb1"], comments_per_video=3, daily_per_account=2)
        self.assertEqual((await self.orchestrator(p1, dry_run=False).run_project()).replies, 1)
        self.assertEqual((await self.orchestrator(p2, dry_run=False).run_project()).replies, 2)


class PoolRecoveryTest(RunCase):
    async def test_a_video_left_in_progress_is_taken_again(self):
        project = await self.project("P", [], ["videoaaaaa1"], comments_per_video=2)
        video = (await self.db.list_pool_videos(project_id=project.id))[0]
        await self.db.update_pool_video_progress(video.id, comments_added=1)  # a run stopped after one reply
        self.assertEqual((await self.db.get_next_pool_video(project.id)).status, "in_progress")
        self.assertEqual(len(await self.db.list_pool_videos("pending", "links", project.id)), 1)
        found_again = await self.db.queue_search_video(watch("searchvid01"), 2, project_id=project.id)
        await self.db.update_pool_video_progress(found_again.id, comments_added=1)
        again = await self.db.queue_search_video(watch("searchvid01"), 2, project_id=project.id)
        self.assertEqual((again.status, again.comments_done), ("pending", 0))


class QueueTest(unittest.IsolatedAsyncioTestCase):
    """youtube_bot.run_projects: the queue, skips, the shared comment set, the stop on an error or Ctrl+C."""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        data = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
        data["app"]["db_path"] = str(root / "q.db")
        data["app"]["log_file"] = str(root / "q.log")
        data["browser"]["session_root"] = str(root / "sessions")
        self.config_path = root / "q.yaml"
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.db = Database(load_config(self.config_path).app.db_path)
        await self.db.connect()
        await self.db.init_schema()
        acc = (await self.db.upsert_account("a", str(root / "a"))).id
        main = (await self.db.list_projects())[0]
        await self.db.update_project(main.id, enabled=False)
        self.ids = {}
        for name, fields in (("Первый", {}), ("Второй", {}), ("Третий", {}), ("Выключен", {"enabled": False}),
                             ("Без шаблонов", {"reply_mode": "custom"})):
            project = await self.db.create_project(name, search_topics=["тема"], **fields)
            await self.db.add_project_accounts(project.id, [acc])
            self.ids[name] = project.id
        self.ids["Пустой"] = (await self.db.create_project("Пустой", search_topics=["тема"])).id
        await self.db.close()
        self.started: list[str] = []
        self.closed: list[str] = []
        self.claimed_sets: list[int] = []
        self.fail_on: str | None = None
        self.interrupt_on: str | None = None

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    def fake_orchestrator(self):
        test = self

        class Fake:
            def __init__(self, *args, project=None, claimed=None, db=None, **kwargs):
                self.project, self.db = project, db
                test.claimed_sets.append(id(claimed))

            async def run_project(self, topics=None):
                test.started.append(self.project.name)
                if self.project.name == test.fail_on:
                    raise RuntimeError("браузер упал")
                if self.project.name == test.interrupt_on:
                    raise KeyboardInterrupt
                return ProjectRunResult(self.project.name, 1, 2, 3)

            async def close(self):
                test.closed.append(self.project.name)
                await self.db.close()

        return Fake

    async def launch(self, **kwargs):
        from youtube_bot import run_projects

        with patch("modules.multi_account_orchestrator.MultiAccountOrchestrator", self.fake_orchestrator()):
            return await run_projects(str(self.config_path), dry_run=True, **kwargs)

    async def test_marked_projects_run_in_order_and_the_others_are_skipped_with_a_reason(self):
        summary = await self.launch()
        self.assertEqual(self.started, ["Первый", "Второй", "Третий"])
        self.assertEqual(self.closed, self.started)
        self.assertEqual(len(set(self.claimed_sets)), 1)  # one comment set for the whole launch
        self.assertEqual([(r.project, r.skipped) for r in summary.results],
                         [("Первый", ""), ("Второй", ""), ("Третий", ""), ("Без шаблонов", "нет шаблонов"),
                          ("Пустой", "нет аккаунтов")])
        self.assertIsNone(summary.stopped_at)

    async def test_an_error_stops_the_queue_on_that_project(self):
        self.fail_on = "Второй"
        summary = await self.launch()
        self.assertEqual(self.started, ["Первый", "Второй"])
        self.assertEqual(self.closed, ["Первый", "Второй"])
        self.assertEqual(summary.stopped_at, "Второй")
        self.assertIn("браузер упал", summary.error)

    async def test_ctrl_c_closes_the_current_project_and_propagates(self):
        self.interrupt_on = "Первый"
        progress: dict = {}
        with self.assertRaises(KeyboardInterrupt):
            await self.launch(progress=progress)
        self.assertEqual(self.closed, ["Первый"])
        self.assertEqual(progress["project"], "Первый")

    async def test_explicit_projects_and_a_topic_for_this_launch_only(self):
        summary = await self.launch(project_ids=[self.ids["Третий"], self.ids["Выключен"]], topics=["другая тема"])
        self.assertEqual(self.started, ["Третий", "Выключен"])
        self.assertEqual(len(summary.results), 2)
        db = Database(load_config(self.config_path).app.db_path)
        await db.connect()
        try:
            self.assertEqual((await db.get_project(self.ids["Третий"])).search_topics, ["тема"])
        finally:
            await db.close()


class ReferenceParsingTest(unittest.IsolatedAsyncioTestCase):
    """related_videos / video_title on a saved (trimmed) InnerTube `next` response of a real watch page."""

    async def asyncSetUp(self):
        self.collector = YouTubeCollector(load_config("config.yaml").collector)
        self.collector._watch_next = AsyncMock(return_value=json.loads(FIXTURE.read_text(encoding="utf-8")))

    async def test_title_and_recommendations(self):
        self.assertEqual(await self.collector.video_title("dQw4w9WgXcQ"),
                         "Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)")
        related = await self.collector.related_videos(watch("dQw4w9WgXcQ"), limit=20)
        self.assertEqual(len(related), 8)
        self.assertNotIn("dQw4w9WgXcQ", [v["video_id"] for v in related])
        first = related[0]
        self.assertEqual((first["video_id"], first["view_count"], first["views_text"], first["channel"],
                          first["published_time"]),
                         ("yPYZpwSpKmA", 208_000_000, "208M", "Rick Astley", "17 years ago"))

    async def test_filters_apply_to_recommendations(self):
        related = await self.collector.related_videos("dQw4w9WgXcQ", limit=20, min_views=1_000_000, max_views=100_000_000)
        self.assertEqual([v["view_count"] for v in related], [4_600_000, 5_700_000, 33_000_000])
        fresh = await self.collector.related_videos("dQw4w9WgXcQ", limit=20, max_age_hours=72)
        self.assertEqual([v["published_time"] for v in fresh], ["2 days ago", "Streamed 8 hours ago"])
        self.assertEqual(len(await self.collector.related_videos("dQw4w9WgXcQ", limit=3)), 3)

    async def test_an_unreadable_page_gives_nothing(self):
        self.collector._watch_next = AsyncMock(return_value=None)
        self.assertEqual(await self.collector.related_videos("dQw4w9WgXcQ"), [])
        self.assertEqual(await self.collector.video_title("dQw4w9WgXcQ"), "")
        self.assertEqual(await self.collector.related_videos("not a video"), [])

    async def test_watch_next_http_error_is_none_and_success_is_cached(self):
        collector = YouTubeCollector(load_config("config.yaml").collector)
        collector._api_key, collector._client_version = "k", "v"
        collector._session = MagicMock(post=AsyncMock(return_value=SimpleNamespace(status_code=500)))
        self.assertIsNone(await collector._watch_next("dQw4w9WgXcQ"))
        ok = SimpleNamespace(status_code=200, json=lambda: {"x": 1})
        collector._session = MagicMock(post=AsyncMock(return_value=ok))
        self.assertEqual(await collector._watch_next("dQw4w9WgXcQ"), {"x": 1})
        await collector._watch_next("dQw4w9WgXcQ")
        self.assertEqual(collector._session.post.await_count, 1)


class TitleKeywordsTest(unittest.TestCase):
    def test_english_title(self):
        self.assertEqual(title_keywords("Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)"),
                         "Rick Astley Never Gonna Give Up")

    def test_russian_title_with_emoji_and_hashtags(self):
        self.assertEqual(title_keywords("🔥🔥 БИТКОИН 🚀 рухнул! Что будет дальше? #crypto #btc"), "БИТКОИН рухнул дальше")

    def test_numbers_stay_only_after_a_word(self):
        self.assertEqual(title_keywords("iPhone 16 Pro Max — обзор | Стоит ли покупать в 2024?"),
                         "iPhone 16 Pro Max обзор Стоит")
        self.assertEqual(title_keywords("Top 10 best moments 1080p 60fps"), "moments")

    def test_only_emoji_or_tags_give_nothing(self):
        for title in ("😂😂😂", "#shorts #viral", "", None, "[Official Video]", "| — |", "12 34"):
            self.assertEqual(title_keywords(title), "", repr(title))

    def test_a_bracketed_title_keeps_its_words(self):
        self.assertEqual(title_keywords("[Эфириум прогноз]"), "Эфириум прогноз")

    def test_a_very_long_title_is_cut_to_six_words(self):
        title = " ".join(f"слово{n}" for n in range(40)) + " https://t.me/x @channel"
        self.assertEqual(title_keywords(title).split(), [f"слово{n}" for n in range(6)])

    def test_duplicates_are_dropped(self):
        self.assertEqual(title_keywords("Bitcoin bitcoin BITCOIN news"), "Bitcoin news")


if __name__ == "__main__":
    unittest.main()
