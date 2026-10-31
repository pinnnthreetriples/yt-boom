import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from config import AppConfig, BrowserSettings, CollectorEndpoint, CollectorSettings, DeepSeekSettings, LimitsSettings, ProcessorSettings, TargetSettings, ValidatorSettings
from database import Database
from modules.collector import CollectedItem
from modules.processor import DeepSeekProcessor
from modules.youtube_collector import YouTubeCollector, YouTubeSubComment
from modules.youtube_runner import YouTubeRunner


class TestEndToEndPipeline(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "e2e_test.db"
        self.db = Database(self.db_path)
        await self.db.connect()
        await self.db.init_schema()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp_dir.cleanup()

    async def test_full_reply_to_sub_comment_flow(self):
        # 1. Setup mock collector returning real-like sub-comment
        collector_settings = CollectorSettings(
            languages=["en", "ru"],
            min_text_length=5,
            endpoints=[CollectorEndpoint(name="test_yt", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")],
        )
        sub_comment = YouTubeSubComment(
            video_id="dQw4w9WgXcQ",
            comment_id="top1.sub2",
            parent_comment_id="top1",
            author="viewer42",
            text="This song never gets old, always brings back memories!",
            parent_author="music_fan",
            parent_text="Who is still listening in 2026?",
            permalink="https://www.youtube.com/watch?v=dQw4w9WgXcQ&lc=top1.sub2",
        )

        ext_id = f"{sub_comment.video_id}#{sub_comment.parent_comment_id}#{sub_comment.comment_id}"

        # 2. Check deduplication before processing
        self.assertFalse(await self.db.has_replied_to(ext_id))

        # 3. Simulate DeepSeek response generation
        api_cfg = DeepSeekSettings(api_key="mock_key")
        proc_cfg = ProcessorSettings(
            system_prompt="You are a friendly YouTube viewer replying to a comment.",
            user_prompt_template="Parent: {parent_text}\nSub: {text}\nLang: {language}",
        )
        item = CollectedItem(
            endpoint=f"youtube:{sub_comment.video_id}",
            external_id=ext_id,
            text=sub_comment.text,
            language="en",
            confidence=0.95,
            parent_text=sub_comment.parent_text,
            author=sub_comment.author,
            is_reply=True,
        )

        processor = DeepSeekProcessor(api_cfg, proc_cfg)
        with patch.object(processor._client.chat.completions, "create", new_callable=AsyncMock) as mock_create:
            mock_choice = MagicMock()
            mock_choice.message.content = "Totally agree, timeless classic."
            mock_resp = MagicMock()
            mock_resp.choices = [mock_choice]
            mock_create.return_value = mock_resp

            reply_text = await processor.generate(item)
            self.assertIn("totally agree, timeless classic", reply_text.lower())

        # 4. Save to Database
        node = await self.db.upsert_test_node("youtube", "https://www.youtube.com")
        payload = await self.db.create_payload(
            node_id=node.id,
            text=reply_text,
            language=item.language,
            source_ref=item.external_id,
            source_text=item.text,
        )
        self.assertEqual(payload.status, "pending")
        self.assertTrue(await self.db.has_replied_to(ext_id))

        # 5. Simulate browser runner
        browser_cfg = BrowserSettings(
            headless=True,
            session_root=self.root / "sessions",
            render_min_seconds=0.01,
            render_max_seconds=0.02,
            targets=[
                TargetSettings(
                    name="youtube",
                    base_url="https://www.youtube.com",
                    item_url_template="https://www.youtube.com/watch?v={external_id}",
                    submit_input_selector="#reply-dialog #contenteditable-root",
                    submit_button_selector="#reply-dialog #submit-button button",
                    confirmation_selector="ytd-comment-view-model",
                )
            ],
        )
        runner = YouTubeRunner(browser_cfg, browser_cfg.targets[0])

        with patch.object(runner, "_render", new_callable=AsyncMock), \
             patch.object(runner, "_scroll_to_comments", new_callable=AsyncMock), \
             patch.object(runner, "_expand_replies", new_callable=AsyncMock), \
             patch.object(runner, "_dismiss_consent", new_callable=AsyncMock), \
             patch.object(runner, "_submit_reply", new_callable=AsyncMock) as mock_submit:

            mock_submit.return_value = "yt_remote_sub_999"

            with patch("modules.youtube_runner.AsyncCamoufox") as mock_camoufox:
                mock_ctx = AsyncMock()
                mock_page = MagicMock()
                mock_page.url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&lc=top1.sub2"
                mock_page.goto = AsyncMock(return_value=MagicMock(status=200))
                mock_page.is_closed = MagicMock(return_value=False)
                mock_ctx.pages = [mock_page]
                mock_camoufox.return_value.__aenter__.return_value = mock_ctx

                res = await runner.run(payload.text, external_id=item.external_id, payload_id=payload.id)
                self.assertTrue(res.success)
                self.assertEqual(res.remote_id, "yt_remote_sub_999")

        # 6. Update database status
        submitted_payload = await self.db.mark_payload_submitted(payload.id, remote_id=res.remote_id)
        self.assertEqual(submitted_payload.status, "submitted")
        self.assertEqual(submitted_payload.remote_id, "yt_remote_sub_999")

        # 7. Check final stats
        stats = await self.db.stats()
        self.assertEqual(stats["payloads"]["submitted"], 1)


if __name__ == "__main__":
    unittest.main()
