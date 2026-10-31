import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from database import (
    AccountCreate,
    AccountRecord,
    Database,
    RecordNotFoundError,
    VideoPoolItem,
)


class TestMultiAccountDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_multi.db"
        self.db = Database(self.db_path)
        await self.db.connect()
        await self.db.init_schema()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp_dir.cleanup()

    # ----------------------------------------------------------- Accounts CRUD

    async def test_upsert_and_get_account(self):
        # 1. Insert new account
        acc1 = await self.db.upsert_account(
            name="account_alpha",
            session_path="./test_sessions/alpha",
            proxy_url="http://proxy1:8080",
            is_active=True,
        )
        self.assertIsInstance(acc1, AccountRecord)
        self.assertEqual(acc1.name, "account_alpha")
        self.assertEqual(acc1.session_path, "./test_sessions/alpha")
        self.assertEqual(acc1.proxy_url, "http://proxy1:8080")
        self.assertTrue(acc1.is_active)
        self.assertEqual(acc1.total_replies, 0)
        self.assertIsNone(acc1.last_used_at)
        self.assertIsNotNone(acc1.created_at)

        # 2. Get account by ID (int)
        by_id = await self.db.get_account(acc1.id)
        self.assertIsNotNone(by_id)
        self.assertEqual(by_id.id, acc1.id)
        self.assertEqual(by_id.name, "account_alpha")

        # 3. Get account by Name (str)
        by_name = await self.db.get_account("account_alpha")
        self.assertIsNotNone(by_name)
        self.assertEqual(by_name.id, acc1.id)

        # 4. Get account by string representation of ID
        by_str_id = await self.db.get_account(str(acc1.id))
        self.assertIsNotNone(by_str_id)
        self.assertEqual(by_str_id.id, acc1.id)

        # 5. Non-existent returns None
        self.assertIsNone(await self.db.get_account(9999))
        self.assertIsNone(await self.db.get_account("non_existent_account"))

        # 6. Upsert same account name with updated properties
        acc1_updated = await self.db.upsert_account(
            name="account_alpha",
            session_path="./test_sessions/alpha_v2",
            proxy_url="http://proxy2:8080",
            is_active=False,
        )
        self.assertEqual(acc1_updated.id, acc1.id)
        self.assertEqual(acc1_updated.session_path, "./test_sessions/alpha_v2")
        self.assertEqual(acc1_updated.proxy_url, "http://proxy2:8080")
        self.assertFalse(acc1_updated.is_active)
        # created_at must remain unchanged
        self.assertEqual(acc1_updated.created_at, acc1.created_at)

    async def test_upsert_account_validation(self):
        with self.assertRaises(ValueError):
            await self.db.upsert_account(name="  ", session_path="./path")
        with self.assertRaises(ValueError):
            await self.db.upsert_account(name="valid_name", session_path="  ")

    async def test_list_active_accounts(self):
        # Create active and inactive accounts
        a1 = await self.db.upsert_account("active_1", "./path1", is_active=True)
        a2 = await self.db.upsert_account("inactive_1", "./path2", is_active=False)
        a3 = await self.db.upsert_account("active_2", "./path3", is_active=True)

        active = await self.db.list_active_accounts()
        self.assertEqual(len(active), 2)
        active_names = [a.name for a in active]
        self.assertIn("active_1", active_names)
        self.assertIn("active_2", active_names)
        self.assertNotIn("inactive_1", active_names)

    async def test_increment_account_stats(self):
        acc = await self.db.upsert_account("stat_tester", "./stat_path")
        self.assertEqual(acc.total_replies, 0)
        self.assertIsNone(acc.last_used_at)

        # Increment replies
        await self.db.increment_account_stats(acc.id, replies=2)
        u1 = await self.db.get_account(acc.id)
        self.assertEqual(u1.total_replies, 2)
        self.assertIsNotNone(u1.last_used_at)

        await self.db.increment_account_stats(acc.id, replies=1)
        u2 = await self.db.get_account(acc.id)
        self.assertEqual(u2.total_replies, 3)

        # Non-existent account raises RecordNotFoundError
        with self.assertRaises(RecordNotFoundError):
            await self.db.increment_account_stats(9999, replies=1)

        # Negative increments raise ValueError
        with self.assertRaises(ValueError):
            await self.db.increment_account_stats(acc.id, replies=-1)

    async def test_rename_account_keeps_session_and_stats(self):
        acc = await self.db.upsert_account("default", "./sessions/youtube", proxy_url="http://p:1")
        await self.db.increment_account_stats(acc.id, replies=2)
        await self.db.upsert_account("other", "./sessions/other")

        renamed = await self.db.rename_account("default", "  main@mail.ru  ")
        self.assertEqual(renamed.id, acc.id)
        self.assertEqual(renamed.name, "main@mail.ru")
        self.assertEqual(renamed.session_path, "./sessions/youtube")
        self.assertEqual(renamed.proxy_url, "http://p:1")
        self.assertEqual(renamed.total_replies, 2)
        self.assertIsNone(await self.db.get_account("default"))

        with self.assertRaises(ValueError):
            await self.db.rename_account("main@mail.ru", "other")  # duplicate
        with self.assertRaises(ValueError):
            await self.db.rename_account("main@mail.ru", "   ")  # empty
        with self.assertRaises(RecordNotFoundError):
            await self.db.rename_account("missing", "x")
        self.assertEqual((await self.db.rename_account("other", "other")).name, "other")  # same name is a no-op

    # -------------------------------------------------------------- Video Pool

    async def test_video_pool_lifecycle(self):
        # 1. Add video via standard watch URL
        v1 = await self.db.add_video_to_pool(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", comments_target=2
        )
        self.assertIsInstance(v1, VideoPoolItem)
        self.assertEqual(v1.video_id, "dQw4w9WgXcQ")
        self.assertEqual(v1.status, "pending")
        self.assertEqual(v1.comments_target, 2)
        self.assertEqual(v1.comments_done, 0)
        self.assertIsNone(v1.last_processed_at)

        # 2. Add video via shorts URL
        v2 = await self.db.add_video_to_pool(
            "https://www.youtube.com/shorts/12345678901", comments_target=1
        )
        self.assertEqual(v2.video_id, "12345678901")

        # 3. Add video via youtu.be URL
        v3 = await self.db.add_video_to_pool(
            "https://youtu.be/abcdefghijk?t=10", comments_target=3
        )
        self.assertEqual(v3.video_id, "abcdefghijk")

        # 4. Upsert existing video updates target
        v1_updated = await self.db.add_video_to_pool(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", comments_target=5
        )
        self.assertEqual(v1_updated.id, v1.id)
        self.assertEqual(v1_updated.comments_target, 5)

        # 5. Get next pool video -> should be v1
        next_vid = await self.db.get_next_pool_video()
        self.assertIsNotNone(next_vid)
        self.assertEqual(next_vid.id, v1.id)

        # 6. Update progress partial
        await self.db.update_pool_video_progress(v1.id, comments_added=2)
        v1_check = await self.db.get_next_pool_video()
        self.assertIsNotNone(v1_check)
        self.assertEqual(v1_check.id, v1.id)
        self.assertEqual(v1_check.status, "in_progress")
        self.assertEqual(v1_check.comments_done, 2)
        self.assertIsNotNone(v1_check.last_processed_at)

        # 7. Complete v1 explicitly
        await self.db.update_pool_video_progress(v1.id, completed=True)

        # 8. Next video should now be v2
        next_vid2 = await self.db.get_next_pool_video()
        self.assertIsNotNone(next_vid2)
        self.assertEqual(next_vid2.id, v2.id)

        # 9. Complete v2 by reaching target (target=1, comments_added=1)
        await self.db.update_pool_video_progress(v2.id, comments_added=1)

        # 10. Next video should now be v3
        next_vid3 = await self.db.get_next_pool_video()
        self.assertIsNotNone(next_vid3)
        self.assertEqual(next_vid3.id, v3.id)

        # Complete v3
        await self.db.update_pool_video_progress(v3.id, completed=True)

        # Pool is exhausted
        self.assertIsNone(await self.db.get_next_pool_video())

    async def test_video_pool_validation_and_errors(self):
        with self.assertRaises(ValueError):
            await self.db.add_video_to_pool("   ")
        with self.assertRaises(ValueError):
            await self.db.add_video_to_pool("https://youtube.com/watch?v=dQw4w9WgXcQ", comments_target=0)

        with self.assertRaises(RecordNotFoundError):
            await self.db.update_pool_video_progress(9999, comments_added=1)

        with self.assertRaises(ValueError):
            await self.db.update_pool_video_progress(1, comments_added=-1)

    # --------------------------------------------------------- Telemetry Stats

    async def test_stats_telemetry(self):
        await self.db.upsert_account("acc_stat1", "./p1", is_active=True)
        await self.db.upsert_account("acc_stat2", "./p2", is_active=False)
        await self.db.add_video_to_pool("https://www.youtube.com/watch?v=dQw4w9WgXcQ", comments_target=2)

        stats = await self.db.stats()
        self.assertIn("accounts", stats)
        self.assertIn("video_pool", stats)

        self.assertEqual(stats["accounts"].get("active"), 1)
        self.assertEqual(stats["accounts"].get("inactive"), 1)
        self.assertEqual(stats["video_pool"].get("pending"), 1)


if __name__ == "__main__":
    unittest.main()
