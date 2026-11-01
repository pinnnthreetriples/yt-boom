from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from loguru import logger

from database import AccountRecord, Database

LifecycleStageName = Literal["quarantine", "aging", "probation", "mature", "restricted"]
SuggestedAction = Literal["comment", "warm_up", "wait", "restricted"]

CIRCUIT_BREAKER_ERRORS = 3
_HOUR = 3600.0


@dataclass(frozen=True)
class StageRule:
    start_hour: float
    end_hour: float  # inf: the stage never ends
    daily_limit: int
    interval_min_sec: float  # inf: no comment is allowed
    interval_max_sec: float


@dataclass(frozen=True)
class EligibilityResult:
    can_comment: bool
    stage: LifecycleStageName
    suggested_action: SuggestedAction
    reason: str = ""
    daily_limit: int = 0
    daily_comments_done: int = 0
    retry_after_sec: float | None = None


@dataclass(frozen=True)
class LifecycleView:
    """What `--lifecycle` and the wizard show for one account; the numbers are None until it has signed in."""

    stage: str  # a LifecycleStageName, or "-" for an account that is not authenticated
    age_hours: float | None = None
    quarantine_left: float | None = None
    quota_done: int | None = None
    quota_limit: int | None = None


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class AccountLifecycleEngine:
    """Four-day ageing of an account: no comments for 48 h, one a day on the third day, five a day after that."""

    STAGE_RULES: dict[str, StageRule] = {
        "quarantine": StageRule(0, 24, 0, float("inf"), float("inf")),
        "aging": StageRule(24, 48, 0, float("inf"), float("inf")),
        "probation": StageRule(48, 72, 1, 3 * _HOUR, 3 * _HOUR),
        "mature": StageRule(72, float("inf"), 5, 2700.0, 5400.0),
    }

    parse_utc = staticmethod(_parse_utc)

    @staticmethod
    def _now(now: datetime | None) -> datetime:
        return now.astimezone(timezone.utc) if now is not None else datetime.now(timezone.utc)

    @staticmethod
    def authenticated_at(account: AccountRecord) -> datetime | None:
        """First sign-in of the account; an authenticated account from before this field is aged from its creation."""
        if account.authenticated_at:
            # a damaged stamp means "unknown": the account starts ageing now instead of looking mature
            return _parse_utc(account.authenticated_at) or datetime.now(timezone.utc)
        return _parse_utc(account.created_at)

    @classmethod
    def age_hours(cls, account: AccountRecord, now: datetime | None = None) -> float:
        started = cls.authenticated_at(account)
        if started is None:
            return 0.0
        return max(0.0, (cls._now(now) - started).total_seconds() / _HOUR)

    @classmethod
    def calculate_stage(
        cls, account: AccountRecord, now: datetime | None = None
    ) -> tuple[LifecycleStageName, int]:
        """(stage, daily comment limit) by the hours since the account signed in."""
        age = cls.age_hours(account, now)
        for name, rule in cls.STAGE_RULES.items():
            if rule.start_hour <= age < rule.end_hour:
                return name, rule.daily_limit  # type: ignore[return-value]
        return "mature", cls.STAGE_RULES["mature"].daily_limit

    @classmethod
    def quarantine_hours_left(cls, account: AccountRecord, now: datetime | None = None) -> float:
        return max(0.0, cls.STAGE_RULES["quarantine"].end_hour - cls.age_hours(account, now))

    @classmethod
    def quota_today(cls, account: AccountRecord, now: datetime | None = None) -> int:
        """Comments counted for today: the stored counter restarts once the daily reset moment has passed."""
        reset_at = _parse_utc(account.daily_limit_reset_at)
        return 0 if reset_at is None or cls._now(now) >= reset_at else account.daily_comments_done

    @classmethod
    def summarize(cls, account: AccountRecord, now: datetime | None = None) -> LifecycleView:
        """Stage, age, quarantine hours left and today's quota of `account` (circuit breaker shows as "restricted")."""
        if account.auth_status != "authenticated":
            return LifecycleView("-")
        now = cls._now(now)
        stage, limit = cls.calculate_stage(account, now)
        if account.consecutive_errors >= CIRCUIT_BREAKER_ERRORS:
            stage = "restricted"
        return LifecycleView(
            stage,
            cls.age_hours(account, now),
            cls.quarantine_hours_left(account, now),
            cls.quota_today(account, now),
            limit,
        )

    @classmethod
    def cooldown_sec(cls, stage: str, last_comment_at: str | None) -> float | None:
        """Pause required after the comment posted at `last_comment_at`; None: this stage has no comments.

        Random inside the stage's range, but seeded by the comment's timestamp: the same comment always gets the
        same pause, so the answer does not change between two checks while the next one still differs.
        """
        rule = cls.STAGE_RULES.get(stage)
        if rule is None or rule.interval_min_sec == float("inf"):
            return None
        return random.Random(last_comment_at).uniform(rule.interval_min_sec, rule.interval_max_sec)

    @classmethod
    async def evaluate_eligibility(
        cls,
        account: AccountRecord,
        db: Database | None = None,
        now: datetime | None = None,
    ) -> EligibilityResult:
        """May this account comment right now? With `db` the derived stage, limit and a restarted day are saved."""
        now = cls._now(now)
        stage, limit = cls.calculate_stage(account, now)
        done = account.daily_comments_done
        updates: dict[str, object] = {}
        if account.lifecycle_stage != stage or account.daily_comments_limit != limit:
            updates.update(lifecycle_stage=stage, daily_comments_limit=limit)

        if account.auth_status != "authenticated":
            return EligibilityResult(False, stage, "wait", "Account is not authenticated", limit, done)

        if account.consecutive_errors >= CIRCUIT_BREAKER_ERRORS:
            if db is not None and account.lifecycle_stage != "restricted":
                await db.update_account_lifecycle(account.id, lifecycle_stage="restricted")
            return EligibilityResult(
                False,
                "restricted",
                "restricted",
                f"Circuit breaker: {account.consecutive_errors} consecutive errors",
                limit,
                done,
            )

        reset_at = _parse_utc(account.daily_limit_reset_at)
        if reset_at is None or now >= reset_at:
            done = 0
            next_reset = Database.next_daily_reset(now)
            updates.update(daily_comments_done=0, daily_limit_reset_at=next_reset.isoformat())

        if db is not None and updates:
            await db.update_account_lifecycle(account.id, **updates)  # type: ignore[arg-type]

        if stage in ("quarantine", "aging"):
            return EligibilityResult(
                False, stage, "warm_up", f"Stage {stage}: commenting is forbidden, warm-up only", limit, done
            )

        if done >= limit:
            return EligibilityResult(False, stage, "wait", "Daily limit reached", limit, done)

        last = _parse_utc(account.last_comment_at)
        if last is not None:
            interval = cls.cooldown_sec(stage, account.last_comment_at) or 0.0
            elapsed = (now - last).total_seconds()
            if elapsed < interval:
                return EligibilityResult(
                    False, stage, "wait", "Interval cooldown active", limit, done, retry_after_sec=interval - elapsed
                )

        logger.debug("Lifecycle: account {!r} may comment (stage {}, {}/{} today)", account.name, stage, done, limit)
        return EligibilityResult(True, stage, "comment", "", limit, done)


__all__ = [
    "AccountLifecycleEngine",
    "CIRCUIT_BREAKER_ERRORS",
    "EligibilityResult",
    "LifecycleStageName",
    "LifecycleView",
    "StageRule",
]
