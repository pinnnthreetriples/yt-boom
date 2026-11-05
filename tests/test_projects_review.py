"""Projects, review fixes: warm-ups finish on every exit and are shared by a launch, no rebuild without a backup, the
project's daily ceiling never breaks the record of a published reply (and its gate only reads), runs without
--project take the project's target and replies, a failed migration is shown and closes its connection, accounts of
config.yaml on the first start join «Основной», a dry run leaves the pool as it was."""
from __future__ import annotations

import asyncio
import random
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import yaml

from config import load_config
from database import Database, MigrationError
from modules.multi_account_orchestrator import MultiAccountOrchestrator, WarmupLedger
from modules.projects import project_seed
from tests.test_multi_account_orchestrator import _make_test_config
from tests.test_projects import SEED, make_master_db
from tests.test_projects_run import FakeCollector, RunCase, found, watch


class WarmupTest(RunCase):
    def warmer(self, success: bool = True) -> MagicMock:
        async def warm_up(acc, db=None):
            await asyncio.sleep(0.05)
            self.warmed.append(acc.name)
            return SimpleNamespace(success=success)

        self.warmed: list[str] = []
        return MagicMock(warm_up=AsyncMock(side_effect=warm_up))

    def live(self, project, warmer, ledger=None, collector=None):
        return MultiAccountOrchestrator(
            self.config, db=self.db, dry_run=False, collector=collector or FakeCollector(), runner_factory=self.runner_factory,
            rng=random.Random(1), project=project, warmer=warmer, warmups=ledger,
        )

    async def setup_accounts(self):
        ready = await self.account("ready", "unlimited", 100)
        young = await self.account("young", None, 1)  # quarantine: the gate starts a warm-up
        return await self.project("P", [ready, young], search_topics=["nothing here"])

    async def test_a_project_without_videos_waits_for_its_warmups(self):
        project = await self.setup_accounts()
        warmer = self.warmer()
        orchestrator = self.live(project, warmer)
        result = await orchestrator.run_project()
        self.assertEqual(result.skipped, "видео не найдено")
        self.assertEqual(self.warmed, ["young"])  # finished before run_project returned, not cancelled by close()
        await orchestrator.close()

    async def test_the_launch_warms_an_account_once_and_does_not_retry_a_failed_one(self):
        project = await self.setup_accounts()
        other = await self.project("Q", [(await self.db.get_account("young")).id, (await self.db.get_account("ready")).id],
                                   search_topics=["nothing either"])
        young = (await self.db.get_account("young")).id
        ledger = WarmupLedger()
        warmer = self.warmer(success=False)
        for p in (project, other):
            orchestrator = self.live(p, warmer, ledger)
            await orchestrator.run_project()
            await orchestrator.close()
        self.assertEqual(warmer.warm_up.await_count, 1)
        self.assertIn(young, ledger.failed_at)
        self.assertIn(young, ledger.started)

    async def test_run_search_waits_for_warmups_when_nothing_is_found(self):
        await self.account("ready", "unlimited", 100)
        await self.account("young", None, 1)
        warmer = self.warmer()
        orchestrator = self.live(None, warmer)
        result = await orchestrator.run_search("nothing", max_videos=1)
        self.assertEqual(result.found, 0)
        self.assertEqual(self.warmed, ["young"])
        await orchestrator.close()


class CeilingWriteTest(RunCase):
    async def test_the_gate_only_reads_and_the_bump_writes_the_new_day(self):
        a = await self.account("a")
        project = await self.project("P", [a], daily_per_account=2)
        now = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
        await self.db.bump_project_daily(project.id, a, now)
        await self.db.bump_project_daily(project.id, a, now)
        tomorrow = now + timedelta(days=1)
        self.assertEqual(await self.db.project_daily_gate(project.id, a, tomorrow), (0, 2, True))
        row = await self.db._fetch_one("SELECT daily_done, daily_reset_at FROM project_accounts WHERE account_id = ?", (a,))
        self.assertEqual(row["daily_done"], 2)  # the gate did not write
        await self.db.bump_project_daily(project.id, a, tomorrow)
        row = await self.db._fetch_one("SELECT daily_done, daily_reset_at FROM project_accounts WHERE account_id = ?", (a,))
        self.assertEqual(row["daily_done"], 1)
        self.assertEqual(row["daily_reset_at"], Database.next_daily_reset(tomorrow).isoformat())

    async def test_a_failed_bump_never_breaks_the_record_of_a_published_reply(self):
        a = await self.account("a", "unlimited", 100)
        project = await self.project("P", [a], ["videoaaaaa1"], comments_per_video=1, daily_per_account=5)
        orchestrator = self.orchestrator(project, dry_run=False)
        with patch.object(self.db, "bump_project_daily", AsyncMock(side_effect=aiosqlite.OperationalError("database is locked"))):
            result = await orchestrator.run_project()
        self.assertEqual(result.replies, 1)
        log = await self.db.list_activity_logs()
        self.assertEqual([entry.status for entry in log], ["success"])
        self.assertTrue(await self.db.has_replied_to(self.posted[0][1]))
        self.assertEqual((await self.db.list_pool_videos("all", project_id=project.id))[0].comments_done, 1)


class DryRunPoolTest(RunCase):
    async def test_a_dry_run_leaves_own_links_and_finds_as_they_were(self):
        for mode in ("each", "share"):
            with self.subTest(mode=mode):
                a = await self.account(f"a_{mode}")
                project = await self.project(f"P_{mode}", [a], ["ownlink0001"], search_topics=["t"], comments_per_video=1)
                collector = FakeCollector(searches={"t": [found("searchvid01")]})
                result = await self.orchestrator(project, collector, dry_run=True, account_mode=mode).run_project()
                self.assertEqual(result.replies, 2)
                pool = await self.db.list_pool_videos("all", project_id=project.id)
                self.assertEqual({(v.source, v.status, v.comments_done) for v in pool},
                                 {("links", "pending", 0), ("search", "pending", 0)})

    async def test_a_live_run_still_records_progress(self):
        a = await self.account("a", "unlimited", 100)
        project = await self.project("P", [a], ["ownlink0001"], comments_per_video=1)
        await self.orchestrator(project, dry_run=False).run_project()
        video = (await self.db.list_pool_videos("all", project_id=project.id))[0]
        self.assertEqual((video.status, video.comments_done), ("completed", 1))


class MigrationSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "old.db"
        make_master_db(self.path)

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_no_backup_no_rebuild_and_the_connection_is_closed(self):
        db = Database(self.path, project_seed=SEED)
        await db.connect()
        with patch.object(Database, "_backup_before_migration", AsyncMock(return_value=None)), \
                self.assertRaises(MigrationError) as caught:
            await db.init_schema()
        self.assertIn("резервную копию", str(caught.exception))
        self.assertFalse(db.is_connected)  # aiosqlite's thread is stopped: the program can exit
        conn = sqlite3.connect(self.path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(video_pool)")}
        count = conn.execute("SELECT COUNT(*) FROM video_pool").fetchone()[0]
        conn.close()
        self.assertNotIn("project_id", columns)
        self.assertEqual(count, 3)

    async def test_the_interface_shows_why_the_database_did_not_update(self):
        from modules.ui.app import AppContext, _ensure_schema, status_cells

        config_path = Path(self.temp_dir.name) / "c.yaml"
        data = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
        data["app"]["db_path"] = str(self.path)
        data["app"]["log_file"] = str(Path(self.temp_dir.name) / "c.log")
        config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        ctx = AppContext(config_path)
        with patch.object(Database, "_backup_before_migration", AsyncMock(return_value=None)):
            error = _ensure_schema(ctx)
        self.assertTrue(error.startswith("База не обновилась: список видео не перестроен"), error)
        self.assertIn("копии нет", error)
        self.assertEqual(status_cells(ctx)[0].text, "база не обновилась")
        copy = Path(self.temp_dir.name) / "old.db.bak-projects-20261104-000000"
        copy.write_bytes(b"x")
        with patch.object(Database, "_backup_before_migration", AsyncMock(return_value=None)):
            self.assertIn(f"копия: {copy}", _ensure_schema(ctx))


class SettingsWithoutProjectTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config_path = root / "c.yaml"
        data = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
        data["app"]["db_path"] = str(root / "c.db")
        data["app"]["log_file"] = str(root / "c.log")
        data["browser"]["accounts"] = []
        data["collector"].update(search_query=None, min_views=1, target_mode="first_reply")
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.db(lambda db: db.list_projects())  # seeds «Основной» from the config above
        main = self.db(lambda db: db.list_projects())[0]
        self.db(lambda db: db.update_project(main.id, min_views=777, target_mode="popular_parent", reply_mode="custom",
                                             custom_replies=["Из проекта"], search_topics=["тема проекта"]))

    def tearDown(self):
        self.temp_dir.cleanup()

    def db(self, fn):
        async def run():
            async with Database(load_config(self.config_path).app.db_path,
                                project_seed=project_seed(load_config(self.config_path))) as db:
                return await fn(db)

        return asyncio.run(run())

    def main(self, *argv: str) -> int:
        import sys

        from youtube_bot import main as bot_main

        with patch.object(sys, "argv", ["youtube_bot.py", "--config", str(self.config_path), *argv]):
            try:
                bot_main()
            except SystemExit as exc:
                return int(exc.code or 0)
        return 0

    def test_search_without_project_takes_the_filters_of_main(self):
        with patch("youtube_bot.run_search_cli", AsyncMock()) as run:
            self.main("--search", "x", "--dry-run")
        self.assertEqual((run.call_args.kwargs["min_views"], run.call_args.kwargs["target_mode"]), (777, "popular_parent"))

    def test_run_search_cli_replies_with_the_projects_templates(self):
        from youtube_bot import run_search_cli

        seen = {}

        class Fake:
            def __init__(self, config_or_path=None, **kwargs):
                seen["cfg"] = config_or_path

            async def run_search(self, *args, **kwargs):
                return "ok"

            async def close(self):
                pass

        with patch("modules.multi_account_orchestrator.MultiAccountOrchestrator", Fake):
            self.assertEqual(asyncio.run(run_search_cli("x", str(self.config_path), dry_run=True)), "ok")
        cfg = seen["cfg"]
        self.assertEqual((cfg.processor.reply_mode, cfg.processor.custom_replies), ("custom", ["Из проекта"]))

    def test_account_without_search_uses_the_projects_topic(self):
        with patch("youtube_bot.run_multi_video_pipeline", AsyncMock()) as run:
            self.main("--account", "someone", "--dry-run")
        self.assertEqual(run.call_args.args[0], "тема проекта")

    def test_several_projects_without_main_need_project(self):
        main = self.db(lambda db: db.list_projects())[0]
        self.db(lambda db: db.rename_project(main.id, "Крипта"))
        self.db(lambda db: db.create_project("Фитнес"))
        with patch("youtube_bot.run_search_cli", AsyncMock()) as run:
            self.assertEqual(self.main("--search", "x", "--dry-run"), 1)
        run.assert_not_called()

    def test_a_single_project_with_another_name_is_used(self):
        main = self.db(lambda db: db.list_projects())[0]
        self.db(lambda db: db.rename_project(main.id, "Крипта"))
        with patch("youtube_bot.run_search_cli", AsyncMock()) as run:
            self.main("--search", "x", "--dry-run")
        self.assertEqual(run.call_args.kwargs["min_views"], 777)


class ConfigAccountsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.config_path = root / "c.yaml"
        data = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
        data["app"]["db_path"] = str(root / "c.db")
        data["app"]["log_file"] = str(root / "c.log")
        data["browser"]["session_root"] = str(root / "sessions")
        data["browser"]["accounts"] = [{"name": "from_yaml", "session_path": str(root / "sessions" / "y")}]
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def members(self) -> list[str]:
        async def run():
            async with Database(load_config(self.config_path).app.db_path) as db:
                return [a.name for a in await db.list_project_accounts((await db.list_projects())[0].id)]

        return asyncio.run(run())

    def test_the_first_start_of_the_interface_puts_them_into_main(self):
        from modules.ui.app import AppContext, _ensure_schema

        self.assertEqual(_ensure_schema(AppContext(self.config_path)), "")
        self.assertEqual(self.members(), ["from_yaml"])

    def test_sync_on_the_seeding_start_adopts_but_not_later(self):
        from modules.proxy_library import sync_config_accounts

        cfg = load_config(self.config_path)

        async def first():
            async with Database(cfg.app.db_path, project_seed=project_seed(cfg)) as db:
                await sync_config_accounts(db, cfg.browser.accounts)

        asyncio.run(first())
        self.assertEqual(self.members(), ["from_yaml"])

        async def later():
            async with Database(cfg.app.db_path) as db:
                await db.upsert_account("added_later", "s/x")
                await sync_config_accounts(db, [cfg.browser.accounts[0].model_copy(update={"name": "yaml_later"})])

        asyncio.run(later())
        self.assertEqual(self.members(), ["from_yaml"])  # a later import stays without a project

    def test_with_several_projects_nothing_is_adopted(self):
        cfg = load_config(self.config_path)

        async def run():
            async with Database(cfg.app.db_path, project_seed=project_seed(cfg)) as db:
                await db.create_project("Второй")
                account = await db.upsert_account("x", "s/x")
                self.assertEqual(await db.adopt_config_accounts([account.id]), 0)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
