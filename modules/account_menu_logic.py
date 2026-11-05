"""Pure helpers of the wizard «Аккаунты» screen: warm-up pick, import and warm-up summaries, lifecycle table rows.

No input/output happens here, so everything is testable without a terminal. Passwords and TOTP secrets never
reach these functions' results: an import summary holds only line numbers and the parser's own messages.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

from database import NAME_CHANGE_WINDOW_DAYS, NAME_CHANGES_PER_WINDOW, recent_name_changes
from modules.lifecycle_engine import MODE_LABELS, MODE_MATURE, MODE_UNLIMITED, AccountLifecycleEngine

_PICK_ALL = {"a", "all", "все", "всех", "в"}
_PICK_NONE = {"", "n", "no", "none", "нет", "н", "0"}
MAX_REPORTED_LINES = 8  # per kind (skipped / errors) in an import summary

STAGE_LABELS = {
    "quarantine": "карантин",
    "aging": "aging",
    "probation": "probation",
    "mature": "mature",
    "restricted": "restricted",
    "unlimited": "∞",
    "-": "—",
}
_STAGE_STYLES = {
    "quarantine": "warn", "aging": "warn", "probation": "accent", "mature": "ok", "restricted": "err",
    "unlimited": "accent", "-": "dim",
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
    mode: str = MODE_LABELS["auto"]
    in_work: str = "да"
    in_work_style: str = ""
    auto_warmup: str = "вкл"
    auto_warmup_style: str = ""


def _stage_label(view: Any) -> str:
    """Stage column of the lifecycle table: «прогрет» when the mode (not the age) makes the account mature."""
    if view.stage == "mature" and view.mode == MODE_MATURE:
        return "прогрет"
    return STAGE_LABELS.get(view.stage, view.stage)


def _quota_text(view: Any) -> str:
    """«2/5»; «2/∞» for an account without a daily limit."""
    return f"{view.quota_done}/{'∞' if view.quota_limit is None else view.quota_limit}"


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
            quota, pause, pause_style = _quota_text(view), "отключён", "dim"
        else:
            quota = _quota_text(view)
            pause, pause_style = _pause_text(acc, view.stage, now)
        rows.append(LifecycleRow(
            name=acc.name,
            stage=_stage_label(view),
            stage_style=_STAGE_STYLES.get(view.stage, ""),
            quota=quota,
            heartbeat=f"{'ok' if alive else 'нет'} · {last}",
            heartbeat_style="ok" if alive else "warn",
            pause=pause,
            pause_style=pause_style,
            mode=MODE_LABELS[AccountLifecycleEngine.mode_of(acc)],
            in_work="да" if acc.is_active else "нет",
            in_work_style="" if acc.is_active else "warn",
            auto_warmup="вкл" if acc.auto_warmup else "выкл",
            auto_warmup_style="" if acc.auto_warmup else "dim",
        ))
    return rows


# ----------------------------------------------------------------------------- account modes: row text and card

_STAGE_TITLES = {
    "quarantine": "карантин (молодой)",
    "aging": "aging (молодой)",
    "probation": "испытательный",
    "mature": "взрослый",
    "restricted": "ограничен",
    "unlimited": "без лимитов",
}
_AGING_END_HOURS = 48.0  # the first hour at which a comment is allowed in "auto" mode


def account_status_label(account: Any, now: datetime | None = None) -> tuple[str, str]:
    """(text ≤ 15 chars, theme style) for the account row: stage / mode / switched off / restricted.

    Single-width glyphs only, so the row keeps the menu width in every console font.
    """
    now = now or datetime.now(timezone.utc)
    if not account.is_active:
        return "– не в работе", "dim"
    view = AccountLifecycleEngine.summarize(account, now)
    if view.stage == "-":  # no lifecycle clock yet (not signed in by the bot); a chosen mode is still worth showing
        mode = AccountLifecycleEngine.mode_of(account)
        if mode == MODE_UNLIMITED:
            return "∞ без лимитов", "accent"
        return ("✓ прогрет", "ok") if mode == MODE_MATURE else ("· без возраста", "dim")
    if view.stage == "restricted":
        return "✗ ограничен", "err"
    if view.stage == "unlimited":
        return "∞ без лимитов", "accent"
    quota = f"{view.quota_done}/{view.quota_limit}"
    if view.stage == "quarantine":
        return f"◐ карантин {view.quarantine_left:.0f}ч", "warn"
    if view.stage == "aging":
        return f"◐ aging {max(0.0, _AGING_END_HOURS - (view.age_hours or 0)):.0f}ч", "warn"
    if view.stage == "probation":
        return f"◑ новичок {quota}", "accent"
    word = "прогрет" if view.mode == MODE_MATURE else "взрослый"
    return f"✓ {word} {quota}", "ok"


def next_comment_text(account: Any, now: datetime | None = None) -> tuple[str, str]:
    """(when the next comment is possible, style): «сейчас» / «через 35м» / «после сброса лимита…» / why not."""
    now = now or datetime.now(timezone.utc)
    if not account.is_active:
        return "аккаунт не в работе", "dim"
    view = AccountLifecycleEngine.summarize(account, now)
    if view.stage == "-":
        return "нет отсчёта (вход не через бота)", "dim"
    if view.stage == "restricted":
        return "после «Снять ограничение»", "err"
    if view.stage == "unlimited":
        return "сразу, без пауз", "ok"
    if view.stage in ("quarantine", "aging"):
        left = _AGING_END_HOURS - (view.age_hours or 0)
        return f"через {format_duration(left * 3600)} (комментарии с 48 ч)", "warn"
    if view.quota_limit and (view.quota_done or 0) >= view.quota_limit:
        reset = AccountLifecycleEngine.parse_utc(account.daily_limit_reset_at)
        when = f" через {format_duration((reset - now).total_seconds())}" if reset and reset > now else ""
        return f"после сброса лимита{when}", "warn"
    last = AccountLifecycleEngine.parse_utc(account.last_comment_at)
    cooldown = AccountLifecycleEngine.cooldown_sec(view.stage, account.last_comment_at)
    if last is not None and cooldown:
        left_sec = cooldown - (now - last).total_seconds()
        if left_sec > 0:
            return f"через {format_duration(left_sec)}", "warn"
    return "сейчас", "ok"


def account_card_rows(account: Any, now: datetime | None = None) -> list[tuple[str, str, str]]:
    """Rows (label, value, style) of the card on top of an account's menu."""
    now = now or datetime.now(timezone.utc)
    view = AccountLifecycleEngine.summarize(account, now)
    mode = AccountLifecycleEngine.mode_of(account)
    if view.stage == "-":
        stage, age, limit = "нет отсчёта (вход не через бота)", "—", "—"
    else:
        if mode == MODE_MATURE and view.stage == "mature":
            stage = "прогрет (вручную)"
        else:
            stage = _STAGE_TITLES.get(view.stage, view.stage)
        age = f"{view.age_hours:.0f} ч с первого входа"
        if mode != "auto" and view.age_stage:
            age += f" · по часам: {_STAGE_TITLES.get(view.age_stage, view.age_stage)}"
        if view.quota_limit is None:
            limit = f"без лимита (сегодня {view.quota_done})"
        elif view.quota_limit == 0:
            limit = "комментарии запрещены"
        else:
            limit = f"{view.quota_done}/{view.quota_limit} сегодня"
    when, when_style = next_comment_text(account, now)
    warm = format_ago(account.last_warmup_at, now)
    if account.warmup_sessions_done:
        warm += f" · всего {account.warmup_sessions_done}"
    return [
        ("Стадия", stage, _STAGE_STYLES.get(view.stage, "")),
        ("Возраст", age, ""),
        ("Лимит сегодня", limit, ""),
        ("Следующий коммент", when, when_style),
        ("Последний прогрев", warm, ""),
        ("Режим", MODE_LABELS[mode], "accent" if mode != "auto" else ""),
        ("В работе", "да" if account.is_active else "нет", "" if account.is_active else "warn"),
        ("Автопрогрев", "вкл" if account.auto_warmup else "выкл", "" if account.auto_warmup else "dim"),
    ]


# ----------------------------------------------------------------------------- channel profile

NICKNAME_MAX = 50  # YouTube: channel name
DESCRIPTION_MAX = 1000  # YouTube: channel description
_PROFILE_FORBIDDEN = "<>"
PROFILE_FIELDS = ("name", "description", "avatar")
PROFILE_FIELD_LABELS = {"name": "Никнейм", "description": "Описание", "avatar": "Аватарка"}
_PROFILE_FIELD_WORDS = {"name": "никнейм", "description": "описание", "avatar": "аватарка"}
_DESCRIPTION_PREVIEW = 28


def validate_nickname(text: str) -> str:
    """The channel name to store: whitespace collapsed; ValueError (user-facing message) when YouTube would refuse it."""
    name = " ".join(text.split())
    if not name:
        raise ValueError("Никнейм не может быть пустым")
    if len(name) > NICKNAME_MAX:
        raise ValueError(f"Слишком длинный никнейм: {len(name)} из {NICKNAME_MAX} символов")
    if any(char in name for char in _PROFILE_FORBIDDEN):
        raise ValueError("В никнейме нельзя использовать < и >")
    return name


def validate_description(lines: Iterable[str]) -> str:
    """The channel description to store (lines joined, outer blanks removed); a lone «-» clears it (returns "")."""
    text = "\n".join(line.rstrip() for line in lines).strip()
    if text == "-":
        return ""
    if len(text) > DESCRIPTION_MAX:
        raise ValueError(f"Слишком длинное описание: {len(text)} из {DESCRIPTION_MAX} символов")
    if any(char in text for char in _PROFILE_FORBIDDEN):
        raise ValueError("В описании нельзя использовать < и >")
    return text


def clean_path_input(text: str) -> str:
    """A path pasted or dragged into the terminal: surrounding quotes and PowerShell's leading «& » removed."""
    value = text.strip()
    if value.startswith("& "):
        value = value[2:].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def applied_snapshot(account: Any) -> dict[str, str | None]:
    """What was last published to the channel (`name` / `description` / `avatar`); {} before the first apply."""
    try:
        data = json.loads(account.profile_applied) if account.profile_applied else {}
    except ValueError:
        return {}
    return {key: value for key, value in data.items() if key in PROFILE_FIELDS} if isinstance(data, dict) else {}


def _draft(account: Any, field: str) -> str | None:
    return getattr(account, f"profile_{field}", None)


def profile_pending_fields(account: Any) -> list[str]:
    """Fields whose draft differs from what is live on the channel (a field without a draft is left alone)."""
    applied = applied_snapshot(account)
    return [
        field
        for field in PROFILE_FIELDS
        if _draft(account, field) is not None and _draft(account, field) != applied.get(field)
    ]


def profile_status_label(account: Any) -> tuple[str, str]:
    """(text, style) for the account picker: nothing set / has unapplied edits / applied."""
    if profile_pending_fields(account):
        return "● есть неприменённые правки", "warn"
    if applied_snapshot(account):
        return "● применён", "ok"
    return "○ не задан", "dim"


def name_changes_left(account: Any, now: datetime | None = None) -> int:
    """How many channel-name changes YouTube still allows right now (2 per 14 days)."""
    used = len(recent_name_changes(account.profile_name_changes, now or datetime.now(timezone.utc)))
    return max(0, NAME_CHANGES_PER_WINDOW - used)


def name_change_wait(account: Any, now: datetime | None = None) -> timedelta | None:
    """Time until the channel name may change again; None when it may change now."""
    now = now or datetime.now(timezone.utc)
    recent = recent_name_changes(account.profile_name_changes, now)
    if len(recent) < NAME_CHANGES_PER_WINDOW:
        return None
    return recent[0] + timedelta(days=NAME_CHANGE_WINDOW_DAYS) - now


def _has_value(account: Any, field: str) -> bool:
    return bool(_draft(account, field) or applied_snapshot(account).get(field))


def profile_action_label(account: Any, field: str) -> str:
    """Menu row text: «Изменить» when the field already has a value, «Добавить» when it is empty."""
    if field == "name":
        return "Изменить никнейм (псевдоним)"
    word = "описание" if field == "description" else "аватарку"
    return f"{'Изменить' if _has_value(account, field) else 'Добавить'} {word}"


def plural_changes(count: int) -> str:
    """«1 изменение» / «2 изменения» / «5 изменений»."""
    tail = count % 100
    if 11 <= tail <= 14:
        word = "изменений"
    else:
        word = {1: "изменение", 2: "изменения", 3: "изменения", 4: "изменения"}.get(count % 10, "изменений")
    return f"{count} {word}"


def _shown_value(account: Any, field: str) -> str:
    value = _draft(account, field)
    if value is None:
        value = applied_snapshot(account).get(field)
    if value is None:
        return "—"
    if field == "description":
        if not value:
            return "(очищено)"
        first = value.splitlines()[0]
        if len(first) > _DESCRIPTION_PREVIEW or "\n" in value:
            first = first[:_DESCRIPTION_PREVIEW].rstrip() + "…"
        return first
    if field == "avatar":
        return value.replace("\\", "/").rsplit("/", 1)[-1] if value else "—"
    return value


def profile_card_lines(account: Any, now: datetime | None = None) -> list[tuple[str, str, str, str]]:
    """Rows of the profile card: (label, value, hint, style) per field; the hint says live / not applied yet."""
    now = now or datetime.now(timezone.utc)
    pending = set(profile_pending_fields(account))
    applied = applied_snapshot(account)
    lines: list[tuple[str, str, str, str]] = []
    for field in PROFILE_FIELDS:
        if field in pending:
            hint, style = "● не применено", "warn"
        elif field in applied:
            hint, style = f"применён {format_ago(account.profile_applied_at, now)}", "dim"
        else:
            hint, style = "", "dim"
        lines.append((PROFILE_FIELD_LABELS[field], _shown_value(account, field), hint, style))
    return lines


def format_profile_summary(
    account_name: str, success: bool, changed: Sequence[str], details: str, skipped: Sequence[str] = ()
) -> tuple[str, str]:
    """(kind, text) for a finished apply: what changed, «already up to date», what was left out, or why it failed."""
    if not success:
        return "err", f"Профиль «{account_name}»: не применён — {details}"
    if not changed and skipped:
        kind, text = "warn", f"Профиль «{account_name}»: изменений на канале нет, часть не применена"
    elif not changed:
        kind, text = "info", f"Профиль «{account_name}»: уже актуален, менять нечего"
    else:
        kind, text = "ok", f"Профиль «{account_name}»: применено — " + ", ".join(_PROFILE_FIELD_WORDS.get(f, f) for f in changed)
    return kind, f"{text} · {details}" if details else text


__all__ = [
    "DESCRIPTION_MAX",
    "account_card_rows",
    "account_status_label",
    "next_comment_text",
    "LifecycleRow",
    "NICKNAME_MAX",
    "PROFILE_FIELDS",
    "PROFILE_FIELD_LABELS",
    "applied_snapshot",
    "clean_path_input",
    "clean_pasted_lines",
    "count_account_lines",
    "format_ago",
    "format_duration",
    "format_import_summary",
    "format_profile_summary",
    "format_warm_summary",
    "lifecycle_rows",
    "name_change_wait",
    "name_changes_left",
    "parse_warm_selection",
    "plural_changes",
    "profile_action_label",
    "profile_card_lines",
    "profile_pending_fields",
    "profile_status_label",
    "validate_description",
    "validate_nickname",
]
