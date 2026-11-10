from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import random
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Iterable, Literal, NamedTuple
from urllib.parse import parse_qs, urlparse

import aiosqlite
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, field_validator

from config import PROXY_LIBRARY_MARKER, ProxyParts, ProxyUnavailableError, compose_proxy_url, split_proxy
from secret_store import SecretStoreError, protect_secret, reveal_secret

NodeStatus = Literal["idle", "busy", "cooldown", "disabled"]
PayloadStatus = Literal["pending", "submitted", "validated", "failed"]

# A failed payload whose error starts with this mark is a target YouTube gives no Reply button (replies off for that
# comment): every account sees the same, so it is skipped by every later run instead of being retried for a minute each.
REPLY_UNAVAILABLE_MARK = "reply_unavailable"
REPLY_UNAVAILABLE_TTL_SECONDS = 7 * 24 * 3600.0
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
    last_used_at        TEXT
);

-- Projects: a set of accounts with its own target (topics, own links, filters) and its own replies.
-- `search_topics` / `custom_replies` are JSON lists of strings; NULL `max_views` / `max_age_hours` = no limit;
-- NULL `daily_per_account` = no ceiling of the project (the account's own lifecycle limits always apply).
CREATE TABLE IF NOT EXISTS projects (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    enabled            INTEGER NOT NULL DEFAULT 1,
    sort               INTEGER NOT NULL DEFAULT 0,
    search_topics      TEXT    NOT NULL DEFAULT '[]',
    use_similar        INTEGER NOT NULL DEFAULT 0,
    use_title_words    INTEGER NOT NULL DEFAULT 0,
    min_views          INTEGER NOT NULL DEFAULT 0,
    max_views          INTEGER,
    max_age_hours      INTEGER,
    max_videos         INTEGER NOT NULL DEFAULT 5 CHECK (max_videos BETWEEN 1 AND 50),
    comments_per_video INTEGER NOT NULL DEFAULT 2 CHECK (comments_per_video BETWEEN 1 AND 100),
    target_mode        TEXT    NOT NULL DEFAULT 'first_reply',
    daily_per_account  INTEGER CHECK (daily_per_account IS NULL OR daily_per_account > 0),
    reply_mode         TEXT    NOT NULL DEFAULT 'ai' CHECK (reply_mode IN ('ai', 'custom')),
    custom_replies     TEXT    NOT NULL DEFAULT '[]',
    paraphrase_custom  INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL
);

-- Members of a project (an account may be in several); `daily_done` counts this project's replies of the account
-- today, restarted at the same daily boundary as the account's own counter (`Database.next_daily_reset`).
CREATE TABLE IF NOT EXISTS project_accounts (
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    account_id     INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    daily_done     INTEGER NOT NULL DEFAULT 0,
    daily_reset_at TEXT,
    added_at       TEXT    NOT NULL,
    PRIMARY KEY (project_id, account_id)
);

{video_pool}

-- The proxy library: one row per proxy (host, port, login), assigned to any number of accounts via accounts.proxy_id.
-- `password` is DPAPI-encrypted; `scheme` NULL = not known yet (a check finds it); `country` is the one the user
-- gave (`country_source` 'user') or the detected exit country ('check'); `last_*` hold the latest check.
CREATE TABLE IF NOT EXISTS proxies (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    scheme            TEXT,
    host              TEXT    NOT NULL,
    port              INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    login             TEXT    NOT NULL DEFAULT '',
    password          TEXT,
    country           TEXT,
    country_source    TEXT,
    label             TEXT,
    last_check_at     TEXT,
    last_ok           INTEGER,
    last_exit_ip      TEXT,
    last_exit_country TEXT,
    last_latency_ms   INTEGER,
    last_error        TEXT,
    created_at        TEXT    NOT NULL,
    UNIQUE (host, port, login)
);

-- Small key/value settings (`search_proxy_id`, migration flags).
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_accounts_name ON accounts(name);
CREATE INDEX IF NOT EXISTS idx_accounts_active ON accounts(is_active);
CREATE INDEX IF NOT EXISTS idx_video_pool_status ON video_pool(status, id);
CREATE INDEX IF NOT EXISTS idx_project_accounts_account ON project_accounts(account_id);
"""

# One row per video of one project: the same link may sit in several projects. `project_id` is never NULL once
# the projects are seeded (the pool methods resolve a missing one); `source` 'links' = pasted by the operator,
# 'search' = found by a topic search or a reference link for one run.
_VIDEO_POOL_DDL = """
CREATE TABLE IF NOT EXISTS {name} (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    video_url         TEXT    NOT NULL,
    video_id          TEXT    NOT NULL,
    status            TEXT    NOT NULL DEFAULT 'pending'
                              CHECK (status IN ('pending', 'in_progress', 'completed', 'failed')),
    comments_target   INTEGER NOT NULL DEFAULT 2 CHECK (comments_target > 0),
    comments_done     INTEGER NOT NULL DEFAULT 0 CHECK (comments_done >= 0),
    created_at        TEXT    NOT NULL,
    last_processed_at TEXT,
    source            TEXT    NOT NULL DEFAULT 'links',
    project_id        INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    UNIQUE (project_id, video_url)
);
"""
_MULTI_ACCOUNT_SCHEMA = _MULTI_ACCOUNT_SCHEMA.replace("{video_pool}", _VIDEO_POOL_DDL.format(name="video_pool").strip())
_VIDEO_POOL_COPY_COLUMNS = (
    "id, video_url, video_id, status, comments_target, comments_done, created_at, last_processed_at, source"
)

# `activity_log.project_id` has no foreign key on purpose: a deleted project keeps its id in the journal, so its
# records show as «удалён» (NULL = a run without a project). The account is a real reference.
_ACTIVITY_PROJECT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("project_id", "INTEGER"),
    ("account_id", "INTEGER REFERENCES accounts(id) ON DELETE SET NULL"),
)

# Result of the anonymous visibility check of a published reply (NULL = never checked). Written after the insert,
# by `Database.set_activity_visibility`; observability only, it never touches `status` or any counter.
_ACTIVITY_VISIBILITY_COLUMNS: tuple[tuple[str, str], ...] = (("visibility", "TEXT"),)
VISIBILITY_STATES: tuple[str, ...] = ("seen", "unseen", "unknown")

# Per-account "deck" of custom reply templates. A deck is one (account, scope) pair: the scope is the project the
# templates belong to ("p<id>", an account may work in several projects with overlapping lists) or "default" without
# one. `reply_deck` holds the current shuffled round (pos = order of the cards still unused, used = 1 once the
# template was really posted), `reply_deck_state` the last template posted from that deck.
DEFAULT_DECK_SCOPE = "default"

_REPLY_DECK_SCHEMA = """
CREATE TABLE IF NOT EXISTS reply_deck (
    account  TEXT    NOT NULL,
    scope    TEXT    NOT NULL DEFAULT 'default',
    template TEXT    NOT NULL,
    pos      INTEGER NOT NULL DEFAULT 0,
    used     INTEGER NOT NULL DEFAULT 0 CHECK (used IN (0, 1)),
    PRIMARY KEY (account, scope, template)
);

CREATE TABLE IF NOT EXISTS reply_deck_state (
    account       TEXT NOT NULL,
    scope         TEXT NOT NULL DEFAULT 'default',
    last_template TEXT,
    PRIMARY KEY (account, scope)
);
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

# Per-account switches: `lifecycle_mode` ("auto" by age / "mature" already warmed up / "unlimited" no limits) and
# `auto_warmup` (the orchestrator may start a background warm-up). Existing accounts become auto / on.
_ACCOUNT_MODE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("lifecycle_mode", "TEXT NOT NULL DEFAULT 'auto'"),
    ("auto_warmup", "INTEGER NOT NULL DEFAULT 1"),
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

# `accounts.proxy_id` -> `proxies.id`. `accounts.proxy_url` then holds PROXY_LIBRARY_MARKER (never an address).
_ACCOUNT_PROXY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("proxy_id", "INTEGER REFERENCES proxies(id) ON DELETE SET NULL"),
)

# Every account read joins its proxy, so `AccountRecord.proxy_url` is the ready address (password decrypted).
_ACCOUNT_SELECT = """
    SELECT accounts.*, proxies.scheme AS px_scheme, proxies.host AS px_host, proxies.port AS px_port,
           proxies.login AS px_login, proxies.password AS px_password
    FROM accounts LEFT JOIN proxies ON proxies.id = accounts.proxy_id
"""
_PROXY_SELECT = """
    SELECT proxies.*, (SELECT COUNT(*) FROM accounts WHERE accounts.proxy_id = proxies.id) AS accounts_count
    FROM proxies
"""
SEARCH_PROXY_KEY = "search_proxy_id"
PROXY_SCHEMES = ("http", "https", "socks5", "socks4")

QUARANTINE_HOURS = 24
DAILY_RESET_HOUR_UTC = 4
NAME_CHANGES_PER_WINDOW = 2  # YouTube: the channel name may change twice within 14 days
NAME_CHANGE_WINDOW_DAYS = 14

_ERROR_LIMIT = 2000
_SCHEMA_ATTEMPTS = 6

PROJECTS_SEEDED_KEY = "projects_seeded"  # app_settings flag: the first project was created (never seeded again)
UI_PROJECT_KEY = "ui_project"  # app_settings: the project open in «Цель» / «Ответы»
ACCOUNTS_REMOVED_KEY = "accounts_removed"  # app_settings flag: an account was deleted (config accounts are never seeded back)
DEFAULT_PROJECT_NAME = "Основной"
PROJECT_NAME_MAX = 40
PROJECT_TOPICS_MAX = 20
TARGET_MODES = ("first_reply", "popular_parent", "sub_comment", "mixed")
_PENDING_POOL_SQL = "status IN ('pending', 'in_progress') AND comments_done < comments_target"

# Every project read carries its counters: members, own links in total and own links still waiting for replies.
_PROJECT_SELECT = f"""
    SELECT projects.*,
           (SELECT COUNT(*) FROM project_accounts pa WHERE pa.project_id = projects.id) AS accounts_count,
           (SELECT COUNT(*) FROM video_pool v WHERE v.project_id = projects.id AND v.source = 'links') AS links_total,
           (SELECT COUNT(*) FROM video_pool v
             WHERE v.project_id = projects.id AND v.source = 'links' AND {_PENDING_POOL_SQL}) AS links_waiting
    FROM projects
"""
_PROJECT_ORDER = " ORDER BY projects.sort ASC, projects.id ASC"
PROJECT_SELECT, PROJECT_ORDER = _PROJECT_SELECT, _PROJECT_ORDER  # the interface reads projects with plain sqlite3


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
    project_id: int | None = None
    account_id: int | None = None
    visibility: str | None = None  # seen / unseen / unknown; None = the reply was not checked


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
    project_id: int | None = None  # the project the run belonged to (None: a run without projects)
    account_id: int | None = None  # the account that answered
    created_at: str | None = None


class AccountRecord(BaseModel):
    """One account. `proxy_url` is the ready address of its own proxy (decrypted; None = direct); `proxy_id` points
    into the proxy library; `proxy_unavailable` = a proxy is assigned but cannot be read (the account must not run:
    `config.resolve_account_proxy` raises ProxyUnavailableError)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    session_path: str
    proxy_url: str | None = Field(default=None, repr=False)
    proxy_id: int | None = None
    proxy_unavailable: bool = False
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
    lifecycle_mode: str = "auto"
    auto_warmup: bool = True
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


class ProxyRecord(BaseModel):
    """A proxy of the library. `password` is decrypted (None when absent or unreadable: `password_unavailable`).

    `accounts_count`: accounts it is assigned to; `is_search`: it is the search proxy; `url`: the address the bot
    uses (None while the password cannot be read); `address`: `host:port` for tables (never the password).
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    scheme: str | None = None
    host: str
    port: int
    login: str | None = None
    password: str | None = Field(default=None, repr=False, exclude=True)
    password_unavailable: bool = False
    country: str | None = None
    country_source: str | None = None
    label: str | None = None
    last_check_at: str | None = None
    last_ok: bool | None = None
    last_exit_ip: str | None = None
    last_exit_country: str | None = None
    last_latency_ms: int | None = None
    last_error: str | None = None
    created_at: str
    accounts_count: int = 0
    is_search: bool = False

    @property
    def address(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

    @property
    def url(self) -> str | None:
        if self.password_unavailable:
            return None
        return compose_proxy_url(self.scheme, self.host, self.port, self.login, self.password)


class ProxyDeleteResult(NamedTuple):
    """What `Database.delete_proxy` did: accounts that lost the proxy (now direct) and whether it was the search one."""

    detached_accounts: list[str]
    was_search: bool


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
    source: str = "links"  # 'links' pasted by the user, 'search' found by a topic search
    project_id: int | None = None


class ProjectRecord(BaseModel):
    """One project: its members (`project_accounts`), target and replies. `search_topics` / `custom_replies` are
    lists; `accounts_count`, `links_total`, `links_waiting` are counted on every read (own links only)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    enabled: bool = True
    sort: int = 0
    search_topics: list[str] = Field(default_factory=list)
    use_similar: bool = False
    use_title_words: bool = False
    min_views: int = 0
    max_views: int | None = None
    max_age_hours: int | None = None
    max_videos: int = 5
    comments_per_video: int = 2
    target_mode: str = "first_reply"
    daily_per_account: int | None = None
    reply_mode: str = "ai"
    custom_replies: list[str] = Field(default_factory=list)
    paraphrase_custom: bool = False
    created_at: str = ""
    updated_at: str = ""
    accounts_count: int = 0
    links_total: int = 0
    links_waiting: int = 0

    @field_validator("search_topics", "custom_replies", mode="before")
    @classmethod
    def _json_list(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return []
        return [str(item) for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


# Fields a project stores besides its name; create / update / the seed accept exactly these.
PROJECT_FIELDS: tuple[str, ...] = (
    "enabled", "search_topics", "use_similar", "use_title_words", "min_views", "max_views", "max_age_hours",
    "max_videos", "comments_per_video", "target_mode", "daily_per_account", "reply_mode", "custom_replies",
    "paraphrase_custom",
)


class ProjectQuota(NamedTuple):
    """Today's replies of one account in one project against the project's ceiling (None = no ceiling)."""

    done: int
    limit: int | None
    allowed: bool


class ProjectDeleteResult(NamedTuple):
    """What `Database.delete_project` removed: members that left it and its videos (the journal stays)."""

    accounts: int
    links: int


class LastProjectError(ValueError):
    """The last project cannot be deleted: a run always needs one."""


class MigrationError(RuntimeError):
    """A schema migration did not run (or was rolled back); `backup` is the copy of the file made before it."""

    def __init__(self, message: str, backup: Path | None = None) -> None:
        super().__init__(message)
        self.backup = backup


def clean_project_name(name: Any) -> str:
    """A project name: trimmed, one line, 1..40 characters. ValueError (Russian text) otherwise."""
    text = str(name or "").strip()
    if any(ch in text for ch in "\r\n\t"):
        raise ValueError("Название — одна строка, без переводов строк")
    text = " ".join(text.split())
    if not text:
        raise ValueError("Название не может быть пустым")
    if len(text) > PROJECT_NAME_MAX:
        raise ValueError(f"Название длиннее {PROJECT_NAME_MAX} символов")
    return text


def clean_topics(values: Iterable[Any]) -> list[str]:
    """Search topics: trimmed, without empty ones and case-insensitive duplicates, at most 20 (ValueError)."""
    topics: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            topics.append(text)
    if len(topics) > PROJECT_TOPICS_MAX:
        raise ValueError(f"Тем не больше {PROJECT_TOPICS_MAX}")
    return topics


def clean_templates(values: Iterable[Any]) -> list[str]:
    """Reply templates: one line each, empty ones dropped, order kept."""
    return [text for text in (" ".join(str(value or "").split()) for value in values) if text]


def _optional_positive(value: Any, field: str) -> int | None:
    """None / 0 / '' = no limit; a positive integer otherwise (ValueError)."""
    if value in (None, "", 0, False):
        return None
    number = int(value)
    if number < 1:
        raise ValueError(f"{field}: нужно положительное число или пусто")
    return number


def _bounded(value: Any, low: int, high: int, field: str) -> int:
    number = int(value)
    if not low <= number <= high:
        raise ValueError(f"{field}: нужно число от {low} до {high}")
    return number


def _clean_project_field(field: str, value: Any) -> Any:
    """One project field as the table stores it; ValueError for a bad value or an unknown field."""
    if field == "search_topics":
        return json.dumps(clean_topics(value or []), ensure_ascii=False)
    if field == "custom_replies":
        return json.dumps(clean_templates(value or []), ensure_ascii=False)
    if field in ("enabled", "use_similar", "use_title_words", "paraphrase_custom"):
        return 1 if value else 0
    if field == "min_views":
        number = int(value or 0)
        if number < 0:
            raise ValueError("Просмотры не могут быть меньше нуля")
        return number
    if field in ("max_views", "max_age_hours", "daily_per_account"):
        return _optional_positive(value, field)
    if field == "max_videos":
        return _bounded(value, 1, 50, "Видео за запуск")
    if field == "comments_per_video":
        return _bounded(value, 1, 100, "Ответов на видео")
    if field == "target_mode":
        mode = "first_reply" if value == "first_sub_comment" else str(value or "")
        if mode not in TARGET_MODES:
            raise ValueError(f"Неизвестный режим «{value}»")
        return mode
    if field == "reply_mode":
        if value not in ("ai", "custom"):
            raise ValueError(f"Неизвестный режим ответов «{value}»")
        return value
    raise ValueError(f"unknown project field {field!r}")


def clean_project_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Validate project fields (white list of PROJECT_FIELDS) into table values; max views never below min views."""
    cleaned = {field: _clean_project_field(field, value) for field, value in fields.items()}
    low, high = cleaned.get("min_views"), cleaned.get("max_views")
    if low is not None and high is not None and high < low:
        raise ValueError("Верхний предел просмотров меньше нижнего")
    return cleaned


def _clean_seed(seed: dict[str, Any] | None) -> dict[str, Any]:
    """The first project's fields from config.yaml: a value the project cannot take keeps its default (logged)."""
    cleaned: dict[str, Any] = {}
    for field, value in (seed or {}).items():
        try:
            cleaned.update(clean_project_fields({field: value}))
        except (TypeError, ValueError) as exc:
            logger.warning("Проект «{}»: значение {} из config.yaml не перенесено: {}", DEFAULT_PROJECT_NAME, field, exc)
    if cleaned.get("max_views") is not None and cleaned["max_views"] < cleaned.get("min_views", 0):
        cleaned["max_views"] = None
    return cleaned


def project_quota_today(done: int, reset_at: str | None, now: datetime | None = None) -> int:
    """Replies counted for today in `project_accounts`: the stored counter restarts once its reset moment passed."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    reset = _parse_utc_text(reset_at)
    return 0 if reset is None or moment >= reset else int(done or 0)


def _parse_utc_text(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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

    def __init__(
        self,
        db_path: str | Path = "test_metrics.db",
        busy_timeout_ms: int = 5000,
        *,
        project_seed: dict[str, Any] | None = None,
    ) -> None:
        """`project_seed`: the first project's fields taken from config.yaml (`modules.projects.project_seed`); the
        database never reads the config itself. Without it the first project starts with the defaults."""
        self._path = Path(db_path).expanduser()
        self._busy_timeout_ms = busy_timeout_ms
        self._project_seed = project_seed
        self.seeded_now = False  # this connection created «Основной» (the first start with projects)
        self.last_backup: Path | None = None  # the copy of the file made before a migration of this start
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

    async def init_schema(self, project_seed: dict[str, Any] | None = None) -> None:
        """Create / migrate the schema; a few retries when another process holds the write lock at the same time.

        `project_seed` (else the one given to the constructor) fills the first project «Основной» once.
        """
        seed = project_seed if project_seed is not None else self._project_seed
        try:
            for attempt in range(1, _SCHEMA_ATTEMPTS + 1):
                try:
                    await self._init_schema_once(seed)
                    return
                except aiosqlite.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == _SCHEMA_ATTEMPTS:
                        raise
                    logger.debug("База занята другим процессом, повтор инициализации схемы ({}/{})", attempt, _SCHEMA_ATTEMPTS)
                    await asyncio.sleep(0.2 * attempt)
        except BaseException:
            # a connection left open keeps aiosqlite's worker thread alive: the program would never exit
            await self.close()
            raise

    async def _init_schema_once(self, seed: dict[str, Any] | None = None) -> None:
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
            cursor = await conn.execute("PRAGMA table_info(accounts)")
            columns = {str(item[1]) for item in await cursor.fetchall()}
            await cursor.close()
            backed_up = False
            if columns and "proxy_id" not in columns:  # first start with the proxy library: keep the old file
                backed_up = await self._backup_before_migration(conn, "proxies") is not None
            await conn.executescript(_MULTI_ACCOUNT_SCHEMA)
            await self._migrate_reply_deck(conn)
            await conn.executescript(_REPLY_DECK_SCHEMA)
            await self._ensure_account_columns(conn)
            await self._ensure_account_mode_columns(conn)
            await self._ensure_pool_columns(conn)
            await conn.commit()
            await self._migrate_account_proxies(conn, backed_up)
            await self._rebuild_video_pool(conn, backed_up)
            await self._ensure_activity_columns(conn)
            await self._ensure_projects(conn, seed)
        logger.debug("Database schema ensured at {}", self._path)

    async def _migrate_account_proxies(self, conn: aiosqlite.Connection, backed_up: bool = False) -> None:
        """Idempotent: plain-text `accounts.proxy_url` values (older versions) move into the proxy library.

        Equal proxies merge into one record; the account gets `proxy_id` and the marker. A copy of the database file
        (`<db>.bak-proxies-<date>`) is written first, unless `backed_up` (the schema upgrade of this start already
        made one). A value that is not a proxy becomes the marker without a record:
        the account then refuses to run instead of going direct. If DPAPI cannot encrypt (no Windows logon session),
        nothing changes and the old values keep working until the next start.
        """
        pending = """
            SELECT id, name, proxy_url FROM accounts
            WHERE proxy_id IS NULL AND proxy_url IS NOT NULL AND TRIM(proxy_url) <> '' AND proxy_url <> ?
        """
        cursor = await conn.execute(pending, (PROXY_LIBRARY_MARKER,))
        found = await cursor.fetchone()
        await cursor.close()
        if found is None:
            return
        if not backed_up:
            await self._backup_before_migration(conn, "proxies")
        moved = broken = 0
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await conn.execute(pending, (PROXY_LIBRARY_MARKER,))
            rows = list(await cursor.fetchall())
            await cursor.close()
            for row in rows:
                try:
                    parts = split_proxy(row["proxy_url"])
                except ValueError:
                    logger.warning("Аккаунт {!r}: сохранённый прокси не разобран, аккаунт не запустится до нового прокси", row["name"])
                    await conn.execute("UPDATE accounts SET proxy_url = ? WHERE id = ?", (PROXY_LIBRARY_MARKER, row["id"]))
                    broken += 1
                    continue
                proxy_id, _ = await self._store_proxy(conn, parts)
                await conn.execute(
                    "UPDATE accounts SET proxy_id = ?, proxy_url = ? WHERE id = ?", (proxy_id, PROXY_LIBRARY_MARKER, row["id"])
                )
                moved += 1
        except (SecretStoreError, OSError) as exc:
            await conn.rollback()
            logger.warning(
                "Прокси аккаунтов не перенесены в библиотеку (пароль не зашифровать: {}); повтор при следующем запуске",
                type(exc).__name__,
            )
            return
        except BaseException:
            await conn.rollback()
            raise
        await conn.commit()
        logger.info("Прокси аккаунтов перенесены в библиотеку: аккаунтов {}, без разбираемого адреса {}", moved, broken)

    async def _backup_before_migration(self, conn: aiosqlite.Connection, tag: str) -> Path | None:
        """Consistent copy of the database (`VACUUM INTO`, WAL included) next to it: `<name>.bak-<tag>-<date>`."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self._path.with_name(f"{self._path.name}.bak-{tag}-{stamp}")
        suffix = 1
        while target.exists():
            suffix += 1
            target = self._path.with_name(f"{self._path.name}.bak-{tag}-{stamp}-{suffix}")
        try:
            await conn.execute("VACUUM INTO ?", (str(target),))
        except aiosqlite.Error as exc:
            logger.warning("Резервная копия базы перед миграцией не создана: {}", exc)
            return None
        logger.info("Резервная копия базы перед миграцией ({}): {}", tag, target)
        self.last_backup = target
        return target

    @staticmethod
    async def _columns(conn: aiosqlite.Connection, table: str) -> set[str]:
        cursor = await conn.execute(f"PRAGMA table_info({table})")
        try:
            return {str(row[1]) for row in await cursor.fetchall()}
        finally:
            await cursor.close()

    async def _migrate_reply_deck(self, conn: aiosqlite.Connection) -> None:
        """Decks made before they had a scope (key = account, template) move into the "default" scope, rows kept."""
        if not await self._columns(conn, "reply_deck") or "scope" in await self._columns(conn, "reply_deck"):
            return
        try:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute("ALTER TABLE reply_deck RENAME TO reply_deck_v1")
            if await self._columns(conn, "reply_deck_state"):
                await conn.execute("ALTER TABLE reply_deck_state RENAME TO reply_deck_state_v1")
            for statement in _REPLY_DECK_SCHEMA.split(";"):
                if statement.strip():
                    await conn.execute(statement)
            await conn.execute(
                "INSERT INTO reply_deck (account, scope, template, pos, used) "
                "SELECT account, ?, template, pos, used FROM reply_deck_v1",
                (DEFAULT_DECK_SCOPE,),
            )
            if await self._columns(conn, "reply_deck_state_v1"):
                await conn.execute(
                    "INSERT INTO reply_deck_state (account, scope, last_template) "
                    "SELECT account, ?, last_template FROM reply_deck_state_v1",
                    (DEFAULT_DECK_SCOPE,),
                )
                await conn.execute("DROP TABLE reply_deck_state_v1")
            await conn.execute("DROP TABLE reply_deck_v1")
            await conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                await conn.execute("ROLLBACK")
            raise

    @staticmethod
    async def _scalar(conn: aiosqlite.Connection, query: str, params: tuple[Any, ...] = ()) -> Any:
        cursor = await conn.execute(query, params)
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        return row[0] if row is not None else None

    async def _rebuild_video_pool(self, conn: aiosqlite.Connection, backed_up: bool) -> None:
        """Once: `video_pool` of an older version gets `project_id` and UNIQUE(project_id, video_url) instead of
        UNIQUE(video_url). SQLite cannot change a constraint, so the table is rebuilt in one transaction after a copy
        of the database file; the row count is compared before and after (a mismatch rolls everything back)."""
        if "project_id" in await self._columns(conn, "video_pool"):
            return
        if not backed_up and await self._backup_before_migration(conn, "projects") is None:
            raise MigrationError(
                "список видео не перестроен под проекты: не удалось сделать резервную копию базы "
                f"(нужно место на диске и права записи рядом с {self._path.name}); база не изменена"
            )
        await conn.execute("BEGIN IMMEDIATE")
        try:
            if "project_id" not in await self._columns(conn, "video_pool"):  # another process may have done it
                before = int(await self._scalar(conn, "SELECT COUNT(*) FROM video_pool"))
                await conn.execute("DROP TABLE IF EXISTS video_pool_new")
                await conn.execute(_VIDEO_POOL_DDL.format(name="video_pool_new"))
                await conn.execute(
                    f"INSERT INTO video_pool_new ({_VIDEO_POOL_COPY_COLUMNS}) "
                    f"SELECT {_VIDEO_POOL_COPY_COLUMNS} FROM video_pool"
                )
                after = int(await self._scalar(conn, "SELECT COUNT(*) FROM video_pool_new"))
                if after != before:
                    raise MigrationError(f"перестройка списка видео скопировала {after} строк из {before}; изменения отменены",
                                         self.last_backup)
                await conn.execute("DROP TABLE video_pool")
                await conn.execute("ALTER TABLE video_pool_new RENAME TO video_pool")
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_video_pool_status ON video_pool(status, id)")
                logger.info("Список видео перестроен под проекты: строк {}", after)
        except BaseException:
            await conn.rollback()
            raise
        await conn.commit()

    async def _ensure_activity_columns(self, conn: aiosqlite.Connection) -> None:
        """Incremental migration: the journal records the project and the account of each reply."""
        existing = await self._columns(conn, "activity_log")
        for column, ddl in (*_ACTIVITY_PROJECT_COLUMNS, *_ACTIVITY_VISIBILITY_COLUMNS):
            if column not in existing:
                try:
                    await conn.execute(f"ALTER TABLE activity_log ADD COLUMN {column} {ddl}")
                except aiosqlite.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_activity_project ON activity_log(project_id, created_at)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_video_pool_project ON video_pool(project_id, status, id)")
        await conn.commit()

    async def _ensure_projects(self, conn: aiosqlite.Connection, seed: dict[str, Any] | None) -> None:
        """Idempotent: the first start with projects creates «Основной» from `seed` (config.yaml values), makes every
        existing account its member and gives it the whole video pool; the `projects_seeded` flag is set in the same
        transaction, so a «Основной» the operator deleted later never comes back. Afterwards: a pool row without a
        project (written by an older version) joins the fallback project, and a database left without any project
        (deleted outside the program) gets an empty «Основной» again."""
        seeded = await self._scalar(conn, "SELECT value FROM app_settings WHERE key = ?", (PROJECTS_SEEDED_KEY,))
        orphans = await self._scalar(conn, "SELECT 1 FROM video_pool WHERE project_id IS NULL LIMIT 1")
        empty = await self._scalar(conn, "SELECT 1 FROM projects LIMIT 1") is None
        if seeded and not orphans and not empty:
            return
        await conn.execute("BEGIN IMMEDIATE")
        try:
            await self._seed_projects(conn, seed)
        except BaseException:
            await conn.rollback()
            raise
        await conn.commit()

    async def _seed_projects(self, conn: aiosqlite.Connection, seed: dict[str, Any] | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        seeded = await self._scalar(conn, "SELECT value FROM app_settings WHERE key = ?", (PROJECTS_SEEDED_KEY,))
        empty = await self._scalar(conn, "SELECT 1 FROM projects LIMIT 1") is None
        if not seeded:
            if empty:
                await self._insert_project(conn, DEFAULT_PROJECT_NAME, _clean_seed(seed))
                self.seeded_now = True
            project_id = await self._fallback_project_id(conn)
            members = await conn.execute(
                "INSERT OR IGNORE INTO project_accounts (project_id, account_id, added_at) SELECT ?, id, ? FROM accounts",
                (project_id, now),
            )
            await conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (PROJECTS_SEEDED_KEY, now),
            )
            logger.info("Проект «{}» создан: аккаунтов {}", DEFAULT_PROJECT_NAME, members.rowcount)
        elif empty:
            await self._insert_project(conn, DEFAULT_PROJECT_NAME, {})
            logger.warning("Проектов не осталось: создан пустой «{}»", DEFAULT_PROJECT_NAME)
        moved = await conn.execute(
            "UPDATE video_pool SET project_id = ? WHERE project_id IS NULL", (await self._fallback_project_id(conn),)
        )
        if moved.rowcount:
            logger.info("Видео без проекта переданы в «{}»: {}", DEFAULT_PROJECT_NAME, moved.rowcount)

    @staticmethod
    async def _insert_project(conn: aiosqlite.Connection, name: str, fields: dict[str, Any]) -> int:
        """Insert a project with already cleaned `fields` at the end of the order; returns its id."""
        now = datetime.now(timezone.utc).isoformat()
        sort = await Database._scalar(conn, "SELECT COALESCE(MAX(sort), -1) + 1 FROM projects")
        columns = ["name", "sort", "created_at", "updated_at", *fields]
        cursor = await conn.execute(
            f"INSERT INTO projects ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            (name, int(sort or 0), now, now, *fields.values()),
        )
        return int(cursor.lastrowid)

    @staticmethod
    async def _fallback_project_id(conn: aiosqlite.Connection) -> int:
        """The project that takes work without an explicit one: «Основной» if it exists, else the first in order."""
        value = await Database._scalar(
            conn, "SELECT id FROM projects ORDER BY (name = ?) DESC, sort ASC, id ASC LIMIT 1", (DEFAULT_PROJECT_NAME,)
        )
        if value is None:
            raise RecordNotFoundError("no project exists")
        return int(value)

    @staticmethod
    async def _ensure_account_columns(conn: aiosqlite.Connection) -> None:
        """Incremental migration: add the auth columns to an `accounts` table created by an older version."""
        cursor = await conn.execute("PRAGMA table_info(accounts)")
        existing = {str(row[1]) for row in await cursor.fetchall()}
        await cursor.close()
        for column, ddl in (
            *_ACCOUNT_AUTH_COLUMNS, *_ACCOUNT_LIFECYCLE_COLUMNS, *_ACCOUNT_PROFILE_COLUMNS, *_ACCOUNT_PROXY_COLUMNS
        ):
            if column not in existing:
                try:
                    await conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {ddl}")
                except aiosqlite.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():  # another process added it first
                        raise
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_accounts_proxy ON accounts(proxy_id)")
        # accounts signed in before the lifecycle existed age from their creation, not from their next re-login
        await conn.execute(
            "UPDATE accounts SET authenticated_at = created_at WHERE auth_status = 'authenticated' AND authenticated_at IS NULL"
        )

    @staticmethod
    async def _ensure_account_mode_columns(conn: aiosqlite.Connection) -> None:
        """Incremental migration: `lifecycle_mode` / `auto_warmup` for accounts created before the age modes existed."""
        cursor = await conn.execute("PRAGMA table_info(accounts)")
        existing = {str(row[1]) for row in await cursor.fetchall()}
        await cursor.close()
        for column, ddl in _ACCOUNT_MODE_COLUMNS:
            if column not in existing:
                try:
                    await conn.execute(f"ALTER TABLE accounts ADD COLUMN {column} {ddl}")
                except aiosqlite.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise

    @staticmethod
    async def _ensure_pool_columns(conn: aiosqlite.Connection) -> None:
        """Incremental migration: `video_pool.source` (own links vs topic search) for pools created before it existed."""
        cursor = await conn.execute("PRAGMA table_info(video_pool)")
        existing = {str(row[1]) for row in await cursor.fetchall()}
        await cursor.close()
        if "source" not in existing:
            try:
                await conn.execute("ALTER TABLE video_pool ADD COLUMN source TEXT NOT NULL DEFAULT 'links'")
            except aiosqlite.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

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

            cursor = await conn.execute(
                "SELECT 1 FROM payloads WHERE source_ref = ? AND status = 'failed' AND error LIKE ? AND created_at > ? LIMIT 1",
                (source_ref, f"{REPLY_UNAVAILABLE_MARK}%", now - REPLY_UNAVAILABLE_TTL_SECONDS),
            )
            no_reply_button = await cursor.fetchone()
            await cursor.close()
            if no_reply_button is not None:
                return None

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
        if count > 0:
            return True
        # a target without a Reply button counts as handled too: no account can answer it
        return await self.is_reply_unavailable(source_ref)

    async def is_reply_unavailable(self, source_ref: str) -> bool:
        """True when a recent run found that YouTube hides the Reply button on this comment."""
        count = await self._count(
            "SELECT COUNT(*) FROM payloads WHERE source_ref = ? AND status = 'failed' AND error LIKE ? AND created_at > ?",
            (source_ref, f"{REPLY_UNAVAILABLE_MARK}%", time.time() - REPLY_UNAVAILABLE_TTL_SECONDS),
        )
        return count > 0

    async def mark_reply_unavailable(
        self,
        *,
        node_id: int | None = None,
        source_ref: str,
        language: str = "en",
        source_text: str | None = None,
        reason: str = "",
    ) -> None:
        """Remember a target without a Reply button (a failed payload carrying REPLY_UNAVAILABLE_MARK)."""
        if not source_ref or not source_ref.strip():
            raise ValueError("source_ref must not be empty")
        now = time.time()
        error = _truncate(f"{REPLY_UNAVAILABLE_MARK}: {reason}".strip(": "))
        async with self._transaction() as conn:
            await conn.execute(
                """
                INSERT INTO payloads (node_id, source_ref, source_text, text, language, status, error, created_at, validated_at)
                VALUES (?, ?, ?, '', ?, 'failed', ?, ?, ?)
                """,
                (node_id, source_ref, source_text, language, error, now, now),
            )

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
        project_id = data.get("project_id")
        account_id = data.get("account_id")

        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO activity_log (
                    created_at, video_id, video_title, video_url, target_id, target_author,
                    target_text, reply_text, mode, status, error, duration_sec, payload_id, project_id, account_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    project_id,
                    account_id,
                ),
            )
            log_id = cursor.lastrowid
            row = await (await conn.execute("SELECT * FROM activity_log WHERE id = ?", (log_id,))).fetchone()
        if row is None:
            raise RecordNotFoundError(f"activity_log {log_id} not found")
        return ActivityLogEntry.model_validate(dict(row))

    async def set_activity_visibility(self, log_id: int, state: str) -> None:
        """Store the visibility check result of one journal record; a record that is gone is a silent no-op."""
        if state not in VISIBILITY_STATES:
            raise ValueError(f"visibility must be one of {VISIBILITY_STATES}, got {state!r}")
        async with self._transaction() as conn:
            await conn.execute("UPDATE activity_log SET visibility = ? WHERE id = ?", (state, log_id))

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
        project_id: int | None = None,
    ) -> list[ActivityLogEntry]:
        """Fetch a paginated list of activity logs, newest first (optionally of one status / one project)."""
        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 50
        offset = (page - 1) * page_size
        where: list[str] = []
        params: list[Any] = []
        if status_filter:
            where.append("status = ?")
            params.append(status_filter)
        if project_id is not None:
            where.append("project_id = ?")
            params.append(project_id)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        rows = await self._fetch_all(
            f"SELECT * FROM activity_log{clause} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, page_size, offset)
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
        """AccountRecord from an `_ACCOUNT_SELECT` row: `password` / `totp_secret` decrypted (values of older versions
        are plain text), `proxy_url` assembled from the joined proxy record, `proxy_unavailable` when it cannot be."""
        data = dict(row)
        joined = {key[3:]: data.pop(key) for key in list(data) if key.startswith("px_")}
        for field in ("password", "totp_secret"):
            try:
                data[field] = reveal_secret(data.get(field))
            except SecretStoreError as exc:
                logger.warning("Аккаунт {!r}: поле {} недоступно: {}", data.get("name"), field, exc)
                data[field] = None
        stored = (data.get("proxy_url") or "").strip()
        data["proxy_url"], data["proxy_unavailable"] = None, False
        if data.get("proxy_id") is not None:
            if joined.get("host") is None:
                data["proxy_unavailable"] = True  # the record is gone
            else:
                try:
                    password = reveal_secret(joined.get("password"))
                    data["proxy_url"] = compose_proxy_url(
                        joined.get("scheme"), joined["host"], joined["port"], joined.get("login") or None, password
                    )
                except SecretStoreError as exc:
                    logger.warning("Аккаунт {!r}: пароль прокси недоступен: {}", data.get("name"), exc)
                    data["proxy_unavailable"] = True
        elif stored == PROXY_LIBRARY_MARKER:
            data["proxy_unavailable"] = True  # assigned once, the record is lost
        elif stored:
            data["proxy_url"] = stored  # written by an older version after the migration: still this account's proxy
        return AccountRecord.model_validate(data)

    @staticmethod
    def _proxy_from_row(row: aiosqlite.Row, search_id: int | None = None) -> ProxyRecord:
        data = dict(row)
        data["login"] = data.get("login") or None
        try:
            data["password"] = reveal_secret(data.get("password"))
        except SecretStoreError as exc:
            logger.warning("Прокси {}:{}: пароль недоступен: {}", data.get("host"), data.get("port"), exc)
            data["password"], data["password_unavailable"] = None, True
        if data.get("last_ok") is not None:
            data["last_ok"] = bool(data["last_ok"])
        data["is_search"] = search_id is not None and data.get("id") == search_id
        return ProxyRecord.model_validate(data)

    @staticmethod
    async def _store_proxy(
        conn: aiosqlite.Connection,
        parts: ProxyParts,
        country: str | None = None,
        label: str | None = None,
    ) -> tuple[int, bool]:
        """Find-or-create by (host, port, login) inside the caller's transaction; returns (id, created).

        An existing record takes the new password, a known scheme and the given country / label; the accounts using
        it keep their sign-in status (the proxy itself did not change).
        """
        host = parts.host.strip().strip("[]").lower()
        if not host:
            raise ValueError("у прокси не указан адрес")
        if not 0 < int(parts.port) < 65536:
            raise ValueError("порт прокси должен быть от 1 до 65535")
        scheme = (parts.scheme or "").lower() or None
        if scheme == "socks5h":
            scheme = "socks5"
        if scheme is not None and scheme not in PROXY_SCHEMES:
            raise ValueError(f"тип прокси {scheme!r} не поддерживается (http, https, socks5)")
        login = (parts.login or "").strip()
        country = country.strip().upper() if country and country.strip() else None
        label = label.strip() if label and label.strip() else None
        password = protect_secret(parts.password) if parts.password else None
        cursor = await conn.execute(
            "SELECT id, password FROM proxies WHERE host = ? AND port = ? AND login = ?", (host, int(parts.port), login)
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            cursor = await conn.execute(
                """
                INSERT INTO proxies (scheme, host, port, login, password, country, country_source, label, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (scheme, host, int(parts.port), login, password, country, "user" if country else None, label,
                 datetime.now(timezone.utc).isoformat()),
            )
            return int(cursor.lastrowid), True
        try:
            same_password = reveal_secret(row["password"]) == parts.password
        except SecretStoreError:
            same_password = False
        await conn.execute(
            """
            UPDATE proxies
            SET scheme = COALESCE(?, scheme),
                password = CASE WHEN ? THEN password ELSE ? END,
                country = COALESCE(?, country),
                country_source = CASE WHEN ? IS NOT NULL THEN 'user' ELSE country_source END,
                label = COALESCE(?, label)
            WHERE id = ?
            """,
            (scheme, 1 if same_password else 0, password, country, country, label, row["id"]),
        )
        return int(row["id"]), False

    @staticmethod
    async def _account_rows(conn: aiosqlite.Connection, accounts: Iterable[int | str]) -> list[aiosqlite.Row]:
        """Rows of the given accounts (ids or names) in the given order; RecordNotFoundError for an unknown one."""
        rows: list[aiosqlite.Row] = []
        for key in accounts:
            column = "id" if isinstance(key, int) else "name"
            cursor = await conn.execute(f"SELECT id, name, proxy_id, proxy_url FROM accounts WHERE {column} = ?", (key,))
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                raise RecordNotFoundError(f"account {key!r} not found")
            if all(row["id"] != seen["id"] for seen in rows):
                rows.append(row)
        return rows

    @staticmethod
    async def _set_account_proxy(
        conn: aiosqlite.Connection, row: aiosqlite.Row, proxy_id: int | None, reset_auth: bool = True
    ) -> bool:
        """Point one account at `proxy_id` (None = direct). False when nothing changes. A real change resets
        `auth_status` to `pending` (the next sign-in is checked from the new address) unless `reset_auth` is False."""
        marker = PROXY_LIBRARY_MARKER if proxy_id is not None else None
        if row["proxy_id"] == proxy_id and (row["proxy_url"] or None) == marker:
            return False
        await conn.execute(
            """
            UPDATE accounts
            SET proxy_id = ?, proxy_url = ?, auth_status = CASE WHEN ? THEN 'pending' ELSE auth_status END
            WHERE id = ?
            """,
            (proxy_id, marker, 1 if reset_auth and row["proxy_id"] != proxy_id else 0, row["id"]),
        )
        return True

    # ------------------------------------------------------------ proxy library
    #
    # API for the wizard («Прокси» stage). A proxy is stored once and assigned to any number of accounts; the
    # address is assembled only by `config.compose_proxy_url`. `AccountRecord.proxy_url` stays the ready address.

    async def add_proxy(
        self,
        parts: ProxyParts,
        country: str | None = None,
        label: str | None = None,
    ) -> tuple[ProxyRecord, bool]:
        """Add a proxy or update the one with the same host, port and login (new password / scheme / country /
        label; nothing is duplicated). Returns (record, created). The accounts using it keep their sign-in status.
        ValueError on a bad port / type; SecretStoreError when DPAPI cannot encrypt the password."""
        async with self._transaction() as conn:
            proxy_id, created = await self._store_proxy(conn, parts, country, label)
        record = await self.get_proxy(proxy_id)
        assert record is not None
        return record, created

    async def add_proxy_url(self, proxy_url: str, country: str | None = None, label: str | None = None) -> tuple[ProxyRecord, bool]:
        """`add_proxy` from any address form `config.parse_proxy` accepts (ValueError otherwise)."""
        return await self.add_proxy(split_proxy(proxy_url), country, label)

    async def list_proxies(self) -> list[ProxyRecord]:
        """All proxies, oldest first, with `accounts_count` and `is_search` filled."""
        search_id = await self._search_proxy_id()
        rows = await self._fetch_all(_PROXY_SELECT + " ORDER BY proxies.id ASC")
        return [self._proxy_from_row(row, search_id) for row in rows]

    async def get_proxy(self, proxy_id: int) -> ProxyRecord | None:
        search_id = await self._search_proxy_id()
        row = await self._fetch_one(_PROXY_SELECT + " WHERE proxies.id = ?", (proxy_id,))
        return self._proxy_from_row(row, search_id) if row else None

    async def update_proxy(
        self,
        proxy_id: int,
        *,
        scheme: str | None | _Unset = UNSET,
        password: str | None | _Unset = UNSET,
        country: str | None | _Unset = UNSET,
        label: str | None | _Unset = UNSET,
    ) -> ProxyRecord:
        """Edit one proxy in place (only the passed fields; None clears). Host, port and login identify a proxy: a
        different one is `add_proxy`. Accounts keep the proxy and their sign-in status. RecordNotFoundError."""
        fields: dict[str, Any] = {}
        if not isinstance(scheme, _Unset):
            scheme = (scheme or "").lower() or None
            if scheme is not None and scheme not in PROXY_SCHEMES:
                raise ValueError(f"тип прокси {scheme!r} не поддерживается (http, https, socks5)")
            fields["scheme"] = scheme
        if not isinstance(password, _Unset):
            fields["password"] = protect_secret(password) if password else None
        if not isinstance(country, _Unset):
            fields["country"] = country.strip().upper() if country and country.strip() else None
            fields["country_source"] = "user" if fields["country"] else None
        if not isinstance(label, _Unset):
            fields["label"] = label.strip() if label and label.strip() else None
        async with self._transaction() as conn:
            if fields:
                assignments = ", ".join(f"{column} = ?" for column in fields)
                cursor = await conn.execute(
                    f"UPDATE proxies SET {assignments} WHERE id = ?", (*fields.values(), proxy_id)
                )
            else:
                cursor = await conn.execute("SELECT 1 FROM proxies WHERE id = ?", (proxy_id,))
                if await cursor.fetchone() is None:
                    raise RecordNotFoundError(f"proxy id={proxy_id} not found")
            if fields and cursor.rowcount == 0:
                raise RecordNotFoundError(f"proxy id={proxy_id} not found")
        record = await self.get_proxy(proxy_id)
        assert record is not None
        return record

    async def delete_proxy(self, proxy_id: int) -> ProxyDeleteResult:
        """Delete a proxy. Its accounts are detached: they go direct from now on, with `auth_status` reset to
        `pending` (show `detached_accounts` to the user). If it was the search proxy, the search goes direct too.
        RecordNotFoundError when there is no such proxy."""
        async with self._transaction() as conn:
            cursor = await conn.execute("SELECT 1 FROM proxies WHERE id = ?", (proxy_id,))
            if await cursor.fetchone() is None:
                raise RecordNotFoundError(f"proxy id={proxy_id} not found")
            cursor = await conn.execute(
                "SELECT id, name, proxy_id, proxy_url FROM accounts WHERE proxy_id = ? ORDER BY id", (proxy_id,)
            )
            rows = list(await cursor.fetchall())
            for row in rows:
                await self._set_account_proxy(conn, row, None)
            cursor = await conn.execute("SELECT value FROM app_settings WHERE key = ?", (SEARCH_PROXY_KEY,))
            setting = await cursor.fetchone()
            was_search = setting is not None and setting["value"] == str(proxy_id)
            if was_search:
                await conn.execute("DELETE FROM app_settings WHERE key = ?", (SEARCH_PROXY_KEY,))
            await conn.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
        return ProxyDeleteResult([row["name"] for row in rows], was_search)

    async def record_proxy_check(
        self,
        proxy_id: int,
        ok: bool,
        *,
        scheme: str | None = None,
        exit_ip: str | None = None,
        exit_country: str | None = None,
        latency_ms: int | None = None,
        error: str | None = None,
    ) -> ProxyRecord:
        """Store a check result (`modules.proxy_check.check_and_record` calls it). A detected `scheme` fills an
        unknown type; a detected `exit_country` fills the country unless the user set one. RecordNotFoundError."""
        exit_country = exit_country.upper() if exit_country else None
        async with self._transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE proxies
                SET last_check_at = ?, last_ok = ?, last_exit_ip = ?, last_exit_country = ?, last_latency_ms = ?,
                    last_error = ?,
                    scheme = COALESCE(scheme, ?),
                    country = CASE WHEN ? IS NOT NULL AND COALESCE(country_source, 'check') = 'check' THEN ? ELSE country END,
                    country_source = CASE WHEN ? IS NOT NULL AND COALESCE(country_source, 'check') = 'check'
                                          THEN 'check' ELSE country_source END
                WHERE id = ?
                """,
                (
                    datetime.now(timezone.utc).isoformat(), 1 if ok else 0, exit_ip, exit_country, latency_ms,
                    _truncate(error), (scheme or "").lower() or None, exit_country, exit_country, exit_country, proxy_id,
                ),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"proxy id={proxy_id} not found")
        record = await self.get_proxy(proxy_id)
        assert record is not None
        return record

    async def assign_proxy(
        self, proxy_id: int | None, accounts: Iterable[int | str], *, reset_auth: bool = True
    ) -> list[str]:
        """Give these accounts (ids or names) the proxy `proxy_id`; None takes their proxy away (they go direct).

        Returns the names whose proxy really changed (an account that already had it is not listed). A change
        resets `auth_status` to `pending` (`reset_auth=False` keeps it: only for moves that keep the same address).
        RecordNotFoundError for an unknown proxy or account; nothing changes then.
        """
        async with self._transaction() as conn:
            if proxy_id is not None:
                cursor = await conn.execute("SELECT 1 FROM proxies WHERE id = ?", (proxy_id,))
                if await cursor.fetchone() is None:
                    raise RecordNotFoundError(f"proxy id={proxy_id} not found")
            changed: list[str] = []
            for row in await self._account_rows(conn, accounts):
                if await self._set_account_proxy(conn, row, proxy_id, reset_auth):
                    changed.append(row["name"])
        return changed

    async def clear_account_proxy(self, accounts: Iterable[int | str]) -> list[str]:
        """`assign_proxy(None, accounts)`: the accounts go direct; returns the names that really changed."""
        return await self.assign_proxy(None, accounts)

    async def get_setting(self, key: str) -> str | None:
        row = await self._fetch_one("SELECT value FROM app_settings WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str | None) -> None:
        """Store a setting; None deletes it."""
        async with self._transaction() as conn:
            if value is None:
                await conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
            else:
                await conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    async def _search_proxy_id(self) -> int | None:
        value = await self.get_setting(SEARCH_PROXY_KEY)
        return int(value) if value and value.isdigit() else None

    async def get_search_proxy(self) -> ProxyRecord | None:
        """The proxy of the search / comment reading (InnerTube collector); None = it goes direct."""
        proxy_id = await self._search_proxy_id()
        return await self.get_proxy(proxy_id) if proxy_id is not None else None

    async def set_search_proxy(self, proxy_id: int | None) -> None:
        """Make `proxy_id` the search proxy (None: the search goes direct). Accounts are not touched."""
        if proxy_id is not None and await self.get_proxy(proxy_id) is None:
            raise RecordNotFoundError(f"proxy id={proxy_id} not found")
        await self.set_setting(SEARCH_PROXY_KEY, None if proxy_id is None else str(proxy_id))

    async def search_proxy_url(self) -> str | None:
        """Address for the collector: None = direct. Fail-closed: a search proxy that is set but cannot be read
        (record gone, password not decryptable) raises ProxyUnavailableError, the search never silently goes direct."""
        proxy_id = await self._search_proxy_id()
        if proxy_id is None:
            return None
        record = await self.get_proxy(proxy_id)
        if record is None or record.url is None:
            raise ProxyUnavailableError(
                "Прокси поиска не читается (запись удалена или пароль не расшифровывается на этом компьютере). "
                "Поиск не запущен, чтобы не идти с вашего IP: выберите прокси поиска заново на этапе «Прокси»"
            )
        return record.url

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
        A `proxy_url` string (any `config.parse_proxy` form) is found-or-created in the proxy library and the account
        points at it. Credentials passed as None keep what is stored. A changed email, password or proxy resets
        `auth_status` to `pending`. `password` and `totp_secret` are stored encrypted (Windows DPAPI).
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
        parts = split_proxy(proxy_url) if isinstance(proxy_url, str) else None  # ValueError on a malformed proxy
        now = datetime.now(timezone.utc).isoformat()
        async with self._transaction() as conn:
            new_proxy_id = (await self._store_proxy(conn, parts))[0] if parts is not None else None
            row = await (await conn.execute(_ACCOUNT_SELECT + " WHERE accounts.name = ?", (name,))).fetchone()
            if row is None:
                if isinstance(session_path, _Unset):
                    raise ValueError("session_path is required for a new account")
                await conn.execute(
                    """
                    INSERT INTO accounts (name, session_path, proxy_url, proxy_id, is_active, total_replies, created_at,
                                          email, password, totp_secret, recovery_email, geo_country,
                                          expected_country, locale)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        session_path,
                        PROXY_LIBRARY_MARKER if new_proxy_id is not None else None,
                        new_proxy_id,
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
                new_active = current.is_active if isinstance(is_active, _Unset) else bool(is_active)
                new_country = current.geo_country if isinstance(geo_country, _Unset) else geo_country
                new_expected = current.expected_country if isinstance(expected_country, _Unset) else expected_country
                new_locale = current.locale if isinstance(locale, _Unset) else locale
                proxy_changed = False
                if not isinstance(proxy_url, _Unset):
                    proxy_changed = await self._set_account_proxy(conn, row, new_proxy_id, reset_auth=False)
                credentials_changed = (
                    (email is not None and email != current.email)
                    or (password is not None and password != current.password)
                    or proxy_changed
                )
                await conn.execute(
                    """
                    UPDATE accounts
                    SET session_path = ?, is_active = ?,
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
            row = await (await conn.execute(_ACCOUNT_SELECT + " WHERE accounts.name = ?", (name,))).fetchone()
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
            row = await (await conn.execute(_ACCOUNT_SELECT + " WHERE accounts.name = ?", (old_name,))).fetchone()
            if row is None:
                raise RecordNotFoundError(f"account {old_name!r} not found")
            if new_name != old_name:
                taken = await (await conn.execute("SELECT 1 FROM accounts WHERE name = ?", (new_name,))).fetchone()
                if taken:
                    raise ValueError(f"account {new_name!r} already exists")
                await conn.execute("UPDATE accounts SET name = ? WHERE id = ?", (new_name, row["id"]))
                # the template deck follows the account (a leftover deck of the new name belongs to a deleted account)
                await conn.execute("DELETE FROM reply_deck WHERE account = ?", (new_name,))
                await conn.execute("DELETE FROM reply_deck_state WHERE account = ?", (new_name,))
                await conn.execute("UPDATE reply_deck SET account = ? WHERE account = ?", (new_name, old_name))
                await conn.execute("UPDATE reply_deck_state SET account = ? WHERE account = ?", (new_name, old_name))
                row = await (await conn.execute(_ACCOUNT_SELECT + " WHERE accounts.id = ?", (row["id"],))).fetchone()
        account = self._account_from_row(row)
        logger.debug("Renamed account {!r} -> {!r} (id={})", old_name, account.name, account.id)
        return account

    async def delete_account(self, name: str) -> AccountRecord:
        """Remove the account (its encrypted credentials, project membership, template deck) for good.

        The journal keeps its rows (`activity_log.account_id` becomes NULL). The `accounts_removed` flag stops the
        config.yaml accounts (or the implicit «default») from being seeded back into an emptied table. The session
        folder and config.yaml are the caller's job.
        """
        async with self._transaction() as conn:
            row = await (await conn.execute(_ACCOUNT_SELECT + " WHERE accounts.name = ?", (name,))).fetchone()
            if row is None:
                raise RecordNotFoundError(f"account {name!r} not found")
            await conn.execute("DELETE FROM accounts WHERE id = ?", (row["id"],))
            await conn.execute("DELETE FROM reply_deck WHERE account = ?", (name,))
            await conn.execute("DELETE FROM reply_deck_state WHERE account = ?", (name,))
            await conn.execute(
                "INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (ACCOUNTS_REMOVED_KEY, datetime.now(timezone.utc).isoformat()),
            )
        account = self._account_from_row(row)
        logger.debug("Deleted account {!r} (id={})", account.name, account.id)
        return account

    # ------------------------------------------------------------- reply deck

    async def deck_load(
        self,
        account: str,
        scope: str,
        templates: list[str],
        rng: random.Random,
    ) -> tuple[list[str], bool]:
        """Bring the (account, scope) deck in line with `templates`: (unused cards in draw order, reshuffled).

        A deck is private to its scope: another scope (project) of the same account never sees or resets its cards,
        even for a template both lists share. Templates missing from `templates` are ignored (their rows stay); new
        ones join the unused part at a random place. When no card is left a new deck is shuffled from all
        `templates`; its first card differs from the last template posted from this deck whenever there is more than
        one template. Nothing is burned here: see `deck_burn`.
        """
        if not templates:
            return [], False
        wanted = list(dict.fromkeys(templates))
        reshuffled = False
        async with self._transaction() as conn:
            rows = await (
                await conn.execute(
                    "SELECT template, used FROM reply_deck WHERE account = ? AND scope = ? ORDER BY pos ASC",
                    (account, scope),
                )
            ).fetchall()
            current = {str(r["template"]): bool(r["used"]) for r in rows}
            unused = [str(r["template"]) for r in rows if not r["used"] and str(r["template"]) in wanted]
            for fresh in (t for t in wanted if t not in current):
                unused.insert(rng.randint(0, len(unused)), fresh)
            if not unused:
                state = await (
                    await conn.execute(
                        "SELECT last_template FROM reply_deck_state WHERE account = ? AND scope = ?", (account, scope)
                    )
                ).fetchone()
                last = state["last_template"] if state else None
                unused = list(wanted)
                rng.shuffle(unused)
                if len(unused) > 1 and unused[0] == last:
                    swap = rng.randint(1, len(unused) - 1)
                    unused[0], unused[swap] = unused[swap], unused[0]
                reshuffled = True
            for pos, template in enumerate(unused):
                await conn.execute(
                    "INSERT INTO reply_deck (account, scope, template, pos, used) VALUES (?, ?, ?, ?, 0) "
                    "ON CONFLICT(account, scope, template) DO UPDATE SET pos = excluded.pos, used = 0",
                    (account, scope, template, pos),
                )
        return unused, reshuffled

    async def deck_burn(self, account: str, scope: str, template: str) -> None:
        """The template was really posted from this deck: it leaves it until the next shuffle."""
        async with self._transaction() as conn:
            await conn.execute(
                "UPDATE reply_deck SET used = 1 WHERE account = ? AND scope = ? AND template = ?",
                (account, scope, template),
            )
            await conn.execute(
                "INSERT INTO reply_deck_state (account, scope, last_template) VALUES (?, ?, ?) "
                "ON CONFLICT(account, scope) DO UPDATE SET last_template = excluded.last_template",
                (account, scope, template),
            )

    async def deck_snapshot(self, account: str, scope: str | None = None) -> tuple[list[tuple[str, bool]], str | None]:
        """Read-only view: ([(template, used)] in draw order, last posted template).

        Without `scope` every deck of the account is merged; the last template is then known only when the account
        has a single deck.
        """
        if scope is None:
            rows = await self._fetch_all(
                "SELECT template, used FROM reply_deck WHERE account = ? ORDER BY scope, used ASC, pos ASC", (account,)
            )
            states = await self._fetch_all("SELECT last_template FROM reply_deck_state WHERE account = ?", (account,))
            last = states[0]["last_template"] if len(states) == 1 else None
        else:
            rows = await self._fetch_all(
                "SELECT template, used FROM reply_deck WHERE account = ? AND scope = ? ORDER BY used ASC, pos ASC",
                (account, scope),
            )
            state = await self._fetch_one(
                "SELECT last_template FROM reply_deck_state WHERE account = ? AND scope = ?", (account, scope)
            )
            last = state["last_template"] if state else None
        return [(str(r["template"]), bool(r["used"])) for r in rows], last

    async def get_account(self, account_id_or_name: int | str) -> AccountRecord | None:
        if isinstance(account_id_or_name, int):
            row = await self._fetch_one(_ACCOUNT_SELECT + " WHERE accounts.id = ?", (account_id_or_name,))
        else:
            row = await self._fetch_one(_ACCOUNT_SELECT + " WHERE accounts.name = ?", (str(account_id_or_name),))
            if row is None and isinstance(account_id_or_name, str) and account_id_or_name.isdigit():
                row = await self._fetch_one(_ACCOUNT_SELECT + " WHERE accounts.id = ?", (int(account_id_or_name),))
        return self._account_from_row(row) if row else None

    async def list_active_accounts(self) -> list[AccountRecord]:
        rows = await self._fetch_all(_ACCOUNT_SELECT + " WHERE accounts.is_active = 1 ORDER BY accounts.id ASC")
        return [self._account_from_row(row) for row in rows]

    async def list_accounts(self, is_active: bool | None = None) -> list[AccountRecord]:
        if is_active is None:
            rows = await self._fetch_all(_ACCOUNT_SELECT + " ORDER BY accounts.id ASC")
        else:
            val = 1 if is_active else 0
            rows = await self._fetch_all(_ACCOUNT_SELECT + " WHERE accounts.is_active = ? ORDER BY accounts.id ASC", (val,))
        return [self._account_from_row(row) for row in rows]

    async def set_account_auth_status(self, account_id: int, status: str) -> None:
        async with self._transaction() as conn:
            cursor = await conn.execute("UPDATE accounts SET auth_status = ? WHERE id = ?", (status, account_id))
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"account id={account_id} not found")

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

    async def set_account_lifecycle_mode(self, account_id: int, mode: str) -> None:
        """"auto" / "mature" / "unlimited"; the stage is re-derived by the engine on its next check."""
        if mode not in ("auto", "mature", "unlimited"):
            raise ValueError(f"unknown lifecycle mode {mode!r}")
        async with self._transaction() as conn:
            cursor = await conn.execute("UPDATE accounts SET lifecycle_mode = ? WHERE id = ?", (mode, account_id))
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"account id={account_id} not found")

    async def set_account_auto_warmup(self, account_id: int, enabled: bool) -> None:
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "UPDATE accounts SET auto_warmup = ? WHERE id = ?", (1 if enabled else 0, account_id)
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"account id={account_id} not found")

    async def set_account_active(self, account_id: int, active: bool) -> None:
        """«В работе»: an inactive account is neither picked for a run nor warmed up automatically."""
        async with self._transaction() as conn:
            cursor = await conn.execute("UPDATE accounts SET is_active = ? WHERE id = ?", (1 if active else 0, account_id))
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"account id={account_id} not found")

    async def reset_account_restriction(self, account_id: int, stage: str, daily_limit: int) -> None:
        """Lift the circuit breaker: the error streak restarts and the stored stage/limit are replaced."""
        async with self._transaction() as conn:
            cursor = await conn.execute(
                "UPDATE accounts SET consecutive_errors = 0, lifecycle_stage = ?, daily_comments_limit = ? WHERE id = ?",
                (stage, daily_limit, account_id),
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

    # ---------------------------------------------------------------- projects
    #
    # A project = its members (`project_accounts`), its target and its replies. Names are unique without regard to
    # case (checked here with casefold: SQLite's NOCASE folds only ASCII, «Крипта» and «крипта» must clash too).

    async def list_projects(self) -> list[ProjectRecord]:
        """All projects in the order of the list and of the launch queue."""
        rows = await self._fetch_all(_PROJECT_SELECT + _PROJECT_ORDER)
        return [ProjectRecord.model_validate(dict(row)) for row in rows]

    async def get_project(self, project_id: int) -> ProjectRecord | None:
        row = await self._fetch_one(_PROJECT_SELECT + " WHERE projects.id = ?", (project_id,))
        return ProjectRecord.model_validate(dict(row)) if row else None

    async def get_project_by_name(self, name: str) -> ProjectRecord | None:
        """The project whose name matches without regard to case or surrounding spaces."""
        wanted = " ".join(str(name or "").split()).casefold()
        return next((p for p in await self.list_projects() if p.name.casefold() == wanted), None)

    async def default_project_id(self) -> int:
        """The project that takes links without an explicit one: «Основной» if it exists, else the first in order."""
        async with self._lock:
            return await self._fallback_project_id(self.connection)

    @staticmethod
    async def _check_name_free(conn: aiosqlite.Connection, name: str, own_id: int | None = None) -> None:
        cursor = await conn.execute("SELECT id, name FROM projects")
        rows = list(await cursor.fetchall())
        await cursor.close()
        if any(row["name"].casefold() == name.casefold() and row["id"] != own_id for row in rows):
            raise ValueError(f"Проект «{name}» уже есть")

    async def create_project(self, name: str, **fields: Any) -> ProjectRecord:
        """A new project at the end of the queue: the defaults of ProjectRecord (AI replies, no topics) plus `fields`
        (PROJECT_FIELDS only). ValueError for a bad / taken name or a bad field."""
        name = clean_project_name(name)
        cleaned = clean_project_fields(fields)
        async with self._transaction() as conn:
            await self._check_name_free(conn, name)
            project_id = await self._insert_project(conn, name, cleaned)
        record = await self.get_project(project_id)
        assert record is not None
        return record

    async def copy_project(self, project_id: int, name: str) -> ProjectRecord:
        """A new project with the settings of `project_id` (target and replies), without its accounts and links."""
        source = await self.get_project(project_id)
        if source is None:
            raise RecordNotFoundError(f"project id={project_id} not found")
        return await self.create_project(name, **{field: getattr(source, field) for field in PROJECT_FIELDS})

    async def update_project(self, project_id: int, **fields: Any) -> ProjectRecord:
        """Change the given fields (PROJECT_FIELDS only, validated). RecordNotFoundError / ValueError."""
        cleaned = clean_project_fields(fields)
        async with self._transaction() as conn:
            row = await (await conn.execute("SELECT min_views, max_views FROM projects WHERE id = ?", (project_id,))).fetchone()
            if row is None:
                raise RecordNotFoundError(f"project id={project_id} not found")
            low = cleaned.get("min_views", row["min_views"])
            high = cleaned["max_views"] if "max_views" in cleaned else row["max_views"]
            if high is not None and high < low:
                raise ValueError("Верхний предел просмотров меньше нижнего")
            if cleaned:
                assignments = ", ".join(f"{column} = ?" for column in cleaned)
                await conn.execute(
                    f"UPDATE projects SET {assignments}, updated_at = ? WHERE id = ?",
                    (*cleaned.values(), datetime.now(timezone.utc).isoformat(), project_id),
                )
        record = await self.get_project(project_id)
        assert record is not None
        return record

    async def rename_project(self, project_id: int, new_name: str) -> ProjectRecord:
        name = clean_project_name(new_name)
        async with self._transaction() as conn:
            await self._check_name_free(conn, name, project_id)
            cursor = await conn.execute(
                "UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
                (name, datetime.now(timezone.utc).isoformat(), project_id),
            )
            if cursor.rowcount == 0:
                raise RecordNotFoundError(f"project id={project_id} not found")
        record = await self.get_project(project_id)
        assert record is not None
        return record

    async def delete_project(self, project_id: int) -> ProjectDeleteResult:
        """Delete a project: its members leave it and its videos go; the journal keeps its records. The last project
        cannot be deleted (LastProjectError). The links are removed explicitly as well as by the foreign keys."""
        async with self._transaction() as conn:
            if await self._scalar(conn, "SELECT 1 FROM projects WHERE id = ?", (project_id,)) is None:
                raise RecordNotFoundError(f"project id={project_id} not found")
            if int(await self._scalar(conn, "SELECT COUNT(*) FROM projects")) <= 1:
                raise LastProjectError("Последний проект удалить нельзя")
            members = await conn.execute("DELETE FROM project_accounts WHERE project_id = ?", (project_id,))
            links = await conn.execute(
                "SELECT COUNT(*) FROM video_pool WHERE project_id = ? AND source = 'links'", (project_id,)
            )
            links_count = int((await links.fetchone())[0])
            await links.close()
            await conn.execute("DELETE FROM video_pool WHERE project_id = ?", (project_id,))
            await conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            await conn.execute("DELETE FROM app_settings WHERE key = ? AND value = ?", (UI_PROJECT_KEY, str(project_id)))
        return ProjectDeleteResult(members.rowcount, links_count)

    async def set_project_order(self, project_ids: Iterable[int]) -> None:
        """The queue order: the given ids first in that order, the rest keep their relative order after them."""
        wanted = list(dict.fromkeys(int(pid) for pid in project_ids))
        async with self._transaction() as conn:
            cursor = await conn.execute("SELECT id FROM projects ORDER BY sort ASC, id ASC")
            current = [int(row[0]) for row in await cursor.fetchall()]
            await cursor.close()
            order = [pid for pid in wanted if pid in current] + [pid for pid in current if pid not in wanted]
            for index, pid in enumerate(order):
                await conn.execute("UPDATE projects SET sort = ? WHERE id = ?", (index, pid))

    async def move_project(self, project_id: int, step: int) -> bool:
        """Move a project up (-1) or down (+1) in the queue; False when it is already at that end."""
        order = [project.id for project in await self.list_projects()]
        if project_id not in order:
            raise RecordNotFoundError(f"project id={project_id} not found")
        index = order.index(project_id)
        target = index + step
        if not 0 <= target < len(order):
            return False
        order[index], order[target] = order[target], order[index]
        await self.set_project_order(order)
        return True

    async def add_project_accounts(self, project_id: int, account_ids: Iterable[int]) -> int:
        """Make the accounts members of the project; returns how many joined (members already in it are kept)."""
        now = datetime.now(timezone.utc).isoformat()
        added = 0
        async with self._transaction() as conn:
            if await self._scalar(conn, "SELECT 1 FROM projects WHERE id = ?", (project_id,)) is None:
                raise RecordNotFoundError(f"project id={project_id} not found")
            for account_id in dict.fromkeys(int(a) for a in account_ids):
                if await self._scalar(conn, "SELECT 1 FROM accounts WHERE id = ?", (account_id,)) is None:
                    raise RecordNotFoundError(f"account id={account_id} not found")
                cursor = await conn.execute(
                    "INSERT OR IGNORE INTO project_accounts (project_id, account_id, added_at) VALUES (?, ?, ?)",
                    (project_id, account_id, now),
                )
                added += cursor.rowcount
        return added

    async def remove_project_accounts(self, project_id: int, account_ids: Iterable[int]) -> int:
        """Take the accounts out of the project; returns how many left it."""
        removed = 0
        async with self._transaction() as conn:
            for account_id in dict.fromkeys(int(a) for a in account_ids):
                cursor = await conn.execute(
                    "DELETE FROM project_accounts WHERE project_id = ? AND account_id = ?", (project_id, account_id)
                )
                removed += cursor.rowcount
        return removed

    async def list_project_accounts(self, project_id: int, active_only: bool = False) -> list[AccountRecord]:
        """Members of the project (only those «в работе» with `active_only`), oldest account first."""
        query = (
            _ACCOUNT_SELECT
            + " JOIN project_accounts pa ON pa.account_id = accounts.id WHERE pa.project_id = ?"
            + (" AND accounts.is_active = 1" if active_only else "")
            + " ORDER BY accounts.id ASC"
        )
        return [self._account_from_row(row) for row in await self._fetch_all(query, (project_id,))]

    async def projects_of_account(self, account_id: int) -> list[ProjectRecord]:
        rows = await self._fetch_all(
            _PROJECT_SELECT
            + " WHERE projects.id IN (SELECT project_id FROM project_accounts WHERE account_id = ?)"
            + _PROJECT_ORDER,
            (account_id,),
        )
        return [ProjectRecord.model_validate(dict(row)) for row in rows]

    @staticmethod
    async def _member_quota(
        conn: aiosqlite.Connection, project_id: int, account_id: int, now: datetime
    ) -> tuple[ProjectQuota, bool] | None:
        """(today's count of a member, a new day began) — read only, a passed day counts as 0; None: not a member."""
        cursor = await conn.execute(
            """
            SELECT pa.daily_done, pa.daily_reset_at, p.daily_per_account
            FROM project_accounts pa JOIN projects p ON p.id = pa.project_id
            WHERE pa.project_id = ? AND pa.account_id = ?
            """,
            (project_id, account_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        reset = _parse_utc_text(row["daily_reset_at"])
        new_day = reset is None or now >= reset
        done = 0 if new_day else int(row["daily_done"])
        limit = row["daily_per_account"]
        return ProjectQuota(done, limit, limit is None or done < int(limit)), new_day

    async def project_daily_gate(self, project_id: int, account_id: int, now: datetime | None = None) -> ProjectQuota:
        """May the account answer once more in this project today (the project's own ceiling only; the account's
        lifecycle limits are checked separately)? An account that is not a member may not. Reads only: a passed day
        counts as 0 here and is written by the next `bump_project_daily`."""
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        async with self._lock:
            found = await self._member_quota(self.connection, project_id, account_id, moment)
        return found[0] if found is not None else ProjectQuota(0, None, False)

    async def bump_project_daily(self, project_id: int, account_id: int, now: datetime | None = None) -> None:
        """One more reply of the account in the project today; a new day restarts the counter in the same write
        (nothing for an account that is not a member)."""
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        async with self._transaction() as conn:
            found = await self._member_quota(conn, project_id, account_id, moment)
            if found is None:
                return
            quota, new_day = found
            await conn.execute(
                "UPDATE project_accounts SET daily_done = ?, daily_reset_at = COALESCE(?, daily_reset_at)"
                " WHERE project_id = ? AND account_id = ?",
                (quota.done + 1, self.next_daily_reset(moment).isoformat() if new_day else None, project_id, account_id),
            )

    async def adopt_config_accounts(self, account_ids: Iterable[int]) -> int:
        """Accounts imported from config.yaml in the run that created «Основной» join it while it is the only project
        (a fresh install keeps working); otherwise a new account stays without a project. Returns how many joined."""
        ids = list(account_ids)
        if not self.seeded_now or not ids:
            return 0
        projects = await self.list_projects()
        if len(projects) != 1:
            return 0
        return await self.add_project_accounts(projects[0].id, ids)

    # ------------------------------------------------------------- video pool

    async def _pool_project(self, conn: aiosqlite.Connection, project_id: int | None) -> int:
        """The project a pool write goes to: the given one (it must exist), else the fallback project."""
        if project_id is None:
            return await self._fallback_project_id(conn)
        if await self._scalar(conn, "SELECT 1 FROM projects WHERE id = ?", (project_id,)) is None:
            raise RecordNotFoundError(f"project id={project_id} not found")
        return int(project_id)

    @staticmethod
    def _pool_filter(
        status: str | None = "all", source: str | None = None, project_id: int | None = None
    ) -> tuple[str, tuple[Any, ...]]:
        """WHERE clause of a pool read: status "pending" (still needs replies) / "all" / exact; source; project
        (None = every project)."""
        where: list[str] = []
        params: list[Any] = []
        if status == "pending":
            where.append(_PENDING_POOL_SQL)
        elif status not in ("all", None):
            where.append("status = ?")
            params.append(status)
        if source is not None:
            where.append("source = ?")
            params.append(source)
        if project_id is not None:
            where.append("project_id = ?")
            params.append(project_id)
        return (f" WHERE {' AND '.join(where)}" if where else ""), tuple(params)

    async def add_video_to_pool(
        self, video_url: str, comments_target: int = 2, project_id: int | None = None
    ) -> VideoPoolItem:
        """Put an own link into a project's pool (None: the fallback project). The same link may sit in several
        projects; inside one project a video a topic search found before becomes the own link with a fresh start."""
        video_url = video_url.strip()
        if not video_url:
            raise ValueError("video_url must not be empty")
        if comments_target < 1:
            raise ValueError("comments_target must be positive")
        video_id = _extract_video_id(video_url)
        now = datetime.now(timezone.utc).isoformat()
        async with self._transaction() as conn:
            project = await self._pool_project(conn, project_id)
            await conn.execute(
                """
                INSERT INTO video_pool (video_url, video_id, status, comments_target, comments_done, created_at, project_id)
                VALUES (?, ?, 'pending', ?, 0, ?, ?)
                ON CONFLICT(project_id, video_url) DO UPDATE SET
                    comments_target = excluded.comments_target,
                    -- a video a topic search found before becomes the user's own link with a fresh start
                    comments_done = CASE WHEN source = 'search' THEN 0 ELSE comments_done END,
                    status = CASE WHEN source = 'search' THEN 'pending' ELSE status END,
                    source = 'links'
                """,
                (video_url, video_id, comments_target, now, project),
            )
            row = await (
                await conn.execute("SELECT * FROM video_pool WHERE project_id = ? AND video_url = ?", (project, video_url))
            ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"pool video {video_url} disappeared during creation")
        item = VideoPoolItem.model_validate(dict(row))
        logger.debug("Added/updated video pool item {} (target={})", item.video_id, item.comments_target)
        return item

    async def queue_search_video(
        self, video_url: str, comments_target: int = 2, project_id: int | None = None
    ) -> VideoPoolItem:
        """Put a video found by a topic search (or a reference link) into a project's pool for this run (source
        'search'; None: the fallback project).

        A video found again starts a new round (pending, 0 replies): new comments may have appeared, and the
        comments already answered are skipped by the payload check. A video that is in the project's own links
        keeps its record (source, progress); a finished one is handed back as pending for this run only.
        """
        video_url = video_url.strip()
        if not video_url:
            raise ValueError("video_url must not be empty")
        if comments_target < 1:
            raise ValueError("comments_target must be positive")
        video_id = _extract_video_id(video_url)
        now = datetime.now(timezone.utc).isoformat()
        async with self._transaction() as conn:
            project = await self._pool_project(conn, project_id)
            await conn.execute(
                """
                INSERT INTO video_pool (video_url, video_id, status, comments_target, comments_done, created_at, source,
                                        project_id)
                VALUES (?, ?, 'pending', ?, 0, ?, 'search', ?)
                ON CONFLICT(project_id, video_url) DO UPDATE SET
                    comments_target = CASE WHEN source = 'search' THEN excluded.comments_target ELSE comments_target END,
                    comments_done = CASE WHEN source = 'search' THEN 0 ELSE comments_done END,
                    status = CASE WHEN source = 'search' THEN 'pending' ELSE status END
                """,
                (video_url, video_id, comments_target, now, project),
            )
            row = await (
                await conn.execute("SELECT * FROM video_pool WHERE project_id = ? AND video_url = ?", (project, video_url))
            ).fetchone()
        if row is None:
            raise RecordNotFoundError(f"pool video {video_url} disappeared during creation")
        item = VideoPoolItem.model_validate(dict(row))
        if item.status == "completed" or item.comments_done >= item.comments_target:
            item = item.model_copy(update={"status": "pending", "comments_done": 0, "comments_target": comments_target})
        return item

    async def get_next_pool_video(self, project_id: int | None = None) -> VideoPoolItem | None:
        """The oldest own link that still needs replies (of one project, or of any with None)."""
        clause, params = self._pool_filter("pending", "links", project_id)
        row = await self._fetch_one(f"SELECT * FROM video_pool{clause} ORDER BY id ASC LIMIT 1", params)
        return VideoPoolItem.model_validate(dict(row)) if row else None

    async def list_pool_videos(
        self, status: str = "pending", source: str | None = None, project_id: int | None = None
    ) -> list[VideoPoolItem]:
        """Pool videos by status ("pending" = still needs replies, "all", or an exact status); source
        'links' / 'search' narrows to the user's own links or the topic search finds, None takes both;
        `project_id` narrows to one project (None: every project)."""
        clause, params = self._pool_filter(status, source, project_id)
        rows = await self._fetch_all(f"SELECT * FROM video_pool{clause} ORDER BY id ASC", params)
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

    async def count_pool_videos(
        self, status: str | None = "all", project_id: int | None = None, source: str | None = None
    ) -> int:
        """Count pool videos; status "pending" matches list_pool_videos (still needs comments)."""
        clause, params = self._pool_filter(status, source, project_id)
        return await self._count(f"SELECT COUNT(*) FROM video_pool{clause}", params)

    async def set_pool_comments_target(self, comments_target: int, project_id: int | None = None) -> int:
        """Set the reply target of unfinished pool videos (pending / in_progress) of one project (None: all);
        returns how many changed. Videos that already have that many replies become 'completed'.
        """
        if comments_target < 1:
            raise ValueError("comments_target must be positive")
        scope, params = ("", ()) if project_id is None else (" AND project_id = ?", (project_id,))
        async with self._transaction() as conn:
            cursor = await conn.execute(
                f"UPDATE video_pool SET comments_target = ? WHERE status IN ('pending', 'in_progress'){scope}",
                (comments_target, *params),
            )
            changed = cursor.rowcount
            await conn.execute(
                "UPDATE video_pool SET status = 'completed'"
                f" WHERE status IN ('pending', 'in_progress') AND comments_done >= comments_target{scope}",
                params,
            )
        return changed

    async def clear_video_pool(self, project_id: int | None = None) -> int:
        """Delete the pool of one project (None: every pool record); returns how many were removed."""
        scope, params = ("", ()) if project_id is None else (" WHERE project_id = ?", (project_id,))
        async with self._transaction() as conn:
            cursor = await conn.execute(f"DELETE FROM video_pool{scope}", params)
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
    "DEFAULT_PROJECT_NAME",
    "Database",
    "LastProjectError",
    "MigrationError",
    "Payload",
    "PayloadStatus",
    "PROJECT_FIELDS",
    "PROJECT_ORDER",
    "PROJECT_SELECT",
    "PROJECTS_SEEDED_KEY",
    "PROXY_SCHEMES",
    "ProjectDeleteResult",
    "ProjectQuota",
    "ProjectRecord",
    "ProxyDeleteResult",
    "ProxyRecord",
    "QueueItem",
    "QueueKind",
    "QueueStatus",
    "RecordNotFoundError",
    "SEARCH_PROXY_KEY",
    "TARGET_MODES",
    "TestNode",
    "UI_PROJECT_KEY",
    "VideoPoolItem",
    "clean_project_fields",
    "clean_project_name",
    "clean_templates",
    "clean_topics",
    "init_db",
    "project_quota_today",
]
