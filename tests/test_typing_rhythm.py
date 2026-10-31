from __future__ import annotations

import random
import statistics
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


if __name__ == "__main__":
    unittest.main()
