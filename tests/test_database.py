import asyncio
import tempfile
import unittest
from pathlib import Path

from database import REPLY_UNAVAILABLE_MARK, REPLY_UNAVAILABLE_TTL_SECONDS, Database, RecordNotFoundError


class TestDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.db = Database(self.db_path)
        await self.db.connect()
        await self.db.init_schema()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp_dir.cleanup()

    async def test_upsert_and_acquire_node(self):
        node = await self.db.upsert_test_node("youtube", "https://www.youtube.com", max_slots=2)
        self.assertEqual(node.name, "youtube")
        self.assertEqual(node.status, "idle")

        acquired = await self.db.acquire_test_node(node.id)
        self.assertIsNotNone(acquired)
        self.assertEqual(acquired.active_slots, 1)
        self.assertEqual(acquired.status, "busy")

        # Second acquire should succeed because max_slots = 2
        acquired2 = await self.db.acquire_test_node(node.id)
        self.assertIsNotNone(acquired2)
        self.assertEqual(acquired2.active_slots, 2)

        # Third acquire should fail (capacity exhausted)
        acquired3 = await self.db.acquire_test_node(node.id)
        self.assertIsNone(acquired3)

        # Release with cooldown
        await self.db.release_test_node(node.id, cooldown_seconds=60.0)
        rel_node = await self.db.get_test_node(node.id)
        self.assertEqual(rel_node.active_slots, 1)

    async def test_payload_lifecycle_and_deduplication(self):
        node = await self.db.upsert_test_node("yt", "https://www.youtube.com")
        source_ref = "dQw4w9WgXcQ#parent1#reply2"

        # Initially not replied to
        self.assertFalse(await self.db.has_replied_to(source_ref))

        payload = await self.db.create_payload(
            node_id=node.id,
            text="Natural reply text",
            language="en",
            source_ref=source_ref,
            source_text="Comment under comment text",
        )
        self.assertEqual(payload.status, "pending")

        # Now has_replied_to should return True (pending)
        self.assertTrue(await self.db.has_replied_to(source_ref))

        # Retrieve by source ref
        retrieved = await self.db.get_payload_by_source_ref(source_ref)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.id, payload.id)

        # Mark submitted
        submitted = await self.db.mark_payload_submitted(payload.id, remote_id="yt_sub_123")
        self.assertEqual(submitted.status, "submitted")
        self.assertEqual(submitted.remote_id, "yt_sub_123")
        self.assertTrue(await self.db.has_replied_to(source_ref))

        # Mark validated
        validated = await self.db.mark_payload_validated(payload.id)
        self.assertEqual(validated.status, "validated")
        self.assertTrue(await self.db.has_replied_to(source_ref))

    async def test_reserve_payload_and_update_text(self):
        node = await self.db.upsert_test_node("yt", "https://www.youtube.com")
        source_ref = "vid999#p1#sub_atomic"

        # Initially unreserved
        self.assertFalse(await self.db.has_replied_to(source_ref))

        # Atomically reserve before LLM generation
        res1 = await self.db.reserve_payload(
            node_id=node.id,
            source_ref=source_ref,
            language="ru",
            source_text="Comment text",
        )
        self.assertIsNotNone(res1)
        self.assertEqual(res1.status, "pending")
        self.assertEqual(res1.source_ref, source_ref)
        self.assertEqual(res1.text, "")

        # Concurrent attempt to reserve the same sub-comment must return None
        res2 = await self.db.reserve_payload(
            node_id=node.id,
            source_ref=source_ref,
            language="ru",
            source_text="Comment text",
        )
        self.assertIsNone(res2)
        self.assertTrue(await self.db.has_replied_to(source_ref))

        # If LLM generation fails, mark failed
        failed_payload = await self.db.mark_payload_failed(res1.id, "LLM timeout")
        self.assertEqual(failed_payload.status, "failed")
        # Sub-comment is now free to be picked up again
        self.assertFalse(await self.db.has_replied_to(source_ref))

        # Retry reservation succeeds
        res3 = await self.db.reserve_payload(
            node_id=node.id,
            source_ref=source_ref,
            language="ru",
            source_text="Comment text",
        )
        self.assertIsNotNone(res3)

        # Update text once generation completes
        updated = await self.db.update_payload_text(res3.id, "Generated reply text")
        self.assertEqual(updated.text, "Generated reply text")
        self.assertEqual(updated.status, "pending")

        # Mark submitted
        submitted = await self.db.mark_payload_submitted(res3.id, "yt_remote_456")
        self.assertEqual(submitted.status, "submitted")
        self.assertEqual(submitted.text, "Generated reply text")
        self.assertTrue(await self.db.has_replied_to(source_ref))

        # Updating text on a submitted payload must raise RuntimeError
        with self.assertRaises(RuntimeError):
            await self.db.update_payload_text(res3.id, "New text")

        # Updating text with empty string must raise ValueError
        with self.assertRaises(ValueError):
            await self.db.update_payload_text(res3.id, "   ")

        # Empty source_ref in reserve_payload must raise ValueError
        with self.assertRaises(ValueError):
            await self.db.reserve_payload(source_ref="  ", language="ru")

        # Test stale pending recovery:
        stale_ref = "vid999#p1#stale_sub"
        stale_res = await self.db.reserve_payload(source_ref=stale_ref, language="en")
        self.assertIsNotNone(stale_res)
        # Immediate retry without timeout returns None
        self.assertIsNone(await self.db.reserve_payload(source_ref=stale_ref, language="en"))
        # But if stale_pending_seconds = 0.0 (simulating expired reservation from crashed worker)
        recovered_res = await self.db.reserve_payload(source_ref=stale_ref, language="en", stale_pending_seconds=0.0)
        self.assertIsNotNone(recovered_res)
        self.assertNotEqual(stale_res.id, recovered_res.id)
        # Prior stale payload should now be marked failed
        old_p = await self.db.get_payload(stale_res.id)
        self.assertIsNotNone(old_p)
        self.assertEqual(old_p.status, "failed")
        self.assertIn("stale pending", old_p.error or "")

    async def test_reply_unavailable_target_is_skipped_by_later_runs(self):
        """A comment YouTube gives no Reply button is remembered: not reserved again, counted as handled."""
        node = await self.db.upsert_test_node("yt-no-reply", "https://example.com")
        ref = "vid#nobutton"
        first = await self.db.reserve_payload(node_id=node.id, source_ref=ref, language="en", source_text="t")
        self.assertIsNotNone(first)
        await self.db.mark_payload_failed(first.id, f"{REPLY_UNAVAILABLE_MARK}: ReplyUnavailableError: hidden")
        self.assertTrue(await self.db.is_reply_unavailable(ref))
        self.assertTrue(await self.db.has_replied_to(ref))
        self.assertIsNone(await self.db.reserve_payload(node_id=node.id, source_ref=ref, language="en"))

        # an ordinary failure stays retryable
        other = await self.db.reserve_payload(node_id=node.id, source_ref="vid#ordinary", language="en")
        await self.db.mark_payload_failed(other.id, "SubmissionError: could not locate Reply button on sub-comment")
        self.assertFalse(await self.db.is_reply_unavailable("vid#ordinary"))
        self.assertIsNotNone(await self.db.reserve_payload(node_id=node.id, source_ref="vid#ordinary", language="en"))

    async def test_mark_reply_unavailable_without_a_reservation(self):
        node = await self.db.upsert_test_node("yt-no-reply-2", "https://example.com")
        await self.db.mark_reply_unavailable(node_id=node.id, source_ref="vid#direct", language="ru", source_text="x", reason="hidden")
        self.assertTrue(await self.db.is_reply_unavailable("vid#direct"))
        self.assertIsNone(await self.db.reserve_payload(node_id=node.id, source_ref="vid#direct", language="ru"))
        with self.assertRaises(ValueError):
            await self.db.mark_reply_unavailable(source_ref="  ")

    async def test_reply_unavailable_mark_expires(self):
        node = await self.db.upsert_test_node("yt-no-reply-3", "https://example.com")
        await self.db.mark_reply_unavailable(node_id=node.id, source_ref="vid#old", reason="hidden")
        async with self.db._transaction() as conn:
            await conn.execute("UPDATE payloads SET created_at = created_at - ? WHERE source_ref = 'vid#old'", (REPLY_UNAVAILABLE_TTL_SECONDS + 60,))
        self.assertFalse(await self.db.is_reply_unavailable("vid#old"))
        self.assertIsNotNone(await self.db.reserve_payload(node_id=node.id, source_ref="vid#old", language="en"))

    async def test_queue_lifecycle(self):
        node = await self.db.upsert_test_node("yt", "https://www.youtube.com")
        payload = await self.db.create_payload(
            node_id=node.id,
            text="Test payload",
            language="ru",
        )

        item = await self.db.enqueue("submit", payload.id, node_id=node.id, max_attempts=2)
        self.assertEqual(item.status, "queued")
        self.assertEqual(item.attempts, 0)

        # Claim item
        claimed = await self.db.claim_queue_item(queue_id=item.id)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.status, "running")
        self.assertEqual(claimed.attempts, 1)

        # Reschedule with error
        rescheduled = await self.db.reschedule_queue_item(claimed.id, "temporary network glitch", delay_seconds=0)
        self.assertEqual(rescheduled.status, "queued")

        # Claim second attempt
        claimed2 = await self.db.claim_queue_item(queue_id=item.id)
        self.assertIsNotNone(claimed2)
        self.assertEqual(claimed2.attempts, 2)

        # Second reschedule should fail because max_attempts=2
        exhausted = await self.db.reschedule_queue_item(claimed2.id, "persistent error", delay_seconds=0)
        self.assertEqual(exhausted.status, "failed")

    async def test_stats(self):
        stats = await self.db.stats()
        self.assertIn("db_path", stats)
        self.assertIn("nodes", stats)
        self.assertIn("payloads", stats)
        self.assertIn("queue", stats)

    async def test_concurrent_reservation_stress(self):
        """Stress-test concurrent workers simultaneously reserving payloads against SQLite."""
        async def worker(db_path: Path, worker_id: int, sub_comment_ids: list[str]) -> list[str]:
            worker_db = Database(db_path)
            await worker_db.connect()
            await worker_db.init_schema()
            acquired = []
            try:
                for cid in sub_comment_ids:
                    p = await worker_db.reserve_payload(source_ref=cid, language="en")
                    if p is not None:
                        acquired.append(cid)
                        await asyncio.sleep(0.005)
                        await worker_db.update_payload_text(p.id, f"Worker {worker_id} reply")
                        await worker_db.mark_payload_submitted(p.id, f"remote_{worker_id}_{cid}")
            finally:
                await worker_db.close()
            return acquired

        sub_comments = [f"stress_vid#p1#sc_{i}" for i in range(15)]
        results = await asyncio.gather(
            worker(self.db_path, 1, sub_comments),
            worker(self.db_path, 2, sub_comments),
            worker(self.db_path, 3, sub_comments),
        )
        all_acquired = [cid for w in results for cid in w]
        self.assertEqual(len(all_acquired), 15)
        self.assertEqual(len(set(all_acquired)), 15)

    async def test_activity_log_crud_and_counting(self):
        from database import ActivityLogCreate

        # Initially empty
        self.assertEqual(await self.db.count_activity_logs(), 0)
        self.assertEqual(await self.db.count_successful_replies_for_video("vid_abc"), 0)

        # 1. Log live success
        entry1 = await self.db.log_activity(
            ActivityLogCreate(
                video_id="vid_abc",
                video_title="Awesome Video",
                video_url="https://www.youtube.com/watch?v=vid_abc",
                target_id="vid_abc#c1",
                target_author="@user1",
                target_text="Awesome comment",
                reply_text="Thank you!",
                mode="live",
                status="success",
                duration_sec=2.5,
            )
        )
        self.assertEqual(entry1.id, 1)
        self.assertEqual(entry1.video_id, "vid_abc")
        self.assertEqual(entry1.status, "success")
        self.assertEqual(entry1.mode, "live")
        self.assertIsNotNone(entry1.created_at)

        # 2. Log dry_run simulated
        entry2 = await self.db.log_activity(
            ActivityLogCreate(
                video_id="vid_abc",
                target_id="vid_abc#c2",
                target_author="@user2",
                target_text="Second comment",
                reply_text="Simulated reply",
                mode="dry_run",
                status="simulated",
                duration_sec=1.1,
            )
        )
        self.assertEqual(entry2.id, 2)
        self.assertEqual(entry2.status, "simulated")

        # 3. Log failed
        entry3 = await self.db.log_activity(
            ActivityLogCreate(
                video_id="vid_abc",
                target_id="vid_abc#c3",
                target_text="Failed comment",
                mode="live",
                status="failed",
                error="Timeout error",
            )
        )
        self.assertEqual(entry3.status, "failed")

        # Check total count and video counts
        self.assertEqual(await self.db.count_activity_logs(), 3)
        self.assertEqual(await self.db.count_activity_logs("success"), 1)
        self.assertEqual(await self.db.count_activity_logs("simulated"), 1)
        self.assertEqual(await self.db.count_activity_logs("failed"), 1)

        # count_successful_replies_for_video
        # Default includes success and simulated
        self.assertEqual(await self.db.count_successful_replies_for_video("vid_abc"), 2)
        self.assertEqual(await self.db.count_successful_replies_for_video("vid_abc", status="success"), 1)
        self.assertEqual(await self.db.count_successful_replies_for_video("other_vid"), 0)

    async def test_activity_log_pagination_and_clear(self):
        from database import ActivityLogCreate

        # Seed 5 records
        for i in range(1, 6):
            await self.db.log_activity(
                ActivityLogCreate(
                    video_id=f"vid_{i}",
                    target_id=f"target_{i}",
                    status="success" if i % 2 == 1 else "failed",
                    mode="live",
                )
            )

        # Test pagination: page 1 of size 2 -> records 5 and 4 (newest first)
        page1 = await self.db.list_activity_logs(page=1, page_size=2)
        self.assertEqual(len(page1), 2)
        self.assertEqual(page1[0].id, 5)
        self.assertEqual(page1[1].id, 4)

        # page 2 of size 2 -> records 3 and 2
        page2 = await self.db.list_activity_logs(page=2, page_size=2)
        self.assertEqual(len(page2), 2)
        self.assertEqual(page2[0].id, 3)
        self.assertEqual(page2[1].id, 2)

        # Filter by status: only "success" (records 1, 3, 5 -> 3 records)
        succ = await self.db.list_activity_logs(page=1, page_size=10, status_filter="success")
        self.assertEqual(len(succ), 3)
        self.assertTrue(all(e.status == "success" for e in succ))

        # Clear activity log
        deleted = await self.db.clear_activity_log()
        self.assertEqual(deleted, 5)
        self.assertEqual(await self.db.count_activity_logs(), 0)
        self.assertEqual(len(await self.db.list_activity_logs()), 0)

    async def test_schema_migration_from_v1_to_v2(self):
        import aiosqlite
        from database import ActivityLogCreate

        v1_db_path = Path(self.temp_dir.name) / "v1_legacy.db"
        # Simulate legacy v1 database (prior schema without activity_log table)
        async with aiosqlite.connect(v1_db_path) as conn:
            await conn.executescript(
                """
                CREATE TABLE test_nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    base_url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'idle',
                    max_slots INTEGER NOT NULL DEFAULT 1,
                    active_slots INTEGER NOT NULL DEFAULT 0,
                    cooldown_until REAL,
                    last_used_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE payloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id INTEGER,
                    source_ref TEXT,
                    source_text TEXT,
                    text TEXT NOT NULL,
                    language TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    remote_id TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    submitted_at REAL,
                    validated_at REAL
                );
                INSERT INTO test_nodes (name, base_url, status, max_slots, active_slots, created_at, updated_at)
                VALUES ('legacy_node', 'https://youtube.com', 'idle', 1, 0, 1000.0, 1000.0);
                INSERT INTO payloads (text, language, status, created_at)
                VALUES ('Legacy payload text', 'en', 'pending', 1000.0);
                PRAGMA user_version = 1;
                """
            )
            await conn.commit()

        # Connect with Database and ensure init_schema migrates to v2
        db_migrated = Database(v1_db_path)
        await db_migrated.connect()
        try:
            await db_migrated.init_schema()

            # Verify user_version is bumped to 2
            cursor = await db_migrated.connection.execute("PRAGMA user_version")
            row = await cursor.fetchone()
            await cursor.close()
            self.assertEqual(int(row[0]), 2)

            # Verify legacy data preserved
            nodes = await db_migrated.list_test_nodes()
            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0].name, "legacy_node")

            # Verify activity_log table exists and is operational
            entry = await db_migrated.log_activity(
                ActivityLogCreate(
                    video_id="migrated_vid",
                    target_id="migrated_cid",
                    status="success",
                    mode="live",
                )
            )
            self.assertIsNotNone(entry.id)
            self.assertEqual(await db_migrated.count_activity_logs(), 1)

            # Verify stats() works without OperationalError
            stats = await db_migrated.stats()
            self.assertIn("activity", stats)
            self.assertEqual(stats["activity"].get("success"), 1)
        finally:
            await db_migrated.close()


if __name__ == "__main__":
    unittest.main()

