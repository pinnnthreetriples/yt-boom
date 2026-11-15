"""Observer account: it only checks that worker replies are visible and never posts.

Everything is offline: the YouTube session, the proxy and the browser are fakes; credentials are fake.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import random
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

from config import CollectorSettings
from database import AccountRecord, Database, RecordNotFoundError
from modules.collector import CollectedItem
from modules.comment_visibility import (
    CommentVisibilityChecker,
    ObserverAwareChecker,
    VisibilityWatcher,
    make_visibility_checker,
)
from modules.multi_account_orchestrator import MultiAccountOrchestrator
from modules.observer_menu_logic import (
    assign_observer,
    check_counts_text,
    observer_status,
    parse_delay,
    proxy_host,
    register_observer,
    remove_observer,
    set_observer_enabled,
)
from modules.observer_session import (
    ObserverSession,
    ObserverUnavailable,
    has_login,
    read_account_cookies,
    read_chrome_observer_cookies,
    read_observer_cookies,
    sapisid_hash,
)
from modules.projects import project_seed
from modules.ui import icons
from modules.ui.app import AppContext, status_cells
from modules.ui.shell import Confirm, Controller, Field, Menu, frags_text, render
from modules.ui.tab_launch import build_tab
from modules.ui_test_runner import SubmissionResult
from tests.test_multi_account_orchestrator import _make_test_config

OBSERVER_LINE = "obs@example.com:pw-fake-observer:JBSWY3DPEHPK3PXP"
HTML = '"INNERTUBE_API_KEY":"key123" "INNERTUBE_CLIENT_VERSION":"2.20261101.00.00"'
SETTINGS = CollectorSettings(endpoints=[{"name": "e", "url": "https://x.example"}])


def _session(next_body: str):
    return SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(status_code=200, text=HTML)),
        post=AsyncMock(
            return_value=SimpleNamespace(status_code=200, text=next_body, json=lambda: json.loads(next_body))
        ),
    )


def _collector(session=None):
    return SimpleNamespace(
        open=AsyncMock(), close=AsyncMock(), _session=session, _extract_comments_token=lambda _html: "tok"
    )


def _cookie_profile(folder: Path, rows: list[tuple]) -> Path:
    """A Firefox-like profile: (name, value, host, expiry_seconds) rows in moz_cookies."""
    folder.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(folder / "cookies.sqlite")
    conn.execute(
        "CREATE TABLE moz_cookies (name TEXT, value TEXT, host TEXT, path TEXT, isSecure INTEGER, expiry INTEGER)"
    )
    conn.executemany("INSERT INTO moz_cookies VALUES (?, ?, ?, '/', 1, ?)", rows)
    conn.commit()
    conn.close()
    return folder


def _chrome_profile(folder: Path, cookies: list[dict], user_agent: str = "Mozilla/5.0 Chrome/134.0.0.0 Safari/537.36") -> Path:
    """A Chrome profile as the runner leaves it: the DPAPI `heartbeat_state.bin` with the exported cookies."""
    from modules.chrome_session import save_chrome_heartbeat_state

    context = SimpleNamespace(cookies=AsyncMock(return_value=cookies))
    asyncio.run(save_chrome_heartbeat_state(folder, context, user_agent))
    return folder


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = Database(self.root / "t.db")
        await self.db.connect()
        await self.db.init_schema()

    async def asyncTearDown(self) -> None:
        await self.db.close()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass


class TestObserverRole(DbCase):
    async def test_new_accounts_are_workers_and_the_observer_is_left_out_of_every_list(self) -> None:
        worker = await self.db.upsert_account("worker", str(self.root / "w"))
        await self.db.upsert_account("obs", str(self.root / "o"))
        observer = await self.db.set_observer("obs")
        self.assertEqual(worker.role, "worker")
        self.assertTrue(observer.is_observer)
        self.assertEqual([a.name for a in await self.db.list_active_accounts()], ["worker"])
        self.assertEqual([a.name for a in await self.db.list_accounts()], ["worker"])
        self.assertEqual([a.name for a in await self.db.list_accounts(is_active=True)], ["worker"])
        self.assertEqual([a.name for a in await self.db.list_accounts(include_observer=True)], ["worker", "obs"])
        self.assertEqual((await self.db.get_observer_account()).name, "obs")
        self.assertIsNotNone(await self.db.get_account("obs"))  # still reachable by name for sign-in

    async def test_role_survives_an_upsert_that_does_not_mention_it(self) -> None:
        await self.db.upsert_account("obs", str(self.root / "o"))
        await self.db.set_observer("obs")
        updated = await self.db.upsert_account("obs", proxy_url="http://1.2.3.4:80", is_active=False)
        self.assertEqual(updated.role, "observer")
        with self.assertRaises(ValueError):
            await self.db.upsert_account("obs", role="boss")

    async def test_only_one_observer_and_unknown_names_fail(self) -> None:
        for name in ("a", "b"):
            await self.db.upsert_account(name, str(self.root / name))
        await self.db.set_observer("a")
        await self.db.set_observer("b")
        self.assertEqual((await self.db.get_observer_account()).name, "b")
        self.assertEqual((await self.db.get_account("a")).role, "worker")
        with self.assertRaises(RecordNotFoundError):
            await self.db.set_observer("ghost")

    async def test_replacing_the_observer_never_turns_the_old_one_into_a_poster(self) -> None:
        for name in ("a", "b"):
            await self.db.upsert_account(name, str(self.root / name))
        await assign_observer(self.db, "a")
        await assign_observer(self.db, "b")
        old = await self.db.get_account("a")
        self.assertEqual((old.role, old.is_active), ("worker", False))
        self.assertEqual(await self.db.list_active_accounts(), [])

    async def test_migration_adds_the_role_column_to_an_old_accounts_table(self) -> None:
        old_path = self.root / "old.db"
        raw = sqlite3.connect(old_path)
        raw.execute(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, "
            "session_path TEXT NOT NULL, proxy_url TEXT, is_active INTEGER NOT NULL DEFAULT 1, "
            "total_replies INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, last_used_at TEXT)"
        )
        raw.execute("INSERT INTO accounts (name, session_path, created_at) VALUES ('legacy', 's', '2026-01-01')")
        raw.commit()
        raw.close()
        async with Database(old_path) as db:
            columns = {row[1] for row in await db._fetch_all("PRAGMA table_info(accounts)")}
            self.assertIn("role", columns)
            legacy = await db.get_account("legacy")
            self.assertEqual(legacy.role, "worker")
            self.assertEqual([a.name for a in await db.list_active_accounts()], ["legacy"])
        async with Database(old_path) as again:  # a second start must not fail on the existing column
            self.assertEqual((await again.get_account("legacy")).role, "worker")

    async def test_remove_observer(self) -> None:
        await self.db.upsert_account("obs", str(self.root / "o"))
        await self.db.set_observer("obs")
        self.assertEqual(await remove_observer(self.db), "obs")
        self.assertIsNone(await self.db.get_observer_account())
        self.assertIsNone(await remove_observer(self.db))


class TestObserverStaysOutOfProjects(DbCase):
    async def test_the_observer_leaves_projects_and_cannot_join_one(self) -> None:
        project = (await self.db.list_projects())[0]
        worker = await self.db.upsert_account("worker", str(self.root / "w"))
        obs = await self.db.upsert_account("obs", str(self.root / "o"))
        await self.db.add_project_accounts(project.id, [worker.id, obs.id])
        self.assertEqual({a.name for a in await self.db.list_project_accounts(project.id)}, {"worker", "obs"})
        await self.db.set_observer("obs")
        self.assertEqual([a.name for a in await self.db.list_project_accounts(project.id)], ["worker"])
        self.assertEqual(await self.db.add_project_accounts(project.id, [obs.id]), 0)  # skipped, not an error
        self.assertEqual([a.name for a in await self.db.list_project_accounts(project.id, active_only=True)], ["worker"])
        self.assertEqual((await self.db.list_projects())[0].accounts_count, 1)

    async def test_the_first_project_seed_leaves_the_observer_out(self) -> None:
        path = self.root / "seed.db"
        raw = Database(path)
        await raw.connect()
        await raw.init_schema()
        await raw.upsert_account("worker", str(self.root / "w"))
        await raw.upsert_account("obs", str(self.root / "o"))
        await raw.set_observer("obs")
        async with raw._transaction() as conn:  # as if projects did not exist yet
            await conn.execute("DELETE FROM project_accounts")
            await conn.execute("DELETE FROM app_settings WHERE key = 'projects_seeded'")
            await conn.execute("DELETE FROM projects")
        await raw.close()
        async with Database(path) as again:
            project = (await again.list_projects())[0]
            self.assertEqual([a.name for a in await again.list_project_accounts(project.id)], ["worker"])


class TestObserverMenuLogic(DbCase):
    async def test_register_observer_encrypts_secrets_and_gives_it_a_separate_profile(self) -> None:
        cfg = _make_test_config(self.root)
        account = await register_observer(self.db, cfg, OBSERVER_LINE)
        self.assertTrue(account.is_observer)
        self.assertEqual(account.session_path, str(cfg.browser.session_root / "obs@example.com"))
        raw = sqlite3.connect(self.root / "t.db").execute("SELECT password, totp_secret FROM accounts").fetchone()
        self.assertNotIn("pw-fake-observer", raw[0])
        self.assertNotIn("JBSWY3DPEHPK3PXP", raw[1])
        self.assertEqual(account.email, "obs@example.com")
        self.assertEqual((await self.db.get_account("obs@example.com")).password, "pw-fake-observer")  # DPAPI round trip
        self.assertEqual(await self.db.list_active_accounts(), [])

    async def test_register_observer_refuses_a_known_account(self) -> None:
        cfg = _make_test_config(self.root)
        await self.db.upsert_account("obs@example.com", str(self.root / "x"))
        with self.assertRaises(ValueError):
            await register_observer(self.db, cfg, OBSERVER_LINE)

    async def test_the_observer_takes_a_proxy_from_the_library_and_has_its_own_enable_flag(self) -> None:
        await self.db.upsert_account("obs", str(self.root / "o"), proxy_url="http://9.9.9.9:1")
        await self.db.upsert_account("worker", str(self.root / "w"), proxy_url="http://8.8.8.8:1")
        await self.db.set_observer("obs")
        record, _ = await self.db.add_proxy_url("socks5://u:p@5.5.5.5:1080")
        await self.db.assign_proxy(record.id, ["obs"])
        await set_observer_enabled(self.db, "obs", False)
        observer = await self.db.get_observer_account()
        self.assertEqual((observer.proxy_url, observer.is_active), ("socks5://u:p@5.5.5.5:1080", False))
        self.assertEqual((await self.db.get_account("worker")).proxy_url, "http://8.8.8.8:1")

    def test_status_text_and_helpers(self) -> None:
        self.assertEqual(observer_status(None, None), ("○ не задан", "warn"))
        self.assertEqual(observer_status("obs", "http://u:secret@5.5.5.5:8080"), ("● obs · прокси 5.5.5.5:8080", ""))
        self.assertEqual(observer_status("obs", None), ("● obs · без прокси", "warn"))
        self.assertIn("выключены", observer_status("obs", "http://5.5.5.5:80", enabled=False)[0])
        self.assertNotIn("secret", proxy_host("http://u:secret@5.5.5.5:8080"))
        self.assertEqual(parse_delay("1,5"), 1.5)
        for bad in ("abc", "-1", "9999"):
            with self.assertRaises(ValueError):
                parse_delay(bad)
        self.assertEqual(check_counts_text({}), "пока нет")
        self.assertEqual(check_counts_text({"seen": 5, "missing": 1}), "виден 5 · не найден 1")


class TestCommentChecks(DbCase):
    async def test_every_check_is_persisted_with_its_viewpoint_and_none_is_kept_as_none(self) -> None:
        await self.db.record_comment_check("c1", "vid", "acc", True, "observer")
        await self.db.record_comment_check("c2", "vid", "acc", False, "anonymous")
        await self.db.record_comment_check("c3", "vid", "acc", None, "observer")
        rows = {r["comment_id"]: r for r in await self.db.list_comment_checks()}
        self.assertEqual((rows["c1"]["seen"], rows["c1"]["checked_by"]), (True, "observer"))
        self.assertIs(rows["c2"]["seen"], False)
        self.assertIsNone(rows["c3"]["seen"])
        self.assertTrue(rows["c1"]["checked_at"])
        self.assertEqual(await self.db.comment_check_counts(), {"seen": 1, "missing": 1, "unknown": 1})
        self.assertEqual((await self.db.stats())["visibility"], {"seen": 1, "missing": 1, "unknown": 1})
        with self.assertRaises(ValueError):
            await self.db.record_comment_check("c4", "vid", "acc", True, "nobody")

    async def test_counts_use_the_latest_check_of_a_comment(self) -> None:
        await self.db.record_comment_check("c1", "vid", "acc", False, "observer")
        await self.db.record_comment_check("c1", "vid", "acc", True, "observer")  # seen after a re-check
        self.assertEqual(await self.db.comment_check_counts(), {"seen": 1})


class TestCheckerPerspective(unittest.IsolatedAsyncioTestCase):
    async def test_injected_session_is_used_instead_of_the_collectors_anonymous_one(self) -> None:
        observer_session = _session('{"commentId": "Ugx.reply"}')
        collector = _collector(_session("{}"))
        provider = AsyncMock(return_value=observer_session)
        checker = CommentVisibilityChecker(collector, session_provider=provider, checked_by="observer")
        self.assertIs(await checker.is_visible("vid", "Ugx.reply"), True)
        provider.assert_awaited()
        collector.open.assert_not_awaited()
        collector._session.get.assert_not_awaited()
        observer_session.get.assert_awaited_once()
        self.assertEqual(checker.checked_by, "observer")

    async def test_watch_records_the_final_result_once_and_names_the_observer(self) -> None:
        recorder = AsyncMock()
        checker = CommentVisibilityChecker(
            _collector(),
            retry_delays=(5,),
            session_provider=AsyncMock(return_value=_session('{"commentId": "Ugx.other"}')),
            checked_by="observer",
            recorder=recorder,
        )
        with patch("modules.comment_visibility.asyncio.sleep", AsyncMock()), patch(
            "modules.comment_visibility.logger"
        ) as log:
            self.assertIs(await checker.watch("vid", "Ugx.reply", "acc"), False)
        recorder.assert_awaited_once_with("Ugx.reply", "vid", "acc", False, "observer")  # retries keep their semantics
        self.assertIn("наблюдател", log.warning.call_args.args[0])

    async def test_unavailable_observer_is_unknown_not_a_miss_and_is_recorded_as_none(self) -> None:
        recorder = AsyncMock()
        provider = AsyncMock(side_effect=ObserverUnavailable("не вошёл"))
        checker = CommentVisibilityChecker(
            SimpleNamespace(), session_provider=provider, checked_by="observer", recorder=recorder
        )
        with patch("modules.comment_visibility.asyncio.sleep", AsyncMock()):
            self.assertIsNone(await checker.watch("vid", "Ugx.reply", "acc"))
        recorder.assert_awaited_once_with("Ugx.reply", "vid", "acc", None, "observer")

    async def test_a_failing_recorder_never_breaks_the_check(self) -> None:
        checker = CommentVisibilityChecker(
            _collector(_session('{"commentId": "Ugx.reply"}')), recorder=AsyncMock(side_effect=RuntimeError("disk full"))
        )
        with patch("modules.comment_visibility.asyncio.sleep", AsyncMock()):
            self.assertIs(await checker.watch("vid", "Ugx.reply", "acc"), True)

    async def test_next_request_carries_auth_headers(self) -> None:
        session = _session('{"x":[{"commentId":"c1"}]}')
        checker = CommentVisibilityChecker(
            _collector(), 0, session_provider=AsyncMock(return_value=session), checked_by="observer",
            auth_headers=lambda: {"Authorization": "SAPISIDHASH 1_x"},
        )
        self.assertTrue(await checker.is_visible("v", "c1"))
        headers = session.post.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "SAPISIDHASH 1_x")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_sapisid_hash_format(self) -> None:
        expected = hashlib.sha1(b"1700000000 abc https://www.youtube.com").hexdigest()
        self.assertEqual(sapisid_hash("abc", now=1700000000), f"SAPISIDHASH 1700000000_{expected}")


class TestObserverAwareChecker(DbCase):
    def _cfg(self):
        return _make_test_config(self.root)

    def _checker(self, collector=None):
        collector = collector or _collector(_session('{"commentId": "Ugx.reply"}'))
        checker = make_visibility_checker(self._cfg(), collector, self.db)
        self.assertIsInstance(checker, ObserverAwareChecker)
        return checker, collector

    async def test_the_check_is_anonymous_without_an_observer_and_its_result_is_stored(self) -> None:
        checker, collector = self._checker()
        with patch("modules.comment_visibility.asyncio.sleep", AsyncMock()):
            self.assertIs(await checker.watch("vid", "Ugx.reply", "worker"), True)
        self.assertEqual(checker.checked_by, "anonymous")
        collector._session.get.assert_awaited()
        (row,) = await self.db.list_comment_checks()
        self.assertEqual((row["seen"], row["checked_by"], row["account_name"]), (True, "anonymous", "worker"))

    async def test_the_wrapper_keeps_the_waiting_numbers_of_the_plain_checker(self) -> None:
        checker, _ = self._checker()
        plain = make_visibility_checker(self._cfg(), _collector())
        self.assertEqual((checker._delay, checker._retry_delays), (plain._delay, plain._retry_delays))

    async def test_an_enabled_observer_looks_through_its_own_session_and_the_result_is_stored(self) -> None:
        await self.db.upsert_account("obs", str(self.root / "o"), proxy_url="http://u:p@5.5.5.5:8080")
        await self.db.set_observer("obs")
        observer_session = _session('{"commentId": "Ugx.reply"}')
        fake = MagicMock(
            get_session=AsyncMock(return_value=observer_session), close=AsyncMock(),
            auth_headers=lambda: {"Authorization": "SAPISIDHASH 1_x"},
        )
        checker, collector = self._checker(_collector(_session("{}")))
        with patch("modules.observer_session.ObserverSession", return_value=fake) as factory, patch(
            "modules.comment_visibility.asyncio.sleep", AsyncMock()
        ):
            self.assertIs(await checker.watch("vid", "Ugx.reply", "worker"), True)
            self.assertIs(await checker.watch("vid", "Ugx.gone", "worker"), False)
        self.assertEqual(factory.call_args.args[0].proxy_url, "http://u:p@5.5.5.5:8080")
        collector._session.get.assert_not_awaited()  # never the anonymous session
        self.assertEqual(observer_session.post.call_args.kwargs["headers"]["Authorization"], "SAPISIDHASH 1_x")
        rows = await self.db.list_comment_checks()
        self.assertEqual(
            {(r["comment_id"], r["seen"], r["checked_by"]) for r in rows},
            {("Ugx.reply", True, "observer"), ("Ugx.gone", False, "observer")},
        )
        await checker.aclose()
        fake.close.assert_awaited_once()

    async def test_a_disabled_observer_means_anonymous_checks(self) -> None:
        await self.db.upsert_account("obs", str(self.root / "o"))
        await self.db.set_observer("obs")
        await set_observer_enabled(self.db, "obs", False)
        checker, _ = self._checker()
        with patch("modules.comment_visibility.asyncio.sleep", AsyncMock()):
            await checker.watch("vid", "Ugx.reply", "worker")
        self.assertEqual(checker.checked_by, "anonymous")

    async def test_an_observer_that_is_not_signed_in_leaves_the_check_unknown_with_one_warning(self) -> None:
        await self.db.upsert_account("obs", str(self.root / "no-profile"))
        await self.db.set_observer("obs")
        checker, collector = self._checker()
        with patch("modules.comment_visibility.asyncio.sleep", AsyncMock()), patch(
            "modules.comment_visibility.logger"
        ) as log:
            self.assertIsNone(await checker.watch("vid", "Ugx.reply", "worker"))
            self.assertIsNone(await checker.watch("vid", "Ugx.reply", "worker"))
        collector._session.get.assert_not_awaited()  # never silently anonymous
        self.assertEqual(sum("проверки пропущены" in str(c.args[0]) for c in log.warning.call_args_list), 1)
        self.assertEqual({r["seen"] for r in await self.db.list_comment_checks()}, {None})

    async def test_the_watcher_closes_the_observer_session_when_the_run_ends(self) -> None:
        checker = MagicMock(aclose=AsyncMock(), _delay=0.0, _retry_delays=())
        watcher = VisibilityWatcher(checker, self.db)
        await watcher.finish()
        checker.aclose.assert_awaited_once()
        await VisibilityWatcher(MagicMock(), self.db).cancel()  # a stand-in checker without `aclose` is fine


class TestObserverSession(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        future = int(time.time()) + 86400
        self.profile = _cookie_profile(
            self.root / "profile",
            [
                ("LOGIN_INFO", "li", ".youtube.com", future),
                ("SID", "sid", ".google.com", future),
                ("PREF", "p", "www.youtube.com", future),
                ("tracker", "t", ".example.com", future),
                ("evilyoutube", "t", "notyoutube.com", future),
                ("OLD", "o", ".youtube.com", int(time.time()) - 100),
            ],
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_only_youtube_and_google_cookies_are_read_and_expired_ones_are_skipped(self) -> None:
        rows = read_observer_cookies(self.profile)
        self.assertEqual({r[0] for r in rows}, {"LOGIN_INFO", "SID", "PREF"})
        self.assertTrue(has_login(rows))
        self.assertFalse(has_login([r for r in rows if r[0] != "LOGIN_INFO"]))

    def test_a_locked_profile_is_read_from_a_copy_and_missing_ones_give_nothing(self) -> None:
        locker = sqlite3.connect(self.profile / "cookies.sqlite")
        locker.execute("BEGIN EXCLUSIVE")
        try:
            self.assertEqual(len(read_observer_cookies(self.profile)), 3)
        finally:
            locker.rollback()
            locker.close()
        self.assertEqual(read_observer_cookies(self.root / "nowhere"), [])

    def test_a_chrome_profile_is_read_from_the_exported_state(self) -> None:
        future = time.time() + 86400
        profile = _chrome_profile(
            self.root / "chrome",
            [
                {"name": "LOGIN_INFO", "value": "li", "domain": ".youtube.com", "expires": future},
                {"name": "SAPISID", "value": "sap", "domain": ".google.com", "expires": future},
                {"name": "OLD", "value": "o", "domain": ".youtube.com", "expires": time.time() - 100},
                {"name": "tracker", "value": "t", "domain": ".example.com", "expires": future},
            ],
        )
        rows = read_chrome_observer_cookies(profile)
        self.assertEqual({r[0] for r in rows}, {"LOGIN_INFO", "SAPISID"})
        self.assertTrue(has_login(rows))
        self.assertEqual(read_account_cookies(profile, "chrome"), rows)
        self.assertEqual(read_account_cookies(profile, "camoufox"), [])  # no cookies.sqlite there
        self.assertEqual(read_chrome_observer_cookies(self.root / "nowhere"), [])

    async def test_session_goes_through_the_observers_proxy_with_its_cookies(self) -> None:
        account = SimpleNamespace(name="obs", session_path=str(self.profile), proxy_url="http://u:p@5.5.5.5:8080")
        fake = MagicMock(close=AsyncMock())
        with patch("modules.observer_session.AsyncSession", return_value=fake) as factory, patch(
            "modules.observer_session.ensure_network_ready", AsyncMock()
        ) as guard:
            obs = ObserverSession(account, SETTINGS)
            self.assertIs(await obs.get_session(), fake)
            self.assertIs(await obs.get_session(), fake)  # opened once
        factory.assert_called_once()
        guard.assert_awaited_once_with("http://u:p@5.5.5.5:8080")  # fail-closed network guard before any request
        self.assertEqual(factory.call_args.kwargs["proxy"], "http://u:p@5.5.5.5:8080")
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        names = {c.args[0] for c in fake.cookies.set.call_args_list}
        self.assertEqual(names, {"LOGIN_INFO", "SID", "PREF"})
        await obs.close()
        fake.close.assert_awaited_once()

    async def test_a_chrome_observer_uses_a_chrome_tls_profile_and_its_saved_user_agent(self) -> None:
        future = time.time() + 86400
        profile = await asyncio.to_thread(
            _chrome_profile,
            self.root / "chrome",
            [
                {"name": "LOGIN_INFO", "value": "li", "domain": ".youtube.com", "expires": future},
                {"name": "SAPISID", "value": "sap", "domain": ".youtube.com", "expires": future},
            ],
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/134.0.0.0 Safari/537.36",
        )
        account = SimpleNamespace(name="obs", session_path=str(profile), proxy_url=None, engine="chrome", locale="en-US")
        fake = MagicMock(close=AsyncMock())
        with patch("modules.observer_session.AsyncSession", return_value=fake) as factory:
            obs = ObserverSession(account, SETTINGS)
            await obs.get_session()
        self.assertTrue(factory.call_args.kwargs["impersonate"].startswith("chrome"))
        self.assertIn("Chrome/134", factory.call_args.kwargs["headers"]["User-Agent"])
        self.assertIsNone(factory.call_args.kwargs["proxy"])
        self.assertTrue(obs.auth_headers()["Authorization"].startswith("SAPISIDHASH "))

    async def test_a_profile_without_login_is_unavailable(self) -> None:
        guest = _cookie_profile(self.root / "guest", [("PREF", "p", ".youtube.com", int(time.time()) + 100)])
        account = SimpleNamespace(name="obs", session_path=str(guest), proxy_url=None)
        with patch("modules.observer_session.AsyncSession") as factory:
            with self.assertRaises(ObserverUnavailable):
                await ObserverSession(account, SETTINGS).get_session()
        factory.assert_not_called()

    async def test_a_proxy_that_fails_the_guard_or_cannot_be_read_never_falls_back_to_the_own_ip(self) -> None:
        from config import PROXY_LIBRARY_MARKER
        from modules.geo_guard import NetworkGuardError

        account = SimpleNamespace(name="obs", session_path=str(self.profile), proxy_url="http://u:p@5.5.5.5:8080")
        with patch("modules.observer_session.AsyncSession") as factory, patch(
            "modules.observer_session.ensure_network_ready", AsyncMock(side_effect=NetworkGuardError("leak"))
        ):
            with self.assertRaises(ObserverUnavailable):
                await ObserverSession(account, SETTINGS).get_session()
        unreadable = SimpleNamespace(
            name="obs", session_path=str(self.profile), proxy_url=PROXY_LIBRARY_MARKER, proxy_unavailable=True
        )
        with patch("modules.observer_session.AsyncSession") as factory2:
            with self.assertRaises(ObserverUnavailable):
                await ObserverSession(unreadable, SETTINGS).get_session()
        factory.assert_not_called()
        factory2.assert_not_called()

    def test_observer_session_headers_need_sapisid(self) -> None:
        obs = ObserverSession(SimpleNamespace(proxy_url=None, name="o", session_path="x"), SETTINGS)
        self.assertEqual(obs.auth_headers(), {})
        obs._sapisid = "abc"
        self.assertTrue(obs.auth_headers()["Authorization"].startswith("SAPISIDHASH "))


class TestOrchestratorWithObserver(DbCase):
    def _config(self):
        return _make_test_config(self.root)

    def _items(self):
        return [
            CollectedItem(
                endpoint="youtube:obsvid11111",
                external_id=f"obsvid11111#root#c_{n}",
                text=f"Comment {n}",
                language="en",
                confidence=0.9,
                source_url="https://www.youtube.com/watch?v=obsvid11111",
                author=f"user_{n}",
                video_id="obsvid11111",
                like_count=1,
            )
            for n in range(6)
        ]

    async def test_observer_is_never_picked_to_post_in_either_account_mode(self) -> None:
        await self.db.upsert_account("worker", str(self.root / "w"))
        await self.db.upsert_account("obs", str(self.root / "o"))
        await self.db.set_observer("obs")
        await self.db.add_video_to_pool("https://www.youtube.com/watch?v=obsvid11111", comments_target=2)
        for mode in ("each", "share"):
            picked: list[str] = []
            runner = MagicMock(
                run=AsyncMock(return_value=SubmissionResult(success=True, remote_id="r", duration_seconds=0.1)),
                close=AsyncMock(),
            )

            def factory(acc, picked=picked, runner=runner):
                picked.append(acc.name)
                return runner

            collector = MagicMock(close=AsyncMock(), fetch_popular_comments=AsyncMock(return_value=self._items()))
            orchestrator = MultiAccountOrchestrator(
                self._config(), db=self.db, account_mode=mode, collector=collector,
                runner_factory=factory, rng=random.Random(3),
            )
            self.assertEqual([a.name for a in await orchestrator._usable_accounts()], ["worker"])
            with patch("modules.multi_account_orchestrator.asyncio.sleep", AsyncMock()):
                await orchestrator.execute_video_pool(max_comments_per_video=1)
            self.assertEqual(set(picked), {"worker"}, mode)
            self.assertEqual((await self.db.get_account("obs")).total_replies, 0)
            async with self.db._transaction() as conn:  # the next mode runs the same video again
                await conn.execute("UPDATE video_pool SET status = 'pending', comments_done = 0")

    async def test_a_project_run_never_takes_the_observer_even_if_a_row_slips_in(self) -> None:
        project = (await self.db.list_projects())[0]
        worker = await self.db.upsert_account("worker", str(self.root / "w"))
        obs = await self.db.upsert_account("obs", str(self.root / "o"))
        await self.db.add_project_accounts(project.id, [worker.id, obs.id])
        await self.db.set_observer("obs")
        async with self.db._transaction() as conn:  # a membership written behind the API's back
            await conn.execute("INSERT INTO project_accounts (project_id, account_id, added_at) VALUES (?, ?, 'x')",
                               (project.id, obs.id))
        orchestrator = MultiAccountOrchestrator(
            self._config(), db=self.db, collector=MagicMock(close=AsyncMock()), project=project
        )
        self.assertEqual([a.name for a in await orchestrator._usable_accounts()], ["worker"])
        self.assertFalse(MultiAccountOrchestrator._is_usable(await self.db.get_account("obs")))

    async def test_a_database_with_only_an_observer_has_no_posting_accounts(self) -> None:
        await self.db.upsert_account("obs", str(self.root / "o"))
        await self.db.set_observer("obs")
        orchestrator = MultiAccountOrchestrator(self._config(), db=self.db, collector=MagicMock(close=AsyncMock()))
        self.assertEqual(await self.db.list_active_accounts(), [])
        self.assertFalse(any(a.is_observer for a in await orchestrator._usable_accounts()))

    async def test_the_orchestrator_checks_as_the_observer_and_closes_its_session(self) -> None:
        await self.db.upsert_account("obs", str(self.root / "o"), proxy_url="http://u:p@5.5.5.5:8080")
        await self.db.set_observer("obs")
        observer_session = _session('{"commentId": "Ugx.reply"}')
        fake = MagicMock(
            get_session=AsyncMock(return_value=observer_session), close=AsyncMock(), auth_headers=lambda: {}
        )
        collector = MagicMock(close=AsyncMock(), open=AsyncMock(), _extract_comments_token=lambda _html: "tok")
        collector._session = _session("{}")
        with patch("modules.observer_session.ObserverSession", return_value=fake):
            orchestrator = MultiAccountOrchestrator(self._config(), db=self.db, collector=collector)
            self.assertIsInstance(orchestrator._visibility, ObserverAwareChecker)
            with patch("modules.comment_visibility.asyncio.sleep", AsyncMock()):
                self.assertIs(await orchestrator._visibility.watch("vid", "Ugx.reply", "worker"), True)
            (row,) = await self.db.list_comment_checks()
            await orchestrator.close()
        collector._session.get.assert_not_awaited()
        fake.close.assert_awaited()
        self.assertEqual((row["seen"], row["checked_by"]), (True, "observer"))

    async def test_an_injected_checker_is_never_replaced_by_the_observer(self) -> None:
        await self.db.upsert_account("obs", str(self.root / "o"))
        await self.db.set_observer("obs")
        injected = MagicMock()
        orchestrator = MultiAccountOrchestrator(
            self._config(), db=self.db, collector=MagicMock(), visibility_checker=injected
        )
        self.assertIs(orchestrator._visibility, injected)


class TestObserverCli(DbCase):
    def _cfg(self):
        cfg = _make_test_config(self.root)
        cfg.app.db_path = self.root / "t.db"
        return cfg

    async def test_set_observer_and_status_cli(self) -> None:
        import youtube_bot

        cfg = self._cfg()
        await self.db.upsert_account("worker", str(self.root / "w"))
        out = io.StringIO()
        with patch("youtube_bot.load_config", return_value=cfg), patch("sys.stdout", out):
            self.assertFalse(await youtube_bot.observer_status_cli("x"))  # none yet
            self.assertTrue(await youtube_bot.set_observer_cli("worker", "x"))
            self.assertTrue(await youtube_bot.observer_status_cli("x"))
            self.assertFalse(await youtube_bot.set_observer_cli("ghost", "x"))
        self.assertIn("is the observer now", out.getvalue())
        self.assertIn("Observer:  worker", out.getvalue())
        self.assertIn("Signed in: no", out.getvalue())

    async def test_list_accounts_marks_the_observer(self) -> None:
        import youtube_bot

        await self.db.upsert_account("worker", str(self.root / "w"))
        await self.db.upsert_account("obs", str(self.root / "o"))
        await self.db.set_observer("obs")
        out = io.StringIO()
        with patch("youtube_bot.load_config", return_value=self._cfg()), patch("sys.stdout", out):
            await youtube_bot.print_accounts_table("x")
        self.assertIn("obs [obs]", out.getvalue())

    async def test_a_run_as_the_observer_is_refused(self) -> None:
        import youtube_bot

        await self.db.upsert_account("obs", str(self.root / "o"))
        await self.db.set_observer("obs")
        cfg = self._cfg()
        with patch("youtube_bot.load_config", return_value=cfg):
            for run in (youtube_bot.run_reply_bot, youtube_bot.run_multi_video_pipeline):
                with self.assertRaises(ValueError) as caught:
                    await run("https://youtu.be/abcdefghijk", account_name="obs", config_path="x")
                self.assertIn("observer", str(caught.exception))

    async def test_the_cookie_robot_and_warm_up_skip_the_observer(self) -> None:
        import youtube_bot

        await self.db.upsert_account("obs", str(self.root / "o"))
        await self.db.set_observer("obs")
        with patch("youtube_bot.load_config", return_value=self._cfg()), patch(
            "youtube_bot.CookieRobot.run", AsyncMock()
        ) as robot:
            results = await youtube_bot.run_cookie_robot("x", ["obs"], report=lambda _line: None)
        robot.assert_not_awaited()
        self.assertFalse(results[0].success)
        base = dict(id=1, name="o", session_path="s", created_at="2026-11-01T10:00:00+00:00", auth_status="authenticated",
                    authenticated_at="2026-11-15T09:00:00+00:00")
        self.assertEqual(youtube_bot.warm_candidates([AccountRecord(**base, role="observer")]), [])
        self.assertEqual(len(youtube_bot.warm_candidates([AccountRecord(**base)])), 1)

    async def test_an_intro_warm_up_is_not_started_for_the_observer(self) -> None:
        import youtube_bot

        await self.db.upsert_account("obs", str(self.root / "o"))
        await self.db.set_observer("obs")
        obs = await self.db.get_account("obs")
        with patch("youtube_bot.AccountWarmer") as warmer:
            await youtube_bot._intro_warm_up(self.db, self._cfg(), obs)
        warmer.assert_not_called()


class TabCase(unittest.TestCase):
    """The «Наблюдатель» entry of the tab «Запуск» and its screen."""

    def setUp(self) -> None:
        icons.use_emoji(False)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "cfg.yaml"
        with open("config.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["app"]["db_path"] = str(self.root / "t.db")
        data["app"]["log_file"] = str(self.root / "t.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["browser"]["accounts"] = []
        data["deepseek"]["api_key"] = "sk-test-key-123456"
        data["processor"]["reply_mode"] = "ai"
        data["reply_mode"] = "ai"
        data["collector"]["search_query"] = "iphone 17 обзор"
        data["limits"]["visibility_check_delay_seconds"] = 45
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.db_path = self.root / "t.db"
        self.run_db(lambda db: asyncio.sleep(0))  # creates the schema and the first project
        self.ctx = AppContext(self.config_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_db(self, fn):
        from config import load_config

        async def go():
            db = Database(str(self.db_path), project_seed=project_seed(load_config(self.config_path)))
            await db.connect()
            await db.init_schema()
            try:
                return await fn(db)
            finally:
                await db.close()

        return asyncio.run(go())

    def seed(self, proxy: str | None = "http://u:secret@5.5.5.5:8080", enabled: bool = True) -> None:
        async def fn(db):
            project = (await db.list_projects())[0]
            worker = await db.upsert_account("worker", str(self.root / "sessions" / "worker"))
            await db.add_project_accounts(project.id, [worker.id])
            await db.upsert_account("obs", str(self.root / "sessions" / "obs"), proxy_url=proxy, is_active=enabled)
            await db.set_observer("obs")

        self.run_db(fn)

    def controller(self) -> Controller:
        return Controller([build_tab(self.ctx)], status=lambda: status_cells(self.ctx))

    @staticmethod
    def screen(c: Controller) -> str:
        return frags_text(render(c))

    def raw(self) -> dict:
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8"))

    def open_screen(self) -> Controller:
        c = self.controller()
        c.select("observer")
        c.press("enter")
        return c


class TestObserverRow(TabCase):
    def test_row_without_an_observer(self) -> None:
        self.seed_worker_only()
        out = self.screen(self.controller())
        self.assertIn("Наблюдатель:", out)
        self.assertIn("○ не задан", out)

    def seed_worker_only(self) -> None:
        async def fn(db):
            project = (await db.list_projects())[0]
            worker = await db.upsert_account("worker", str(self.root / "sessions" / "worker"))
            await db.add_project_accounts(project.id, [worker.id])

        self.run_db(fn)

    def test_row_shows_name_and_proxy_host_but_no_credentials(self) -> None:
        self.seed()
        out = self.screen(self.controller())
        self.assertIn("● obs · прокси 5.5.5.5:8080", out)
        self.assertNotIn("secret", out)

    def test_row_says_so_without_a_proxy_or_when_disabled(self) -> None:
        self.seed(proxy=None)
        self.assertIn("● obs · без прокси", self.screen(self.controller()))
        self.run_db(lambda db: set_observer_enabled(db, "obs", False))
        self.assertIn("○ obs · проверки выключены", self.screen(self.controller()))

    def test_the_observer_is_not_a_posting_account_anywhere_on_the_tab(self) -> None:
        self.seed()
        from modules.ui.tab_launch import _active_count, _posting_count, _without_project

        self.assertEqual(_active_count(self.ctx), 1)
        self.assertEqual(_without_project(self.ctx), 0)
        project = self.ctx.projects()[0]
        self.assertEqual(_posting_count(self.ctx, [project.id]), 1)

    def test_an_observer_alone_does_not_count_as_an_account(self) -> None:
        self.run_db(lambda db: db.upsert_account("obs", str(self.root / "o")))
        self.run_db(lambda db: db.set_observer("obs"))
        self.assertEqual("".join(c.text for c in status_cells(self.ctx)), "нет аккаунтов")
        self.assertIn("добавить аккаунт", self.screen(self.controller()))


class TestObserverScreen(TabCase):
    def test_enter_opens_the_screen_with_the_rows_of_an_observer(self) -> None:
        self.seed()
        c = self.open_screen()
        out = self.screen(c)
        self.assertIn("Запуск › наблюдатель", out)
        for word in ("аккаунт", "прокси", "вход", "проверки", "задержка", "удалить наблюдателя"):
            self.assertIn(word, out)
        self.assertIn("5.5.5.5:8080", out)
        self.assertIn("45 с после ответа", out)
        self.assertIn("проверено: пока нет", out)
        self.assertNotIn("secret", out)
        c.press("escape")
        self.assertIn("Наблюдатель:", self.screen(c))  # back on the tab

    def test_the_screen_without_an_observer_offers_only_to_add_one(self) -> None:
        self.seed_none()
        c = self.open_screen()
        self.assertIn("не задан", self.screen(c))
        self.assertEqual([r.id for r in c.selectable_rows()], ["o-account"])

    def seed_none(self) -> None:
        self.run_db(lambda db: db.upsert_account("worker", str(self.root / "w")))

    def test_checks_toggle_with_the_arrows(self) -> None:
        self.seed()
        c = self.open_screen()
        c.select("o-on")
        c.press("right")
        self.assertFalse(self.run_db(lambda db: db.get_observer_account()).is_active)
        self.assertIn("выключены", c.notice.text)
        c.press("left")
        self.assertTrue(self.run_db(lambda db: db.get_observer_account()).is_active)

    def test_the_delay_is_written_to_the_config_and_a_bad_one_is_refused(self) -> None:
        self.seed()
        c = self.open_screen()
        c.select("o-delay")
        c.press("enter")
        self.assertIsInstance(c.edit.field, Field)
        c.press("c-u")
        for ch in "90":
            c.press(ch)
        c.press("enter")
        self.assertEqual(self.raw()["limits"]["visibility_check_delay_seconds"], 90)
        c.select("o-delay")
        c.press("enter")
        c.press("c-u")
        for ch in "abc":
            c.press(ch)
        c.press("enter")
        self.assertEqual(self.raw()["limits"]["visibility_check_delay_seconds"], 90)
        self.assertEqual(c.notice.kind, "err")

    def test_the_proxy_comes_from_the_proxy_library(self) -> None:
        self.seed(proxy=None)
        record = self.run_db(lambda db: db.add_proxy_url("http://u:p@7.7.7.7:3128"))[0]
        c = self.open_screen()
        c.select("o-proxy")
        c.press("enter")
        self.assertIsInstance(c.menu, Menu)
        out = self.screen(c)
        self.assertIn("7.7.7.7:3128", out)
        self.assertIn("вставить новый", out)
        c.press("enter")  # the first row: the library proxy
        self.assertEqual(self.run_db(lambda db: db.get_observer_account()).proxy_id, record.id)
        self.assertIn("● obs · прокси 7.7.7.7:3128", self.screen(self.controller()))
        self.assertIsNone(self.run_db(lambda db: db.get_account("worker")).proxy_id)  # a worker keeps its own

    def test_a_new_observer_is_added_from_a_line(self) -> None:
        self.seed_none()
        c = self.open_screen()
        c.press("enter")  # «новый аккаунт»
        self.assertIsInstance(c.menu, Menu)
        c.press("enter")
        self.assertIsInstance(c.edit.field, Field)
        c.press(f"paste:{OBSERVER_LINE}")
        c.press("enter")
        observer = self.run_db(lambda db: db.get_observer_account())
        self.assertEqual(observer.email, "obs@example.com")
        self.assertEqual([a.name for a in self.run_db(lambda db: db.list_active_accounts())], ["worker"])
        self.assertIn("добавлен", c.notice.text)
        self.assertNotIn("pw-fake-observer", self.screen(c))

    def test_an_existing_account_becomes_the_observer_after_a_question(self) -> None:
        self.seed_none()
        c = self.open_screen()
        c.press("enter")
        c.press("down")
        c.press("enter")  # «существующий аккаунт»
        c.press("enter")  # worker
        self.assertIsInstance(c.confirm, Confirm)
        c.press("enter")  # «Нет»
        self.assertIsNone(self.run_db(lambda db: db.get_observer_account()))
        c.press("enter")
        c.press("down")
        c.press("enter")
        c.press("enter")
        c.press("right")
        c.press("enter")
        self.assertEqual(self.run_db(lambda db: db.get_observer_account()).name, "worker")
        self.assertEqual(self.run_db(lambda db: db.list_active_accounts()), [])

    def test_removal_asks_first_and_keeps_the_profile_folder(self) -> None:
        self.seed()
        profile = self.root / "sessions" / "obs"
        profile.mkdir(parents=True)
        c = self.open_screen()
        c.select("o-remove")
        c.press("enter")
        self.assertIsInstance(c.confirm, Confirm)
        c.press("enter")  # «Нет»
        self.assertIsNotNone(self.run_db(lambda db: db.get_observer_account()))
        c.press("enter")
        c.press("right")
        c.press("enter")
        self.assertIsNone(self.run_db(lambda db: db.get_observer_account()))
        self.assertTrue(profile.exists())
        self.assertIn("анонимно", c.notice.text)

    def test_login_check_reads_cookies_only(self) -> None:
        self.seed()
        _cookie_profile(self.root / "sessions" / "obs", [("LOGIN_INFO", "x", ".youtube.com", int(time.time()) + 100)])
        c = self.open_screen()
        c.select("o-login")
        c.press("enter")
        c.press("down")
        c.press("down")
        c.press("enter")  # «проверить вход»
        self.assertEqual(c.notice.kind, "ok")
        self.assertIn("вошёл", c.notice.text)

    def test_login_check_of_a_chrome_observer_without_a_saved_session_says_so(self) -> None:
        from modules.ui.tab_observer import check_login

        obs = SimpleNamespace(name="obs", session_path=str(self.root / "nowhere"), engine="chrome")
        notice = check_login(obs)
        self.assertEqual(notice.kind, "warn")
        self.assertIn("Chrome", notice.text)


class TestObserverStaysOutOfOtherLists(TabCase):
    def test_the_accounts_tab_and_the_proxy_warning_do_not_list_the_observer(self) -> None:
        from modules.ui import tab_accounts

        self.seed(proxy=None)
        self.assertEqual([a.name for a, _, _ in tab_accounts.load(self.ctx)], ["worker"])
        self.run_db(lambda db: db.upsert_account("worker", is_active=False))  # only the proxy-less observer stays active
        self.assertEqual(self.ctx.query("SELECT 1 FROM accounts WHERE is_active = 1 AND role <> 'observer' AND proxy_id IS NULL"), [])

    def test_logged_in_names_ignore_the_observer(self) -> None:
        from config import load_config
        from modules.wizard import _logged_in_names

        self.seed()
        _cookie_profile(self.root / "sessions" / "obs", [("LOGIN_INFO", "x", ".youtube.com", int(time.time()) + 100)])
        self.assertNotIn("obs", _logged_in_names(load_config(self.config_path)))

    def test_the_proxy_offer_for_proxy_less_accounts_leaves_the_observer_out(self) -> None:
        self.seed(proxy=None)
        accounts = self.run_db(lambda db: db.list_accounts(is_active=True))
        self.assertEqual([a.name for a in accounts if a.proxy_url is None], ["worker"])


class TestRunnerSelectors(unittest.TestCase):
    def test_linked_comment_fallbacks_use_the_linked_attribute_not_the_badge(self) -> None:
        source = (Path(__file__).resolve().parent.parent / "modules" / "youtube_runner.py").read_text(encoding="utf-8")
        self.assertIn("ytd-comment-view-model[linked] #reply-button-end", source)
        self.assertIn("ytd-comment-renderer[linked] #reply-button-end", source)
        self.assertNotIn("[linked-comment-badge]", source)  # `div#linked-comment-badge` is in every view-model
        self.assertNotIn(":has(#linked-comment-badge)", source)


if __name__ == "__main__":
    unittest.main()
