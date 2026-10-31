from __future__ import annotations

import asyncio
from pathlib import Path
import random
import tempfile
import unittest

from database import Database, VideoPoolItem
from modules.pool_manager import PoolManager


class TestPoolManager(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_pool.db"
        self.db = Database(self.db_path)
        await self.db.connect()
        await self.db.init_schema()
        self.manager = PoolManager(db=self.db, rng=random.Random(42))

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.temp_dir.cleanup()

    # ------------------------------------------------- Video Pool File Loading

    async def test_load_videos_from_file_with_edge_cases(self) -> None:
        file_path = Path(self.temp_dir.name) / "videos.txt"
        content = """
# Test Videos List
https://www.youtube.com/watch?v=dQw4w9WgXcQ

   
# Duplicate via short URL:
https://youtu.be/dQw4w9WgXcQ
# Valid Shorts URL:
https://www.youtube.com/shorts/12345678901
# Bare 11-char ID:
abcdefghijk
# Invalid lines:
not_a_valid_url
http://example.com/index.html
# Duplicate bare ID:
https://www.youtube.com/watch?v=abcdefghijk
"""
        file_path.write_text(content, encoding="utf-8")

        items = await self.manager.load_videos_from_file(file_path, comments_target=3)

        # Expected 3 unique valid videos: dQw4w9WgXcQ, 12345678901, abcdefghijk
        self.assertEqual(len(items), 3)
        video_ids = [item.video_id for item in items]
        self.assertEqual(video_ids, ["dQw4w9WgXcQ", "12345678901", "abcdefghijk"])
        for item in items:
            self.assertEqual(item.comments_target, 3)
            self.assertEqual(item.comments_done, 0)
            self.assertEqual(item.status, "pending")

        # Verify items are in DB
        first = await self.manager.get_next_video_task()
        self.assertIsNotNone(first)
        self.assertEqual(first.video_id, "dQw4w9WgXcQ")

    async def test_load_videos_from_file_not_found(self) -> None:
        missing_path = Path(self.temp_dir.name) / "missing_videos.txt"
        with self.assertRaises(FileNotFoundError):
            await self.manager.load_videos_from_file(missing_path)

    # ---------------------------- Video Task Progress & Status Delegation

    async def test_video_task_progress_and_delegation(self) -> None:
        item = await self.db.add_video_to_pool("https://www.youtube.com/watch?v=task_vid_11", comments_target=2)
        self.assertEqual(item.status, "pending")
        self.assertEqual(item.comments_done, 0)

        # Next task retrieves it
        task = await self.manager.get_next_video_task()
        self.assertIsNotNone(task)
        self.assertEqual(task.id, item.id)

        # Record progress +1
        await self.manager.record_video_progress(item.id, comments_added=1)
        progressed = await self.manager.get_next_video_task()
        self.assertIsNotNone(progressed)
        self.assertEqual(progressed.comments_done, 1)
        self.assertEqual(progressed.status, "in_progress")

        # Record completion
        await self.manager.record_video_progress(item.id, comments_added=1)
        no_more = await self.manager.get_next_video_task()
        self.assertIsNone(no_more)


if __name__ == "__main__":
    unittest.main()
