"""Per-account deck of custom reply templates and the Latin letter mixing applied to the final text."""
from __future__ import annotations

import asyncio
import random
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from loguru import logger

from config import ProcessorSettings
from database import Database
from modules.collector import CollectedItem
from modules.multi_account_orchestrator import MultiAccountOrchestrator
from modules.processor import ProcessorError
from modules.reply_deck import ReplyDeck, prepare_reply
from modules.ui_test_runner import SubmissionResult
from tests.test_multi_account_orchestrator import _make_test_config

TEMPLATES = ["one", "two", "three", "four", "five"]


def _item(text: str = "Great video", language: str = "en") -> CollectedItem:
    return CollectedItem(
        endpoint="youtube:vid", external_id="vid#c1", text=text, language=language, confidence=0.9, author="viewer"
    )


class DeckCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "deck.db"
        self.db = Database(self.db_path)
        await self.db.connect()
        await self.db.init_schema()
        self.deck = ReplyDeck(self.db, random.Random(7))

    async def asyncTearDown(self) -> None:
        await self.db.close()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    async def post(self, account: str, templates: list[str] = TEMPLATES) -> str:
        """Draw a card and burn it, as after a successful post."""
        card = await self.deck.draw(account, templates)
        await self.deck.settle(card, posted=True)
        return card.template


class TestDeckDraws(DeckCase):
    async def test_no_repeats_within_a_deck(self) -> None:
        round_one = [await self.post("alpha") for _ in TEMPLATES]
        self.assertEqual(sorted(round_one), sorted(TEMPLATES))

    async def test_reshuffle_never_starts_with_the_last_used_template(self) -> None:
        for seed in range(25):
            deck = ReplyDeck(self.db, random.Random(seed))
            account = f"acc{seed}"
            previous: str | None = None
            for _ in range(4):  # four full decks
                cards = []
                for _ in TEMPLATES:
                    card = await deck.draw(account, TEMPLATES)
                    await deck.settle(card, posted=True)
                    cards.append(card.template)
                self.assertEqual(sorted(cards), sorted(TEMPLATES))
                if previous is not None:
                    self.assertNotEqual(cards[0], previous)
                previous = cards[-1]

    async def test_failed_post_keeps_the_template_in_the_deck(self) -> None:
        card = await self.deck.draw("alpha", TEMPLATES)
        await self.deck.settle(card, posted=False)
        again = await self.deck.draw("alpha", TEMPLATES)
        self.assertEqual(again.template, card.template)  # not burned: still the next card
        cards, last = await self.db.deck_snapshot("alpha")
        self.assertTrue(all(not used for _, used in cards))
        self.assertIsNone(last)
        await self.deck.settle(again, posted=True)
        cards, last = await self.db.deck_snapshot("alpha")
        self.assertEqual([t for t, used in cards if used], [card.template])
        self.assertEqual(last, card.template)

    async def test_settle_is_idempotent(self) -> None:
        card = await self.deck.draw("alpha", TEMPLATES)
        await self.deck.settle(card, posted=True)
        await self.deck.settle(card, posted=False)  # the finally-block after the burn must not undo or double it
        cards, _ = await self.db.deck_snapshot("alpha")
        self.assertEqual(sum(used for _, used in cards), 1)

    async def test_dry_run_never_burns_or_writes(self) -> None:
        for _ in range(12):
            card = await self.deck.draw("alpha", TEMPLATES, dry_run=True)
            self.assertIn(card.template, TEMPLATES)
            await self.deck.settle(card, posted=True)  # even a stray "posted" must not burn a dry-run card
        cards, last = await self.db.deck_snapshot("alpha")
        self.assertEqual(cards, [])
        self.assertIsNone(last)

    async def test_accounts_have_independent_decks(self) -> None:
        first_alpha = await self.post("alpha")
        first_beta = await self.post("beta")
        # beta's first pick is not excluded by alpha's burn: over many accounts the same template can come first
        seen_first = set()
        for i in range(30):
            deck = ReplyDeck(self.db, random.Random(i))
            card = await deck.draw(f"acc{i}", TEMPLATES)
            seen_first.add(card.template)
            await deck.settle(card, posted=True)
        self.assertGreater(len(seen_first), 1)
        alpha_cards, alpha_last = await self.db.deck_snapshot("alpha")
        beta_cards, beta_last = await self.db.deck_snapshot("beta")
        self.assertEqual([t for t, used in alpha_cards if used], [first_alpha])
        self.assertEqual([t for t, used in beta_cards if used], [first_beta])
        self.assertEqual((alpha_last, beta_last), (first_alpha, first_beta))

    async def test_state_survives_a_new_connection(self) -> None:
        burned = [await self.post("alpha") for _ in range(3)]
        await self.db.close()

        db2 = Database(self.db_path)
        await db2.connect()
        await db2.init_schema()
        try:
            deck2 = ReplyDeck(db2, random.Random(99))  # a fresh process: new rng, no in-memory state
            rest = []
            for _ in range(2):
                card = await deck2.draw("alpha", TEMPLATES)
                await deck2.settle(card, posted=True)
                rest.append(card.template)
            self.assertEqual(sorted(burned + rest), sorted(TEMPLATES))
        finally:
            await db2.close()
        self.db = Database(self.db_path)  # asyncTearDown closes it
        await self.db.connect()

    async def test_template_edits_join_and_leave_the_current_deck(self) -> None:
        used = [await self.post("alpha", ["a", "b", "c"]) for _ in range(2)]
        left = next(t for t in ["a", "b", "c"] if t not in used)

        # a new template joins the current deck: it is drawn before the deck is reshuffled
        edited = ["a", "b", "c", "d"]
        got = {await self.post("alpha", edited), await self.post("alpha", edited)}
        self.assertEqual(got, {left, "d"})

        # a removed template drops out of the deck for good
        edited = ["a", "b", "x"]
        for _ in range(9):
            self.assertIn(await self.post("alpha", edited), edited)
        cards, _ = await self.db.deck_snapshot("alpha")
        self.assertEqual(sorted(t for t, _ in cards), sorted(edited))

    async def test_duplicate_and_blank_templates_are_one_card(self) -> None:
        cards = [await self.post("alpha", ["x", " x ", "", "y", "x"]) for _ in range(2)]
        self.assertEqual(sorted(cards), ["x", "y"])

    async def test_empty_template_list_is_an_error(self) -> None:
        with self.assertRaises(ProcessorError):
            await self.deck.draw("alpha", ["  ", ""])

    async def test_single_template_warns_once_and_repeats(self) -> None:
        messages: list[str] = []
        sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            picked = [await self.post("alpha", ["only"]) for _ in range(3)]
            await self.post("beta", ["only"])
        finally:
            logger.remove(sink)
        self.assertEqual(picked, ["only"] * 3)
        alpha_warnings = [m for m in messages if "'alpha'" in m]
        self.assertEqual(len(alpha_warnings), 1)  # once per account, not per reply
        self.assertEqual(len([m for m in messages if "'beta'" in m]), 1)

    async def test_renaming_an_account_keeps_its_deck(self) -> None:
        await self.db.upsert_account("old_name", str(Path(self.temp_dir.name) / "s"))
        burned = await self.post("old_name")
        await self.db.rename_account("old_name", "new_name")
        cards, last = await self.db.deck_snapshot("new_name")
        self.assertEqual([t for t, used in cards if used], [burned])
        self.assertEqual(last, burned)
        self.assertEqual(await self.db.deck_snapshot("old_name"), ([], None))


class TestDeckConcurrency(DeckCase):
    async def test_parallel_workers_of_one_account_get_different_cards(self) -> None:
        cards = await asyncio.gather(*(self.deck.draw("alpha", TEMPLATES) for _ in range(5)))
        self.assertEqual(sorted(c.template for c in cards), sorted(TEMPLATES))
        for card in cards:
            await self.deck.settle(card, posted=True)

    async def test_last_card_is_not_handed_out_twice(self) -> None:
        templates = ["a", "b"]
        first, second = await asyncio.gather(self.deck.draw("alpha", templates), self.deck.draw("alpha", templates))
        self.assertNotEqual(first.template, second.template)
        third = asyncio.create_task(self.deck.draw("alpha", templates))  # both cards are reserved: has to wait
        await asyncio.sleep(0.05)
        self.assertFalse(third.done())
        await self.deck.settle(first, posted=True)  # burned: the deck is not exhausted while the other card is in flight
        await asyncio.sleep(0.05)
        self.assertFalse(third.done())
        await self.deck.settle(second, posted=False)  # its post failed: the card is free again
        card = await asyncio.wait_for(third, 2)
        self.assertEqual(card.template, second.template)
        await self.deck.settle(card, posted=False)

    async def test_released_card_wakes_a_waiting_worker(self) -> None:
        templates = ["a"]
        held = await self.deck.draw("alpha", templates)
        waiter = asyncio.create_task(self.deck.draw("alpha", templates))
        await asyncio.sleep(0.05)
        self.assertFalse(waiter.done())
        await self.deck.settle(held, posted=False)
        card = await asyncio.wait_for(waiter, 2)
        self.assertEqual(card.template, "a")

    async def test_parallel_accounts_do_not_block_each_other(self) -> None:
        held = await self.deck.draw("alpha", ["a"])  # alpha's only card is in flight
        card = await asyncio.wait_for(self.deck.draw("beta", ["a"]), 2)
        self.assertEqual(card.template, "a")
        await self.deck.settle(held, posted=False)
        await self.deck.settle(card, posted=False)


class _FakeProcessor:
    def __init__(self, answer: str = "paraphrased text") -> None:
        self.answer = answer
        self.calls: list[str] = []

    async def paraphrase(self, text: str) -> str:
        self.calls.append(text)
        return self.answer


class TestPrepareReply(DeckCase):
    def settings(self, **kwargs) -> ProcessorSettings:
        return ProcessorSettings(system_prompt="s", user_prompt_template="{text}", max_output_length=400, **kwargs)

    async def test_placeholders_and_card(self) -> None:
        prepared = await prepare_reply(
            _item("Nice"), deck=self.deck, account="alpha", text_settings=self.settings(),
            custom_replies=["Re {text} [{language}]"], rng=random.Random(1),
        )
        self.assertEqual(prepared.text, "Re Nice [EN]")
        self.assertEqual(prepared.card.template, "Re {text} [{language}]")  # the deck key is the raw template
        await self.deck.settle(prepared.card, posted=True)

    async def test_explicit_custom_reply_bypasses_the_deck(self) -> None:
        prepared = await prepare_reply(
            _item(), deck=self.deck, account="alpha", text_settings=self.settings(),
            custom_reply="Explicit text", custom_replies=TEMPLATES, rng=random.Random(1),
        )
        self.assertEqual(prepared.text, "Explicit text")
        self.assertIsNone(prepared.card)
        self.assertEqual(await self.db.deck_snapshot("alpha"), ([], None))

    async def test_paraphrase_runs_on_top_of_the_deck_pick(self) -> None:
        processor = _FakeProcessor("reworded")
        prepared = await prepare_reply(
            _item(), deck=self.deck, account="alpha", text_settings=self.settings(),
            processor=processor, custom_replies=["only template"], rng=random.Random(1),
        )
        self.assertEqual(processor.calls, ["only template"])
        self.assertEqual(prepared.text, "reworded")
        await self.deck.settle(prepared.card, posted=False)

    async def test_latin_mix_applies_once_to_a_plain_custom_reply(self) -> None:
        text = "Привет, приходи завтра на стрим"
        settings = self.settings(latin_mix_probability=1.0)
        prepared = await prepare_reply(
            _item(), deck=self.deck, account="alpha", text_settings=settings,
            custom_replies=[text], rng=random.Random(3),
        )
        self.assertNotEqual(prepared.text, text)
        self.assertTrue(re.search(r"[A-Za-z]", prepared.text))
        self.assertEqual(prepared.card.template, text)  # the deck keeps the clean template
        await self.deck.settle(prepared.card, posted=False)

        off = await prepare_reply(
            _item(), deck=self.deck, account="alpha", text_settings=self.settings(),
            custom_replies=[text], rng=random.Random(3),
        )
        self.assertEqual(off.text, text)
        await self.deck.settle(off.card, posted=False)

    async def test_latin_mix_is_not_applied_a_second_time_after_paraphrase(self) -> None:
        processor = _FakeProcessor("Привет, приходи завтра на стрим")  # stands for an already finished paraphrase
        prepared = await prepare_reply(
            _item(), deck=self.deck, account="alpha", text_settings=self.settings(latin_mix_probability=1.0),
            processor=processor, custom_replies=["template"], rng=random.Random(3),
        )
        self.assertEqual(prepared.text, "Привет, приходи завтра на стрим")
        await self.deck.settle(prepared.card, posted=False)

    async def test_failed_preparation_releases_the_card(self) -> None:
        processor = _FakeProcessor()
        processor.paraphrase = AsyncMock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            await prepare_reply(
                _item(), deck=self.deck, account="alpha", text_settings=self.settings(),
                processor=processor, custom_replies=["a"], rng=random.Random(1),
            )
        card = await asyncio.wait_for(self.deck.draw("alpha", ["a"]), 2)  # not stuck on a leaked reservation
        self.assertEqual(card.template, "a")
        await self.deck.settle(card, posted=False)


class TestDeckInOrchestrator(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.config = _make_test_config(self.base)
        self.config.processor.custom_replies = ["Nice point!", "Interesting thought!"]
        self.db = Database(self.base / "test.db")
        await self.db.connect()
        await self.db.init_schema()
        await self.db.upsert_account("acc_1", str(self.base / "acc1"))

    async def asyncTearDown(self) -> None:
        await self.db.close()
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    async def run_once(self, result: SubmissionResult, *, dry_run: bool, vid: str) -> list[str]:
        posted: list[str] = []

        def factory(acc):
            runner = MagicMock()

            async def run(text, external_id=None, payload_id=None, dry_run=False):
                posted.append(text)
                return result

            runner.run = AsyncMock(side_effect=run)
            runner.close = AsyncMock()
            return runner

        collector = MagicMock()

        async def fetch(video_url, limit=4, target_mode="first_reply"):
            if not video_url.endswith(vid):  # videos left in the pool by earlier runs have nothing to answer
                return []
            return [
                CollectedItem(
                    endpoint=f"youtube:{vid}", external_id=f"{vid}#root#c", text="Cool video", language="en",
                    confidence=0.95, source_url=f"https://www.youtube.com/watch?v={vid}", author="viewer",
                    video_id=vid, like_count=3,
                )
            ]

        collector.fetch_popular_comments = AsyncMock(side_effect=fetch)
        collector.collect_from_videos = AsyncMock(return_value=[])
        collector.close = AsyncMock()
        await self.db.add_video_to_pool(f"https://www.youtube.com/watch?v={vid}", comments_target=1)
        orchestrator = MultiAccountOrchestrator(
            self.config, db=self.db, concurrency=1, account_mode="share", collector=collector,
            runner_factory=factory, rng=random.Random(11), dry_run=dry_run,
        )
        await orchestrator.execute_video_pool(max_comments_per_video=1)
        return posted

    async def test_failed_dry_and_successful_posts(self) -> None:
        failed = await self.run_once(SubmissionResult(success=False, error="captcha", duration_seconds=0.1),
                                     dry_run=False, vid="vid_fail0001")
        self.assertEqual(len(failed), 1)
        cards, last = await self.db.deck_snapshot("acc_1")
        self.assertTrue(all(not used for _, used in cards))  # a failed post burns nothing
        self.assertIsNone(last)

        await self.run_once(SubmissionResult(success=True, remote_id="r1", duration_seconds=0.1),
                            dry_run=True, vid="vid_dry00001")
        cards, last = await self.db.deck_snapshot("acc_1")
        self.assertTrue(all(not used for _, used in cards))  # neither does a dry run
        self.assertIsNone(last)

        good = await self.run_once(SubmissionResult(success=True, remote_id="r2", duration_seconds=0.1),
                                   dry_run=False, vid="vid_good0001")
        cards, last = await self.db.deck_snapshot("acc_1")
        self.assertEqual([t for t, used in cards if used], good)  # exactly the posted template is burned
        self.assertEqual(last, good[0])


if __name__ == "__main__":
    unittest.main()
