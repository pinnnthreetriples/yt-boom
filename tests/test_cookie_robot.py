"""Cookie robot: the site plan, the progress bar, one run over a fake browser (fail-safe, closing, proxy, banners)."""
from __future__ import annotations

import asyncio
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from config import BrowserSettings, TargetSettings
from database import AccountRecord
from modules import cookie_robot
from modules.cookie_robot import (
    GOOGLE_QUERIES,
    GOOGLE_SEARCH_BOX,
    SITE_POOL,
    CookieRobot,
    format_progress_bar,
    plan_sites,
)
from modules.page_helpers import CMP_ACCEPT_SELECTORS, CONSENT_ACCEPT_SELECTORS

TARGET = TargetSettings(
    name="youtube",
    base_url="https://www.youtube.com",
    item_url_template="https://www.youtube.com/watch?v={external_id}",
    submit_input_selector="#contenteditable-root",
    submit_button_selector="#submit-button button",
    confirmation_selector="ytd-comment-view-model",
)
REQUIRED = {"wikipedia.org", "reddit.com", "weather.com", "cnn.com", "yahoo.com", "bbc.com", "amazon.com"}


class TestSitePlan(unittest.TestCase):
    def test_pool_has_12_to_15_unique_trusted_sites(self) -> None:
        names = [site.name for site in SITE_POOL]
        self.assertTrue(12 <= len(names) <= 15)
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(REQUIRED <= set(names))
        self.assertNotIn("google.com", names)  # google.com is added by the plan, with its search
        for site in SITE_POOL:
            self.assertTrue(site.url.startswith("https://"))

    def test_plan_is_4_or_5_sites_with_google_first_or_second(self) -> None:
        sizes = set()
        for seed in range(300):
            sites = plan_sites(random.Random(seed))
            sizes.add(len(sites))
            names = [s.name for s in sites]
            self.assertEqual(len(names), len(set(names)))
            self.assertIn("google.com", names[:2])
            google = sites[names.index("google.com")]
            self.assertIn(google.query, GOOGLE_QUERIES)
            self.assertTrue(all(s.query is None for s in sites if s.name != "google.com"))
        self.assertEqual(sizes, {4, 5})

    def test_plans_differ_between_runs_but_repeat_for_one_seed(self) -> None:
        plans = {tuple(s.name for s in plan_sites(random.Random(seed))) for seed in range(50)}
        self.assertGreater(len(plans), 25)
        self.assertEqual(plan_sites(random.Random(7)), plan_sites(random.Random(7)))

    def test_count_is_kept_inside_the_pool(self) -> None:
        self.assertEqual(len(plan_sites(random.Random(1), count=3)), 3)
        self.assertEqual(len(plan_sites(random.Random(1), count=99)), len(SITE_POOL) + 1)


class TestProgressBar(unittest.TestCase):
    def test_example_from_the_spec(self) -> None:
        self.assertEqual(format_progress_bar(3, 5, "wikipedia.org"), "[=====>    ] 3/5 wikipedia.org")

    def test_edges(self) -> None:
        self.assertEqual(format_progress_bar(0, 5, "x"), "[          ] 0/5 x")
        self.assertEqual(format_progress_bar(5, 5, "x"), "[=========>] 5/5 x")
        self.assertEqual(format_progress_bar(9, 5, "x"), "[=========>] 5/5 x")  # never past the end
        self.assertEqual(len(format_progress_bar(2, 4, "")), len("[          ] 2/4"))


class TestRun(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.account = AccountRecord(id=1, name="acc", session_path=str(Path(self.tmp.name)), created_at="2026-01-01T00:00:00Z")
        self.cfg = BrowserSettings(headless=True, targets=[TARGET])
        self.buckets: list[dict] = []  # one per goto: what the robot did on that page
        self.consent_button: object | None = None
        self.search_box: object | None = MagicMock(name="search_box")

        self.page = AsyncMock()
        self.page.keyboard = MagicMock()
        self.page.keyboard.press = AsyncMock()
        self.page.goto = AsyncMock(side_effect=self._goto)
        self.goto_error: dict[int, Exception] = {}  # goto number (1-based) -> exception

        self.sim = MagicMock()
        self.sim.human_pause = AsyncMock()
        self.sim.click_locator = AsyncMock()
        self.sim.type_text = AsyncMock()
        self.sim.scroll = AsyncMock(side_effect=lambda page, px: self._bucket("scrolls", px))

        self.runner = AsyncMock()
        self.runner.context = MagicMock()
        self.runner.context.pages = [self.page]
        self.runner_cls = MagicMock(return_value=self.runner)

        async def sleep(seconds: float) -> None:
            self._bucket("sleeps", seconds)

        self.sleep = sleep

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _bucket(self, key: str, value) -> None:
        self.buckets[-1].setdefault(key, []).append(value)

    async def _goto(self, url: str, **kwargs):
        self.buckets.append({"url": url, "kwargs": kwargs})
        error = self.goto_error.get(len(self.buckets))
        if error is not None:
            raise error

    async def _first_visible(self, page, selectors, timeout_seconds=0.0, **_kw):
        chosen = (selectors,) if isinstance(selectors, str) else tuple(selectors)
        if chosen == GOOGLE_SEARCH_BOX:
            return self.search_box
        return self.consent_button

    def robot(self, seed: int = 3) -> CookieRobot:
        return CookieRobot(self.cfg, simulator=self.sim, rng=random.Random(seed), sleep=self.sleep)

    async def go(self, robot: CookieRobot | None = None, **kwargs):
        with patch("modules.cookie_robot.YouTubeRunner", self.runner_cls), patch(
            "modules.cookie_robot.first_visible", side_effect=self._first_visible
        ):
            return await (robot or self.robot()).run(self.account, **kwargs)

    async def test_visits_every_planned_site_and_reports_progress(self) -> None:
        calls: list[tuple[int, int, str]] = []
        result = await self.go(on_progress=lambda *args: calls.append(args))
        planned = plan_sites(random.Random(3))
        total = len(planned)
        self.assertTrue(result.success)
        self.assertEqual(len(result.visited), total)
        self.assertEqual(result.failed, ())
        self.assertEqual([c[0] for c in calls], list(range(1, total + 1)))
        self.assertEqual({c[1] for c in calls}, {total})
        self.assertEqual([c[2] for c in calls], list(result.visited))
        self.assertIn("google.com", [c[2] for c in calls][:2])
        self.runner.start.assert_awaited_once()
        self.runner.close.assert_awaited_once()

    async def test_stays_5_to_8_seconds_and_scrolls_once_or_twice(self) -> None:
        for seed in range(5):
            self.buckets.clear()
            await self.go(self.robot(seed))
            for bucket in self.buckets:
                self.assertTrue(5.0 <= sum(bucket["sleeps"]) <= 8.0 + 1e-6, bucket)
                self.assertIn(len(bucket["scrolls"]), (1, 2))
                self.assertTrue(all(300 <= px <= 700 for px in bucket["scrolls"]))
                self.assertEqual(bucket["kwargs"]["wait_until"], "domcontentloaded")
                self.assertTrue(10_000 <= bucket["kwargs"]["timeout"] <= 12_000)

    async def test_one_failing_site_does_not_stop_the_others(self) -> None:
        self.goto_error = {2: RuntimeError("net::ERR_CONNECTION_RESET")}
        calls: list[tuple[int, int, str]] = []
        result = await self.go(on_progress=lambda *args: calls.append(args))
        self.assertTrue(result.success)
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(len(result.visited) + len(result.failed), len(calls))
        self.assertEqual(result.failed[0], calls[1][2])
        self.assertIn(result.failed[0], result.details)
        self.runner.close.assert_awaited_once()

    async def test_every_site_failing_is_a_failed_run(self) -> None:
        self.goto_error = {n: asyncio.TimeoutError() for n in range(1, 8)}
        result = await self.go()
        self.assertFalse(result.success)
        self.assertEqual(result.visited, ())
        self.assertGreaterEqual(len(result.failed), 4)
        self.runner.close.assert_awaited_once()

    async def test_a_hanging_page_is_cut_off(self) -> None:
        async def hang(url, **kwargs):
            self.buckets.append({"url": url})
            await asyncio.sleep(30)

        self.page.goto = AsyncMock(side_effect=hang)
        with patch.multiple(
            cookie_robot, NAV_TIMEOUT_MIN=0.01, NAV_TIMEOUT_MAX=0.02, DWELL_MIN=0.0, DWELL_MAX=0.01, SITE_MARGIN=0.05
        ):
            result = await asyncio.wait_for(self.go(), timeout=10)
        self.assertFalse(result.success)
        self.assertEqual(len(result.failed), len(self.buckets))
        self.runner.close.assert_awaited_once()

    async def test_browser_is_closed_when_it_does_not_start(self) -> None:
        self.runner.start.side_effect = RuntimeError("camoufox did not start")
        result = await self.go()
        self.assertFalse(result.success)
        self.assertIn("camoufox did not start", result.details)
        self.page.goto.assert_not_awaited()
        self.runner.close.assert_awaited_once()

    async def test_browser_is_closed_when_the_run_is_cancelled(self) -> None:
        started = asyncio.Event()

        async def hang(url, **kwargs):
            started.set()
            await asyncio.sleep(30)

        self.page.goto = AsyncMock(side_effect=hang)
        task = asyncio.ensure_future(self.go())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.runner.close.assert_awaited_once()

    async def test_a_close_error_does_not_lose_the_result(self) -> None:
        self.runner.close.side_effect = RuntimeError("already closed")
        result = await self.go()
        self.assertTrue(result.success)

    async def test_runs_in_the_accounts_own_profile_with_its_proxy(self) -> None:
        self.account = self.account.model_copy(update={"proxy_url": "socks5://1.2.3.4:1080", "locale": "pl-PL"})
        await self.go()
        kwargs = self.runner_cls.call_args.kwargs
        self.assertEqual(kwargs["session_dir"], self.account.session_path)
        self.assertEqual(kwargs["proxy"], "socks5://1.2.3.4:1080")
        self.assertEqual(kwargs["locale"], "pl-PL")
        self.assertEqual(kwargs["account_name"], "acc")

    async def test_no_proxy_means_direct_not_the_global_one(self) -> None:
        self.cfg = BrowserSettings(headless=True, targets=[TARGET], proxy="socks5://9.9.9.9:1080")
        await self.go()
        self.assertIsNone(self.runner_cls.call_args.kwargs["proxy"])

    async def test_unreadable_proxy_fails_closed_without_a_browser(self) -> None:
        self.account = self.account.model_copy(update={"proxy_unavailable": True})
        result = await self.go()
        self.assertFalse(result.success)
        self.runner_cls.assert_not_called()

    async def test_cookie_banner_is_accepted(self) -> None:
        self.consent_button = MagicMock(name="accept")
        await self.go()
        clicked = [call.args[1] for call in self.sim.click_locator.await_args_list]
        self.assertIn(self.consent_button, clicked)

    async def test_banner_selectors_are_the_consent_ones(self) -> None:
        seen: list[tuple[str, ...]] = []

        async def spy(page, selectors, timeout_seconds=0.0, **_kw):
            seen.append((selectors,) if isinstance(selectors, str) else tuple(selectors))
            return None

        with patch("modules.cookie_robot.YouTubeRunner", self.runner_cls), patch("modules.cookie_robot.first_visible", spy):
            await self.robot().run(self.account)
        self.assertIn(CONSENT_ACCEPT_SELECTORS + CMP_ACCEPT_SELECTORS, seen)

    async def test_a_banner_that_cannot_be_clicked_is_not_an_error(self) -> None:
        self.consent_button = MagicMock(name="accept")
        self.sim.click_locator.side_effect = RuntimeError("detached")
        result = await self.go()
        self.assertTrue(result.success)
        self.assertEqual(result.failed, ())

    async def test_google_search_is_typed_into_the_search_box(self) -> None:
        result = await self.go()
        typed = [call.args[1] for call in self.sim.type_text.await_args_list]
        self.assertEqual(len(typed), 1)
        self.assertIn(typed[0], GOOGLE_QUERIES)
        self.page.keyboard.press.assert_awaited_with("Enter")
        self.assertTrue(result.success)

    async def test_google_search_falls_back_to_the_url(self) -> None:
        self.search_box = None
        result = await self.go()
        self.sim.type_text.assert_not_awaited()
        search_urls = [b["url"] for b in self.buckets if "/search?" in b["url"]]
        self.assertEqual(len(search_urls), 1)
        self.assertTrue(result.success)

    async def test_a_search_box_that_cannot_be_used_falls_back_to_the_url(self) -> None:
        self.sim.type_text.side_effect = RuntimeError("detached")
        result = await self.go()
        self.assertTrue(result.success)
        self.assertEqual(result.failed, ())
        self.assertEqual(len([b for b in self.buckets if "/search?" in b["url"]]), 1)

    async def test_a_search_that_did_not_reach_the_results_falls_back_to_the_url(self) -> None:
        # a covered search box types into nothing: the page never goes to /search
        self.page.wait_for_url = AsyncMock(side_effect=asyncio.TimeoutError())
        result = await self.go()
        self.assertTrue(result.success)
        self.page.wait_for_url.assert_awaited_once()
        self.assertEqual(self.page.wait_for_url.await_args.args[0], cookie_robot.GOOGLE_RESULTS_URL)
        self.assertEqual(len([b for b in self.buckets if "/search?" in b["url"]]), 1)

    async def test_banner_click_has_a_short_timeout(self) -> None:
        self.consent_button = MagicMock(name="accept")
        await self.go()
        banner_clicks = [c for c in self.sim.click_locator.await_args_list if c.args[1] is self.consent_button]
        self.assertTrue(banner_clicks)
        self.assertTrue(all(c.kwargs.get("timeout_ms") == cookie_robot.CLICK_TIMEOUT_MS for c in banner_clicks))

    async def test_a_browser_that_will_not_close_does_not_hang_the_run(self) -> None:
        async def hang() -> None:
            await asyncio.sleep(30)

        self.runner.close.side_effect = hang
        with patch.object(cookie_robot, "CLOSE_TIMEOUT", 0.05):
            result = await asyncio.wait_for(self.go(), timeout=10)
        self.assertTrue(result.success)

    async def test_start_error_is_one_line(self) -> None:
        self.runner.start.side_effect = RuntimeError("Timeout 30000ms exceeded.\nCall log:\n  - launching\n  - waiting")
        result = await self.go()
        self.assertFalse(result.success)
        self.assertNotIn("\n", result.details)
        self.assertIn("Call log: - launching - waiting", result.details)

    async def test_broken_progress_callback_does_not_stop_the_run(self) -> None:
        def broken(*_args) -> None:
            raise RuntimeError("bar crashed")

        result = await self.go(on_progress=broken)
        self.assertTrue(result.success)
        self.assertEqual(len(result.failed), 0)


class TestRunCookieRobot(unittest.IsolatedAsyncioTestCase):
    """`youtube_bot.run_cookie_robot`: several accounts, a pause only between runs that opened a browser."""

    async def test_pause_only_between_browser_runs(self) -> None:
        import youtube_bot

        accounts = {
            name: AccountRecord(id=i, name=name, session_path=f"s/{name}", created_at="2026-01-01T00:00:00Z")
            for i, name in enumerate(("a", "c"), 1)
        }

        class FakeDb:
            async def get_account(self, name):
                return accounts.get(name)

        class FakeContext:
            async def __aenter__(self):
                return FakeDb()

            async def __aexit__(self, *exc):
                return False

        robot = MagicMock()
        robot.run = AsyncMock(side_effect=lambda account, progress: cookie_robot.CookieRobotResult(account.name, True, ("x",), (), 0.1, "ok"))
        pauses: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            pauses.append(seconds)

        lines: list[str] = []
        progress: list[tuple] = []
        cfg = MagicMock()
        with patch.object(youtube_bot, "load_config", return_value=cfg), patch.object(
            youtube_bot, "_open_db", lambda _cfg: FakeContext()
        ), patch.object(youtube_bot, "CookieRobot", return_value=robot), patch.object(youtube_bot.asyncio, "sleep", fake_sleep):
            results = await youtube_bot.run_cookie_robot(
                "cfg.yaml", ["missing", "a", "c"], on_progress=lambda *args: progress.append(args), report=lines.append
            )
        self.assertEqual([r.success for r in results], [False, True, True])
        self.assertEqual(results[0].details, "account not found")
        self.assertEqual(len(pauses), 1)  # a missing account costs no pause; the first real run neither
        self.assertEqual(len(lines), 3)
        # the account's name is put in front of the robot's (step, total, site)
        robot.run.await_args_list[0].args[1](2, 4, "google.com")
        self.assertEqual(progress, [("a", 2, 4, "google.com")])


if __name__ == "__main__":
    unittest.main()
