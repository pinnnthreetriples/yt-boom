import random
import unittest
from unittest.mock import MagicMock
from config import DeepSeekSettings, ProcessorSettings
from modules.collector import CollectedItem
from modules.processor import DeepSeekProcessor, ProcessorError, normalize_text


class TestProcessor(unittest.TestCase):
    def test_normalize_text_em_dashes_and_guillemets(self):
        text = "Это пример — с тире и – коротким тире, а также «кавычки»."
        res = normalize_text(text, drop_trailing_period_probability=0.0, lowercase_initial_probability=0.0)
        self.assertNotIn("—", res)
        self.assertNotIn("–", res)
        self.assertNotIn("«", res)
        self.assertNotIn("»", res)
        self.assertIn("-", res)
        self.assertIn('"', res)

    def test_normalize_text_drop_trailing_period(self):
        text = "Hello world."
        # With prob=1.0, trailing period must be removed
        res_drop = normalize_text(text, drop_trailing_period_probability=1.0, lowercase_initial_probability=0.0)
        self.assertEqual(res_drop, "Hello world")

        # With prob=0.0, trailing period is kept
        res_keep = normalize_text(text, drop_trailing_period_probability=0.0, lowercase_initial_probability=0.0)
        self.assertEqual(res_keep, "Hello world.")

        # Ellipsis should NOT be stripped
        ellipsis_text = "To be continued..."
        res_ellipsis = normalize_text(ellipsis_text, drop_trailing_period_probability=1.0)
        self.assertTrue(res_ellipsis.endswith("..."))

    def test_normalize_text_lowercase_initial(self):
        text = "Great video."
        # With prob=1.0, first letter must be lowercased
        res_lower = normalize_text(text, lowercase_initial_probability=1.0, drop_trailing_period_probability=0.0)
        self.assertEqual(res_lower, "great video.")

        # With prob=0.0, preserved
        res_keep = normalize_text(text, lowercase_initial_probability=0.0, drop_trailing_period_probability=0.0)
        self.assertEqual(res_keep, "Great video.")

    def test_normalize_text_empty_and_whitespace(self):
        self.assertEqual(normalize_text(""), "")
        self.assertEqual(normalize_text("   \n\t  "), "")

    def test_normalize_text_sentence_truncation(self):
        # 3 sentences truncated to 2
        text_3 = "First sentence. Second sentence! Third sentence?"
        res_3 = normalize_text(text_3, drop_trailing_period_probability=0.0, lowercase_initial_probability=0.0)
        self.assertEqual(res_3, "First sentence. Second sentence!")

        # 4 sentences truncated to 1
        text_4 = "First sentence. Second sentence. Third. Fourth."
        res_1 = normalize_text(text_4, max_sentences=1, drop_trailing_period_probability=0.0, lowercase_initial_probability=0.0)
        self.assertEqual(res_1, "First sentence.")

        # 1 sentence preserved
        text_1 = "Only one sentence."
        res_single = normalize_text(text_1, drop_trailing_period_probability=0.0, lowercase_initial_probability=0.0)
        self.assertEqual(res_single, "Only one sentence.")

        # Text without end punctuation preserved
        text_no_punct = "Just a short fragment without ending punctuation"
        res_no_punct = normalize_text(text_no_punct, drop_trailing_period_probability=0.0, lowercase_initial_probability=0.0)
        self.assertEqual(res_no_punct, "Just a short fragment without ending punctuation")

        # Invalid max_sentences raises ValueError
        with self.assertRaises(ValueError):
            normalize_text("Hello world.", max_sentences=0)
        with self.assertRaises(ValueError):
            normalize_text("Hello world.", max_sentences=-5)
        with self.assertRaises(ValueError):
            normalize_text("Hello world.", max_sentences="two")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            normalize_text("Hello world.", max_sentences=True)  # type: ignore[arg-type]

        # Multi-sentence truncation with ellipsis
        ellipsis_multi = "First idea... Second thought! Third concept? Fourth point."
        res_ellipsis_multi = normalize_text(ellipsis_multi, max_sentences=2, drop_trailing_period_probability=0.0, lowercase_initial_probability=0.0)
        self.assertEqual(res_ellipsis_multi, "First idea... Second thought!")

    def test_deepseek_processor_prompt_formatting_with_parent_context(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        api_cfg = DeepSeekSettings(api_key="mock_key")
        proc_cfg = ProcessorSettings(
            system_prompt="Test system prompt.",
            user_prompt_template="Parent: {parent_text} (by {parent_author})\nSub: {text} (by {author})\nLang: {language}",
            drop_trailing_period_probability=1.0,
            lowercase_initial_probability=0.0,
        )
        item = CollectedItem(
            endpoint="youtube:dQw4w9WgXcQ",
            external_id="v1#p1#c1",
            text="Sub-comment text",
            language="en",
            confidence=0.9,
            parent_text="Parent comment text",
            parent_author="OriginalAuthor",
            author="UserA",
            is_reply=True,
        )
        proc = DeepSeekProcessor(api_cfg, proc_cfg)

        with patch.object(proc._client.chat.completions, "create", new_callable=AsyncMock) as mock_create:
            mock_choice = MagicMock()
            mock_choice.message.content = "Awesome response."
            mock_resp = MagicMock()
            mock_resp.choices = [mock_choice]
            mock_create.return_value = mock_resp

            result = asyncio.run(proc.generate(item))
            self.assertEqual(result, "Awesome response")

            mock_create.assert_called_once()
            call_kwargs = mock_create.call_args.kwargs
            messages = call_kwargs["messages"]
            self.assertEqual(messages[0]["role"], "system")
            self.assertEqual(messages[0]["content"], "Test system prompt.")
            self.assertEqual(messages[1]["role"], "user")
            self.assertIn("Parent: Parent comment text (by OriginalAuthor)", messages[1]["content"])
            self.assertIn("Sub: Sub-comment text (by UserA)", messages[1]["content"])
            self.assertIn("Lang: EN", messages[1]["content"])

    def test_processor_empty_input_raises(self):
        import asyncio

        api_cfg = DeepSeekSettings(api_key="mock_key")
        proc_cfg = ProcessorSettings(
            system_prompt="System",
            user_prompt_template="User: {text}",
        )
        proc = DeepSeekProcessor(api_cfg, proc_cfg)
        item = CollectedItem(
            endpoint="test",
            external_id="1",
            text="   ",
            language="en",
            confidence=0.9,
        )
        with self.assertRaises(ProcessorError):
            asyncio.run(proc.generate(item))

    def test_processor_styles_in_prompt_formatting(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        api_cfg = DeepSeekSettings(api_key="mock_key")
        proc_cfg = ProcessorSettings(
            system_prompt="Test sys",
            user_prompt_template="Comment: {text}\nTone: {style}",
            styles=["humorous, witty remark or light banter"],
            drop_trailing_period_probability=0.0,
            lowercase_initial_probability=0.0,
        )
        proc = DeepSeekProcessor(api_cfg, proc_cfg)
        item = CollectedItem(
            endpoint="yt",
            external_id="vid#c1",
            text="Very cool video!",
            language="en",
            confidence=0.95,
        )

        with patch.object(proc._client.chat.completions, "create", new_callable=AsyncMock) as mock_create:
            mock_choice = MagicMock()
            mock_choice.message.content = "Haha thanks!"
            mock_resp = MagicMock(choices=[mock_choice])
            mock_create.return_value = mock_resp

            res = asyncio.run(proc.generate(item))
            self.assertEqual(res, "Haha thanks!")
            call_kwargs = mock_create.call_args.kwargs
            user_msg = call_kwargs["messages"][1]["content"]
            self.assertIn("Tone: humorous, witty remark or light banter", user_msg)

    def _paraphrase(self, text, *replies, error=None):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        proc = DeepSeekProcessor(
            DeepSeekSettings(api_key="mock_key"),
            ProcessorSettings(system_prompt="sys", user_prompt_template="{text}"),
        )

        def resp(content):
            return MagicMock(choices=[MagicMock(message=MagicMock(content=content))])

        async def run():
            with patch.object(proc._client.chat.completions, "create", new_callable=AsyncMock) as create:
                if error:
                    create.side_effect = error
                else:
                    create.side_effect = [resp(r) for r in replies]
                out = [await proc.paraphrase(text) for _ in replies or [None]]
                return out, create

        return asyncio.run(run())

    def test_paraphrase_returns_new_wording_and_avoids_recent_variants(self):
        out, create = self._paraphrase("Согласен с @sam, отличное видео", "«Полностью поддерживаю @sam, видео огонь»", "Тоже так думаю @sam, классное видео")
        self.assertEqual(out[0], "Полностью поддерживаю @sam, видео огонь")  # wrapping quotes stripped
        self.assertEqual(len(set(out)), 2)
        second_prompt = create.call_args_list[1].kwargs["messages"][1]["content"]
        self.assertIn("Полностью поддерживаю @sam, видео огонь", second_prompt)  # told not to repeat it

    def test_paraphrase_falls_back_to_template_on_bad_result(self):
        text = "Согласен с @sam, отличное видео"
        self.assertEqual(self._paraphrase(text, "Отличное видео, согласен")[0][0], text)  # mention dropped
        self.assertEqual(self._paraphrase(text, "   ")[0][0], text)  # empty
        self.assertEqual(self._paraphrase(text, error=RuntimeError("boom"))[0][0], text)  # API failure

    def test_should_paraphrase_needs_switch_and_key(self):
        from types import SimpleNamespace
        from modules.processor import should_paraphrase

        on, off = SimpleNamespace(paraphrase_custom=True), SimpleNamespace(paraphrase_custom=False)
        self.assertTrue(should_paraphrase(SimpleNamespace(api_key="k"), on))
        self.assertFalse(should_paraphrase(SimpleNamespace(api_key=""), on))
        self.assertFalse(should_paraphrase(SimpleNamespace(api_key="k"), off))

    def test_resolve_reply_text_custom_string_and_placeholders(self):
        from modules.processor import resolve_reply_text

        item = CollectedItem(
            endpoint="yt",
            external_id="vid#c1#c2",
            text="Love your analysis",
            language="en",
            confidence=0.9,
            author="sam_coder",
            parent_author="creator_pro",
        )

        # 1. Direct custom reply without placeholders
        res1 = resolve_reply_text(item, custom_reply="Thanks for tuning in!")
        self.assertEqual(res1, "Thanks for tuning in!")

        # 2. Custom reply with placeholders
        res2 = resolve_reply_text(
            item,
            custom_reply="Totally agree with: {text} ({language})",
        )
        self.assertEqual(res2, "Totally agree with: Love your analysis (EN)")

        # 3. Custom reply picked from custom_replies list
        templates = ["Awesome point!", "Appreciate the comment!"]
        mock_rng = MagicMock()
        mock_rng.choice.return_value = templates[1]
        res3 = resolve_reply_text(item, custom_replies=templates, rng=mock_rng)
        self.assertEqual(res3, "Appreciate the comment!")

        # 4. Raises error when neither custom_reply nor custom_replies provided
        with self.assertRaises(ProcessorError):
            resolve_reply_text(item, custom_reply=None, custom_replies=[])


if __name__ == "__main__":
    unittest.main()
