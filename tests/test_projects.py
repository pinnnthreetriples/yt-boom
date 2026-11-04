"""Projects, data layer: migration of a database of the previous version, the first project «Основной», names,
members, the per-project video pool, the daily ceiling of a project, and the config / plan of one project's run."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from config import load_config
from database import (
    DEFAULT_PROJECT_NAME,
    PROJECTS_SEEDED_KEY,
    ActivityLogCreate,
    Database,
    LastProjectError,
    RecordNotFoundError,
    clean_project_name,
    clean_topics,
)
from modules.projects import project_config, project_plan, project_problems, project_seed

# The schema a database of the previous version (master @ 218d5d0) has: `sqlite_master` of `init_db` there.
MASTER_SCHEMA = """
CREATE TABLE test_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, base_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'idle' CHECK (status IN ('idle', 'busy', 'cooldown', 'disabled')),
    max_slots INTEGER NOT NULL DEFAULT 1 CHECK (max_slots > 0),
    active_slots INTEGER NOT NULL DEFAULT 0 CHECK (active_slots >= 0 AND active_slots <= max_slots),
    cooldown_until REAL, last_used_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE payloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER REFERENCES test_nodes(id) ON DELETE SET NULL,
    source_ref TEXT, source_text TEXT, text TEXT NOT NULL, language TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'submitted', 'validated', 'failed')),
    remote_id TEXT, error TEXT, created_at REAL NOT NULL, submitted_at REAL, validated_at REAL
);
CREATE TABLE execution_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, payload_id INTEGER NOT NULL REFERENCES payloads(id) ON DELETE CASCADE,
    node_id INTEGER REFERENCES test_nodes(id) ON DELETE SET NULL,
    kind TEXT NOT NULL DEFAULT 'submit' CHECK (kind IN ('submit', 'validate')),
    priority INTEGER NOT NULL DEFAULT 100,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0), scheduled_at REAL NOT NULL,
    started_at REAL, finished_at REAL, last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, video_id TEXT NOT NULL, video_title TEXT,
    video_url TEXT, target_id TEXT NOT NULL, target_author TEXT, target_text TEXT, reply_text TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('live', 'dry_run')),
    status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'simulated')),
    error TEXT, duration_sec REAL, payload_id INTEGER REFERENCES payloads(id) ON DELETE SET NULL
);
CREATE INDEX idx_nodes_status ON test_nodes(status, cooldown_until);
CREATE INDEX idx_payloads_status ON payloads(status, created_at);
CREATE INDEX idx_payloads_source_ref ON payloads(source_ref, status);
CREATE INDEX idx_queue_ready ON execution_queue(status, kind, scheduled_at, priority);
CREATE INDEX idx_activity_created ON activity_log(created_at DESC);
CREATE INDEX idx_activity_video ON activity_log(video_id);
CREATE INDEX idx_activity_target ON activity_log(target_id);
CREATE INDEX idx_activity_status ON activity_log(status);
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, session_path TEXT NOT NULL, proxy_url TEXT,
    is_active INTEGER NOT NULL DEFAULT 1, total_replies INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    last_used_at TEXT, email TEXT, password TEXT, totp_secret TEXT, recovery_email TEXT,
    auth_status TEXT DEFAULT 'pending', fingerprint_data TEXT, geo_country TEXT, expected_country TEXT, locale TEXT,
    lifecycle_stage TEXT NOT NULL DEFAULT 'quarantine', authenticated_at TEXT, quarantine_until TEXT,
    daily_comments_done INTEGER NOT NULL DEFAULT 0, daily_comments_limit INTEGER NOT NULL DEFAULT 0,
    daily_limit_reset_at TEXT, last_comment_at TEXT, warmup_sessions_done INTEGER NOT NULL DEFAULT 0,
    last_warmup_at TEXT, consecutive_errors INTEGER NOT NULL DEFAULT 0, profile_name TEXT, profile_description TEXT,
    profile_avatar TEXT, profile_applied TEXT, profile_applied_at TEXT, profile_name_changes TEXT,
    proxy_id INTEGER REFERENCES proxies(id) ON DELETE SET NULL, lifecycle_mode TEXT NOT NULL DEFAULT 'auto',
    auto_warmup INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE video_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT, video_url TEXT NOT NULL UNIQUE, video_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'completed', 'failed')),
    comments_target INTEGER NOT NULL DEFAULT 2 CHECK (comments_target > 0),
    comments_done INTEGER NOT NULL DEFAULT 0 CHECK (comments_done >= 0), created_at TEXT NOT NULL,
    last_processed_at TEXT, source TEXT NOT NULL DEFAULT 'links'
);
CREATE TABLE proxies (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scheme TEXT, host TEXT NOT NULL,
    port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535), login TEXT NOT NULL DEFAULT '', password TEXT,
    country TEXT, country_source TEXT, label TEXT, last_check_at TEXT, last_ok INTEGER, last_exit_ip TEXT,
    last_exit_country TEXT, last_latency_ms INTEGER, last_error TEXT, created_at TEXT NOT NULL,
    UNIQUE (host, port, login)
);
CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX idx_accounts_name ON accounts(name);
CREATE INDEX idx_accounts_active ON accounts(is_active);
CREATE INDEX idx_video_pool_status ON video_pool(status, id);
CREATE INDEX idx_accounts_proxy ON accounts(proxy_id);
PRAGMA user_version = 2;
"""

SEED = {
    "search_topics": ["биткоин новости"],
    "min_views": 10_000,
    "max_views": 500_000,
    "max_age_hours": 48,
    "max_videos": 7,
    "comments_per_video": 3,
    "target_mode": "popular_parent",
    "reply_mode": "custom",
    "custom_replies": ["Отличное видео", "Спасибо!"],
    "paraphrase_custom": True,
}


def make_master_db(path: Path) -> None:
    """A database as the previous version leaves it: two accounts, own links and search finds, a journal record."""
    conn = sqlite3.connect(path)
    conn.executescript(MASTER_SCHEMA)
    now = "2026-11-01T10:00:00+00:00"
    conn.executemany(
        "INSERT INTO accounts (name, session_path, created_at) VALUES (?, ?, ?)",
        [("alpha", "s/alpha", now), ("beta", "s/beta", now)],
    )
    conn.executemany(
        "INSERT INTO video_pool (video_url, video_id, status, comments_target, comments_done, created_at, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("https://www.youtube.com/watch?v=aaaaaaaaaaa", "aaaaaaaaaaa", "pending", 2, 0, now, "links"),
            ("https://www.youtube.com/watch?v=bbbbbbbbbbb", "bbbbbbbbbbb", "completed", 2, 2, now, "links"),
            ("https://www.youtube.com/watch?v=ccccccccccc", "ccccccccccc", "in_progress", 2, 1, now, "search"),
        ],
    )
    conn.execute(
        "INSERT INTO activity_log (created_at, video_id, target_id, mode, status) VALUES (?, 'aaaaaaaaaaa', 'c1', 'dry_run', 'simulated')",
        (now,),
    )
    conn.commit()
    conn.close()


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.path = self.root / "p.db"
        self.dbs: list[Database] = []

    async def asyncTearDown(self):
        for db in self.dbs:
            await db.close()
        self.temp_dir.cleanup()

    async def open(self, seed=None) -> Database:
        db = Database(self.path, project_seed=seed)
        await db.connect()
        await db.init_schema()
        self.dbs.append(db)
        return db

    async def account(self, db: Database, name: str) -> int:
        return (await db.upsert_account(name, session_path=f"s/{name}")).id


class MigrationTest(DbCase):
    async def test_old_database_moves_into_the_first_project(self):
        make_master_db(self.path)
        db = await self.open(SEED)

        projects = await db.list_projects()
        self.assertEqual([p.name for p in projects], [DEFAULT_PROJECT_NAME])
        main = projects[0]
        self.assertEqual(main.search_topics, ["биткоин новости"])
        self.assertEqual((main.min_views, main.max_views, main.max_age_hours), (10_000, 500_000, 48))
        self.assertEqual((main.max_videos, main.comments_per_video, main.target_mode), (7, 3, "popular_parent"))
        self.assertEqual((main.reply_mode, main.custom_replies, main.paraphrase_custom),
                         ("custom", ["Отличное видео", "Спасибо!"], True))
        self.assertTrue(main.enabled)
        self.assertEqual([a.name for a in await db.list_project_accounts(main.id)], ["alpha", "beta"])

        videos = await db.list_pool_videos(status="all")
        self.assertEqual(len(videos), 3)
        self.assertEqual({v.project_id for v in videos}, {main.id})
        self.assertEqual([(v.id, v.status, v.comments_done, v.source) for v in videos],
                         [(1, "pending", 0, "links"), (2, "completed", 2, "links"), (3, "in_progress", 1, "search")])
        self.assertEqual((main.links_total, main.links_waiting), (2, 1))
        self.assertTrue(list(self.root.glob("p.db.bak-projects-*")), "a copy of the file is made before the rebuild")

        log = await db.list_activity_logs()
        self.assertEqual(len(log), 1)
        self.assertIsNone(log[0].project_id)  # history from before the projects stays without one
        self.assertIsNotNone(await db.get_setting(PROJECTS_SEEDED_KEY))

    async def test_the_rebuilt_pool_allows_one_link_in_several_projects(self):
        make_master_db(self.path)
        db = await self.open(SEED)
        other = await db.create_project("Фитнес")
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        item = await db.add_video_to_pool(url, 2, project_id=other.id)
        self.assertEqual(item.project_id, other.id)
        await db.add_video_to_pool(url, 4, project_id=other.id)  # the same project: updated, not duplicated
        self.assertEqual(await db.count_pool_videos(project_id=other.id), 1)
        self.assertEqual(len([v for v in await db.list_pool_videos("all") if v.video_url == url]), 2)
        conn = sqlite3.connect(self.path)
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'video_pool'").fetchone()[0]
        conn.close()
        self.assertIn("UNIQUE (project_id, video_url)", ddl)
        self.assertNotIn("video_url         TEXT    NOT NULL UNIQUE", ddl)

    async def test_migration_is_idempotent_and_a_deleted_first_project_stays_deleted(self):
        make_master_db(self.path)
        db = await self.open(SEED)
        await db.init_schema(SEED)
        self.assertEqual(len(await db.list_projects()), 1)
        self.assertEqual(len(list(self.root.glob("p.db.bak-projects-*"))), 1)
        main = (await db.list_projects())[0]
        other = await db.create_project("Крипта")
        await db.delete_project(main.id)
        await db.close()
        again = await self.open(SEED)
        self.assertEqual([p.name for p in await again.list_projects()], ["Крипта"])
        self.assertEqual(other.id, (await again.list_projects())[0].id)

    async def test_fresh_database_gets_an_empty_first_project(self):
        db = await self.open(SEED)
        projects = await db.list_projects()
        self.assertEqual([(p.name, p.accounts_count, p.search_topics) for p in projects],
                         [(DEFAULT_PROJECT_NAME, 0, ["биткоин новости"])])
        await self.account(db, "new")  # decision 7: a new account joins no project
        self.assertEqual((await db.list_projects())[0].accounts_count, 0)

    async def test_without_a_seed_the_first_project_takes_the_defaults(self):
        db = await self.open()
        main = (await db.list_projects())[0]
        self.assertEqual((main.reply_mode, main.search_topics, main.max_videos, main.comments_per_video), ("ai", [], 5, 2))

    async def test_a_bad_value_of_the_config_keeps_the_default(self):
        db = await self.open({**SEED, "target_mode": "nonsense", "max_videos": 500, "max_views": 5})
        main = (await db.list_projects())[0]
        self.assertEqual((main.target_mode, main.max_videos, main.max_views), ("first_reply", 5, None))
        self.assertEqual(main.search_topics, ["биткоин новости"])

    async def test_seed_from_config_yaml(self):
        data = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
        data["collector"].update(search_query="  тема  ", min_views=5, max_views=None, max_age_hours=None,
                                 max_videos=9, target_mode="mixed")
        data["limits"]["max_iterations_per_session"] = 4
        data["processor"].update(reply_mode="custom", custom_replies=["a", "b"], paraphrase_custom=False)
        path = self.root / "c.yaml"
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        seed = project_seed(load_config(path))
        self.assertEqual(seed["search_topics"], ["тема"])
        self.assertEqual((seed["comments_per_video"], seed["max_videos"], seed["target_mode"]), (4, 9, "mixed"))
        self.assertEqual((seed["reply_mode"], seed["custom_replies"], seed["paraphrase_custom"]), ("custom", ["a", "b"], False))
        data["collector"]["search_query"] = None
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.assertEqual(project_seed(load_config(path))["search_topics"], [])

    async def test_journal_records_project_and_account(self):
        db = await self.open()
        acc = await self.account(db, "a")
        project = (await db.list_projects())[0]
        entry = await db.log_activity(ActivityLogCreate(video_id="v", target_id="t", project_id=project.id, account_id=acc))
        self.assertEqual((entry.project_id, entry.account_id), (project.id, acc))
        self.assertEqual(len(await db.list_activity_logs(project_id=project.id)), 1)
        self.assertEqual(await db.list_activity_logs(project_id=project.id + 1), [])


class ProjectCrudTest(DbCase):
    async def test_names_are_trimmed_unique_without_case_and_bounded(self):
        db = await self.open()
        project = await db.create_project("  Крипта  ")
        self.assertEqual(project.name, "Крипта")
        for bad in ("крипта", "КРИПТА ", "", "   ", "a" * 41, "две\nстроки"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                await db.create_project(bad)
        self.assertEqual(clean_project_name("a" * 40), "a" * 40)
        fit = await db.create_project("Фитнес")
        with self.assertRaises(ValueError):
            await db.rename_project(fit.id, "КРИПТА")
        self.assertEqual((await db.rename_project(fit.id, "фитнес")).name, "фитнес")  # its own name in another case
        self.assertEqual((await db.get_project_by_name("ФИТНЕС")).id, fit.id)

    async def test_topics_are_clean_and_limited(self):
        self.assertEqual(clean_topics([" биткоин ", "Биткоин", "", "эфир  цена", None]), ["биткоин", "эфир цена"])
        with self.assertRaises(ValueError):
            clean_topics([f"t{i}" for i in range(21)])
        db = await self.open()
        project = await db.create_project("Темы", search_topics=["Эфир", "эфир", "крипта"])
        self.assertEqual(project.search_topics, ["Эфир", "крипта"])

    async def test_update_validates_fields(self):
        db = await self.open()
        project = await db.create_project("P")
        self.assertEqual((project.reply_mode, project.daily_per_account), ("ai", None))
        project = await db.update_project(project.id, daily_per_account=5, min_views=100, max_views=0, use_similar=True)
        self.assertEqual((project.daily_per_account, project.min_views, project.max_views, project.use_similar),
                         (5, 100, None, True))
        for field, value in (("max_videos", 0), ("comments_per_video", 101), ("reply_mode", "x"),
                             ("target_mode", "x"), ("daily_per_account", -1), ("unknown", 1)):
            with self.assertRaises(ValueError, msg=field):
                await db.update_project(project.id, **{field: value})
        with self.assertRaises(ValueError):
            await db.update_project(project.id, max_views=50)  # below the stored minimum
        with self.assertRaises(RecordNotFoundError):
            await db.update_project(999, enabled=False)

    async def test_copy_takes_settings_without_accounts_and_links(self):
        db = await self.open()
        acc = await self.account(db, "a")
        source = await db.create_project("Исходный", search_topics=["x"], reply_mode="custom", custom_replies=["t"])
        await db.add_project_accounts(source.id, [acc])
        await db.add_video_to_pool("https://www.youtube.com/watch?v=aaaaaaaaaaa", 2, project_id=source.id)
        copy = await db.copy_project(source.id, "Копия")
        self.assertEqual((copy.search_topics, copy.reply_mode, copy.custom_replies), (["x"], "custom", ["t"]))
        self.assertEqual((copy.accounts_count, copy.links_total), (0, 0))

    async def test_order_and_moves(self):
        db = await self.open()
        a = await db.create_project("A")
        b = await db.create_project("B")
        names = lambda projects: [p.name for p in projects]  # noqa: E731
        self.assertEqual(names(await db.list_projects()), [DEFAULT_PROJECT_NAME, "A", "B"])
        self.assertTrue(await db.move_project(b.id, -1))
        self.assertEqual(names(await db.list_projects()), [DEFAULT_PROJECT_NAME, "B", "A"])
        self.assertFalse(await db.move_project(a.id, 1))
        await db.set_project_order([a.id])
        self.assertEqual(names(await db.list_projects()), ["A", DEFAULT_PROJECT_NAME, "B"])

    async def test_delete_cascades_and_keeps_the_journal(self):
        db = await self.open()
        acc = await self.account(db, "a")
        main = (await db.list_projects())[0]
        doomed = await db.create_project("Удаляемый")
        await db.add_project_accounts(doomed.id, [acc])
        await db.add_video_to_pool("https://www.youtube.com/watch?v=aaaaaaaaaaa", 2, project_id=doomed.id)
        await db.queue_search_video("https://www.youtube.com/watch?v=bbbbbbbbbbb", 2, project_id=doomed.id)
        await db.log_activity(ActivityLogCreate(video_id="v", target_id="t", project_id=doomed.id, account_id=acc))
        await db.set_setting("ui_project", str(doomed.id))
        result = await db.delete_project(doomed.id)
        self.assertEqual((result.accounts, result.links), (1, 1))
        self.assertEqual(await db.count_pool_videos(project_id=doomed.id), 0)
        self.assertEqual(await db.projects_of_account(acc), [])
        log = await db.list_activity_logs()
        self.assertEqual(log[0].project_id, doomed.id)  # kept: shown as a deleted project
        self.assertIsNone(await db.get_setting("ui_project"))
        with self.assertRaises(LastProjectError):
            await db.delete_project(main.id)
        with self.assertRaises(RecordNotFoundError):
            await db.delete_project(doomed.id)

    async def test_foreign_keys_cascade_on_every_connection(self):
        db = await self.open()
        acc = await self.account(db, "a")
        project = await db.create_project("P")
        await db.add_project_accounts(project.id, [acc])
        await db.add_video_to_pool("https://www.youtube.com/watch?v=aaaaaaaaaaa", 2, project_id=project.id)
        await db.log_activity(ActivityLogCreate(video_id="v", target_id="t", project_id=project.id, account_id=acc))
        async with db._transaction() as conn:  # an account deleted outside the program's own methods
            await conn.execute("DELETE FROM accounts WHERE id = ?", (acc,))
        self.assertEqual(await db.list_project_accounts(project.id), [])
        self.assertIsNone((await db.list_activity_logs())[0].account_id)
        async with db._transaction() as conn:  # a project deleted by a plain DELETE: the foreign keys clean up
            await conn.execute("DELETE FROM projects WHERE id = ?", (project.id,))
        self.assertEqual(await db.count_pool_videos(project_id=project.id), 0)

    async def test_members_and_their_projects(self):
        db = await self.open()
        a, b = await self.account(db, "a"), await self.account(db, "b")
        await db.set_account_active(b, False)
        p1, p2 = await db.create_project("P1"), await db.create_project("P2")
        self.assertEqual(await db.add_project_accounts(p1.id, [a, b, a]), 2)
        self.assertEqual(await db.add_project_accounts(p1.id, [a]), 0)
        await db.add_project_accounts(p2.id, [a])
        self.assertEqual([x.name for x in await db.list_project_accounts(p1.id)], ["a", "b"])
        self.assertEqual([x.name for x in await db.list_project_accounts(p1.id, active_only=True)], ["a"])
        self.assertEqual([p.name for p in await db.projects_of_account(a)], ["P1", "P2"])
        self.assertEqual(await db.remove_project_accounts(p1.id, [a, a]), 1)
        self.assertEqual([p.name for p in await db.projects_of_account(a)], ["P2"])
        with self.assertRaises(RecordNotFoundError):
            await db.add_project_accounts(p1.id, [999])


class PoolTest(DbCase):
    async def test_pool_reads_and_writes_are_per_project(self):
        db = await self.open()
        main = (await db.list_projects())[0]
        other = await db.create_project("Другой")
        url1, url2 = "https://www.youtube.com/watch?v=aaaaaaaaaaa", "https://www.youtube.com/watch?v=bbbbbbbbbbb"
        await db.add_video_to_pool(url1, 2)  # no project given: the fallback project «Основной»
        await db.add_video_to_pool(url2, 2, project_id=other.id)
        self.assertEqual((await db.get_next_pool_video(other.id)).video_url, url2)
        self.assertEqual((await db.get_next_pool_video(main.id)).video_url, url1)
        self.assertEqual(await db.count_pool_videos("pending", project_id=other.id), 1)
        self.assertEqual(await db.set_pool_comments_target(5, project_id=other.id), 1)
        self.assertEqual((await db.list_pool_videos(project_id=main.id))[0].comments_target, 2)
        self.assertEqual(await db.clear_video_pool(other.id), 1)
        self.assertEqual(await db.count_pool_videos(), 1)
        with self.assertRaises(RecordNotFoundError):
            await db.add_video_to_pool(url1, 2, project_id=999)

    async def test_search_find_inside_a_project_keeps_its_own_link(self):
        db = await self.open()
        project = await db.create_project("P")
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        await db.add_video_to_pool(url, 2, project_id=project.id)
        found = await db.queue_search_video(url, 3, project_id=project.id)
        self.assertEqual((found.source, found.comments_target), ("links", 2))
        elsewhere = await db.queue_search_video(url, 3, project_id=(await db.list_projects())[0].id)
        self.assertEqual(elsewhere.source, "search")


class DailyCeilingTest(DbCase):
    async def test_ceiling_counts_per_project_and_restarts_with_the_day(self):
        db = await self.open()
        acc = await self.account(db, "a")
        p1 = await db.create_project("P1", daily_per_account=2)
        p2 = await db.create_project("P2")
        await db.add_project_accounts(p1.id, [acc])
        await db.add_project_accounts(p2.id, [acc])
        now = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(await db.project_daily_gate(p1.id, acc, now), (0, 2, True))
        await db.bump_project_daily(p1.id, acc, now)
        await db.bump_project_daily(p1.id, acc, now)
        self.assertEqual(await db.project_daily_gate(p1.id, acc, now), (2, 2, False))
        self.assertEqual(await db.project_daily_gate(p2.id, acc, now), (0, None, True))  # no ceiling there
        tomorrow = now + timedelta(days=1)  # past the 04:00 UTC boundary
        self.assertEqual(await db.project_daily_gate(p1.id, acc, tomorrow), (0, 2, True))
        await db.bump_project_daily(p1.id, acc, tomorrow)
        self.assertEqual(await db.project_daily_gate(p1.id, acc, tomorrow), (1, 2, True))

    async def test_an_account_outside_the_project_may_not_answer_in_it(self):
        db = await self.open()
        acc = await self.account(db, "a")
        project = await db.create_project("P")
        self.assertFalse((await db.project_daily_gate(project.id, acc)).allowed)
        await db.bump_project_daily(project.id, acc)  # nothing happens
        self.assertEqual(await db.list_project_accounts(project.id), [])


class ProjectConfigTest(DbCase):
    async def test_project_config_lays_the_project_over_a_copy(self):
        cfg = load_config("config.yaml")
        cfg.processor.reply_mode = "ai"
        db = await self.open()
        project = await db.create_project(
            "P", search_topics=["a"], min_views=10, max_views=100, max_age_hours=24, max_videos=3, comments_per_video=4,
            target_mode="mixed", reply_mode="custom", custom_replies=["t1", "t2"], paraphrase_custom=True,
        )
        run = project_config(cfg, project, "тема")
        self.assertEqual((run.collector.search_query, run.collector.min_views, run.collector.max_views,
                          run.collector.max_age_hours, run.collector.max_videos, run.collector.comments_per_video,
                          run.collector.target_mode), ("тема", 10, 100, 24, 3, 4, "mixed"))
        self.assertEqual((run.processor.reply_mode, run.reply_mode), ("custom", "custom"))
        self.assertEqual((run.processor.custom_replies, run.custom_replies), (["t1", "t2"], ["t1", "t2"]))
        self.assertTrue(run.processor.paraphrase_custom)
        self.assertEqual(run.limits.max_iterations_per_session, 4)
        self.assertEqual(cfg.processor.reply_mode, "ai")  # the global config is not touched
        self.assertEqual(run.deepseek.api_key, cfg.deepseek.api_key)

    async def test_plan_and_problems(self):
        db = await self.open()
        project = await db.create_project("P")
        self.assertEqual(project_plan(project), ([], "нет тем и ссылок"))
        self.assertEqual(project_problems(project, 0, True), ["нет аккаунтов", "нет тем и ссылок"])
        project = await db.update_project(project.id, search_topics=["a", "b"], use_similar=True, use_title_words=True,
                                          reply_mode="custom")
        self.assertEqual([s.kind for s in project_plan(project).steps], ["topic", "topic"])  # no links: no references
        self.assertEqual([s.kind for s in project_plan(project, 2, 3).steps],
                         ["topic", "topic", "similar", "title_words", "links"])
        self.assertEqual(project_plan(project.model_copy(update={"search_topics": []}), 0, 3).reason, "")
        self.assertEqual(project_plan(project.model_copy(update={"search_topics": [], "use_similar": False,
                                                                 "use_title_words": False}), 0, 3).reason,
                         "свои ссылки пройдены, тем нет")
        acc = await self.account(db, "a")
        await db.add_project_accounts(project.id, [acc])
        project = await db.get_project(project.id)
        self.assertEqual(project_problems(project, 0, False), ["нет аккаунтов в работе", "нет шаблонов"])
        project = await db.update_project(project.id, reply_mode="ai")
        self.assertEqual(project_problems(project, 1, False), ["нет ключа DeepSeek"])
        self.assertEqual(project_problems(project, 1, True), [])


if __name__ == "__main__":
    unittest.main()
