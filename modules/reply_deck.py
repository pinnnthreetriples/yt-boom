"""Per-account "deck" of custom reply templates.

Every account shuffles its own deck of the `processor.custom_replies` templates and takes the next card for each reply.
A card is *burned* (stored in SQLite, so a restart keeps it) only after the reply was really posted; a failed post or
a dry run leaves the deck untouched. Used templates are not eligible again for that account until the deck is
exhausted, then all templates are shuffled again (the first card differs from the last posted one). The deck is
never reset by time.

A deck belongs to one (account, scope) pair: the scope is the project whose templates it holds, so an account that
works in several projects keeps one independent deck per project, even when their lists share a template.

Flow for a caller: `draw()` before posting (reserves the card so a parallel worker of the same account takes
another one), then `settle(card, posted=True/False)` once the outcome is known (always, e.g. in `finally`).
`settle` never raises: a deck error must not hide a reply that was really posted.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

from database import DEFAULT_DECK_SCOPE
from modules.processor import ProcessorError, mix_latin, resolve_reply_text

if TYPE_CHECKING:
    from database import Database
    from modules.collector import CollectedItem

_REAP_INTERVAL_SECONDS = 30.0  # a waiting worker re-checks this often whether the holder of a card is still alive


@dataclass
class DeckCard:
    """A template picked for one reply; `settle()` burns or releases it exactly once."""

    account: str
    template: str
    persistent: bool = True  # False for a dry run or an explicit single reply: nothing to burn or release
    settled: bool = field(default=False, repr=False)


@dataclass
class PreparedReply:
    text: str
    card: DeckCard | None = None


def clean_templates(templates: Sequence[str] | None) -> list[str]:
    """Stripped, non-empty, de-duplicated templates in their original order (the template text is the deck key)."""
    return list(dict.fromkeys(t.strip() for t in (templates or []) if t and t.strip()))


def deck_scope(project_id: int | None) -> str:
    return f"p{project_id}" if project_id else DEFAULT_DECK_SCOPE


class ReplyDeck:
    """Draws templates per account; safe for parallel accounts and for parallel workers of one account.

    One instance serves one scope (a project, or none). Reservations live in this process; the database stays
    consistent across processes through its own write transactions.
    """

    def __init__(self, db: Database, rng: random.Random | None = None, project_id: int | None = None) -> None:
        self._db = db
        self._rng = rng or random.Random()
        self._scope = deck_scope(project_id)
        self._conditions: dict[str, asyncio.Condition] = {}
        self._reserved: dict[str, dict[str, asyncio.Task[Any] | None]] = {}  # account -> template -> holding task
        self._warned: set[str] = set()

    @property
    def scope(self) -> str:
        return self._scope

    def _condition(self, account: str) -> asyncio.Condition:
        condition = self._conditions.get(account)
        if condition is None:
            condition = self._conditions[account] = asyncio.Condition()
        return condition

    def _reap_dead_holders(self, account: str) -> None:
        """A reservation whose task is gone (cancelled without a settle) must not block the account for ever."""
        held = self._reserved.get(account, {})
        for template, task in list(held.items()):
            if task is not None and task.done():
                logger.warning("Аккаунт {!r}: карта шаблона осталась за завершённой задачей, освобождаю", account)
                held.pop(template, None)

    async def draw(self, account: str, templates: Sequence[str], *, dry_run: bool = False) -> DeckCard:
        """Pick the next template for `account` (does not burn it).

        A dry run only looks at the deck: it takes a random card that is still unused and writes nothing. When
        every card left is held by a parallel worker of this account the call waits for that worker's outcome (a
        post can be queued behind a browser lock for long, so there is no time limit while the holder is alive).
        """
        wanted = clean_templates(templates)
        if not wanted:
            raise ProcessorError("No custom reply text provided and custom_replies list is empty")
        if len(wanted) == 1 and account not in self._warned:
            self._warned.add(account)
            logger.warning(
                "Аккаунт {!r}: в списке шаблонов только один - повторы неизбежны, добавьте ещё шаблонов", account
            )
        if dry_run:
            cards, _ = await self._db.deck_snapshot(account, self._scope)
            unused = [t for t, used in cards if not used and t in wanted]
            known = {t for t, _ in cards}
            fresh = [t for t in wanted if t not in known]
            pool = unused + fresh or wanted
            return DeckCard(account, self._rng.choice(pool), persistent=False)

        me = asyncio.current_task()
        condition = self._condition(account)
        announced = False
        async with condition:
            while True:
                held = self._reserved.setdefault(account, {})
                unused, reshuffled = await self._db.deck_load(account, self._scope, wanted, self._rng)
                if reshuffled and len(wanted) > 1:
                    logger.info("Аккаунт {!r}: колода шаблонов исчерпана, перетасована заново", account)
                free = [t for t in unused if t not in held]
                if free:
                    held[free[0]] = me
                    return DeckCard(account, free[0])
                if me is not None and any(held.get(t) is me for t in unused):
                    # this very task keeps the card it would wait for: waiting would be a deadlock with itself
                    raise ProcessorError(f"Аккаунт {account!r}: шаблон уже взят этой же задачей и не освобождён")
                if not announced:
                    announced = True
                    logger.info("Аккаунт {!r}: все свободные шаблоны сейчас в работе, жду их результата", account)
                try:
                    await asyncio.wait_for(condition.wait(), _REAP_INTERVAL_SECONDS)
                except asyncio.TimeoutError:
                    self._reap_dead_holders(account)

    async def settle(self, card: DeckCard | None, *, posted: bool) -> None:
        """Burn the card when the reply was posted, otherwise just give it back. Safe to call more than once.

        Never raises (except for the caller's own cancellation): if the burn fails the card simply stays in the
        deck and the failure is logged loudly, so a reply that WAS posted is still recorded everywhere else.
        """
        if card is None or card.settled:
            return
        card.settled = True
        if not card.persistent:
            return
        try:
            if posted:
                try:
                    await self._db.deck_burn(card.account, self._scope, card.template)
                except Exception as exc:
                    logger.error(
                        "КОЛОДА | Ответ опубликован, но шаблон не отмечен использованным (аккаунт {!r}): {}. "
                        "Шаблон останется в колоде и может выпасть ещё раз",
                        card.account,
                        exc,
                    )
        finally:
            # drop the reservation synchronously first: it must go even when this task is being cancelled
            self._reserved.get(card.account, {}).pop(card.template, None)
            try:
                async with self._condition(card.account):
                    self._condition(card.account).notify_all()
            except Exception:  # waiters re-check on their own every _REAP_INTERVAL_SECONDS
                pass


async def prepare_reply(
    item: CollectedItem,
    *,
    deck: ReplyDeck | None,
    account: str,
    text_settings: Any,
    processor: Any | None = None,
    custom_reply: str | None = None,
    custom_replies: Sequence[str] | None = None,
    dry_run: bool = False,
    rng: random.Random | None = None,
) -> PreparedReply:
    """Text of a custom ("as is") reply: deck pick -> placeholders -> DeepSeek paraphrase -> Latin mixing.

    An explicit `custom_reply` is used as is, without the deck. The returned card must be settled by the caller.
    Latin mixing runs exactly once on the final text: inside `processor.paraphrase` when there is a processor,
    here otherwise (template goes out unchanged); it never touches the stored "recent variants".
    """
    card: DeckCard | None = None
    template = (custom_reply or "").strip()
    if not template:
        pool = clean_templates(custom_replies)
        if deck is not None and pool:
            card = await deck.draw(account, pool, dry_run=dry_run)
            template = card.template
        elif pool:
            template = (rng or random.Random()).choice(pool)
    try:
        text = resolve_reply_text(item, custom_reply=template or None, custom_replies=None, rng=rng)
        if processor is not None:
            # the language of the rewrite follows the raw template, not a comment substituted into its placeholders
            text = await processor.paraphrase(text, source=template)
        else:
            limit = max(int(getattr(text_settings, "max_output_length", 0) or 0), len(text))
            text = mix_latin(text, float(getattr(text_settings, "latin_mix_probability", 0.0) or 0.0), rng, max_length=limit)
    except BaseException:
        if deck is not None:
            await deck.settle(card, posted=False)
        raise
    return PreparedReply(text, card)


__all__ = ["DeckCard", "PreparedReply", "ReplyDeck", "clean_templates", "deck_scope", "prepare_reply"]
