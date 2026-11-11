"""Latin letter mixing (`mix_latin`): applied by code to the finished reply, never asked from DeepSeek."""
from __future__ import annotations

import asyncio
import random
import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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

    def test_links_without_a_scheme_and_cyrillic_domains_stay_intact(self) -> None:
        text = "Смотри vk.com/рекомендации и привет.рф/страница, а ещё кино.онлайн интересно"
        for seed in range(80):
            mixed = self.mix(text, seed)
            for link in ("vk.com/рекомендации", "привет.рф/страница,", "кино.онлайн"):
                self.assertIn(link, mixed)
        self.assertEqual(self.mix("привет.рф"), "привет.рф")
        self.assertEqual(self.mix("vk.com/рекомендации"), "vk.com/рекомендации")

    def test_ukrainian_and_belarusian_text_is_never_mixed(self) -> None:
        for text in ("Дякую дуже гарно", "Привіт усім, чудове відео", "Це їжак і єнот", "Добры дзень, ўсім дзякуй"):
            for seed in range(30):
                self.assertEqual(self.mix(text, seed), text)

    def test_only_russian_messages_are_mixed(self) -> None:
        for text in ("Great video, Москва rocks", "Nice video, спасибо", "Thanks a lot, привет"):
            for seed in range(30):
                self.assertEqual(self.mix(text, seed), text)  # Cyrillic is not the majority of the letters
        russian = "Спасибо за подробный разбор, очень полезно"
        self.assertTrue(any(self.mix(russian, seed) != russian for seed in range(30)))

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


class TestParaphraseLanguageRule(unittest.TestCase):
    """The language rule of the paraphrase prompt follows the raw template, and says Russian only when it is Russian."""

    def system_prompt(self, text: str, source: str | None = None) -> str:
        proc = DeepSeekProcessor(
            DeepSeekSettings(api_key="k"), ProcessorSettings(system_prompt="s", user_prompt_template="{text}")
        )
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = "A completely different wording of it"
        with patch.object(proc._client.chat.completions, "create", new_callable=AsyncMock, return_value=response) as create:
            asyncio.run(proc.paraphrase(text, source=source))
        return create.call_args.kwargs["messages"][0]["content"]

    def test_russian_template_gets_the_russian_rule(self) -> None:
        self.assertIn("ONLY in Russian", self.system_prompt("Спасибо за подробный разбор"))
        # a Russian template stays Russian when an English comment was substituted into it
        self.assertIn(
            "ONLY in Russian", self.system_prompt("Спасибо за great video", source="Спасибо за {text}")
        )

    def test_ukrainian_template_does_not_get_the_russian_rule(self) -> None:
        for text in ("Дякую за відео, чудово", "Дякую дуже гарно"):
            prompt = self.system_prompt(text)
            self.assertNotIn("ONLY in Russian", prompt)
            self.assertIn("exact same language as the text", prompt)

    def test_english_template_with_a_russian_comment_does_not_get_the_russian_rule(self) -> None:
        prompt = self.system_prompt("Thanks for the comment: Отличное видео", source="Thanks for the comment: {text}")
        self.assertNotIn("ONLY in Russian", prompt)
        self.assertIn("exact same language as the text", prompt)
        self.assertIn("decided by the wording of the template itself", prompt)

    def test_forced_language_still_wins(self) -> None:
        proc = DeepSeekProcessor(
            DeepSeekSettings(api_key="k"),
            ProcessorSettings(system_prompt="s", user_prompt_template="{text}", reply_language="en"),
        )
        response = MagicMock()
        response.choices = [MagicMock()]
        response.choices[0].message.content = "Another way to say it"
        with patch.object(proc._client.chat.completions, "create", new_callable=AsyncMock, return_value=response) as create:
            asyncio.run(proc.paraphrase("Спасибо за разбор", source="Спасибо за разбор"))
        self.assertIn("translate it into English", create.call_args.kwargs["messages"][0]["content"])


if __name__ == "__main__":
    unittest.main()
