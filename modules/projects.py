"""Projects: what one project's run uses (its config) and does (its plan), and why it cannot run.

A project keeps its own target (topics, own links, reference links, filters) and replies (AI or templates) in the
database; everything else (DeepSeek key and prompts, languages, browser, pauses, thread limits) stays global in
config.yaml. `project_config` lays a project over the global config for one run; nothing reads config.yaml again
behind it.
"""
from __future__ import annotations

from typing import Any, NamedTuple

from config import AppConfig
from database import Database, ProjectRecord

DEEPSEEK_PLACEHOLDER = "sk-your-deepseek-key"  # the key of config.example.yaml: the config needs one, it is not real
NO_VIDEOS = "нет видео"
REFERENCE_LINKS = 5  # own links a run takes as references (the newest ones), to keep the requests few


def project_seed(cfg: AppConfig) -> dict[str, Any]:
    """The first project «Основной» from the current config.yaml: topic, filters and replies of the old global
    target. «Ответов на видео» is `limits.max_iterations_per_session` (what the interface showed as that)."""
    col, proc = cfg.collector, cfg.processor
    query = (col.search_query or "").strip()
    return {
        "search_topics": [query] if query else [],
        "min_views": col.min_views or 0,
        "max_views": col.max_views or None,
        "max_age_hours": col.max_age_hours,
        "max_videos": col.max_videos,
        "comments_per_video": cfg.limits.max_iterations_per_session,
        "target_mode": col.target_mode,
        "reply_mode": proc.reply_mode,
        "custom_replies": list(proc.custom_replies or []),
        "paraphrase_custom": bool(proc.paraphrase_custom),
    }


def open_database(cfg: AppConfig) -> Database:
    """The configured database; its first start with projects seeds «Основной» from this config."""
    return Database(cfg.app.db_path, project_seed=project_seed(cfg))


def project_config(cfg: AppConfig, project: ProjectRecord, topic: str | None = None) -> AppConfig:
    """A deep copy of the global config with the project's target and replies laid over it.

    `topic` becomes `collector.search_query` (None: no search). Both copies of the reply settings (the top-level
    `reply_mode` / `custom_replies` and `processor.*`) are filled, so nothing falls back to the global ones.
    """
    run = cfg.model_copy(deep=True)
    col = run.collector
    col.search_query = topic
    col.min_views = project.min_views
    col.max_views = project.max_views
    col.max_age_hours = project.max_age_hours
    col.max_videos = project.max_videos
    col.comments_per_video = project.comments_per_video
    col.target_mode = project.target_mode
    templates = list(project.custom_replies)
    run.processor.reply_mode = project.reply_mode  # type: ignore[assignment]
    run.processor.custom_replies = templates
    run.processor.paraphrase_custom = project.paraphrase_custom
    run.reply_mode = project.reply_mode  # type: ignore[assignment]
    run.custom_replies = list(templates)
    run.limits.max_iterations_per_session = project.comments_per_video
    return run


class PlanStep(NamedTuple):
    """One part of a project's run: a topic search, a kind of reference links, or the own links waiting."""

    kind: str  # "topic" / "similar" / "title_words" / "links"
    value: str = ""  # the topic
    count: int = 0  # own links waiting ("links")


class ProjectPlan(NamedTuple):
    steps: list[PlanStep]
    reason: str  # why the plan is empty ("" when there is something to run)


def project_plan(project: ProjectRecord, links_waiting: int | None = None, links_total: int | None = None) -> ProjectPlan:
    """What a run does for the project, in order: every topic, the reference links (similar videos, title words —
    only while the project has own links), then the own links still waiting. The counts default to the record's."""
    waiting = project.links_waiting if links_waiting is None else links_waiting
    total = project.links_total if links_total is None else links_total
    steps = [PlanStep("topic", topic) for topic in project.search_topics]
    if total:
        if project.use_similar:
            steps.append(PlanStep("similar"))
        if project.use_title_words:
            steps.append(PlanStep("title_words"))
    if waiting:
        steps.append(PlanStep("links", count=waiting))
    if steps:
        return ProjectPlan(steps, "")
    if total:
        return ProjectPlan([], "свои ссылки пройдены, тем нет")
    return ProjectPlan([], "нет тем и ссылок")


def has_deepseek_key(cfg: AppConfig) -> bool:
    key = (cfg.deepseek.api_key or "").strip()
    return bool(key) and key != DEEPSEEK_PLACEHOLDER


def project_problems(project: ProjectRecord, active_members: int, has_key: bool) -> list[str]:
    """Why the project cannot run as it is (short Russian phrases for the status and «Чтобы начать»); [] = ready."""
    problems: list[str] = []
    if not project.accounts_count:
        problems.append("нет аккаунтов")
    elif not active_members:
        problems.append("нет аккаунтов в работе")
    if not project_plan(project).steps:
        problems.append(NO_VIDEOS)  # the plan's reason says why (Space on the project)
    if project.reply_mode == "custom" and not project.custom_replies:
        problems.append("нет шаблонов")
    if project.reply_mode == "ai" and not has_key:
        problems.append("нет ключа DeepSeek")
    return problems


__all__ = [
    "DEEPSEEK_PLACEHOLDER",
    "NO_VIDEOS",
    "PlanStep",
    "ProjectPlan",
    "REFERENCE_LINKS",
    "has_deepseek_key",
    "open_database",
    "project_config",
    "project_plan",
    "project_problems",
    "project_seed",
]
