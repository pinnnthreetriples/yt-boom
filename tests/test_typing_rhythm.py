from __future__ import annotations

import random
import re
import statistics
import unicodedata
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from modules.interaction_simulator import InteractionConfig, InteractionSimulator


def _simulator(seed: int = 7, **config: int) -> InteractionSimulator:
    return InteractionSimulator(InteractionConfig(**config), rng=random.Random(seed))


class TestLognormalTiming(unittest.TestCase):
    def test_range_holds_about_ninety_percent_of_draws(self) -> None:
        sim = _simulator()
        draws = [sim._lognormal_ms(45, 130) for _ in range(20000)]
        inside = sum(45 <= value <= 130 for value in draws) / len(draws)
        self.assertGreater(inside, 0.85)
        self.assertLess(inside, 0.95)

    def test_distribution_is_right_skewed_with_a_tail_past_max(self) -> None:
        sim = _simulator()
        draws = [sim._lognormal_ms(45, 130) for _ in range(20000)]
        self.assertGreater(statistics.mean(draws), statistics.median(draws))
        self.assertGreater(max(draws), 130)

    def test_draws_stay_inside_hard_clamps(self) -> None:
        sim = _simulator()
        draws = [sim._lognormal_ms(45, 130) for _ in range(20000)]
        self.assertGreaterEqual(min(draws), 45 * 0.6)
        self.assertLessEqual(max(draws), 130 * 2.5)

    def test_fixed_range_returns_exact_value(self) -> None:
        self.assertEqual(_simulator()._lognormal_ms(65, 65), 65.0)

    def test_delays_are_not_a_uniform_spread(self) -> None:
        """Uniform draws fill [min, max] evenly; lognormal ones crowd around the median."""
        sim = _simulator()
        draws = [sim._lognormal_ms(45, 130) for _ in range(20000)]
        middle = sum(70 <= value <= 90 for value in draws) / len(draws)
        self.assertGreater(middle, 0.28)  # uniform over 45..130 would give 20/85 = 0.235


class TestCharacterDelay(unittest.TestCase):
    @staticmethod
    def _mean_delay(sim: InteractionSimulator, character: str, following: str = "", runs: int = 3000) -> float:
        return statistics.mean(sim._character_delay(character, following) for _ in range(runs))

    def test_space_is_slower_than_a_letter(self) -> None:
        sim = _simulator()
        self.assertGreater(self._mean_delay(sim, " ", "a"), self._mean_delay(sim, "a", "b") * 1.3)

    def test_sentence_end_pauses_only_before_a_space(self) -> None:
        sim = _simulator()
        end_of_sentence = self._mean_delay(sim, ".", " ")
        inside_address = self._mean_delay(sim, ".", "c")
        self.assertGreater(end_of_sentence, inside_address * 1.5)
        self.assertAlmostEqual(inside_address, self._mean_delay(sim, "a", "b"), delta=0.01)

    def test_shift_keys_are_slightly_slower(self) -> None:
        sim = _simulator()
        plain = self._mean_delay(sim, "a", "b")
        self.assertGreater(self._mean_delay(sim, "A", "b"), plain * 1.1)
        self.assertGreater(self._mean_delay(sim, "@", "b"), plain * 1.1)

    def test_word_hesitation_happens_only_after_a_space(self) -> None:
        sim = _simulator(key_delay_ms_min=50, key_delay_ms_max=50)
        with patch.object(sim._rng, "random", return_value=0.0):
            self.assertGreaterEqual(sim._character_delay(" ", "a"), 0.25)
            for character in "a1.,Z@":
                self.assertLess(sim._character_delay(character, "x"), 0.25)
        with patch.object(sim._rng, "random", return_value=0.99):
            self.assertLess(sim._character_delay(" ", "a"), 0.25)

    def test_delay_is_in_seconds(self) -> None:
        delay = _simulator(key_delay_ms_min=50, key_delay_ms_max=50)._character_delay("a", "b")
        self.assertAlmostEqual(delay, 0.05)


class TestTypeTextTiming(unittest.IsolatedAsyncioTestCase):
    async def test_type_text_passes_the_next_character_to_the_delay(self) -> None:
        page = MagicMock(is_closed=MagicMock(return_value=False))
        page.keyboard = MagicMock(down=AsyncMock(), up=AsyncMock(), type=AsyncMock(), press=AsyncMock())
        sim = _simulator()
        calls: list[tuple[str, str]] = []
        original = sim._character_delay

        def spy(character: str, following: str = "") -> float:
            calls.append((character, following))
            return original(character, following)

        with patch.object(sim, "_character_delay", side_effect=spy), patch("asyncio.sleep", new_callable=AsyncMock):
            await sim.type_text(page, "a.b c", allow_typos=False)
        self.assertEqual(calls, [("a", "."), (".", "b"), ("b", " "), (" ", "c"), ("c", "")])

    async def test_hold_time_varies_between_keystrokes(self) -> None:
        page = MagicMock(is_closed=MagicMock(return_value=False))
        page.keyboard = MagicMock(down=AsyncMock(), up=AsyncMock(), type=AsyncMock())
        sim = _simulator()
        sleeps: list[float] = []

        async def record(seconds: float) -> None:
            sleeps.append(seconds)

        with patch("asyncio.sleep", side_effect=record):
            for _ in range(40):
                await sim._press_character(page, "a")
        self.assertGreater(len(set(round(value, 4) for value in sleeps)), 30)


class _FakeKeyboard:
    """Models a contenteditable: typed characters append, Backspace removes exactly one."""

    def __init__(self) -> None:
        self.buffer: list[str] = []
        self.deleted_positions: list[int] = []
        self.shift_held = False
        self.capitals_without_shift: list[str] = []

    async def down(self, key: str) -> None:
        if key == "Shift":  # a modifier types nothing
            self.shift_held = True
            return
        if (key.isupper() or key in '!@#$%^&*()_+{}|:"<>?~') and not self.shift_held:
            self.capitals_without_shift.append(key)
        self.buffer.append(key)

    async def up(self, key: str) -> None:
        if key == "Shift":
            self.shift_held = False

    async def type(self, text: str) -> None:
        self.buffer.extend(text)

    async def press(self, key: str, delay: int | None = None) -> None:
        if key == "Backspace":
            self.deleted_positions.append(len(self.buffer) - 1)
            self.buffer.pop()
        elif key == "Enter":
            self.buffer.append("\n")

    @property
    def text(self) -> str:
        return "".join(self.buffer)


_SAMPLES = [
    "Спасибо за подробный разбор, очень полезно и наглядно",
    "почему так происходит? раньше всё работало по-другому",
    "Thanks for the detailed breakdown, really helpful and clear",
    "does this work on the older model or only the new one?",
    "Ёлка зелёная, лол 😂 это жесть",
    "line one\nline two, with Заглавные and CAPS",
    "a",
    "ab",
    "aa bb  cc",
]


class TestTypoFinalTextInvariant(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _page() -> tuple[MagicMock, _FakeKeyboard]:
        keyboard = _FakeKeyboard()
        return MagicMock(is_closed=MagicMock(return_value=False), keyboard=keyboard), keyboard

    async def test_final_text_always_equals_intended_text(self) -> None:
        transposed = 0
        for seed in range(60):
            for sample in _SAMPLES:
                page, keyboard = self._page()
                sim = _simulator(seed, typo_probability=0.5)
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    await sim.type_text(page, sample)
                self.assertEqual(keyboard.text, sample, f"seed={seed}")
                self.assertEqual(keyboard.capitals_without_shift, [], f"seed={seed}")
                self.assertFalse(keyboard.shift_held, f"seed={seed}: Shift left pressed")
                run = 0
                for first, second in zip(keyboard.deleted_positions, keyboard.deleted_positions[1:]):
                    run = run + 1 if second == first - 1 else 0
                    transposed += run == 1
        self.assertGreater(transposed, 50)  # multi-character corrections really happen

    async def test_transposition_is_noticed_late_and_backspaces_exactly(self) -> None:
        page, keyboard = self._page()
        sim = _simulator()
        typed: list[str] = []
        original = sim._press_character

        async def spy(page_arg: object, character: str) -> None:
            typed.append(character)
            await original(page_arg, character)

        with patch.object(sim, "_press_character", side_effect=spy), patch("asyncio.sleep", new_callable=AsyncMock):
            await sim._type_with_transposition(page, "the quick", 0, 2)
        self.assertEqual("".join(typed[:4]), "hte ")  # swapped pair plus two characters typed before noticing
        self.assertEqual(len(keyboard.deleted_positions), 4)
        self.assertEqual(keyboard.text, "the ")

    async def test_transposition_swaps_only_letters_inside_a_word(self) -> None:
        sim = _simulator()
        self.assertTrue(sim._can_transpose("teh", 0))
        self.assertFalse(sim._can_transpose("aa", 0))  # swapping identical letters is invisible
        self.assertFalse(sim._can_transpose("a b", 0))  # next character is not a letter
        self.assertFalse(sim._can_transpose("Th", 0))  # no capital swaps
        self.assertFalse(sim._can_transpose("ab", 1))  # nothing after the last character

    async def test_no_typos_inside_mentions_and_links(self) -> None:
        text = "hi @teh.channel see https://teh.example/tehran or www.tehran.ru now"
        protected = [match.span() for match in re.finditer(r"@[\w.\-]+|(?:https?://|www\.)\S+", text)]
        for seed in range(80):
            page, keyboard = self._page()
            sim = _simulator(seed, typo_probability=0.5)
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await sim.type_text(page, text)
            self.assertEqual(keyboard.text, text)
            for position in keyboard.deleted_positions:
                self.assertFalse(any(start <= position < end for start, end in protected), f"seed={seed} pos={position}")

    async def test_no_wrong_character_next_to_a_combining_mark(self) -> None:
        sim = _simulator()
        self.assertFalse(sim._can_transpose("ab\u0301c", 0))  # the mark follows the swapped pair
        self.assertFalse(sim._can_transpose("x\u0301ab", 2))  # the mark precedes it
        with patch.object(sim._rng, "choice", return_value=2):
            # "tehe\u0301x": the lag may not end on a base letter that carries a mark
            self.assertEqual(sim._late_notice_chars("tehe\u0301x", 0, []), 1)
            self.assertEqual(sim._late_notice_chars("te\u0301hx", 0, []), 0)
        marked = ["e\u0301то прове\u0301рка с ударе\u0301нием", "cafe\u0301 au lait, tre\u0300s bien", "ab\u0301c abc\u0301 a\u0301bc"]
        for seed in range(80):
            for sample in marked:
                page, keyboard = self._page()
                sim = _simulator(seed, typo_probability=0.5)
                with patch("asyncio.sleep", new_callable=AsyncMock):
                    await sim.type_text(page, sample)
                self.assertEqual(keyboard.text, sample, f"seed={seed}")
                for position in keyboard.deleted_positions:
                    near = sample[max(0, position - 1) : position + 2]
                    self.assertFalse(any(unicodedata.category(c).startswith("M") for c in near), f"seed={seed} {sample!r} pos={position}")

    async def test_transposition_share_zero_means_substitution_only(self) -> None:
        page, keyboard = self._page()
        sim = _simulator(3, typo_probability=0.5, transposition_share=0.0)
        with patch("asyncio.sleep", new_callable=AsyncMock), patch.object(
            sim, "_type_with_transposition", new_callable=AsyncMock
        ) as transposition:
            await sim.type_text(page, "Thanks for the detailed breakdown")
        transposition.assert_not_awaited()
        self.assertEqual(keyboard.text, "Thanks for the detailed breakdown")

    async def test_missing_backspace_is_an_error_not_corrupted_text(self) -> None:
        from modules.interaction_simulator import InteractionError

        page = MagicMock(is_closed=MagicMock(return_value=False))
        page.keyboard = MagicMock(spec=["down", "up", "type"], down=AsyncMock(), up=AsyncMock(), type=AsyncMock())
        sim = _simulator()
        with patch("asyncio.sleep", new_callable=AsyncMock), self.assertRaises(InteractionError):
            await sim._press_backspace(page)


if __name__ == "__main__":
    unittest.main()
