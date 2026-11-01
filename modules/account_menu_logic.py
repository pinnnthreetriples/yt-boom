"""Pure helpers of the wizard «Аккаунты» screen: warm-up pick, import and warm-up summaries, lifecycle table rows.

No input/output happens here, so everything is testable without a terminal. Passwords and TOTP secrets never
reach these functions' results: an import summary holds only line numbers and the parser's own messages.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from modules.lifecycle_engine import AccountLifecycleEngine

_PICK_ALL = {"a", "all", "все", "всех", "в"}
_PICK_NONE = {"", "n", "no", "none", "нет", "н", "0"}
MAX_REPORTED_LINES = 8  # per kind (skipped / errors) in an import summary

STAGE_LABELS = {
    "quarantine": "карантин",
    "aging": "aging",
    "probation": "probation",
    "mature": "mature",
    "restricted": "restricted",
    "-": "—",
}
_STAGE_STYLES = {
    "quarantine": "warn", "aging": "warn", "probation": "accent", "mature": "ok", "restricted": "err", "-": "dim",
}


def parse_warm_selection(text: str, count: int) -> list[int]:
    """Who goes to warm-up: «a»/«все» → every index, «»/«n» → nobody, «1 3 5» (or «1,3») → those numbers.

    Returns 0-based indexes without repeats in the order typed; ValueError (with a user-facing message) on a
    non-number or a number outside 1..count.
    """
    answer = text.strip().lower()
    if answer in _PICK_ALL:
        return list(range(count))
    if answer in _PICK_NONE:
        return []
    picked: list[int] = []
    for token in answer.replace(",", " ").split():
        if not token.isdigit():
            raise ValueError("Не распознано. Введите a, n или номера через пробел")  # the token is never echoed
        number = int(token)
        if not 1 <= number <= count:
            raise ValueError(f"Номер {number} вне списка (1–{count})")
        if number - 1 not in picked:
            picked.append(number - 1)
    return picked


def clean_pasted_lines(lines: Iterable[str]) -> list[str]:
    """Source lines with the BOM and line ends removed; blank lines and comments stay, so line numbers match the source."""
    return [line.lstrip("\ufeff").rstrip("\r\n") for line in lines]


def count_account_lines(lines: Iterable[str]) -> int:
    """Lines that carry an account (not blank, not a comment)."""
    return sum(1 for line in lines if line.strip() and not line.strip().startswith("#"))


def format_import_summary(
    added: int, skipped: Sequence[tuple[int, str]], errors: Sequence[tuple[int, str]], account_lines: int
) -> list[tuple[str, str]]:
    """Status lines (kind, text) for the end of an import: totals first, then the first lines that were not taken."""
    if account_lines == 0:
        return [("warn", "В тексте нет строк с аккаунтами (пустые строки и # пропущены)")]
    kind = "err" if errors and not added else "ok" if added else "warn"
    out = [(kind, f"Добавлено: {added} · пропущено: {len(skipped)} · ошибок: {len(errors)}")]
    for label, items, style in (("пропущена", skipped, "info"), ("ошибка", errors, "warn")):
        for number, reason in items[:MAX_REPORTED_LINES]:
            out.append((style, f"строка {number}: {label} — {reason}"))
        if len(items) > MAX_REPORTED_LINES:
            out.append((style, f"… ещё {len(items) - MAX_REPORTED_LINES}"))
    return out


def format_warm_summary(results: Sequence[Any]) -> tuple[str, str]:
    """(kind, text) for a finished warm-up round: «Прогрев: 2 из 3 успешно · не удалось: name». Results are WarmUpResult."""
    if not results:
        return "warn", "Прогрев: некого прогревать"
    failed = [r.account_name for r in results if not r.success]
    text = f"Прогрев: {len(results) - len(failed)} из {len(results)} успешно"
    if failed:
        text += " · не удалось: " + ", ".join(failed)
    return ("warn" if failed else "ok"), text


def format_ago(value: str | None, now: datetime | None = None) -> str:
    """«5м назад» / «3ч назад» / «2д назад» for an ISO timestamp; «—» when it is missing or unreadable."""
    then = AccountLifecycleEngine.parse_utc(value)
    if then is None:
        return "—"
    seconds = max(0.0, ((now or datetime.now(timezone.utc)) - then).total_seconds())
    if seconds < 3600:
        return f"{int(seconds // 60)}м назад"
    if seconds < 48 * 3600:
        return f"{int(seconds // 3600)}ч назад"
    return f"{int(seconds // 86400)}д назад"


def format_duration(seconds: float) -> str:
    """«2ч 10м» / «45м» / «<1м»."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "<1м"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}ч {minutes}м" if hours else f"{minutes}м"


@dataclass(frozen=True)
class LifecycleRow:
    """One line of the lifecycle table; `*_style` are tui theme names (ok / warn / err / accent / dim)."""

    name: str
    stage: str
    stage_style: str
    quota: str
    heartbeat: str
    heartbeat_style: str
    pause: str
    pause_style: str


def _pause_text(account: Any, stage: str, now: datetime) -> tuple[str, str]:
    """What keeps the account from commenting right now: breaker, an interval cooldown, the day's limit, ageing."""
    if stage == "restricted":
        return f"breaker ×{account.consecutive_errors}", "err"
    if stage in ("quarantine", "aging"):
        return "ещё рано", "dim"
    view = AccountLifecycleEngine.summarize(account, now)  # same order as evaluate_eligibility: limit, then cooldown
    if view.quota_limit and (view.quota_done or 0) >= view.quota_limit:
        return "лимит дня", "warn"
    last = AccountLifecycleEngine.parse_utc(account.last_comment_at)
    cooldown = AccountLifecycleEngine.cooldown_sec(stage, account.last_comment_at)
    if last is not None and cooldown:
        left = cooldown - (now - last).total_seconds()
        if left > 0:
            return f"пауза {format_duration(left)}", "warn"
    return "—", "dim"


def lifecycle_rows(
    accounts: Iterable[Any],
    session_alive: Callable[[Any], bool],
    now: datetime | None = None,
) -> list[LifecycleRow]:
    """Table rows for `accounts`: stage, today's quota, heartbeat (session alive + last warm-up) and pause/breaker.

    `session_alive(account)` is the only outside look (the profile's cookies), so tests pass a stub.
    """
    now = now or datetime.now(timezone.utc)
    rows: list[LifecycleRow] = []
    for acc in accounts:
        view = AccountLifecycleEngine.summarize(acc, now)
        alive = session_alive(acc)
        last = format_ago(acc.last_warmup_at, now)
        if view.age_hours is None:  # not signed in yet: no age, no quota
            quota, pause, pause_style = "—", "нужен вход", "warn"
        elif not acc.is_active:  # switched off: the orchestrator never picks it
            quota, pause, pause_style = f"{view.quota_done}/{view.quota_limit}", "отключён", "dim"
        else:
            quota = f"{view.quota_done}/{view.quota_limit}"
            pause, pause_style = _pause_text(acc, view.stage, now)
        rows.append(LifecycleRow(
            name=acc.name,
            stage=STAGE_LABELS.get(view.stage, view.stage),
            stage_style=_STAGE_STYLES.get(view.stage, ""),
            quota=quota,
            heartbeat=f"{'ok' if alive else 'нет'} · {last}",
            heartbeat_style="ok" if alive else "warn",
            pause=pause,
            pause_style=pause_style,
        ))
    return rows


__all__ = [
    "LifecycleRow",
    "clean_pasted_lines",
    "count_account_lines",
    "format_ago",
    "format_duration",
    "format_import_summary",
    "format_warm_summary",
    "lifecycle_rows",
    "parse_warm_selection",
]
