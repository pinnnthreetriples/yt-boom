"""Channel profile: avatar files, pure menu logic, the draft/applied columns, the Studio editor and the wizard card."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

from config import BrowserSettings, TargetSettings
from database import UNSET, AccountRecord, Database, RecordNotFoundError, recent_name_changes
from modules import profile_manager
from modules.account_menu_logic import (
    applied_snapshot,
    clean_path_input,
    format_profile_summary,
    name_change_wait,
    name_changes_left,
    plural_changes,
    profile_action_label,
    profile_card_lines,
    profile_pending_fields,
    profile_status_label,
    validate_description,
    validate_nickname,
)
from modules.profile_manager import (
    AvatarError,
    ProfileEditor,
    ProfileResult,
    forget_avatar,
    import_avatar,
    validate_avatar,
)
from modules.wizard import _setup_accounts, _setup_profile, load_accounts
from tests.test_account_menu import MenuCase
from tests.tui_keys import DOWN, ENTER, ESC, SPACE

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def account(**overrides) -> AccountRecord:
    fields = dict(id=1, name="acc", session_path="s", created_at="2026-01-01T00:00:00+00:00", auth_status="authenticated")
    return AccountRecord(**{**fields, **overrides})


def applied(**values) -> str:
    return json.dumps(values, ensure_ascii=False)


class TestValidation(unittest.TestCase):
    def test_nickname(self):
        self.assertEqual(validate_nickname("  Bakyt \n Dev "), "Bakyt Dev")
        for bad in ("", "   ", "x" * 51, "a<b", "a>b"):
            with self.assertRaises(ValueError, msg=bad):
                validate_nickname(bad)
        self.assertEqual(validate_nickname("я" * 50), "я" * 50)

    def test_description(self):
        self.assertEqual(validate_description(["first  ", "", "second", ""]), "first\n\nsecond")
        self.assertEqual(validate_description(["-"]), "")
        self.assertEqual(validate_description(["x" * 1000]), "x" * 1000)
        with self.assertRaises(ValueError):
            validate_description(["x" * 1001])
        with self.assertRaises(ValueError):
            validate_description(["<b>bold</b>"])

    def test_clean_path_input_strips_quotes_and_powershell_prefix(self):
        for raw in ('"C:\\a b\\p.png"', "'C:\\a b\\p.png'", "& 'C:\\a b\\p.png'", "  C:\\a b\\p.png  "):
            self.assertEqual(clean_path_input(raw), "C:\\a b\\p.png", raw)

    def test_plural(self):
        self.assertEqual([plural_changes(n) for n in (1, 2, 5, 11, 21, 22)],
                         ["1 изменение", "2 изменения", "5 изменений", "11 изменений", "21 изменение", "22 изменения"])


class TestAvatarFiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_valid_files(self):
        self.assertEqual(validate_avatar(self.write("a.PNG", PNG))[1], ".png")
        self.assertEqual(validate_avatar(self.write("a.jpeg", JPEG))[1], ".jpg")

    def test_rejected_files(self):
        cases = {
            "missing": self.root / "nope.png",
            "wrong type": self.write("a.txt", b"hello"),
            "empty": self.write("e.png", b""),
            "wrong content": self.write("fake.png", b"not an image at all"),
            "mismatched content": self.write("j.png", JPEG),
        }
        for label, path in cases.items():
            with self.assertRaises(AvatarError, msg=label):
                validate_avatar(path)

    def test_too_big(self):
        big = self.write("big.png", PNG)
        with patch.object(profile_manager, "AVATAR_MAX_BYTES", 10), self.assertRaises(AvatarError):
            validate_avatar(big)

    def test_import_names_by_content_and_copies(self):
        store = self.root / "avatars"
        first = import_avatar(self.write("a.png", PNG), "me@example.com", store)
        again = import_avatar(self.write("b.png", PNG), "me@example.com", store)
        other = import_avatar(self.write("c.jpg", JPEG), "me@example.com", store)
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)
        self.assertTrue(Path(first).name.startswith("me_example.com-"))
        self.assertEqual(Path(first).read_bytes(), PNG)
        self.assertEqual(sorted(p.suffix for p in store.iterdir()), [".jpg", ".png"])  # no *.part left behind

    def test_accounts_with_the_same_sanitized_name_do_not_share_a_file(self):
        store = self.root / "avatars"
        picture = self.write("a.png", PNG)
        self.assertNotEqual(import_avatar(picture, "a b", store), import_avatar(picture, "a_b", store))

    def test_forget_deletes_only_inside_the_store_and_respects_keep(self):
        store = self.root / "avatars"
        kept = Path(import_avatar(self.write("a.png", PNG), "x", store))
        gone = Path(import_avatar(self.write("c.jpg", JPEG), "x", store))
        outside = self.write("outside.png", PNG)
        with patch.object(profile_manager, "AVATARS_DIR", store):
            forget_avatar(str(kept), keep=(str(kept),))
            forget_avatar(str(outside))
            forget_avatar(str(gone))
            forget_avatar(None)
        self.assertTrue(kept.exists())
        self.assertTrue(outside.exists())
        self.assertFalse(gone.exists())


class TestProfileState(unittest.TestCase):
    def test_pending_status_and_labels(self):
        empty = account()
        self.assertEqual(profile_pending_fields(empty), [])
        self.assertEqual(profile_status_label(empty), ("○ не задан", "dim"))
        self.assertEqual(profile_action_label(empty, "description"), "Добавить описание")
        self.assertEqual(profile_action_label(empty, "avatar"), "Добавить аватарку")

        draft = account(profile_name="Bakyt", profile_description="About", profile_avatar="p/a.png")
        self.assertEqual(profile_pending_fields(draft), ["name", "description", "avatar"])
        self.assertEqual(profile_status_label(draft)[1], "warn")
        self.assertEqual(profile_action_label(draft, "avatar"), "Изменить аватарку")

        live = account(profile_name="Bakyt", profile_applied=applied(name="Bakyt"))
        self.assertEqual(profile_pending_fields(live), [])
        self.assertEqual(profile_status_label(live), ("● применён", "ok"))

        edited = account(profile_name="New", profile_applied=applied(name="Bakyt"))
        self.assertEqual(profile_pending_fields(edited), ["name"])

    def test_cleared_description_is_a_pending_change(self):
        cleared = account(profile_description="", profile_applied=applied(description="old"))
        self.assertEqual(profile_pending_fields(cleared), ["description"])
        self.assertEqual(profile_card_lines(cleared)[1][1], "(очищено)")

    def test_broken_snapshot_is_ignored(self):
        self.assertEqual(applied_snapshot(account(profile_applied="{not json")), {})
        self.assertEqual(applied_snapshot(account(profile_applied="[1]")), {})
        self.assertEqual(applied_snapshot(account(profile_applied=applied(name="A", junk="x"))), {"name": "A"})

    def test_card_lines(self):
        stamp = (NOW - timedelta(days=3)).isoformat()
        acc = account(
            profile_name="Bakyt Dev", profile_description="line one\nline two", profile_avatar="profiles/avatars/a-1.png",
            profile_applied=applied(name="Bakyt Dev"), profile_applied_at=stamp,
        )
        lines = profile_card_lines(acc, NOW)
        self.assertEqual([line[0] for line in lines], ["Никнейм", "Описание", "Аватарка"])
        self.assertEqual(lines[0][1:], ("Bakyt Dev", "применён 3д назад", "dim"))
        self.assertEqual(lines[1][1:], ("line one…", "● не применено", "warn"))
        self.assertEqual(lines[2][1], "a-1.png")

    def test_name_change_limit(self):
        two = [(NOW - timedelta(days=10)).isoformat(), (NOW - timedelta(days=2)).isoformat()]
        limited = account(profile_name_changes=json.dumps(two))
        self.assertEqual(name_changes_left(limited, NOW), 0)
        wait = name_change_wait(limited, NOW)
        self.assertEqual(wait, timedelta(days=4))  # the older change leaves the 14-day window first

        one = account(profile_name_changes=json.dumps(two[1:]))
        self.assertEqual(name_changes_left(one, NOW), 1)
        self.assertIsNone(name_change_wait(one, NOW))

        old = account(profile_name_changes=json.dumps([(NOW - timedelta(days=20)).isoformat()] * 2))
        self.assertEqual(name_changes_left(old, NOW), 2)
        self.assertEqual(recent_name_changes("garbage", NOW), [])

    def test_summary(self):
        self.assertEqual(format_profile_summary("a", True, ["name", "avatar"], ""), ("ok", "Профиль «a»: применено — никнейм, аватарка"))
        self.assertEqual(format_profile_summary("a", True, [], "")[0], "info")
        kind, text = format_profile_summary("a", True, ["name"], "лимит")
        self.assertEqual((kind, text.endswith("· лимит")), ("ok", True))
        self.assertEqual(format_profile_summary("a", False, [], "boom"), ("err", "Профиль «a»: не применён — boom"))
        kind, text = format_profile_summary("a", True, [], "лимит", ["name"])
        self.assertEqual(kind, "warn")
        self.assertNotIn("уже актуален", text)


class TestProfileDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "t.db"
        self.db = Database(self.path)
        await self.db.connect()
        await self.db.init_schema()
        self.acc = await self.db.upsert_account("acc", "sessions/acc")

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def test_draft_keeps_unpassed_fields_and_none_clears(self):
        await self.db.set_account_profile_draft(self.acc.id, name="Bakyt", description="About")
        await self.db.set_account_profile_draft(self.acc.id, avatar="p/a.png")
        await self.db.set_account_profile_draft(self.acc.id, description=None)
        got = await self.db.get_account(self.acc.id)
        self.assertEqual((got.profile_name, got.profile_description, got.profile_avatar), ("Bakyt", None, "p/a.png"))
        await self.db.set_account_profile_draft(self.acc.id, name=UNSET)  # nothing to write
        with self.assertRaises(RecordNotFoundError):
            await self.db.set_account_profile_draft(999, name="x")

    async def test_applied_snapshot_and_name_change_window(self):
        await self.db.record_account_profile_applied(self.acc.id, {"name": "A"}, name_changed=True)
        await self.db.record_account_profile_applied(self.acc.id, {"name": "A", "description": "d"}, name_changed=False)
        got = await self.db.get_account(self.acc.id)
        self.assertEqual(applied_snapshot(got), {"name": "A", "description": "d"})
        self.assertIsNotNone(got.profile_applied_at)
        self.assertEqual(name_changes_left(got), 1)
        await self.db.record_account_profile_applied(self.acc.id, {"name": "B"}, name_changed=True)
        self.assertEqual(name_changes_left(await self.db.get_account(self.acc.id)), 0)
        with self.assertRaises(RecordNotFoundError):
            await self.db.record_account_profile_applied(999, {}, name_changed=False)

    async def test_old_name_changes_are_pruned_on_write(self):
        stale = json.dumps([(datetime.now(timezone.utc) - timedelta(days=30)).isoformat()])
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE accounts SET profile_name_changes = ? WHERE id = ?", (stale, self.acc.id))
        conn.commit()
        conn.close()
        await self.db.record_account_profile_applied(self.acc.id, {"name": "A"}, name_changed=False)
        self.assertEqual((await self.db.get_account(self.acc.id)).profile_name_changes, "[]")

    async def test_old_accounts_table_gets_the_profile_columns(self):
        await self.db.close()
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TABLE accounts")
        conn.execute(
            "CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, session_path TEXT NOT NULL,"
            " proxy_url TEXT, is_active INTEGER NOT NULL DEFAULT 1, total_replies INTEGER NOT NULL DEFAULT 0,"
            " created_at TEXT NOT NULL, last_used_at TEXT)"
        )
        conn.execute("INSERT INTO accounts (name, session_path, created_at) VALUES ('old', 's', '2026-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()
        self.db = Database(self.path)
        await self.db.connect()
        await self.db.init_schema()
        old = await self.db.get_account("old")
        self.assertIsNone(old.profile_name)
        await self.db.set_account_profile_draft(old.id, name="Migrated")
        self.assertEqual((await self.db.get_account("old")).profile_name, "Migrated")


class TestProfileEditor(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sim = MagicMock()
        self.sim.human_pause = AsyncMock()
        self.sim.click_locator = AsyncMock()
        self.sim.type_text = AsyncMock()
        target = TargetSettings(
            name="youtube",
            base_url="https://www.youtube.com",
            item_url_template="https://www.youtube.com/watch?v={external_id}",
            submit_input_selector="#contenteditable-root",
            submit_button_selector="#submit-button button",
            confirmation_selector="ytd-comment-view-model",
        )
        self.editor = ProfileEditor(BrowserSettings(headless=True, targets=[target]), simulator=self.sim)
        self.page = MagicMock()
        self.page.keyboard.press = AsyncMock()
        self.runner = AsyncMock()
        self.runner.context = MagicMock(pages=[self.page])
        self.db = AsyncMock()

    def patched(self, **steps):
        """Replace the Studio steps by mocks (defaults: a fill that changed `name`)."""
        defaults = dict(
            _open_profile_tab=AsyncMock(), _fill=AsyncMock(return_value=(["name"], [])), _publish=AsyncMock(),
            _verify=AsyncMock(), _avatar_src=AsyncMock(return_value=None),
        )
        for name, mock in {**defaults, **steps}.items():
            setattr(self.editor, name, mock)
        return patch.object(profile_manager, "YouTubeRunner", return_value=self.runner)

    async def test_nothing_pending_opens_no_browser(self):
        with patch.object(profile_manager, "YouTubeRunner") as factory:
            res = await self.editor.apply(account(profile_name="A", profile_applied=applied(name="A")), self.db)
        self.assertEqual(res, ProfileResult("acc", True, (), ""))
        factory.assert_not_called()

    async def test_applies_publishes_verifies_and_records(self):
        acc = account(profile_name="New", profile_description="About", profile_applied=applied(avatar="p/a.png"))
        with self.patched():
            res = await self.editor.apply(acc, self.db)
        self.assertTrue(res.success)
        self.assertEqual(res.changed_fields, ("name",))
        self.editor._publish.assert_awaited_once()
        self.editor._verify.assert_awaited_once()
        # the name change is counted right after the publish, the snapshot after the check; description matched the
        # live channel already: it is recorded as applied without being "changed"
        self.assertEqual(self.db.record_account_profile_applied.await_args_list, [
            call(1, {"avatar": "p/a.png"}, name_changed=True),
            call(1, {"avatar": "p/a.png", "name": "New", "description": "About"}, name_changed=False),
        ])
        self.runner.close.assert_awaited_once()

    async def test_no_difference_on_the_channel_skips_publish_but_marks_applied(self):
        with self.patched(_fill=AsyncMock(return_value=([], []))):
            res = await self.editor.apply(account(profile_name="Same"), self.db)
        self.assertTrue(res.success)
        self.assertEqual(res.changed_fields, ())
        self.editor._publish.assert_not_awaited()
        self.db.record_account_profile_applied.assert_awaited_once_with(1, {"name": "Same"}, name_changed=False)

    async def test_name_limit_drops_the_name_but_applies_the_rest(self):
        recent = json.dumps([(datetime.now(timezone.utc) - timedelta(days=d)).isoformat() for d in (1, 2)])
        acc = account(profile_name="New", profile_description="About", profile_name_changes=recent)
        fill = AsyncMock(return_value=(["description"], ["name"]))
        with self.patched(_fill=fill):
            res = await self.editor.apply(acc, self.db)
        self.assertTrue(res.success)
        self.assertEqual((fill.await_args.args[1], fill.await_args.args[2]), ({"name": "New", "description": "About"}, True))
        self.assertEqual(res.skipped_fields, ("name",))
        self.assertIn("лимит", res.details)
        self.db.record_account_profile_applied.assert_awaited_once_with(1, {"description": "About"}, name_changed=False)

    async def test_only_a_blocked_name_is_a_failure_and_records_nothing(self):
        recent = json.dumps([(datetime.now(timezone.utc) - timedelta(days=d)).isoformat() for d in (1, 2)])
        with self.patched(_fill=AsyncMock(return_value=([], ["name"]))):
            res = await self.editor.apply(account(profile_name="New", profile_name_changes=recent), self.db)
        self.assertFalse(res.success)
        self.assertEqual(res.skipped_fields, ("name",))
        self.editor._publish.assert_not_awaited()
        self.db.record_account_profile_applied.assert_not_awaited()
        self.runner.close.assert_awaited_once()

    async def test_failed_verification_still_counts_the_published_name_change(self):
        acc = account(profile_name="New", profile_applied=applied(description="d"))
        with self.patched(_verify=AsyncMock(side_effect=RuntimeError("не совпал"))):
            res = await self.editor.apply(acc, self.db)
        self.assertFalse(res.success)
        self.assertIn("не совпал", res.details)
        # YouTube already saved the name: it is logged against the 14-day limit, but the snapshot stays untouched
        self.db.record_account_profile_applied.assert_awaited_once_with(1, {"description": "d"}, name_changed=True)
        self.runner.close.assert_awaited_once()

    async def test_failed_publish_records_nothing(self):
        with self.patched(_publish=AsyncMock(side_effect=RuntimeError("timeout"))):
            res = await self.editor.apply(account(profile_name="New"), self.db)
        self.assertFalse(res.success)
        self.db.record_account_profile_applied.assert_not_awaited()

    async def test_a_replaced_applied_avatar_copy_is_forgotten_after_success(self):
        acc = account(profile_avatar="p/new.png", profile_applied=applied(avatar="p/old.png"))
        with self.patched(_fill=AsyncMock(return_value=(["avatar"], []))), patch.object(profile_manager, "forget_avatar") as forget:
            res = await self.editor.apply(acc, self.db)
        self.assertTrue(res.success)
        forget.assert_called_once_with("p/old.png", keep=("p/new.png", "p/new.png"))

    async def test_browser_start_failure_is_reported_and_closed(self):
        self.runner.start.side_effect = RuntimeError("profile locked")
        with self.patched():
            res = await self.editor.apply(account(profile_name="New"), self.db)
        self.assertFalse(res.success)
        self.assertIn("profile locked", res.details)
        self.runner.close.assert_awaited_once()

    async def test_set_text_types_only_when_the_value_differs(self):
        field = MagicMock()
        field.input_value = AsyncMock(return_value="Same")
        with patch.object(profile_manager, "first_visible", AsyncMock(return_value=field)):
            self.assertTrue(await self.editor._set_text(self.page, ("#x",), "Same"))
            self.sim.type_text.assert_not_awaited()
            field.input_value.return_value = "Old"
            self.assertFalse(await self.editor._set_text(self.page, ("#x",), "New"))
        self.sim.type_text.assert_awaited_once_with(self.page, "New", allow_typos=False)
        self.assertEqual([c.args[0] for c in self.page.keyboard.press.await_args_list], ["Control+A", "Delete"])

    async def test_set_text_missing_field_raises(self):
        with patch.object(profile_manager, "first_visible", AsyncMock(return_value=None)):
            with self.assertRaises(RuntimeError):
                await self.editor._set_text(self.page, ("#x",), "v")

    async def test_set_text_without_write_never_types(self):
        field = MagicMock()
        field.input_value = AsyncMock(return_value="Old")
        with patch.object(profile_manager, "first_visible", AsyncMock(return_value=field)):
            self.assertFalse(await self.editor._set_text(self.page, ("#x",), "New", write=False))
        self.sim.type_text.assert_not_awaited()
        self.page.keyboard.press.assert_not_awaited()

    async def test_fill_blocked_name_is_skipped_only_when_it_differs(self):
        self.editor._set_text = AsyncMock(return_value=False)
        self.assertEqual(await self.editor._fill(self.page, {"name": "N"}, True), ([], ["name"]))
        self.assertIs(self.editor._set_text.await_args.kwargs["write"], False)
        self.editor._set_text = AsyncMock(return_value=True)  # the live name already equals the draft
        self.assertEqual(await self.editor._fill(self.page, {"name": "N"}, True), ([], []))
        self.editor._set_text = AsyncMock(return_value=False)
        self.assertEqual(await self.editor._fill(self.page, {"name": "N"}, False), (["name"], []))

    async def test_fill_changes_description_and_uploads_avatar(self):
        self.editor._set_text = AsyncMock(return_value=False)
        self.editor._upload_avatar = AsyncMock()
        changed, skipped = await self.editor._fill(self.page, {"description": "D", "avatar": "p/a.png"}, False)
        self.assertEqual((changed, skipped), (["description", "avatar"], []))
        self.editor._upload_avatar.assert_awaited_once_with(self.page, "p/a.png")

    async def test_upload_avatar_sets_the_file_and_confirms_the_crop(self):
        with tempfile.TemporaryDirectory() as folder:
            picture = Path(folder) / "a.png"
            picture.write_bytes(PNG)
            file_input = MagicMock()
            file_input.count = AsyncMock(return_value=1)
            file_input.first.set_input_files = AsyncMock()
            self.page.locator = MagicMock(return_value=file_input)
            done = MagicMock()
            with patch.object(profile_manager, "first_visible", AsyncMock(return_value=done)):
                await self.editor._upload_avatar(self.page, str(picture))
            file_input.first.set_input_files.assert_awaited_once_with(str(picture))
            self.sim.click_locator.assert_awaited_once_with(self.page, done)

            file_input.count = AsyncMock(return_value=0)
            with self.assertRaises(RuntimeError):
                await self.editor._upload_avatar(self.page, str(picture))
            picture.unlink()
            with self.assertRaises(RuntimeError):
                await self.editor._upload_avatar(self.page, str(picture))

    async def test_wait_published_returns_when_publish_is_disabled_and_times_out_otherwise(self):
        self.page.wait_for_timeout = AsyncMock()
        button = MagicMock()
        button.is_disabled = AsyncMock(side_effect=[False, False, True])
        await ProfileEditor._wait_published(self.page, button)
        self.assertEqual(self.page.wait_for_timeout.await_count, 2)
        button.is_disabled = AsyncMock(return_value=False)
        with patch.object(profile_manager, "PUBLISH_TIMEOUT_SECONDS", 1.0), self.assertRaises(RuntimeError):
            await ProfileEditor._wait_published(self.page, button)

    async def test_verify_detects_a_value_that_did_not_stick(self):
        self.page.reload = AsyncMock()
        field = MagicMock()
        field.input_value = AsyncMock(return_value="Other")
        with patch.object(profile_manager, "first_visible", AsyncMock(return_value=field)):
            with self.assertRaises(RuntimeError):
                await self.editor._verify(self.page, {"name": "New"}, ["name"], None)
            field.input_value.return_value = "New"
            await self.editor._verify(self.page, {"name": "New"}, ["name"], None)

    async def test_verify_detects_an_unchanged_avatar(self):
        self.page.reload = AsyncMock()
        self.editor._avatar_src = AsyncMock(return_value="https://yt/old.jpg")
        with self.assertRaises(RuntimeError):
            await self.editor._verify(self.page, {"avatar": "p/a.png"}, ["avatar"], "https://yt/old.jpg")
        self.editor._avatar_src.return_value = None  # the preview cannot be read: not a silent success
        with self.assertRaises(RuntimeError):
            await self.editor._verify(self.page, {"avatar": "p/a.png"}, ["avatar"], "https://yt/old.jpg")
        self.editor._avatar_src.return_value = "https://yt/new.jpg"
        await self.editor._verify(self.page, {"avatar": "p/a.png"}, ["avatar"], "https://yt/old.jpg")


class TestProfileWizard(MenuCase):
    def seed(self, *names: str, signed_in: bool = True) -> None:
        async def _seed():
            db = Database(self.db_path)
            await db.connect()
            await db.init_schema()
            for name in names:
                await db.upsert_account(name, str(self.root / "sessions" / name))
            await db.close()

        asyncio.run(_seed())
        for name in names:
            if signed_in:
                self.set_stage(name, 100)

    def card(self, *keys: str, inputs: list | None = None, pasted: list[str] | None = None):
        accounts = _setup_accounts(self.config_path)
        with patch("builtins.input", side_effect=pasted if pasted is not None else EOFError):
            return self.run_keys(lambda: _setup_profile(self.config_path, accounts), *keys, inputs=inputs)

    def test_menu_row_exists(self):
        from modules.wizard import step_account

        _, output = self.run_keys(lambda: step_account(self.config_path, self.root / "sessions"), ENTER)
        self.assertIn("Профиль канала", output)

    def test_nickname_is_saved_as_a_draft(self):
        self.seed("solo")
        notice, output = self.card("1", ESC, inputs=["  Bakyt   Dev "])
        self.assertEqual(self.accounts()["solo"].profile_name, "Bakyt Dev")
        self.assertEqual(notice[0], "ok")
        self.assertIn("Добавить описание", output)
        self.assertIn("Добавить аватарку", output)

    def test_invalid_nickname_and_cancel_change_nothing(self):
        self.seed("solo")
        notice, _ = self.card("1", ESC, inputs=["a<b"])
        self.assertIsNone(self.accounts()["solo"].profile_name)
        self.assertIsNone(notice)
        self.card("1", ESC, inputs=[""])
        self.assertIsNone(self.accounts()["solo"].profile_name)

    def test_description_lines_until_blank(self):
        self.seed("solo")
        self.card("2", ESC, pasted=["Пишу про Python", "и про боты", "", ""])
        self.assertEqual(self.accounts()["solo"].profile_description, "Пишу про Python\nи про боты")
        self.card("2", ESC, pasted=["-", "", ""])
        self.assertEqual(self.accounts()["solo"].profile_description, "")

    def test_description_keeps_paragraphs_and_a_first_blank_line_cancels(self):
        self.seed("solo")
        self.card("2", ESC, pasted=["Привет", "", "О канале", "", ""])
        self.assertEqual(self.accounts()["solo"].profile_description, "Привет\n\nО канале")
        self.card("2", ESC, pasted=[""])
        self.assertEqual(self.accounts()["solo"].profile_description, "Привет\n\nО канале")

    def test_avatar_is_validated_copied_and_the_old_copy_removed(self):
        self.seed("solo")
        store = self.root / "avatars"
        first, second = self.root / "one.png", self.root / "two.jpg"
        first.write_bytes(PNG)
        second.write_bytes(JPEG)
        with patch.object(profile_manager, "AVATARS_DIR", store):
            self.card("3", ESC, inputs=[f'"{first}"'])
            old = Path(self.accounts()["solo"].profile_avatar)
            self.assertTrue(old.is_file())
            self.card("3", ESC, inputs=[str(second)])
            new = Path(self.accounts()["solo"].profile_avatar)
            self.assertTrue(new.is_file())
            self.assertFalse(old.exists())
            _, output = self.card("3", ESC, inputs=[str(self.root / "missing.png")])
            self.assertIn("Файл не найден", output)
            self.assertEqual(Path(self.accounts()["solo"].profile_avatar), new)

    def test_space_on_an_account_opens_its_card(self):
        self.seed("first", "second")
        # picker: ↓ to «second», Space opens the card, «1» edits the nickname, Esc → picker, Esc → leave
        self.card(DOWN, SPACE, "1", ESC, ESC, inputs=["Second Nick"])
        accounts = self.accounts()
        self.assertEqual(accounts["second"].profile_name, "Second Nick")
        self.assertIsNone(accounts["first"].profile_name)

    def test_picker_shows_profile_state(self):
        self.seed("first", "second")
        asyncio.run(self._draft("second", name="Nick"))
        _, output = self.card(ESC)
        self.assertIn("○ не задан", output)
        self.assertIn("● есть неприменённые правки", output)

    async def _draft(self, account_name: str, **fields) -> None:
        db = Database(self.db_path)
        await db.connect()
        await db.init_schema()
        await db.set_account_profile_draft((await db.get_account(account_name)).id, **fields)
        await db.close()

    def test_apply_runs_the_editor_and_shows_the_summary(self):
        self.seed("solo")
        asyncio.run(self._draft("solo", name="Nick"))
        result = ProfileResult("solo", True, ("name",), "")
        with patch.object(ProfileEditor, "apply", AsyncMock(return_value=result)) as apply:
            notice, output = self.card("4", ESC, inputs=["y"])
        apply.assert_awaited_once()
        self.assertEqual(notice, ("ok", "Профиль «solo»: применено — никнейм"))
        self.assertIn("● не применено", output)

    def test_apply_declined_or_without_edits_or_without_sign_in_does_not_open_a_browser(self):
        self.seed("solo")
        with patch.object(ProfileEditor, "apply", AsyncMock()) as apply:
            self.card("4", ESC)  # nothing to apply
            asyncio.run(self._draft("solo", name="Nick"))
            self.card("4", ESC, inputs=["n"])  # declined
            self.set_stage("solo", 100, auth_status="pending")
            _, output = self.card("4", ESC)  # not signed in
        apply.assert_not_awaited()
        self.assertIn("Сначала войдите", output)

    def test_apply_failure_shows_only_the_error_class(self):
        self.seed("solo")
        asyncio.run(self._draft("solo", name="Nick"))
        with patch.object(ProfileEditor, "apply", AsyncMock(side_effect=RuntimeError("secret-detail"))):
            notice, _ = self.card("4", ESC, inputs=["y"])
        self.assertEqual(notice[0], "err")
        self.assertIn("RuntimeError", notice[1])
        self.assertNotIn("secret-detail", notice[1])


class TestApplyProfileCli(unittest.IsolatedAsyncioTestCase):
    async def test_cli_paths(self):
        import youtube_bot

        db = AsyncMock()
        db.__aenter__.return_value = db
        cfg = MagicMock()
        cases = [
            (None, False),
            (account(auth_status="pending", profile_name="N"), False),
            (account(), True),  # nothing pending
        ]
        for found, expected in cases:
            with patch.object(youtube_bot, "load_config", return_value=cfg), patch.object(youtube_bot, "Database", return_value=db), \
                 patch.object(youtube_bot, "_find_account", AsyncMock(return_value=found)):
                self.assertEqual(await youtube_bot.apply_profile_cli("acc"), expected)

        for result, expected in (
            (ProfileResult("acc", True, ("name",), ""), True),
            (ProfileResult("acc", True, ("description",), "лимит", ("name",)), False),  # the name is still pending
            (ProfileResult("acc", False, (), "boom"), False),
        ):
            editor = MagicMock()
            editor.return_value.apply = AsyncMock(return_value=result)
            with patch.object(youtube_bot, "load_config", return_value=cfg), patch.object(youtube_bot, "Database", return_value=db), \
                 patch.object(youtube_bot, "_find_account", AsyncMock(return_value=account(profile_name="N"))), \
                 patch.object(youtube_bot, "ProfileEditor", editor):
                self.assertEqual(await youtube_bot.apply_profile_cli("acc"), expected)
            editor.return_value.apply.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
