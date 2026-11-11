from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import aiosqlite
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from config import EngineType, get_account_session_path, validate_proxy_for_engine
from secret_store import SecretStoreError, protect_secret, reveal_secret

NodeStatus = Literal["idle", "busy", "cooldown", "disabled"]
PayloadStatus = Literal["pending", "submitted", "validated", "failed"]
QueueStatus = Literal["queued", "running", "done", "failed"]
QueueKind = Literal["submit", "validate"]
ActivityMode = Literal["live", "dry_run"]
ActivityStatus = Literal["success", "failed", "simulated"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS test_nodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL UNIQUE,
    base_url      TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'idle'
                          CHECK (status IN ('idle', 'busy', 'cooldown', 'disabled')),
    max_slots     INTEGER NOT NULL DEFAULT 1 CHECK (max_slots > 0),
    active_slots  INTEGER NOT NULL DEFAULT 0
                          CHECK (active_slots >= 0 AND active_slots <= max_slots),
    cooldown_until REAL,
    last_used_at  REAL,
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS payloads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id      INTEGER REFERENCES test_nodes(id) ON DELETE SET NULL,
    source_ref   TEXT,
    source_text  TEXT,
    text         TEXT    NOT NULL,
    language     TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'submitted', 'validated', 'failed')),
    remote_id    TEXT,
    error        TEXT,
    created_at   REAL    NOT NULL,
    submitted_at REAL,
    validated_at REAL
);

CREATE TABLE IF NOT EXISTS execution_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    payload_id   INTEGER NOT NULL REFERENCES payloads(id) ON DELETE CASCADE,
    node_id      INTEGER REFERENCES test_nodes(id) ON DELETE SET NULL,
    kind         TEXT    NOT NULL DEFAULT 'submit'
                         CHECK (kind IN ('submit', 'validate')),
    priority     INTEGER NOT NULL DEFAULT 100,
    status       TEXT    NOT NULL DEFAULT 'queued'
                         CHECK (status IN ('queued', 'running', 'done', 'failed')),
    attempts     INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts > 0),
    scheduled_at REAL    NOT NULL,
    started_at   REAL,
    finished_at  REAL,
    last_error   TEXT,
    created_at   REAL    NOT NULL,
    updated_at   REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,
    video_id      TEXT    NOT NULL,
    video_title   TEXT,
    video_url     TEXT,
    target_id     TEXT    NOT NULL,
    target_author TEXT,
    target_text   TEXT,
    reply_text    TEXT,
    mode          TEXT    NOT NULL CHECK (mode IN ('live', 'dry_run')),
    status        TEXT    NOT NULL CHECK (status IN ('success', 'failed', 'simulated')),
    error         TEXT,
    duration_sec  REAL,
    payload_id    INTEGER REFERENCES payloads(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_status ON test_nodes(status, cooldown_until);
CREATE INDEX IF NOT EXISTS idx_payloads_status ON payloads(status, created_at);
CREATE INDEX IF NOT EXISTS idx_payloads_source_ref ON payloads(source_ref, status);
CREATE INDEX IF NOT EXISTS idx_queue_ready ON execution_queue(status, kind, scheduled_at, priority);
CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_video ON activity_log(video_id);
CREATE INDEX IF NOT EXISTS idx_activity_target ON activity_log(target_id);
CREATE INDEX IF NOT EXISTS idx_activity_status ON activity_log(status);
"""

_MULTI_ACCOUNT_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT    NOT NULL UNIQUE,
    session_path        TEXT    NOT NULL,
    proxy_url           TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1,
    total_replies       INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL,
    last_used_at        TEXT,
    engine              TEXT    NOT NULL DEFAULT 'camoufox'
);

CREATE TABLE IF NOT EXISTS video_pool (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    video_url         TEXT    NOT NULL UNIQUE,
    video_id          TEXT    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending', 'in_progress', 'completed', 'failed')),
    comments_target   INTEGER NOT NULL DEFAULT 2 CHECK (comments_target > 0),
    comments_done     INTEGER NOT NULL DEFAULT 0 CHECK (comments_done >= 0),
    created_at        TEXT    NOT NULL,
    last_processed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_accounts_name ON accounts(name);
CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts(is_active);
CREATE INDEX IF NOT EXISTS idx_video_pool_status ON video_pool(status, id);
"""

_ACCOUNT_AUTH_COLUMNS: tuple[tuple[str, str], ...] = (
    ("email", "TEXT"),
    ("password", "TEXT"),
    ("totp_secret", "TEXT"),
    ("recovery_email", "TEXT"),
    ("auth_status", "TEXT DEFAULT 'pending'"),
    ("fingerprint_data", "TEXT"),
    ("geo_country", "TEXT"),
    ("expected_country", "TEXT"),
    ("locale", "TEXT"),
    ("engine", "TEXT NOT NULL DEFAULT 'camoufox'"),
)

_ACCOUNT_LIFECYCLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("lifecycle_stage", "TEXT NOT NULL DEFAULT 'quarantine'"),
    ("authenticated_at", "TEXT"),
    ("quarantine_until", "TEXT"),
    ("daily_comments_done", "INTEGER NOT NULL DEFAULT 0"),
    ("daily_comments_limit", "INTEGER NOT NULL DEFAULT 0"),
    ("daily_limit_reset_at", "TEXT"),
    ("last_comment_at", "TEXT"),
    ("warmup_sessions_done", "INTEGER NOT NULL DEFAULT 0"),
    ("last_warmup_at", "TEXT"),
    ("consecutive_errors", "INTEGER NOT NULL DEFAULT 0"),
)

# Channel profile: the draft the operator typed (`profile_name` / `profile_description` / `profile_avatar`), the JSON
# snapshot of what was last published to YouTube (`profile_applied`) and the JSON list of recent channel-name changes.
_ACCOUNT_PROFILE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("profile_name", "TEXT"),
    ("profile_description", "TEXT"),
    ("profile_avatar", "TEXT"),
    ("profile_applied", "TEXT"),
    ("profile_applied_at", "TEXT"),
    ("profile_name_changes", "TEXT"),
)

QUARANTINE_HOURS = 24
DAILY_RESET_HOUR_UTC = 4
NAME_CHANGES_PER_WINDOW = 2  # YouTube: the channel name may change twice within 14 days
NAME_CHANGE_WINDOW_DAYS = 14

_ERROR_LIMIT = 2000
_SCHEMA_ATTEMPTS = 6


class _Unset:
    """Marker for "argument not passed": an explicit None must stay distinguishable from it."""

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _Unset()


class RecordNotFoundError(RuntimeError):
    """Raised when an operation targets a row that does not exist."""


class TestNode(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    base_url: str
    status: NodeStatus
    max_slots: int
    active_slots: int
    cooldown_until: float | None
    last_used_at: float | None
    created_at: float
    updated_at: float


class Payload(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    node_id: int | None
    source_ref: str | None
    source_text: str | None
    text: str
    language: str
    status: PayloadStatus
    remote_id: str | None
    error: str | None
    created_at: float
    submitted_at: float | None
    validated_at: float | None


class QueueItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    payload_id: int
    node_id: int | None
    kind: QueueKind
    priority: int
    status: QueueStatus
    attempts: int
    max_attempts: int
    scheduled_at: float
    started_at: float | None
    finished_at: float | None
    last_error: str | None
    created_at: float
    updated_at: float

    @property
    def attempts_left(self) -> int:
        return max(0, self.max_attempts - self.attempts)


class ActivityLogEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: str
    video_id: str
    video_title: str | None = None
    video_url: str | None = None
    target_id: str
    target_author: str | None = None
    target_text: str | None = None
    reply_text: str | None = None
    mode: str
    status: str
    error: str | None = None
    duration_sec: float | None = None
    payload_id: int | None = None


class ActivityLogCreate(BaseModel):
    video_id: str
    video_title: str | None = None
    video_url: str | None = None
    target_id: str
    target_author: str | None = None
    target_text: str | None = None
    reply_text: str | None = None
    mode: str = "dry_run"
    status: str = "simulated"
    error: str | None = None
    duration_sec: float | None = None
    payload_id: int | None = None
    created_at: str | None = None


class AccountRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    session_path: str
    proxy_url: str | None = None
    is_active: bool = True
    total_replies: int = 0
    created_at: str
    last_used_at: str | None = None
    email: str | None = None
    password: str | None = Field(default=None, repr=False, exclude=True)
    totp_secret: str | None = Field(default=None, repr=False, exclude=True)
    recovery_email: str | None = None
    auth_status: str = "pending"
    fingerprint_data: str | None = None
    geo_country: str | None = None
    expected_country: str | None = None
    locale: str | None = None
    engine: EngineType = "camoufox"
    lifecycle_stage: str = "quarantine"
    authenticated_at: str | None = None
    quarantine_until: str | None = None
    daily_comments_done: int = 0
    daily_comments_limit: int = 0
    daily_limit_reset_at: str | None = None
    last_comment_at: str | None = None
    warmup_sessions_done: int = 0
    last_warmup_at: str | None = None
    consecutive_errors: int = 0
    profile_name: str | None = None
    profile_description: str | None = None
    profile_avatar: str | None = None
    profile_applied: str | None = None
    profile_applied_at: str | None = None
    profile_name_changes: str | None = None


class AccountCreate(BaseModel):
    name: str
    session_path: str
    proxy_url: str | None = None
    is_active: bool = True
    email: str | None = None
    password: str | None = Field(default=None, repr=False, exclude=True)
    totp_secret: str | None = Field(default=None, repr=False, exclude=True)
    recovery_email: str | None = None
    geo_country: str | None = None
    expected_country: str | None = None
    locale: str | None = None
    lifecycle_stage: str = "quarantine"
    authenticated_at: str | None = None
    quarantine_until: str | None = None
    daily_comments_done: int = 0
    daily_comments_limit: int = 0
    daily_limit_reset_at: str | None = None
    last_comment_at: str | None = None
    warmup_sessions_done: int = 0
    last_warmup_at: str | None = None
    consecutive_errors: int = 0


class VideoPoolItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    video_url: str
    video_id: str
    status: str = "pending"
    comments_target: int = 2
    comments_done: int = 0
    created_at: str
    last_processed_at: str | None = None


def _extract_video_id(url_or_id: str) -> str:
    """Extract YouTube video ID from URL or return the identifier string."""
    s = url_or_id.strip()
    if len(s) == 11 and re.fullmatch(r"[A-Za-z0-9_-]{11}", s):
        return s
    match = re.search(r"(?:v=|\/shorts\/|youtu\.be\/|embed\/)([A-Za-z0-9_-]{11})", s)
    if match:
        return match.group(1)
    parsed = urlparse(s)
    if parsed.query:
        params = parse_qs(parsed.query)
        if "v" in params and params["v"]:
            return params["v"][0]
    return s


def _truncate(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:_ERROR_LIMIT]


def recent_name_changes(stored: str | None, now: datetime) -> list[datetime]:
    """Channel-name changes of `profile_name_changes` that are still inside YouTube's limit window (unreadable data: none)."""
    try:
        raw = json.loads(stored) if stored else []
    except ValueError:
        return []
    cutoff = now - timedelta(days=NAME_CHANGE_WINDOW_DAYS)
    changes: list[datetime] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            moment = datetime.fromisoformat(str(item))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if moment > cutoff:
            changes.append(moment)
    return sorted(changes)


class Database:
    """Async SQLite repository for nodes, payloads and the execution queue."""

    def __init__(self, db_path: str | Path = "test_metrics.db", busy_timeout_ms: int = 5000) -> None:
        self._path = Path(db_path).expanduser()
        self._busy_timeout_ms = busy_timeout_ms
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("database is not connected; call connect() first")
        return self._conn

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path, isolation_level=None, timeout=self._busy_timeout_ms / 1000)
        conn.row_factory = aiosqlite.Row
        try:
            await conn.execute(f"PRAGMA busy_timeout={int(self._busy_timeout_ms)}")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("PRAGMA synchronous=NORMAL")
            cursor = await conn.execute("PRAGMA journal_mode")
            row = await cursor.fetchone()
            await cursor.close()
            if not row or str(row[0]).lower() != "wal":
                for attempt in range(1, _SCHEMA_ATTEMPTS + 1):  # switching the mode ignores busy_timeout while another process does it
                    try:
                        await conn.execute("PRAGMA journal_mode=WAL")
                        break
                    except aiosqlite.OperationalError as exc:
                        if "locked" not in str(exc).lower() or attempt == _SCHEMA_ATTEMPTS:
                            raise
                        await asyncio.sleep(0.1 * attempt)
        except BaseException:
            await conn.close()
            raise
        self._conn = conn
        logger.debug("Connected to SQLite database {}", self._path)

    async def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        async with self._lock:
            await conn.close()
        logger.debug("Closed SQLite database {}", self._path)

    async def __aenter__(self) -> "Database":
        await self.connect()
        await self.init_schema()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def init_schema(self) -> None:
        """Create / migrate the schema; a few retries when another process holds the write lock at the same time."""
        for attempt in range(1, _SCHEMA_ATTEMPTS + 1):
            try:
                await self._init_schema_once()
                return
            except aiosqlite.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == _SCHEMA_ATTEMPTS:
                    raise
                logger.debug("База занята другим процессом, повтор инициализации схемы ({}/{})", attempt, _SCHEMA_ATTEMPTS)
                await asyncio.sleep(0.2 * attempt)

    async def _init_schema_once(self) -> None:
        conn = self.connection
        async with self._lock:
            cursor = await conn.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            await cursor.close()
            version = int(row[0]) if row else 0
            if version < 2:
                await conn.executescript(_SCHEMA)
                await conn.execute("PRAGMA user_version = 2")
                await conn.commit()
            await conn.executescript(_MULTI_ACCOUNT_SCHEMA)
            await self._ensure_account_columns(conn)
            await conn.commit()
        logger.debug("Database schema ensured at {}", self._path)

    @staticmethod
    async def _ensure_account_columns(conn: aiosqlite.Connection) -> None:
        """Incremental migration: add the auth columns to an `accounts` table created by an older version."""
        cursor = await conn.execute("PRAGMA table_info(accounts)")
        existing = {str(row[1]) for row in await cursor.fetchall()}
        await cursor.close()
        for column, ddl in (*_ACCOUNT_AUTH_COLUMNS, *_ACCOUNT_LIFECYCLE_COLUMNS, *_ACCOUNT_PROFILE_COLUMNS):
            if column not in existing:
                try:
                    await conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {ddl}")
                except aiosqlite.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():  # another process added it first
                        raise
        # accounts signed in before the lifecycle existed age from their creation, not from their next re-login
        await conn.execute(
            "UPDATE accounts SET authenticated_at = created_at WHERE auth_status = 'authenticated' AND authenticated_at IS NULL"
        )

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = self.connection
        async with self._lock:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                await conn.rollback()
                raise
            else:
                await conn.commit()

    async def _fetch_one(self, query: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        conn = self.connection
        async with self._lock:
            cursor = await conn.execute(query, params)
            try:
                return await cursor.fetchone()
            finally:
                await cursor.close()

    async def _fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        conn = self.connection
        async with self._lock:
            cursor = await conn.execute(query, params)
            try:
                return list(await cursor.fetchall())
            finally:
                await cursor.close()

    async def _count(self, query: str, params: tuple[Any, ...] = ()) -> int:
        row = await self._fetch_one(query, params)
        return int(row[0]) if row is not None else 0

    # ------------------------------------------------------------------ nodes

    async def upsert_test_node(self, name: str, base_url: str, max_slots: int = 1) -> TestNode:
        if max_slots < 1:
            raise ValueError("max_slots must be positive")
        now = time.time()
        async with self._transaction() as conn:
            await conn.execute(
                """
                INSERT INTO test_nodes (name, base_url, status, max_slots, active_slots, created_at, updated_at)
                VALUES (?, ?, 'idle', ?, 0, ?, ?)
                ON CONFLICT(name) DO UPDATE
                    SET base_url = excluded.base_url,
                        max_slots = excluded.max_slots,
                        updated_at = excluded.updated_at
                """,
                (name, base_url, max_slots, now, now),
            )
            row = await (await conn.execute("SELECT * FROM test_nodes WHERE name = ?", (name,))).fetchone()
        if row is None:
            raise RecordNotFoundError(f"test node {name!r} disappeared during upsert")
        return TestNode.model_validate(dict(row))

    async def get_test_node(self, node_id: int) -> TestNode | None:
        row = await self._fetch_one("SELECT * FROM test_nodes WHERE id = ?", (node_id,))
        return TestNode.model_validate(dict(row)) if row else None

    async def list_test_nodes(self, status: NodeStatus | None = None) -> list[TestNode]:
        if status is None:
            rows = await self._fetch_all("SELECT * FROM test_nodes ORDER BY id")
        else:
            rows = await self._fetch_all("SELECT * FROM test_nodes WHERE status = ? ORDER BY id", (status,))
        return [TestNode.model_validate(dict(row)) for row in rows]

    async def acquire_test_node(self, node_id: int | None = None, now: float | None = None) -> TestNode | None:
        """Reserve a slot on a free node, honouring cooldowns and capacity."""
        moment = time.time() if now is None else now
        clauses = [
            "status != 'disabled'",
            "active_slots < max_slots",
            "(cooldown_until IS NULL OR cooldown_until <= ?)",
        ]
        params: list[Any] = [moment]
        if node_id is not None:
            clauses.append("id = ?")
            params.append(node_id)

        async with self._transaction() as conn:
            cursor = await conn.execute(
                f"""
                SELECT * FROM test_nodes
                WHERE {' AND '.join(clauses)}
                ORDER BY active_slots ASC, COALESCE(last_used_at, 0) ASC, id ASC
                LIMIT 1
                """,
                tuple(params),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None
            await conn.execute(
                """
                UPDATE test_nodes
                SET active_slots = active_slots + 1, status = 'busy', last_used_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (moment, moment, row["id"]),
            )
            updated = await (await conn.execute("SELECT * FROM test_nodes WHERE id = ?", (row["id"],))).fetchone()

        node = TestNode.model_validate(dict(updated)) if updated else None
        if node is not None:
            logger.debug("Acquired test node {} ({}/{})", node.name, node.active_slots, node.max_slots)
        return node

    async def release_test_node(
        self,
        node_id: int,
        cooldown_seconds: float = 0.0,
        disable: bool = False,
    ) -> TestNode | None:
        now = time.time()
        cooldown_until = now + cooldown_seconds if cooldown_seconds > 0 else None
        async with self._transaction() as conn:
            row = await (await conn.execute("SELECT * FROM test_nodes WHERE id = ?", (node_id,))).fetchone()
            if row is None:
                logger.warning("Cannot release unknown test node {}", node_id)
                return None
            active_slots = max(0, int(row["active_slots"]) - 1)
            if disable:
                status: NodeStatus = "disabled"
            elif cooldown_until is not None:
                status = "cooldown"
            elif active_slots > 0:
                status = "busy"
            else:
                status = "idle"
            await conn.execute(
                """
                UPDATE test_nodes
                SET active_slots = ?, status = ?, cooldown_until = ?, updated_at = ?
                WHERE id = ?
                """,
                (active_slots, status, cooldown_until, now, node_id),
            )
            updated = await (await conn.execute("SELECT * FROM test_nodes WHERE id = ?", (node_id,))).fetchone()
        node = TestNode.model_validate(dict(updated)) if updated else None
        if node is not None:
            logger.debug("Released test node {} -> {}", node.name, node.status)
        return node

    async def sweep_cooldowns(self, now: float | None = None) -> int:
        moment = time.time() if now is None else now
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE test_nodes
                SET status = CASE WHEN active_slots > 0 THEN 'busy' ELSE 'idle' END,
                    cooldown_until = NULL,
                    updated_at = ?
                WHERE status = 'cooldown' AND cooldown_until IS NOT NULL AND cooldown_until <= ?
                """,
                (moment, moment),
            )
            changed = cursor.rowcount or 0
        if changed:
            logger.debug("Cleared cooldown for {} test node(s)", changed)
        return changed

    async def set_node_disabled(self, node_id: int, disabled: bool = True) -> TestNode | None:
        now = time.time()
        async with self._transaction() as conn:
            await conn.execute(
                "UPDATE test_nodes SET status = ?, updated_at = ? WHERE id = ?",
                ("disabled" if disabled else "idle", now, node_id),
            )
            row = await (await conn.execute("SELECT * FROM test_nodes WHERE id = ?", (node_id,))).fetchone()
        return TestNode.model_validate(dict(row)) if row else None

    # --------------------------------------------------------------- payloads

    async def create_payload(
        self,
        *,
        node_id: int | None,
        text: str,
        language: str,
        source_ref: str | None = None,
        source_text: str | None = None,
    ) -> Payload:
        text = text.strip()
        if not text:
            raise ValueError("payload text must not be empty")
        language = language.strip().lower()
        if not language:
            raise ValueError("payload language must not be empty")
        now = time.time()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO payloads (node_id, source_ref, source_text, text, language, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (node_id, source_ref, source_text, text, language, now),
            )
            payload_id = cursor.lastrowid
            row = await (await conn.execute("SELECT * FROM payloads WHERE id = ?", (payload_id,))).fetchone()
        if row is None:
            raise RecordNotFoundError(f"payload {payload_id} disappeared during creation")
        payload = Payload.model_validate(dict(row))
        logger.debug("Created payload {} ({} chars, {})", payload.id, len(payload.text), payload.language)
        return payload

    async def get_payload(self, payload_id: int) -> Payload | None:
        row = await self._fetch_one("SELECT * FROM payloads WHERE id = ?", (payload_id,))
        return Payload.model_validate(dict(row)) if row else None

    async def get_payload_by_source_ref(self, source_ref: str) -> Payload | None:
        row = await self._fetch_one(
            "SELECT * FROM payloads WHERE source_ref = ? ORDER BY id DESC LIMIT 1",
            (source_ref,),
        )
        return Payload.model_validate(dict(row)) if row else None

    async def reserve_payload(
        self,
        *,
        node_id: int | None = None,
        source_ref: str,
        language: str,
        source_text: str | None = None,
        placeholder_text: str = "",
        stale_pending_seconds: float = 600.0,
    ) -> Payload | None:
        """Atomically reserve a sub-comment before generation if not already reserved/replied.

        If a prior reservation was abandoned (pending for longer than stale_pending_seconds),
        it is marked as failed and the new reservation is allowed.
        Returns the created pending Payload, or None if already replied/reserved.
        """
        if not source_ref or not source_ref.strip():
            raise ValueError("source_ref must not be empty")

        now = time.time()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                SELECT id, status, created_at
                FROM payloads
                WHERE source_ref = ? AND status IN ('pending', 'submitted', 'validated')
                ORDER BY id DESC
                """,
                (source_ref,),
            )
            rows = await cursor.fetchall()
            await cursor.close()

            for row in rows:
                status = row["status"]
                if status in ("submitted", "validated"):
                    return None
                if status == "pending":
                    age = now - float(row["created_at"])
                    if age < stale_pending_seconds:
                        return None
                    # Stale pending reservation - mark failed so it can be retried
                    await conn.execute(
                        """
                        UPDATE payloads
                        SET status = 'failed', error = 'reservation timed out / stale pending'
                        WHERE id = ?
                        """,
                        (row["id"],),
                    )
                    logger.warning(
                        "Expired stale pending payload {} (age {:.1f}s) for {}",
                        row["id"],
                        age,
                        source_ref,
                    )

            cursor = await conn.execute(
                """
                INSERT INTO payloads (node_id, source_ref, source_text, text, language, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (node_id, source_ref, source_text, placeholder_text, language, now),
            )
            payload_id = cursor.lastrowid
            row = await (await conn.execute("SELECT * FROM payloads WHERE id = ?", (payload_id,))).fetchone()
        if row is None:
            raise RecordNotFoundError(f"payload {payload_id} disappeared during creation")
        payload = Payload.model_validate(dict(row))
        logger.debug("Reserved payload {} for source_ref {}", payload.id, source_ref)
        return payload

    async def update_payload_text(self, payload_id: int, text: str) -> Payload:
        """Update the text of a reserved payload once generation completes."""
        if not text or not text.strip():
            raise ValueError("payload text must not be empty")
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "UPDATE payloads SET text = ? WHERE id = ? AND status = 'pending'",
                (text, payload_id),
            )
            if cursor.rowcount == 0:
                row = await (await conn.execute("SELECT status FROM payloads WHERE id = ?", (payload_id,))).fetchone()
                if row is None:
                    raise RecordNotFoundError(f"payload {payload_id} not found")
                raise RuntimeError(f"cannot update text for payload {payload_id} with status '{row['status']}'")
            row = await (await conn.execute("SELECT * FROM payloads WHERE id = ?", (payload_id,))).fetchone()
        if row is None:
            raise RecordNotFoundError(f"payload {payload_id} not found")
        return Payload.model_validate(dict(row))

    async def has_replied_to(self, source_ref: str) -> bool:
        """Check whether a reply has already been created/submitted for this comment."""
        count = await self._count(
            "SELECT COUNT(*) FROM payloads WHERE source_ref = ? AND status IN ('pending', 'submitted', 'validated')",
            (source_ref,),
        )
        return count > 0

    async def list_payloads(self, status: PayloadStatus | None = None, limit: int = 50) -> list[Payload]:
        if limit < 1:
            raise ValueError("limit must be positive")
        if status is None:
            rows = await self._fetch_all("SELECT * FROM payloads ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = await self._fetch_all(
                "SELECT * FROM payloads WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
            )
        return [Payload.model_validate(dict(row)) for row in rows]

    async def mark_payload_submitted(self, payload_id: int, remote_id: str | None = None) -> Payload:
        now = time.time()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE payloads
                SET status = 'submitted', submitted_at = ?, remote_id = COALESCE(?, remote_id), error = NULL
                WHERE id = ?
                """,
                (now, remote_id, payload_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"payload {payload_id} not found")
            row = await (await conn.execute("SELECT * FROM payloads WHERE id = ?", (payload_id,))).fetchone()
        if row is None:
            raise RecordNotFoundError(f"payload {payload_id} not found")
        return Payload.model_validate(dict(row))

    async def mark_payload_validated(self, payload_id: int) -> Payload:
        now = time.time()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE payloads
                SET status = 'validated', validated_at = ?, error = NULL
                WHERE id = ?
                """,
                (now, payload_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"payload {payload_id} not found")
            row = await (await conn.execute("SELECT * FROM payloads WHERE id = ?", (payload_id,))).fetchone()
        if row is None:
            raise RecordNotFoundError(f"payload {payload_id} not found")
        return Payload.model_validate(dict(row))

    async def mark_payload_failed(self, payload_id: int, error: str) -> Payload:
        now = time.time()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "UPDATE payloads SET status = 'failed', error = ?, validated_at = ? WHERE id = ?",
                (_truncate(error), now, payload_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"payload {payload_id} not found")
            row = await (await conn.execute("SELECT * FROM payloads WHERE id = ?", (payload_id,))).fetchone()
        if row is None:
            raise RecordNotFoundError(f"payload {payload_id} not found")
        return Payload.model_validate(dict(row))

    # ------------------------------------------------------------------ queue

    async def enqueue(
        self,
        kind: QueueKind,
        payload_id: int,
        *,
        node_id: int | None = None,
        priority: int = 100,
        delay_seconds: float = 0.0,
        max_attempts: int = 3,
    ) -> QueueItem:
        if kind not in ("submit", "validate"):
            raise ValueError(f"unsupported queue kind: {kind!r}")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        now = time.time()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO execution_queue
                    (payload_id, node_id, kind, priority, status, attempts, max_attempts,
                     scheduled_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', 0, ?, ?, ?, ?)
                """,
                (payload_id, node_id, kind, priority, max_attempts, now + delay_seconds, now, now),
            )
            queue_id = cursor.lastrowid
            row = await (await conn.execute("SELECT * FROM execution_queue WHERE id = ?", (queue_id,))).fetchone()
        if row is None:
            raise RecordNotFoundError(f"queue item {queue_id} disappeared during creation")
        item = QueueItem.model_validate(dict(row))
        logger.debug("Enqueued {} job {} for payload {} (due in {:.0f}s)", kind, item.id, payload_id, delay_seconds)
        return item

    async def get_queue_item(self, queue_id: int) -> QueueItem | None:
        row = await self._fetch_one("SELECT * FROM execution_queue WHERE id = ?", (queue_id,))
        return QueueItem.model_validate(dict(row)) if row else None

    async def recover_running_jobs(self, error: str = "queue job was interrupted by a previous shutdown") -> int:
        """Requeue jobs left running by a crash; fail them once their attempts are exhausted."""
        now = time.time()
        recovered = 0
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "SELECT id, payload_id, attempts, max_attempts FROM execution_queue WHERE status = 'running'"
            )
            rows = list(await cursor.fetchall())
            await cursor.close()
            for row in rows:
                recovered += 1
                if int(row["attempts"]) >= int(row["max_attempts"]):
                    await conn.execute(
                        """
                        UPDATE execution_queue
                        SET status = 'failed', finished_at = ?, last_error = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, _truncate(error), now, row["id"]),
                    )
                    await conn.execute(
                        """
                        UPDATE payloads
                        SET status = 'failed', error = ?, validated_at = ?
                        WHERE id = ? AND status != 'validated'
                        """,
                        (_truncate(error), now, row["payload_id"]),
                    )
                else:
                    await conn.execute(
                        """
                        UPDATE execution_queue
                        SET status = 'queued', scheduled_at = ?, started_at = NULL, last_error = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, _truncate(error), now, row["id"]),
                    )
        if recovered:
            logger.warning("Recovered {} interrupted queue job(s)", recovered)
        return recovered

    async def claim_queue_item(
        self,
        *,
        kind: QueueKind | None = None,
        queue_id: int | None = None,
        now: float | None = None,
    ) -> QueueItem | None:
        """Atomically move a due queued item to running and bump its attempt counter."""
        moment = time.time() if now is None else now
        clauses = ["status = 'queued'", "scheduled_at <= ?"]
        params: list[Any] = [moment]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        if queue_id is not None:
            clauses.append("id = ?")
            params.append(queue_id)

        async with self._transaction() as conn:
            cursor = await conn.execute(
                f"""
                SELECT id FROM execution_queue
                WHERE {' AND '.join(clauses)}
                ORDER BY priority ASC, scheduled_at ASC, id ASC
                LIMIT 1
                """,
                tuple(params),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return None
            await conn.execute(
                """
                UPDATE execution_queue
                SET status = 'running', attempts = attempts + 1, started_at = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (moment, moment, row["id"]),
            )
            updated = await (
                await conn.execute("SELECT * FROM execution_queue WHERE id = ?", (row["id"],))
            ).fetchone()
        return QueueItem.model_validate(dict(updated)) if updated else None

    async def complete_queue_item(self, queue_id: int) -> QueueItem:
        now = time.time()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE execution_queue
                SET status = 'done', finished_at = ?, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, now, queue_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"queue item {queue_id} not found")
            row = await (await conn.execute("SELECT * FROM execution_queue WHERE id = ?", (queue_id,))).fetchone()
        if row is None:
            raise RecordNotFoundError(f"queue item {queue_id} not found")
        return QueueItem.model_validate(dict(row))

    async def fail_queue_item(self, queue_id: int, error: str) -> QueueItem:
        now = time.time()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE execution_queue
                SET status = 'failed', finished_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, _truncate(error), now, queue_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"queue item {queue_id} not found")
            row = await (await conn.execute("SELECT * FROM execution_queue WHERE id = ?", (queue_id,))).fetchone()
        if row is None:
            raise RecordNotFoundError(f"queue item {queue_id} not found")
        return QueueItem.model_validate(dict(row))

    async def reschedule_queue_item(self, queue_id: int, error: str, delay_seconds: float) -> QueueItem:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        now = time.time()
        async with self._transaction() as conn:
            row = await (await conn.execute("SELECT * FROM execution_queue WHERE id = ?", (queue_id,))).fetchone()
            if row is None:
                raise RecordNotFoundError(f"queue item {queue_id} not found")
            if int(row["attempts"]) >= int(row["max_attempts"]):
                await conn.execute(
                    """
                    UPDATE execution_queue
                    SET status = 'failed', finished_at = ?, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now, _truncate(error), now, queue_id),
                )
            else:
                await conn.execute(
                    """
                    UPDATE execution_queue
                    SET status = 'queued', scheduled_at = ?, started_at = NULL, finished_at = NULL,
                        last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (now + delay_seconds, _truncate(error), now, queue_id),
                )
            updated = await (await conn.execute("SELECT * FROM execution_queue WHERE id = ?", (queue_id,))).fetchone()
        if updated is None:
            raise RecordNotFoundError(f"queue item {queue_id} not found")
        return QueueItem.model_validate(dict(updated))

    async def list_queue(self, status: QueueStatus | None = None, limit: int = 100) -> list[QueueItem]:
        if limit < 1:
            raise ValueError("limit must be positive")
        if status is None:
            rows = await self._fetch_all("SELECT * FROM execution_queue ORDER BY id DESC LIMIT ?", (limit,))
        else:
            rows = await self._fetch_all(
                "SELECT * FROM execution_queue WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
            )
        return [QueueItem.model_validate(dict(row)) for row in rows]

    async def count_queue(self, status: QueueStatus | None = None, kind: QueueKind | None = None) -> int:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return await self._count(f"SELECT COUNT(*) FROM execution_queue{where}", tuple(params))

    async def stats(self) -> dict[str, Any]:
        conn = self.connection
        async with self._lock:
            async def group(query: str) -> dict[str, int]:
                cursor = await conn.execute(query)
                try:
                    rows = await cursor.fetchall()
                finally:
                    await cursor.close()
                return {str(row[0]): int(row[1]) for row in rows}

            nodes = await group("SELECT status, COUNT(*) FROM test_nodes GROUP BY status")
            payloads = await group("SELECT status, COUNT(*) FROM payloads GROUP BY status")
            queue = await group("SELECT status, COUNT(*) FROM execution_queue GROUP BY status")
            activity = await group("SELECT status, COUNT(*) FROM activity_log GROUP BY status")
            accounts = await group(
                "SELECT CASE WHEN is_active = 1 THEN 'active' ELSE 'inactive' END, COUNT(*) FROM accounts GROUP BY is_active"
            )
            video_pool = await group("SELECT status, COUNT(*) FROM video_pool GROUP BY status")

        return {
            "db_path": str(self._path),
            "nodes": nodes,
            "payloads": payloads,
            "queue": queue,
            "activity": activity,
            "accounts": accounts,
            "video_pool": video_pool,
            "active_slots": sum(
                int(row[0]) for row in await self._fetch_all("SELECT COALESCE(SUM(active_slots), 0) FROM test_nodes")
            ),
        }

    async def log_activity(self, entry: ActivityLogCreate | ActivityLogEntry | dict[str, Any]) -> ActivityLogEntry:
        """Insert a new audit record into activity_log."""
        if isinstance(entry, BaseModel):
            data = entry.model_dump()
        else:
            data = dict(entry)

        created_at = data.get("created_at") or datetime.now(timezone.utc).isoformat()
        video_id = str(data.get("video_id", ""))
        video_title = data.get("video_title")
        video_url = data.get("video_url")
        target_id = str(data.get("target_id", ""))
        target_author = data.get("target_author")
        target_text = data.get("target_text")
        reply_text = data.get("reply_text")
        mode = str(data.get("mode", "dry_run"))
        status = str(data.get("status", "simulated"))
        error = _truncate(data.get("error"))
        duration_sec = data.get("duration_sec")
        payload_id = data.get("payload_id")

        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO activity_log (
                    created_at, video_id, video_title, video_url, target_id, target_author,
                    target_text, reply_text, mode, status, error, duration_sec, payload_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    video_id,
                    video_title,
                    video_url,
                    target_id,
                    target_author,
                    target_text,
                    reply_text,
                    mode,
                    status,
                    error,
                    duration_sec,
                    payload_id,
                ),
            )
            log_id = cursor.lastrowid
            row = await (await conn.execute("SELECT * FROM activity_log WHERE id = ?", (log_id,))).fetchone()
        if row is None:
            raise RecordNotFoundError(f"activity_log {log_id} not found")
        return ActivityLogEntry.model_validate(dict(row))

    async def count_successful_replies_for_video(self, video_id: str, status: str | None = None) -> int:
        """Count replies successfully submitted or simulated for a specific video."""
        if status is not None:
            return await self._count(
                "SELECT COUNT(*) FROM activity_log WHERE video_id = ? AND status = ?",
                (video_id, status),
            )
        return await self._count(
            "SELECT COUNT(*) FROM activity_log WHERE video_id = ? AND status IN ('success', 'simulated')",
            (video_id,),
        )

    async def list_activity_logs(
        self,
        page: int = 1,
        page_size: int = 50,
        status_filter: str | None = None,
    ) -> list[ActivityLogEntry]:
        """Fetch a paginated list of activity logs, newest first."""
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 50
        offset = (page - 1) * page_size
        if status_filter:
            rows = await self._fetch_all(
                "SELECT * FROM activity_log WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (status_filter, page_size, offset),
            )
        else:
            rows = await self._fetch_all(
                "SELECT * FROM activity_log ORDER BY id DESC LIMIT ? OFFSET ?",
                (page_size, offset),
            )
        return [ActivityLogEntry.model_validate(dict(row)) for row in rows]

    async def count_activity_logs(self, status_filter: str | None = None) -> int:
        """Count total activity logs, optionally filtered by status."""
        if status_filter:
            return await self._count("SELECT COUNT(*) FROM activity_log WHERE status = ?", (status_filter,))
        return await self._count("SELECT COUNT(*) FROM activity_log")

    async def clear_activity_log(self) -> int:
        """Remove all records from activity_log."""
        async with self._transaction() as conn:
            cursor = await conn.execute("DELETE FROM activity_log")
            return cursor.rowcount

    # ----------------------------------------------------------------- accounts

    @staticmethod
    def _account_from_row(row: aiosqlite.Row) -> AccountRecord:
        """AccountRecord with `password` / `totp_secret` decrypted (values of older versions are plain text)."""
        data = dict(row)
        for field in ("password", "totp_secret"):
            try:
                data[field] = reveal_secret(data.get(field))
            except SecretStoreError as exc:
                logger.warning("Аккаунт {!r}: поле {} недоступно: {}", data.get("name"), field, exc)
                data[field] = None
        return AccountRecord.model_validate(data)

    async def upsert_account(
        self,
        name: str,
        session_path: str | _Unset = UNSET,
        proxy_url: str | None | _Unset = UNSET,
        is_active: bool | _Unset = UNSET,
        email: str | None = None,
        password: str | None = None,
        totp_secret: str | None = None,
        recovery_email: str | None = None,
        geo_country: str | None | _Unset = UNSET,
        expected_country: str | None | _Unset = UNSET,
        locale: str | None | _Unset = UNSET,
    ) -> AccountRecord:
        """Create or update an account by name.

        `session_path`, `proxy_url` and `is_active` keep the stored values when they are not passed (an explicit
        `proxy_url=None` removes the proxy); a new account needs `session_path`, starts active and without proxy.
        Credentials passed as None keep what is stored. A changed email, password or proxy resets `auth_status`
        to `pending`. `password` and `totp_secret` are stored encrypted (Windows DPAPI).
        `expected_country` (ISO alpha-2, upper-cased here) and `locale` (e.g. `pl-PL`) keep the stored value when not
        passed; an explicit None clears them. Callers validate them (`modules.geo_guard`).
        """
        name = name.strip()
        if not name:
            raise ValueError("account name must not be empty")
        if not isinstance(session_path, _Unset):
            session_path = str(session_path).strip()
            if not session_path:
                raise ValueError("session_path must not be empty")
        if isinstance(proxy_url, str):
            proxy_url = proxy_url.strip() or None
        email, password, totp_secret, recovery_email = (
            (value.strip() or None) if value else None for value in (email, password, totp_secret, recovery_email)
        )
        if isinstance(expected_country, str):
            expected_country = expected_country.strip().upper() or None
        if isinstance(locale, str):
            locale = locale.strip() or None
        now = datetime.now(timezone.utc).isoformat()
        async with self._transaction() as conn:
            row = await (await conn.execute("SELECT * FROM accounts WHERE name = ?", (name,))).fetchone()
            if row is None:
                if isinstance(session_path, _Unset):
                    raise ValueError("session_path is required for a new account")
                await conn.execute(
                    """
                    INSERT INTO accounts (name, session_path, proxy_url, is_active, total_replies, created_at,
                                          email, password, totp_secret, recovery_email, geo_country,
                                          expected_country, locale)
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        session_path,
                        None if isinstance(proxy_url, _Unset) else proxy_url,
                        0 if not isinstance(is_active, _Unset) and not is_active else 1,
                        now,
                        email,
                        protect_secret(password),
                        protect_secret(totp_secret),
                        recovery_email,
                        None if isinstance(geo_country, _Unset) else geo_country,
                        None if isinstance(expected_country, _Unset) else expected_country,
                        None if isinstance(locale, _Unset) else locale,
                    ),
                )
            else:
                current = self._account_from_row(row)
                new_session = current.session_path if isinstance(session_path, _Unset) else session_path
                new_proxy = current.proxy_url if isinstance(proxy_url, _Unset) else proxy_url
                validate_proxy_for_engine(new_proxy, current.engine)
                new_active = current.is_active if isinstance(is_active, _Unset) else bool(is_active)
                new_country = current.geo_country if isinstance(geo_country, _Unset) else geo_country
                new_expected = current.expected_country if isinstance(expected_country, _Unset) else expected_country
                new_locale = current.locale if isinstance(locale, _Unset) else locale
                credentials_changed = (
                    (email is not None and email != current.email)
                    or (password is not None and password != current.password)
                    or new_proxy != current.proxy_url
                )
                await conn.execute(
                    """
                    UPDATE accounts
                    SET session_path = ?, proxy_url = ?, is_active = ?,
                        email = COALESCE(?, email),
                        password = COALESCE(?, password),
                        totp_secret = COALESCE(?, totp_secret),
                        recovery_email = COALESCE(?, recovery_email),
                        geo_country = ?,
                        expected_country = ?,
                        locale = ?,
                        auth_status = CASE WHEN ? THEN 'pending' ELSE auth_status END
                    WHERE id = ?
                    """,
                    (
                        new_session,
                        new_proxy,
                        1 if new_active else 0,
                        email,
                        protect_secret(password),
                        protect_secret(totp_secret),
                        recovery_email,
                        new_country,
                        new_expected,
                        new_locale,
                        1 if credentials_changed else 0,
                        row["id"],
                    ),
                )
            row = await (await conn.execute("SELECT * FROM accounts WHERE name = ?", (name,))).fetchone()
        if row is None:
            raise RecordNotFoundError(f"account {name!r} disappeared during upsert")
        account = self._account_from_row(row)
        logger.debug("Upserted account {} (id={}, active={})", account.name, account.id, account.is_active)
        return account

    async def rename_account(self, old_name: str, new_name: str) -> AccountRecord:
        """Change only the account name; session_path and statistics stay as they are."""
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("account name must not be empty")
        async with self._transaction() as conn:
            row = await (await conn.execute("SELECT * FROM accounts WHERE name = ?", (old_name,))).fetchone()
            if row is None:
                raise RecordNotFoundError(f"account {old_name!r} not found")
            if new_name != old_name:
                taken = await (await conn.execute("SELECT 1 FROM accounts WHERE name = ?", (new_name,))).fetchone()
                if taken:
                    raise ValueError(f"account {new_name!r} already exists")
                await conn.execute("UPDATE accounts SET name = ? WHERE id = ?", (new_name, row["id"]))
                row = await (await conn.execute("SELECT * FROM accounts WHERE id = ?", (row["id"],))).fetchone()
        account = self._account_from_row(row)
        logger.debug("Renamed account {!r} -> {!r} (id={})", old_name, account.name, account.id)
        return account

    async def get_account(self, account_id_or_name: int | str) -> AccountRecord | None:
        if isinstance(account_id_or_name, int):
            row = await self._fetch_one("SELECT * FROM accounts WHERE id = ?", (account_id_or_name,))
        else:
            row = await self._fetch_one("SELECT * FROM accounts WHERE name = ?", (str(account_id_or_name),))
            if row is None and isinstance(account_id_or_name, str) and account_id_or_name.isdigit():
                row = await self._fetch_one("SELECT * FROM accounts WHERE id = ?", (int(account_id_or_name),))
        return self._account_from_row(row) if row else None

    async def list_active_accounts(self) -> list[AccountRecord]:
        rows = await self._fetch_all("SELECT * FROM accounts WHERE is_active = 1 ORDER BY id ASC")
        return [self._account_from_row(row) for row in rows]

    async def list_accounts(self, is_active: bool | None = None) -> list[AccountRecord]:
        if is_active is None:
            rows = await self._fetch_all("SELECT * FROM accounts ORDER BY id ASC")
        else:
            val = 1 if is_active else 0
            rows = await self._fetch_all("SELECT * FROM accounts WHERE is_active = ? ORDER BY id ASC", (val,))
        return [self._account_from_row(row) for row in rows]

    async def set_account_auth_status(self, account_id: int, status: str) -> None:
        async with self._transaction() as conn:
            cursor = await conn.execute("UPDATE accounts SET auth_status = ? WHERE id = ?", (status, account_id))
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"account id={account_id} not found")

    async def set_account_engine(self, account_id: int, engine: EngineType) -> AccountRecord:
        """Switch the account's browser engine and point `session_path` at that engine's own profile folder.

        The profile of the other engine stays on disk untouched; the new engine needs its own sign-in. The folder
        becomes `<session_root>/<name>` (Camoufox) or `<session_root>/chrome/<name>` (Chrome), where `session_root` is
        the stored path's parent (its `chrome` level removed). Raises ValueError for an unknown engine or when the
        account's proxy cannot be used by Chrome (SOCKS5 with a login), RecordNotFoundError for an unknown account.
        """
        if engine not in ("camoufox", "chrome"):
            raise ValueError(f"unknown engine: {engine!r}")
        async with self._transaction() as conn:
            row = await (await conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))).fetchone()
            if row is None:
                raise RecordNotFoundError(f"account id={account_id} not found")
            current = self._account_from_row(row)
            validate_proxy_for_engine(current.proxy_url, engine)
            if current.engine == engine:
                new_path = current.session_path
            else:
                folder = Path(current.session_path)
                if current.engine == "chrome" and folder.parent.name == "chrome":
                    root = folder.parent.parent
                else:
                    root = folder.parent
                new_path = str(get_account_session_path(root, current.name, engine))
                if current.engine == "chrome" and folder.parent.name != "chrome":
                    logger.warning(
                        "Аккаунт {!r}: путь сессии Chrome {} нестандартный, новая папка вычислена от {}",
                        current.name, current.session_path, root,
                    )
            await conn.execute(
                "UPDATE accounts SET engine = ?, session_path = ? WHERE id = ?", (engine, new_path, account_id)
            )
            row = await (await conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))).fetchone()
        account = self._account_from_row(row)
        logger.info("Аккаунт {!r}: движок {}, профиль {}", account.name, account.engine, account.session_path)
        return account

    async def set_account_geo_country(self, account_id: int, geo_country: str | None) -> None:
        async with self._transaction() as conn:
            cursor = await conn.execute("UPDATE accounts SET geo_country = ? WHERE id = ?", (geo_country, account_id))
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"account id={account_id} not found")

    @staticmethod
    def next_daily_reset(now: datetime) -> datetime:
        """The next 04:00 UTC strictly after `now`: the moment the per-account daily comment counter restarts."""
        now = now.astimezone(timezone.utc)
        reset = now.replace(hour=DAILY_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
        return reset if reset > now else reset + timedelta(days=1)

    async def mark_account_authenticated(self, account_id: int) -> None:
        """Successful sign-in: `authenticated`, and the account enters (or keeps) the 24 h quarantine.

        The clock starts at the first sign-in: re-authenticating an account that is already authenticated
        (an expired session refreshed) does not send a mature account back to quarantine.
        """
        now = datetime.now(timezone.utc)
        async with self._transaction() as conn:
            row = await (
                await conn.execute("SELECT auth_status, authenticated_at FROM accounts WHERE id = ?", (account_id,))
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"account id={account_id} not found")
            if row["auth_status"] == "authenticated" and row["authenticated_at"]:
                await conn.execute("UPDATE accounts SET consecutive_errors = 0 WHERE id = ?", (account_id,))
                return
            await conn.execute(
                """
                UPDATE accounts
                SET auth_status = 'authenticated',
                    authenticated_at = ?,
                    quarantine_until = ?,
                    lifecycle_stage = 'quarantine',
                    daily_comments_done = 0,
                    daily_comments_limit = 0,
                    daily_limit_reset_at = ?,
                    consecutive_errors = 0
                WHERE id = ?
                """,
                (
                    now.isoformat(),
                    (now + timedelta(hours=QUARANTINE_HOURS)).isoformat(),
                    self.next_daily_reset(now).isoformat(),
                    account_id,
                ),
            )

    async def update_account_lifecycle(
        self,
        account_id: int,
        *,
        lifecycle_stage: str | None = None,
        daily_comments_limit: int | None = None,
        daily_comments_done: int | None = None,
        daily_limit_reset_at: str | None = None,
    ) -> None:
        """Persist what the lifecycle engine derived (stage, daily limit, a restarted daily counter)."""
        fields = {
            "lifecycle_stage": lifecycle_stage,
            "daily_comments_limit": daily_comments_limit,
            "daily_comments_done": daily_comments_done,
            "daily_limit_reset_at": daily_limit_reset_at,
        }
        fields = {column: value for column, value in fields.items() if value is not None}
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        async with self._transaction() as conn:
            cursor = await conn.execute(
                f"UPDATE accounts SET {assignments} WHERE id = ?", (*fields.values(), account_id)
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"account id={account_id} not found")

    async def record_account_comment_success(self, account_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE accounts
                SET total_replies = total_replies + 1,
                    daily_comments_done = daily_comments_done + 1,
                    last_comment_at = ?,
                    last_used_at = ?,
                    consecutive_errors = 0
                WHERE id = ?
                """,
                (now, now, account_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"account {account_id} not found")

    async def record_account_warmup_success(self, account_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE accounts
                SET warmup_sessions_done = warmup_sessions_done + 1,
                    last_warmup_at = ?,
                    last_used_at = ?
                WHERE id = ?
                """,
                (now, now, account_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"account {account_id} not found")

    async def set_account_profile_draft(
        self,
        account_id: int,
        name: str | None | _Unset = UNSET,
        description: str | None | _Unset = UNSET,
        avatar: str | None | _Unset = UNSET,
    ) -> None:
        """Store what the operator wants the channel profile to be; a field that is not passed keeps its value."""
        fields = {
            column: value
            for column, value in (("profile_name", name), ("profile_description", description), ("profile_avatar", avatar))
            if not isinstance(value, _Unset)
        }
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        async with self._transaction() as conn:
            cursor = await conn.execute(
                f"UPDATE accounts SET {assignments} WHERE id = ?", (*fields.values(), account_id)
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"account id={account_id} not found")

    async def record_account_profile_applied(
        self, account_id: int, applied: dict[str, str | None], name_changed: bool
    ) -> None:
        """The profile as it is now live on YouTube (`applied`: name / description / avatar).

        A name change is also logged, keeping only the changes still inside YouTube's 14-day window.
        """
        moment = datetime.now(timezone.utc)
        now = moment.isoformat()
        async with self._transaction() as conn:
            row = await (
                await conn.execute("SELECT profile_name_changes FROM accounts WHERE id = ?", (account_id,))
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"account {account_id} not found")
            changes = recent_name_changes(row["profile_name_changes"], moment)
            if name_changed:
                changes.append(moment)
            await conn.execute(
                """
                UPDATE accounts
                SET profile_applied = ?, profile_applied_at = ?, profile_name_changes = ?, last_used_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(applied, ensure_ascii=False),
                    now,
                    json.dumps([change.isoformat() for change in changes]),
                    now,
                    account_id,
                ),
            )

    async def record_account_error(self, account_id: int) -> None:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "UPDATE accounts SET consecutive_errors = consecutive_errors + 1 WHERE id = ?", (account_id,)
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"account {account_id} not found")

    async def sync_account_fingerprint(self, account_id: int, session_dir: Path) -> None:
        """Keep `accounts.fingerprint_data` (SQLite) and `fingerprint.json` (disk) identical.

        The file is what the browser reads, so it wins when both exist; a missing side is filled from the other.
        A pinned fingerprint is never replaced by an empty or unreadable one.
        """
        path = Path(session_dir) / "fingerprint.json"
        account = await self.get_account(account_id)
        if account is None:
            raise RecordNotFoundError(f"account id={account_id} not found")
        on_disk = await asyncio.to_thread(path.read_text, "utf-8") if path.is_file() else None
        if on_disk is not None:
            try:
                valid = bool(json.loads(on_disk))
            except ValueError:
                valid = False
            if not valid:
                logger.warning("Аккаунт {!r}: файл отпечатка {} повреждён, синхронизация пропущена", account.name, path)
                on_disk = None
        stored = account.fingerprint_data
        if on_disk is not None and on_disk != stored:
            async with self._transaction() as conn:
                await conn.execute("UPDATE accounts SET fingerprint_data = ? WHERE id = ?", (on_disk, account_id))
        elif on_disk is None and stored:
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(path.write_text, stored, "utf-8")

    async def increment_account_stats(
        self,
        account_id: int,
        replies: int = 0,
    ) -> None:
        if replies < 0:
            raise ValueError("increments must not be negative")
        now = datetime.now(timezone.utc).isoformat()
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE accounts
                SET total_replies = total_replies + ?,
                    last_used_at = ?
                WHERE id = ?
                """,
                (replies, now, account_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"account {account_id} not found")
        logger.debug(
            "Incremented stats for account {}: +{} replies",
            account_id,
            replies,
        )

    # ------------------------------------------------------------- video pool

    async def add_video_to_pool(self, video_url: str, comments_target: int = 2) -> VideoPoolItem:
        video_url = video_url.strip()
        if not video_url:
            raise ValueError("video_url must not be empty")
        if comments_target < 1:
            raise ValueError("comments_target must be positive")
        video_id = _extract_video_id(video_url)
        now = datetime.now(timezone.utc).isoformat()
        async with self._transaction() as conn:
            await conn.execute(
                """
                INSERT INTO video_pool (video_url, video_id, status, comments_target, comments_done, created_at)
                VALUES (?, ?, 'pending', ?, 0, ?)
                ON CONFLICT(video_url) DO UPDATE
                    SET comments_target = excluded.comments_target
                """,
                (video_url, video_id, comments_target, now),
            )
            row = await (await conn.execute("SELECT * FROM video_pool WHERE video_url = ?", (video_url,))).fetchone()
        if row is None:
            raise RecordNotFoundError(f"pool video {video_url} disappeared during creation")
        item = VideoPoolItem.model_validate(dict(row))
        logger.debug("Added/updated video pool item {} (target={})", item.video_id, item.comments_target)
        return item

    async def get_next_pool_video(self) -> VideoPoolItem | None:
        row = await self._fetch_one(
            """
            SELECT * FROM video_pool
            WHERE status IN ('pending', 'in_progress') AND comments_done < comments_target
            ORDER BY id ASC
            LIMIT 1
            """
        )
        return VideoPoolItem.model_validate(dict(row)) if row else None

    async def list_pool_videos(self, status: str = "pending") -> list[VideoPoolItem]:
        if status == "all" or status is None:
            rows = await self._fetch_all("SELECT * FROM video_pool ORDER BY id ASC")
        elif status == "pending":
            rows = await self._fetch_all(
                """
                SELECT * FROM video_pool
                WHERE status IN ('pending', 'in_progress') AND comments_done < comments_target
                ORDER BY id ASC
                """
            )
        else:
            rows = await self._fetch_all(
                "SELECT * FROM video_pool WHERE status = ? ORDER BY id ASC",
                (status,),
            )
        return [VideoPoolItem.model_validate(dict(row)) for row in rows]

    async def update_pool_video_progress(
        self,
        pool_video_id: int,
        comments_added: int = 1,
        completed: bool = False,
    ) -> None:
        if comments_added < 0:
            raise ValueError("comments_added must not be negative")
        now = datetime.now(timezone.utc).isoformat()
        async with self._transaction() as conn:
            row = await (
                await conn.execute(
                    "SELECT comments_target, comments_done FROM video_pool WHERE id = ?",
                    (pool_video_id,),
                )
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(f"pool video {pool_video_id} not found")
            new_done = int(row["comments_done"]) + comments_added
            target = int(row["comments_target"])
            new_status = "completed" if (completed or new_done >= target) else "in_progress"
            await conn.execute(
                """
                UPDATE video_pool
                SET comments_done = ?,
                    status = ?,
                    last_processed_at = ?
                WHERE id = ?
                """,
                (new_done, new_status, now, pool_video_id),
            )
        logger.debug(
            "Updated pool video {} progress: done={}, status={}",
            pool_video_id,
            new_done,
            new_status,
        )

    async def count_pool_videos(self, status: str | None = "all") -> int:
        """Count pool videos; status "pending" matches list_pool_videos (still needs comments)."""
        if status == "all" or status is None:
            return await self._count("SELECT COUNT(*) FROM video_pool")
        if status == "pending":
            return await self._count(
                "SELECT COUNT(*) FROM video_pool"
                " WHERE status IN ('pending', 'in_progress') AND comments_done < comments_target"
            )
        return await self._count("SELECT COUNT(*) FROM video_pool WHERE status = ?", (status,))

    async def set_pool_comments_target(self, comments_target: int) -> int:
        """Set the reply target of unfinished pool videos (pending / in_progress); returns how many changed.

        Videos that already have that many replies become 'completed'.
        """
        if comments_target < 1:
            raise ValueError("comments_target must be positive")
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "UPDATE video_pool SET comments_target = ? WHERE status IN ('pending', 'in_progress')",
                (comments_target,),
            )
            changed = cursor.rowcount
            await conn.execute(
                "UPDATE video_pool SET status = 'completed'"
                " WHERE status IN ('pending', 'in_progress') AND comments_done >= comments_target"
            )
        return changed

    async def clear_video_pool(self) -> int:
        """Delete every video pool record; returns how many were removed."""
        async with self._transaction() as conn:
            cursor = await conn.execute("DELETE FROM video_pool")
            return cursor.rowcount



async def init_db(db_path: str | Path = "test_metrics.db") -> None:
    """Create (or verify) the schema. Used as a standalone smoke check."""
    db = Database(db_path)
    await db.connect()
    try:
        await db.init_schema()
    finally:
        await db.close()


__all__ = [
    "AccountCreate",
    "AccountRecord",
    "ActivityLogCreate",
    "ActivityLogEntry",
    "ActivityMode",
    "ActivityStatus",
    "Database",
    "Payload",
    "PayloadStatus",
    "QueueItem",
    "QueueKind",
    "QueueStatus",
    "RecordNotFoundError",
    "TestNode",
    "VideoPoolItem",
    "init_db",
]
