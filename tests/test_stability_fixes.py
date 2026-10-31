import asyncio
import ctypes
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from config import BrowserSettings, TargetSettings, ValidatorSettings
from database import AccountRecord, Payload
from modules.interaction_simulator import InteractionConfig, InteractionSimulator
from modules.multi_account_orchestrator import MultiAccountOrchestrator
from modules import process_guard
from modules.process_guard import (
    JobBasicLimits,
    JobExtendedLimits,
    bind_to_kill_on_close_job,
    configure_kill_on_close_job,
)
from modules.validator import DeliveryValidator
from modules.wizard import check_youtube_session
from modules.youtube_runner import YouTubeRunner

_PAYLOAD = dict(
    node_id=1, source_text=None, language="en", status="submitted", error=None,
    created_at=0.0, submitted_at=0.0, validated_at=None,
)


def _payload(**overrides) -> Payload:
    data = dict(_PAYLOAD, id=7, source_ref="vid12345678#parent1", text="Nice video, thanks", remote_id="UgzABC.9xyz")
    data.update(overrides)
    return Payload(**data)


def _youtube_page(title: str, description: str) -> str:
    return (
        f'<html><head><meta property="og:title" content="{title}">'
        f'<meta content="{description}" property="og:description"></head></html>'
    )


class TestRunnerBrowserConfig(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        cfg = BrowserSettings(
            headless=True,
            session_root=Path(self.temp_dir.name),
            targets=[TargetSettings(
                name="youtube", base_url="https://www.youtube.com", submit_input_selector="i",
                submit_button_selector="b", confirmation_selector="c",
            )],
        )
        fingerprint_patch = patch("modules.youtube_runner.get_or_create_fingerprint", return_value={"pinned": True})
        fingerprint_patch.start()  # _launch_options must not generate a real fingerprint (0.4 s, needs the fpgen data)
        self.addCleanup(fingerprint_patch.stop)
        self.runner = YouTubeRunner(cfg, cfg.targets[0])

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_resource_and_background_prefs(self):
        prefs = self.runner._launch_options()["firefox_user_prefs"]
        self.assertEqual(prefs["media.volume_scale"], "0.0")
        self.assertIs(prefs["network.proxy.socks_remote_dns"], True)
        self.assertIs(prefs["widget.windows.window_occlusion_tracking.enabled"], False)
        self.assertIs(prefs["dom.timeout.enable_budget_timer_throttling"], False)

    async def test_start_does_not_patch_dom_prototypes(self):
        context = MagicMock(pages=[], add_init_script=AsyncMock(), new_page=AsyncMock())
        context.new_page.return_value = MagicMock(set_default_timeout=MagicMock(), set_default_navigation_timeout=MagicMock())
        camoufox = MagicMock(__aenter__=AsyncMock(return_value=context), __aexit__=AsyncMock())
        with patch("modules.youtube_runner.AsyncCamoufox", return_value=camoufox):
            await self.runner.start()
        context.add_init_script.assert_not_called()

    async def test_remote_id_taken_from_create_comment_response(self):
        body = '{"actions":[{"createCommentAction":{"commentId":"UgzQ1w.AbC-9_x","other":"x"}}]}'
        response = MagicMock(text=AsyncMock(return_value=body))
        self.assertEqual(await YouTubeRunner._extract_remote_id(response), "UgzQ1w.AbC-9_x")
        response = MagicMock(text=AsyncMock(return_value='{"commentId":"nope"}'))
        self.assertIsNone(await YouTubeRunner._extract_remote_id(response))

    async def test_channel_creation_dialog_is_confirmed_and_reply_reclicked(self):
        reply_button, reply_input = MagicMock(name="reply"), MagicMock(name="input")
        sim = self.runner._simulator
        with patch.object(self.runner, "_find_sub_comment_reply_button", AsyncMock(return_value=reply_button)), \
             patch.object(self.runner, "_wait_reply_input", AsyncMock(side_effect=[None, reply_input])), \
             patch.object(self.runner, "_confirm_channel_creation", AsyncMock(return_value=True)) as confirm, \
             patch.object(sim, "click_locator", new_callable=AsyncMock) as click, \
             patch.object(sim, "human_pause", new_callable=AsyncMock), \
             patch.object(sim, "thinking_pause", new_callable=AsyncMock), \
             patch.object(sim, "type_text", new_callable=AsyncMock):
            await self.runner._submit_reply(MagicMock(), "text", dry_run=True)
        confirm.assert_awaited_once()
        reply_clicks = [c for c in click.await_args_list if c.args[1] is reply_button]
        self.assertEqual(len(reply_clicks), 2)

    async def test_confirm_channel_creation_clicks_create_button(self):
        dialog = MagicMock(wait_for=AsyncMock())
        button = MagicMock()
        sim = self.runner._simulator
        with patch.object(self.runner, "_first_visible", AsyncMock(side_effect=[dialog, button])), \
             patch.object(sim, "click_locator", new_callable=AsyncMock) as click, \
             patch.object(sim, "human_pause", new_callable=AsyncMock):
            self.assertTrue(await self.runner._confirm_channel_creation(MagicMock()))
        click.assert_awaited_once()
        self.assertIs(click.await_args.args[1], button)
        dialog.wait_for.assert_awaited_once_with(state="hidden", timeout=15000)

    async def test_confirm_channel_creation_without_dialog(self):
        with patch.object(self.runner, "_first_visible", AsyncMock(return_value=None)):
            self.assertFalse(await self.runner._confirm_channel_creation(MagicMock()))


class TestTypeTextAllowTypos(unittest.IsolatedAsyncioTestCase):
    async def test_no_typos_when_disabled(self):
        page = MagicMock(is_closed=MagicMock(return_value=False))
        page.keyboard = MagicMock(type=AsyncMock(), press=AsyncMock())
        rng = MagicMock()
        rng.random.return_value = 0.0  # would trigger a typo on every letter
        rng.uniform.return_value = 5.0
        rng.lognormvariate.return_value = 5.0
        rng.choice.side_effect = lambda choices: choices[0]
        sim = InteractionSimulator(InteractionConfig(typo_probability=0.5), rng=rng)
        with patch("asyncio.sleep", new_callable=AsyncMock), \
             patch.object(sim, "_type_with_typo", new_callable=AsyncMock) as typo:
            await sim.type_text(page, "token", allow_typos=False)
            typo.assert_not_awaited()
            await sim.type_text(page, "token")
            self.assertEqual(typo.await_count, 5)


class TestWizardSessionMarkers(unittest.TestCase):
    def _session(self, rows):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        yt_dir = Path(temp.name) / "youtube"
        yt_dir.mkdir()
        conn = sqlite3.connect(yt_dir / "cookies.sqlite")
        conn.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT)")
        conn.executemany("INSERT INTO moz_cookies VALUES (?, ?, 'v')", rows)
        conn.commit()
        conn.close()
        return Path(temp.name)

    def test_google_auth_cookies_alone_are_not_a_session(self):
        root = self._session([(".google.com", "SID"), (".google.com", "SAPISID"), (".youtube.com", "VISITOR_INFO1_LIVE")])
        ok, msg = check_youtube_session(root)
        self.assertFalse(ok)
        self.assertIn("Гостевой режим", msg)

    def test_login_info_for_other_domain_is_not_a_session(self):
        ok, _ = check_youtube_session(self._session([(".google.com", "LOGIN_INFO")]))
        self.assertFalse(ok)

    def test_login_info_on_youtube_is_a_session(self):
        ok, _ = check_youtube_session(self._session([(".youtube.com", "LOGIN_INFO")]))
        self.assertTrue(ok)


class TestDeliveryValidatorYouTube(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.validator = DeliveryValidator(
            ValidatorSettings(endpoint_template="https://www.youtube.com/watch?v={video_id}&lc={remote_id}")
        )

    def test_build_url_uses_video_id_from_source_ref(self):
        self.assertEqual(
            self.validator.build_url(_payload()),
            "https://www.youtube.com/watch?v=vid12345678&lc=UgzABC.9xyz",
        )

    def test_build_url_without_video_id_is_value_error(self):
        with self.assertRaises(ValueError):
            self.validator.build_url(_payload(source_ref=None))

    def test_build_url_keeps_old_templates_working(self):
        old = DeliveryValidator(ValidatorSettings(endpoint_template="https://example.com/{remote_id}"))
        self.assertEqual(old.build_url(_payload(source_ref=None)), "https://example.com/UgzABC.9xyz")

    async def _validate(self, page: str, status: int = 200, **overrides):
        session = MagicMock(get=AsyncMock(return_value=MagicMock(status_code=status, text=page)))
        self.validator._session = session
        result = await self.validator.validate(_payload(**overrides))
        return result, session

    async def test_delivered_when_og_meta_matches(self):
        result, session = await self._validate(_youtube_page("Comment from Alice", "Nice video, thanks"))
        self.assertTrue(result.delivered, result.error)
        self.assertEqual(session.get.await_args.args[0], "https://www.youtube.com/watch?v=vid12345678&lc=UgzABC.9xyz")

    async def test_delivered_with_truncated_description(self):
        result, _ = await self._validate(
            _youtube_page("Comment from Alice", "Nice video…"), text="Nice video, thanks for the upload"
        )
        self.assertTrue(result.delivered, result.error)

    async def test_not_delivered_for_plain_watch_page(self):
        result, _ = await self._validate(_youtube_page("Some video - YouTube", "Nice video, thanks"))
        self.assertFalse(result.delivered)
        self.assertIn("og:title", result.error)

    async def test_not_delivered_when_text_differs(self):
        result, _ = await self._validate(_youtube_page("Comment from Alice", "something else"))
        self.assertFalse(result.delivered)
        self.assertIn("og:description", result.error)

    async def test_http_error(self):
        result, _ = await self._validate("", status=404)
        self.assertFalse(result.delivered)
        self.assertEqual(result.http_status, 404)


class TestRunnerPoolLimit(unittest.IsolatedAsyncioTestCase):
    def _orchestrator(self, concurrency: int):
        orch = MultiAccountOrchestrator.__new__(MultiAccountOrchestrator)
        orch._concurrency = concurrency
        orch._runners = {}
        orch._runner_locks = __import__("collections").defaultdict(asyncio.Lock)
        orch._in_flight = __import__("collections").defaultdict(int)
        orch._runner_factory = lambda acc: MagicMock(close=AsyncMock(), name=acc.name)
        return orch

    @staticmethod
    def _acc(acc_id: int) -> AccountRecord:
        return AccountRecord(id=acc_id, name=f"acc{acc_id}", session_path=f"s{acc_id}", created_at="now")

    async def test_pool_is_capped_and_idle_runner_unloaded(self):
        orch = self._orchestrator(2)
        first = await orch._get_runner(self._acc(1))
        await orch._get_runner(self._acc(2))
        await orch._get_runner(self._acc(3))
        self.assertEqual(len(orch._runners), 2)
        self.assertNotIn(1, orch._runners)
        first.close.assert_awaited_once()

    async def test_busy_runners_are_not_unloaded(self):
        orch = self._orchestrator(2)
        await orch._get_runner(self._acc(1))
        await orch._get_runner(self._acc(2))
        orch._in_flight[1] += 1
        await orch._runner_locks[2].acquire()
        await orch._get_runner(self._acc(3))
        self.assertEqual(set(orch._runners), {1, 2, 3})  # nothing idle: the pool exceeds the cap rather than killing work


class TestProcessGuard(unittest.TestCase):
    """The tests never bind the test process itself: structures and calls are checked through a fake kernel32."""

    def test_extended_limits_layout_matches_winapi(self):
        wide = ctypes.sizeof(ctypes.c_void_p) == 8
        self.assertEqual(ctypes.sizeof(JobBasicLimits), 64 if wide else 48)
        self.assertEqual(ctypes.sizeof(JobExtendedLimits), 144 if wide else 112)

    def test_job_is_created_with_kill_on_close_and_the_process_is_assigned(self):
        calls: dict = {}
        kernel32 = MagicMock()
        kernel32.CreateJobObjectW.return_value = 77
        kernel32.GetCurrentProcess.return_value = -1

        def set_information(job, info_class, limits_ref, size):
            calls.update(job=job, info_class=info_class, size=size, flags=limits_ref._obj.BasicLimitInformation.LimitFlags)
            return True

        kernel32.SetInformationJobObject.side_effect = set_information
        kernel32.AssignProcessToJobObject.return_value = True
        self.assertEqual(configure_kill_on_close_job(kernel32), 77)
        self.assertEqual(calls["job"], 77)
        self.assertEqual(calls["info_class"], 9)  # JobObjectExtendedLimitInformation
        self.assertEqual(calls["flags"], 0x2000)  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self.assertEqual(calls["size"], ctypes.sizeof(JobExtendedLimits))
        kernel32.AssignProcessToJobObject.assert_called_once_with(77, -1)
        kernel32.CloseHandle.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "ctypes.WinError")
    def test_failed_assignment_closes_the_job_handle(self):
        kernel32 = MagicMock()
        kernel32.CreateJobObjectW.return_value = 77
        kernel32.SetInformationJobObject.return_value = True
        kernel32.AssignProcessToJobObject.return_value = False
        with self.assertRaises(OSError):
            configure_kill_on_close_job(kernel32)
        kernel32.CloseHandle.assert_called_once_with(77)

    def test_bind_configures_the_job_only_once(self):
        with patch.object(process_guard, "_job_handle", None),              patch.object(process_guard.sys, "platform", "win32"),              patch.object(process_guard.ctypes, "WinDLL", MagicMock(), create=True),              patch.object(process_guard, "configure_kill_on_close_job", return_value=123) as configure:
            self.assertTrue(bind_to_kill_on_close_job())
            self.assertTrue(bind_to_kill_on_close_job())
        configure.assert_called_once()

    def test_failed_setup_is_reported_not_raised(self):
        with patch.object(process_guard, "_job_handle", None),              patch.object(process_guard.sys, "platform", "win32"),              patch.object(process_guard.ctypes, "WinDLL", MagicMock(), create=True),              patch.object(process_guard, "configure_kill_on_close_job", side_effect=OSError("denied")):
            self.assertFalse(bind_to_kill_on_close_job())

    def test_other_platforms_are_a_no_op(self):
        with patch.object(process_guard, "_job_handle", None), patch.object(process_guard.sys, "platform", "linux"):
            self.assertFalse(bind_to_kill_on_close_job())

    @unittest.skipUnless(sys.platform == "win32", "Windows Job Objects")
    def test_children_die_with_a_bound_process(self):
        """A separate interpreter binds itself, starts a child and exits: the child must be killed with the job."""
        guard_file = Path(process_guard.__file__).resolve()
        code = (
            "import importlib.util, subprocess, sys\n"
            f"spec = importlib.util.spec_from_file_location('pg', r'{guard_file}')\n"
            "pg = importlib.util.module_from_spec(spec); spec.loader.exec_module(pg)\n"
            "if not pg.bind_to_kill_on_close_job():\n"
            "    print('UNBOUND'); sys.exit(0)\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "print(child.pid, flush=True)\n"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        output = result.stdout.strip().splitlines()
        if not output or output[-1] == "UNBOUND":
            self.skipTest("no Job Object available here (an outer job forbids nesting)")
        pid = output[-1]
        self.addCleanup(subprocess.run, ["taskkill", "/F", "/PID", pid], capture_output=True)

        def alive() -> bool:
            listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
            return pid in listing.stdout

        deadline = time.monotonic() + 15
        while alive() and time.monotonic() < deadline:
            time.sleep(0.3)
        self.assertFalse(alive(), "the child survived the exit of the process bound to the job")


if __name__ == "__main__":
    unittest.main()
