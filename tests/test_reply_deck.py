"""Per-account deck of custom reply templates and the Latin letter mixing applied to the final text."""
from __future__ import annotations

import asyncio
import random
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
        self.assertEqual(sorted(t for t, _ in cards if t in edited), sorted(edited))

    async def test_overlapping_lists_of_two_projects_keep_independent_decks(self) -> None:
        list_a = ["shared", "a2", "a3", "a4"]
        list_b = ["shared", "b2", "b3"]
        deck_a = ReplyDeck(self.db, random.Random(1), project_id=1)
        deck_b = ReplyDeck(self.db, random.Random(2), project_id=2)

        # project A is halfway through its round: "shared" and "a3" are posted
        await self.db.deck_load("alpha", deck_a.scope, list_a, random.Random(3))
        await self.db.deck_burn("alpha", deck_a.scope, "shared")
        await self.db.deck_burn("alpha", deck_a.scope, "a3")

        # project B posts its whole deck, then its deck reshuffles
        round_one = []
        for _ in list_b:
            card = await deck_b.draw("alpha", list_b)
            await deck_b.settle(card, posted=True)
            round_one.append(card.template)
        self.assertEqual(sorted(round_one), sorted(list_b))
        card = await deck_b.draw("alpha", list_b)  # exhausted: reshuffled
        await deck_b.settle(card, posted=True)

        # that reshuffle did not make "shared" eligible again in A's unfinished round
        cards_a, _ = await self.db.deck_snapshot("alpha", deck_a.scope)
        self.assertEqual(sorted(t for t, used in cards_a if used), ["a3", "shared"])
        left = []
        for _ in range(2):
            card = await deck_a.draw("alpha", list_a)
            await deck_a.settle(card, posted=True)
            left.append(card.template)
        self.assertEqual(sorted(left), ["a2", "a4"])
        # and A burning cards never touched B's deck either
        cards_b, _ = await self.db.deck_snapshot("alpha", deck_b.scope)
        self.assertEqual(sum(used for _, used in cards_b), 1)

    async def test_decks_made_before_scopes_move_into_the_default_scope(self) -> None:
        import sqlite3

        path = Path(self.temp_dir.name) / "old.db"
        old = sqlite3.connect(path)
        old.executescript(
            """
            CREATE TABLE reply_deck (account TEXT NOT NULL, template TEXT NOT NULL, pos INTEGER NOT NULL DEFAULT 0,
                used INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (account, template));
            CREATE TABLE reply_deck_state (account TEXT PRIMARY KEY, last_template TEXT);
            INSERT INTO reply_deck VALUES ('alpha', 'one', 0, 1), ('alpha', 'two', 1, 0);
            INSERT INTO reply_deck_state VALUES ('alpha', 'one');
            """
        )
        old.commit()
        old.close()
        db = Database(path)
        await db.connect()
        await db.init_schema()
        await db.init_schema()  # a second start changes nothing
        try:
            cards, last = await db.deck_snapshot("alpha", "default")
            self.assertEqual(cards, [("two", False), ("one", True)])
            self.assertEqual(last, "one")
            deck = ReplyDeck(db, random.Random(1))
            card = await deck.draw("alpha", ["one", "two"])
            self.assertEqual(card.template, "two")  # the saved round continues
            await deck.settle(card, posted=True)
        finally:
            await db.close()

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


    async def test_a_waiting_worker_is_not_timed_out_while_the_holder_is_alive(self) -> None:
        held = await self.deck.draw("alpha", ["a"])
        waiter = asyncio.create_task(self.deck.draw("alpha", ["a"]))
        with patch("modules.reply_deck._REAP_INTERVAL_SECONDS", 0.05):
            await asyncio.sleep(0.4)  # many reap intervals later the holder is still alive: no error, still waiting
        self.assertFalse(waiter.done())
        await self.deck.settle(held, posted=True)
        card = await asyncio.wait_for(waiter, 2)
        self.assertEqual(card.template, "a")
        await self.deck.settle(card, posted=False)

    async def test_a_reservation_of_a_dead_task_does_not_block_the_account(self) -> None:
        holder = asyncio.create_task(self.deck.draw("alpha", ["a"]))
        await holder  # drew the card and finished without ever settling it
        with patch("modules.reply_deck._REAP_INTERVAL_SECONDS", 0.05):
            card = await asyncio.wait_for(asyncio.create_task(self.deck.draw("alpha", ["a"])), 2)
        self.assertEqual(card.template, "a")

    async def test_a_task_waiting_for_its_own_card_gets_an_error_not_a_deadlock(self) -> None:
        held = await self.deck.draw("alpha", ["only"])
        with self.assertRaises(ProcessorError):
            await self.deck.draw("alpha", ["only"])  # same task: must fail at once, not wait for itself
        await self.deck.settle(held, posted=False)

    async def test_a_failing_burn_never_raises_and_releases_the_card(self) -> None:
        card = await self.deck.draw("alpha", ["a"])
        with patch.object(Database, "deck_burn", AsyncMock(side_effect=RuntimeError("database is locked"))):
            await self.deck.settle(card, posted=True)  # must not raise
        again = await asyncio.wait_for(asyncio.create_task(self.deck.draw("alpha", ["a"])), 2)
        self.assertEqual(again.template, "a")  # not burned (logged), and no longer reserved


class _FakeProcessor:
    def __init__(self, answer: str = "paraphrased text") -> None:
        self.answer = answer
        self.calls: list[str] = []
        self.sources: list[str | None] = []

    async def paraphrase(self, text: str, *, source: str | None = None) -> str:
        self.calls.append(text)
        self.sources.append(source)
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
        self.assertEqual(processor.sources, ["only template"])  # the raw template decides the language of the rewrite
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

    async def build(
        self, behaviour, *, dry_run: bool, vid: str, custom_reply: str | None = None
    ) -> tuple[MultiAccountOrchestrator, list[str]]:
        """An orchestrator whose runner calls `behaviour(text)` (returns a result, raises, or blocks)."""
        posted: list[str] = []

        def factory(acc):
            runner = MagicMock()

            async def run(text, external_id=None, payload_id=None, dry_run=False):
                posted.append(text)
                return await behaviour(text)

            runner.run = AsyncMock(side_effect=run)
            runner.close = AsyncMock()
            return runner

        collector = MagicMock()

        async def fetch(video_url, limit=4, target_mode="first_reply", skip=()):
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
            runner_factory=factory, rng=random.Random(11), dry_run=dry_run, custom_reply=custom_reply,
        )
        return orchestrator, posted

    async def run_once(self, result: SubmissionResult, *, dry_run: bool, vid: str) -> list[str]:
        async def behaviour(text):
            return result

        orchestrator, posted = await self.build(behaviour, dry_run=dry_run, vid=vid)
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

    async def test_reply_unavailable_and_runner_exception_keep_the_card(self) -> None:
        unavailable = SubmissionResult(success=False, error="no reply button", duration_seconds=0.1, reply_unavailable=True)
        await self.run_once(unavailable, dry_run=False, vid="vid_nobtn001")

        async def crash(text):
            raise RuntimeError("Camoufox died")

        orchestrator, posted = await self.build(crash, dry_run=False, vid="vid_crash001")
        await orchestrator.execute_video_pool(max_comments_per_video=1)
        self.assertEqual(len(posted), 1)
        cards, last = await self.db.deck_snapshot("acc_1")
        self.assertTrue(cards and all(not used for _, used in cards))
        self.assertIsNone(last)
        self.assertFalse(orchestrator._deck._reserved.get("acc_1"))  # nothing stays reserved

    async def test_cancellation_releases_the_card(self) -> None:
        self.config.processor.custom_replies = ["Only one"]  # a leaked reservation would block every later draw
        entered = asyncio.Event()

        async def hang(text):
            entered.set()
            await asyncio.Event().wait()

        orchestrator, _ = await self.build(hang, dry_run=False, vid="vid_cancel01")
        task = asyncio.create_task(orchestrator.execute_video_pool(max_comments_per_video=1))
        await asyncio.wait_for(entered.wait(), 10)
        self.assertEqual(list(orchestrator._deck._reserved["acc_1"]), ["Only one"])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(orchestrator._deck._reserved["acc_1"])
        card = await asyncio.wait_for(orchestrator._deck.draw("acc_1", ["Only one"]), 2)
        self.assertEqual(card.template, "Only one")
        cards, _ = await self.db.deck_snapshot("acc_1")
        self.assertTrue(all(not used for _, used in cards))

    async def test_a_failing_burn_does_not_hide_the_posted_reply(self) -> None:
        async def success(text):
            return SubmissionResult(success=True, remote_id="rem_ok", duration_seconds=0.1)

        orchestrator, posted = await self.build(success, dry_run=False, vid="vid_burnerr1")
        orchestrator._count_project_reply = AsyncMock()
        messages: list[str] = []
        sink = logger.add(lambda m: messages.append(str(m)), level="ERROR")
        try:
            with patch.object(Database, "deck_burn", AsyncMock(side_effect=RuntimeError("database is locked"))):
                done = await orchestrator.execute_video_pool(max_comments_per_video=1)
        finally:
            logger.remove(sink)
        self.assertEqual(done, 1)
        self.assertEqual(len(posted), 1)
        orchestrator._count_project_reply.assert_awaited_once()
        self.assertIsNotNone((await self.db.get_account("acc_1")).last_comment_at)  # the account's counter ran
        payload = await self.db.get_payload_by_source_ref("vid_burnerr1#root#c")
        self.assertIsNotNone(payload)  # payload tracking ran
        self.assertEqual(payload.status, "submitted")
        logs = await self.db.list_activity_logs()
        self.assertEqual([entry.status for entry in logs], ["success"])
        self.assertTrue(any("КОЛОДА" in m and "database is locked" in m for m in messages))  # and the failure is loud
        self.assertFalse(orchestrator._deck._reserved.get("acc_1"))


    async def test_an_explicit_custom_reply_is_posted_without_touching_the_deck(self) -> None:
        self.config.processor.custom_replies = ["Explicit text"]  # as run_search_cli does for --custom-reply

        async def success(text):
            return SubmissionResult(success=True, remote_id="rem", duration_seconds=0.05)

        messages: list[str] = []
        sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            posted_all: list[str] = []
            for vid in ("vid_expl0001", "vid_expl0002"):
                orchestrator, posted = await self.build(success, dry_run=False, vid=vid, custom_reply="Explicit text")
                orchestrator._deck.draw = AsyncMock(side_effect=AssertionError("an explicit reply must not use the deck"))
                await orchestrator.execute_video_pool(max_comments_per_video=1)
                posted_all += posted
        finally:
            logger.remove(sink)
        self.assertEqual(posted_all, ["Explicit text", "Explicit text"])
        self.assertEqual(await self.db.deck_snapshot("acc_1"), ([], None))
        self.assertFalse([m for m in messages if "только один" in m])

    async def test_parallel_workers_of_one_account_are_not_serialized_by_an_explicit_reply(self) -> None:
        # two videos at once, one account, ONE template in the list: through the deck the second worker would wait
        # for the first one's post; with an explicit reply neither of them waits for a card
        self.config.processor.custom_replies = ["Explicit text"]

        async def success(text):
            await asyncio.sleep(0.05)
            return SubmissionResult(success=True, remote_id="rem", duration_seconds=0.05)

        orchestrator, posted = await self.build(success, dry_run=False, vid="vid_par00001", custom_reply="Explicit text")
        orchestrator._concurrency = 2
        orchestrator._semaphore = asyncio.Semaphore(2)
        draws = AsyncMock(side_effect=AssertionError("the deck must not be asked"))
        orchestrator._deck.draw = draws
        await self.db.add_video_to_pool("https://www.youtube.com/watch?v=vid_par00002", comments_target=1)

        async def fetch(video_url, limit=4, target_mode="first_reply", skip=()):
            vid = video_url.split("v=")[-1]
            return [CollectedItem(endpoint=f"youtube:{vid}", external_id=f"{vid}#root#c", text="Cool video",
                                  language="en", confidence=0.95, source_url=video_url, author="viewer",
                                  video_id=vid, like_count=3)]

        orchestrator._collector.fetch_popular_comments = AsyncMock(side_effect=fetch)
        await orchestrator.execute_video_pool(max_comments_per_video=1)
        self.assertEqual(sorted(posted), ["Explicit text"] * 2)
        draws.assert_not_called()


class TestDeckInYoutubeBot(unittest.IsolatedAsyncioTestCase):
    """run_reply_bot / run_multi_video_pipeline: a template leaves the deck only after a post that really happened."""

    async def asyncSetUp(self) -> None:
        import yaml

        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "test.db"
        self.config_path = self.root / "cfg.yaml"
        with open("config.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["app"]["db_path"] = str(self.db_path)
        data["app"]["log_file"] = str(self.root / "test.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["collector"]["target_mode"] = "raw"
        data["limits"]["visibility_check_enabled"] = False
        data["reply_mode"] = data["processor"]["reply_mode"] = "custom"
        data["custom_replies"] = data["processor"]["custom_replies"] = ["First template", "Second template"]
        data["processor"]["paraphrase_custom"] = False
        data["processor"]["latin_mix_probability"] = 0.0
        data["browser"]["accounts"] = []  # the search run must pick the account made by the --video run, nobody else
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.runs = 0

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def deck_state(self) -> tuple[list[str], list[str]]:
        db = Database(self.db_path)
        await db.connect()
        try:
            cards, _ = await db.deck_snapshot("default")
            return [t for t, used in cards if used], [t for t, used in cards if not used]
        finally:
            await db.close()

    async def run_bot(self, which: str, outcome, *, dry_run: bool = False, account_name: str | None = None) -> list[str]:
        """Run one reply with the runner answering `outcome` (a result, or an exception to raise); returns the texts."""
        from contextlib import ExitStack

        from youtube_bot import run_multi_video_pipeline, run_reply_bot

        sent: list[str] = []
        self.runs += 1  # a comment that failed as "no Reply button" is never offered again: every run gets a new one
        item = CollectedItem(
            endpoint="youtube:vid", external_id=f"vidbot#c{self.runs}", text="Great breakdown!", language="en",
            confidence=0.99, author="@fan", is_reply=False,
        )

        async def fake_run(self_runner, text, **kwargs):
            sent.append(text)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        patches = [
            patch("modules.youtube_runner.YouTubeRunner.run", fake_run),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ]
        if which == "single":
            patches.append(patch("modules.youtube_collector.YouTubeCollector.collect_from_videos",
                                 new_callable=AsyncMock, return_value=[item]))
        else:
            patches += [
                patch("modules.youtube_collector.YouTubeCollector.search_videos", new_callable=AsyncMock,
                      return_value=[{"video_id": "vidbot", "title": "Topic", "view_count": 5000}]),
                patch("modules.youtube_collector.YouTubeCollector.fetch_popular_comments", new_callable=AsyncMock,
                      return_value=[item]),
            ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            if which == "single":
                await run_reply_bot("https://www.youtube.com/watch?v=dQw4w9WgXcQ", config_path=str(self.config_path),
                                    target_mode="raw", limit=1, dry_run=dry_run, account_name=account_name)
            else:
                await run_multi_video_pipeline("python", config_path=str(self.config_path), max_videos=1,
                                               comments_per_video=1, dry_run=dry_run)
        return sent

    async def check_paths(self, which: str) -> None:
        for outcome in (
            RuntimeError("browser died"),
            SubmissionResult(success=False, error="captcha", duration_seconds=0.5),
            SubmissionResult(success=False, error="no reply button", duration_seconds=0.5, reply_unavailable=True),
        ):
            sent = await self.run_bot(which, outcome)
            self.assertEqual(len(sent), 1, outcome)
            used, unused = await self.deck_state()
            self.assertEqual(used, [], f"{which}: {outcome!r} must not burn a template")
            self.assertEqual(sorted(unused), ["First template", "Second template"])

        sent = await self.run_bot(which, SubmissionResult(success=True, remote_id="ok1", duration_seconds=0.5))
        used, unused = await self.deck_state()
        self.assertEqual(used, sent)  # exactly the posted template, once
        self.assertEqual(len(unused), 1)
        self.assertNotEqual(unused[0], sent[0])

    async def test_single_video_paths(self) -> None:
        await self.check_paths("single")

    async def test_multi_video_paths(self) -> None:
        await self.check_paths("multi")

    async def run_search(self, *, custom_reply: str | None = None) -> list[str]:
        """`--search` through the accounts (run_search_cli) with a runner that always posts; returns the texts."""
        from youtube_bot import run_search_cli

        self.runs += 1
        vid = f"srch{self.runs:07d}"
        item = CollectedItem(
            endpoint=f"youtube:{vid}", external_id=f"{vid}#c1", text="Great breakdown!", language="en",
            confidence=0.99, author="@fan", is_reply=False,
        )
        sent: list[str] = []

        async def fake_run(self_runner, text, **kwargs):
            sent.append(text)
            return SubmissionResult(success=True, remote_id=f"rem{self.runs}", duration_seconds=0.1)

        found = [{"video_id": vid, "url": f"https://www.youtube.com/watch?v={vid}", "title": "Topic", "view_count": 5000}]
        with patch("modules.youtube_runner.YouTubeRunner.run", fake_run), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             patch("modules.youtube_collector.YouTubeCollector.search_videos", new_callable=AsyncMock, return_value=found), \
             patch("modules.youtube_collector.YouTubeCollector.fetch_popular_comments",
                   new_callable=AsyncMock, return_value=[item]):
            await run_search_cli("topic", str(self.config_path), min_views=0, max_views=None, max_age_hours=None,
                                 max_videos=1, comments_per_video=1, dry_run=False, custom_reply=custom_reply)
        return sent

    async def all_deck_rows(self) -> list[tuple]:
        db = Database(self.db_path)
        await db.connect()
        try:
            return [tuple(r) for r in await db._fetch_all("SELECT account, scope, template, used FROM reply_deck")]
        finally:
            await db.close()

    async def test_search_and_video_runs_share_one_deck_per_account(self) -> None:
        first = await self.run_bot("single", SubmissionResult(success=True, remote_id="v1", duration_seconds=0.1),
                                   account_name="acc_1")
        second = await self.run_search()  # the other entry point, the same account and the same template list
        self.assertEqual(len(first) + len(second), 2)
        rows = await self.all_deck_rows()
        self.assertEqual({r[1] for r in rows}, {"p1"}, rows)  # one scope for both entry points
        self.assertEqual({r[0] for r in rows}, {"acc_1"})
        self.assertEqual(sorted(r[2] for r in rows if r[3]), ["First template", "Second template"])
        self.assertEqual(len(set(first + second)), 2)  # the search run took the card the --video run had left

    async def test_an_explicit_custom_reply_bypasses_the_deck(self) -> None:
        messages: list[str] = []
        sink = logger.add(lambda m: messages.append(str(m)), level="WARNING")
        try:
            sent = await self.run_search(custom_reply="Explicit text")
        finally:
            logger.remove(sink)
        self.assertEqual(sent, ["Explicit text"])
        self.assertEqual(await self.all_deck_rows(), [])  # no deck rows at all
        self.assertFalse([m for m in messages if "только один" in m])  # and no single-template warning

    async def test_dry_run_never_burns(self) -> None:
        for which in ("single", "multi"):
            await self.run_bot(which, SubmissionResult(success=True, remote_id="sim", duration_seconds=0.1), dry_run=True)
            used, _ = await self.deck_state()
            self.assertEqual(used, [], which)


if __name__ == "__main__":
    unittest.main()
