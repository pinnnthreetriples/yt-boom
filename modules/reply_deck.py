"""Per-account "deck" of custom reply templates.

Every account shuffles its own deck of the `processor.custom_replies` templates and takes the next card for each reply.
A card is *burned* (stored in SQLite, so a restart keeps it) only after the reply was really posted; a failed post or
a dry run leaves the deck untouched. Used templates are not eligible again for that account until the deck is
exhausted, then all templates are shuffled again (the first card differs from the last posted one). The deck is
never reset by time.

Flow for a caller: `draw()` before posting (reserves the card so a parallel worker of the same account takes
another one), then `settle(card, posted=True/False)` once the outcome is known (always, e.g. in `finally`).
"""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

from modules.processor import ProcessorError, mix_latin, resolve_reply_text

if TYPE_CHECKING:
    from database import Database
    from modules.collector import CollectedItem

_WAIT_TIMEOUT_SECONDS = 600.0


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


class ReplyDeck:
    """Draws templates per account; safe for parallel accounts and for parallel workers of one account."""

    def __init__(self, db: Database, rng: random.Random | None = None) -> None:
        self._db = db
        self._rng = rng or random.Random()
        self._conditions: defaultdict[str, asyncio.Condition] = defaultdict(asyncio.Condition)
        self._reserved: defaultdict[str, set[str]] = defaultdict(set)
        self._warned: set[str] = set()

    async def draw(self, account: str, templates: Sequence[str], *, dry_run: bool = False) -> DeckCard:
        """Pick the next template for `account` (does not burn it).

        A dry run only looks at the deck: it takes a random card that is still unused and writes nothing.
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
            cards, _ = await self._db.deck_snapshot(account)
            unused = [t for t, used in cards if not used and t in wanted]
            known = {t for t, _ in cards}
            fresh = [t for t in wanted if t not in known]
            pool = unused + fresh or wanted
            return DeckCard(account, self._rng.choice(pool), persistent=False)

        condition = self._conditions[account]
        async with condition:
            try:
                async with asyncio.timeout(_WAIT_TIMEOUT_SECONDS):
                    while True:
                        unused, reshuffled = await self._db.deck_load(account, wanted, self._rng)
                        if reshuffled and len(wanted) > 1:
                            logger.info("Аккаунт {!r}: колода шаблонов исчерпана, перетасована заново", account)
                        free = [t for t in unused if t not in self._reserved[account]]
                        if free:
                            self._reserved[account].add(free[0])
                            return DeckCard(account, free[0])
                        # every card left is being posted by a parallel worker of this account: wait for its outcome
                        await condition.wait()
            except TimeoutError as exc:
                raise ProcessorError(f"Колода шаблонов аккаунта {account!r} занята другим ответом слишком долго") from exc

    async def settle(self, card: DeckCard | None, *, posted: bool) -> None:
        """Burn the card when the reply was posted, otherwise just give it back. Safe to call more than once."""
        if card is None or card.settled:
            return
        card.settled = True
        if not card.persistent:
            return
        condition = self._conditions[card.account]
        try:
            if posted:
                await self._db.deck_burn(card.account, card.template)
        finally:
            async with condition:
                self._reserved[card.account].discard(card.template)
                condition.notify_all()


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
            text = await processor.paraphrase(text)
        else:
            limit = max(int(getattr(text_settings, "max_output_length", 0) or 0), len(text))
            text = mix_latin(text, float(getattr(text_settings, "latin_mix_probability", 0.0) or 0.0), rng, max_length=limit)
    except BaseException:
        if deck is not None:
            await deck.settle(card, posted=False)
        raise
    return PreparedReply(text, card)


__all__ = ["DeckCard", "PreparedReply", "ReplyDeck", "clean_templates", "prepare_reply"]
