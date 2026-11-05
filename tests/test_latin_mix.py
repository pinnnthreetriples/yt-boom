"""Latin letter mixing (`mix_latin`): applied by code to the finished reply, never asked from DeepSeek."""
from __future__ import annotations

import asyncio
import random
import re
import unittest
from unittest.mock import AsyncMock, MagicMock

from config import DeepSeekSettings, ProcessorSettings
from modules.collector import CollectedItem
from modules.processor import DeepSeekProcessor, mix_latin


def _item(text: str = "Great video", language: str = "en") -> CollectedItem:
    return CollectedItem(
        endpoint="youtube:vid", external_id="vid#c1", text=text, language=language, confidence=0.9, author="viewer"
    )


class TestMixLatinEdgeCases(unittest.TestCase):
    def mix(self, text: str, seed: int = 1, **kwargs) -> str:
        return mix_latin(text, 1.0, random.Random(seed), **kwargs)

    def test_zero_probability_returns_the_text_unchanged(self) -> None:
        text = "Привет, приходи завтра"
        for seed in range(10):
            self.assertEqual(mix_latin(text, 0.0, random.Random(seed)), text)

    def test_urls_mentions_hashtags_and_emoji_stay_intact(self) -> None:
        text = "Смотрите https://example.com/привет и @канал.Петров #новости 😀 пожалуйста"
        for seed in range(60):
            mixed = self.mix(text, seed)
            self.assertIn("https://example.com/привет", mixed)
            self.assertIn("@канал.Петров", mixed)
            self.assertIn("#новости", mixed)
            self.assertIn("😀", mixed)
            self.assertNotEqual(mixed, text)  # "Смотрите" / "пожалуйста" are still eligible

    def test_text_with_nothing_eligible_is_unchanged(self) -> None:
        for text in ("https://привет.рф/страница", "@Вася #тема", "Great video 😀", ""):
            self.assertEqual(self.mix(text), text)

    def test_words_with_soft_hard_sign_and_yery_are_skipped(self) -> None:
        text = "объяснить мальчик быстрый подъезд"
        for seed in range(30):
            self.assertEqual(self.mix(text, seed), text)

    def test_uppercase_first_letter_is_kept(self) -> None:
        seen_prefix = False
        for seed in range(80):
            mixed = self.mix("Привет", seed)
            if re.match(r"^[A-Za-z]", mixed):
                seen_prefix = True
                self.assertTrue(mixed[0].isupper(), mixed)  # "Priвет", never "priвет"
        self.assertTrue(seen_prefix)

    def test_all_caps_word_stays_all_caps(self) -> None:
        for seed in range(40):
            mixed = self.mix("ПРИВЕТ", seed)
            self.assertEqual(mixed, mixed.upper())

    def test_length_limit_is_respected(self) -> None:
        text = "щука жужжит яблоко"
        for seed in range(60):
            mixed = self.mix(text, seed, max_length=len(text))
            self.assertLessEqual(len(mixed), len(text))
        grown = [self.mix(text, seed, max_length=400) for seed in range(60)]
        self.assertTrue(any(len(m) > len(text) for m in grown))  # transliteration really can lengthen

    def test_generate_respects_max_output_length_after_mixing(self) -> None:
        proc_cfg = ProcessorSettings(
            system_prompt="s", user_prompt_template="{text}", max_output_length=40, latin_mix_probability=1.0,
            drop_trailing_period_probability=0.0, lowercase_initial_probability=0.0,
        )
        proc = DeepSeekProcessor(DeepSeekSettings(api_key="k"), proc_cfg, rng=random.Random(5))
        answer = "щука жужжит яблоко щавель шершень " * 3
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = answer
        proc._client.chat.completions.create = AsyncMock(return_value=response)
        for _ in range(30):
            text = asyncio.run(proc.generate(_item("Привет", "ru")))
            self.assertLessEqual(len(text), 40)


if __name__ == "__main__":
    unittest.main()
