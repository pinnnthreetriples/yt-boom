"""Sections «Цель», «Запуск», preview and stats: arrow navigation and compact output."""
from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from config import load_config
from modules.wizard import (
    WizardBack,
    _launch,
    _show_stats,
    _target_counts,
    _target_mode_label,
    step_launch,
    step_video,
)
from tests.tui_keys import DOWN, ENTER, ESC, SPACE, UP, scripted_keys

VIDEO = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class TargetCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "cfg.yaml"
        with open("config.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["app"]["db_path"] = str(self.root / "t.db")
        data["app"]["log_file"] = str(self.root / "t.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["processor"]["reply_mode"] = "ai"
        data["deepseek"]["api_key"] = "sk-test-key-123456"
        data["collector"]["target_mode"] = "first_reply"
        data["collector"]["search_query"] = None
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    POOL = ["https://www.youtube.com/watch?v=abcdefghijk", "https://www.youtube.com/watch?v=12345678901"]

    def db(self, fn):
        from database import Database

        async def run():
            db = Database(self.root / "t.db")
            await db.connect()
            await db.init_schema()
            try:
                return await fn(db)
            finally:
                await db.close()
        return asyncio.run(run())

    def seed_pool(self, target: int = 2):
        async def fn(db):
            for url in self.POOL:
                await db.add_video_to_pool(url, comments_target=target)
        self.db(fn)

    def run_keys(self, func, keys: tuple[str, ...], answers=None):
        """Run func() with scripted menu keys; text prompts answer from `answers` (or '')."""
        out = io.StringIO()
        prompt = {"side_effect": answers} if answers is not None else {"return_value": ""}
        with scripted_keys(*keys) as screen, \
             patch("modules.wizard.prompt_user", **prompt) as prompt_mock, \
             patch("sys.stdout", out):
            result = func()
        self.prompt_mock = prompt_mock
        return result, screen.getvalue() + out.getvalue()


class TestTargetSection(TargetCase):
    def test_layout_and_esc_returns_values(self):
        result, output = self.run_keys(lambda: step_video(self.config_path, 3), (ESC,))
        self.assertEqual(result, 3)
        for text in ("Цель", "Видео", "список пуст", "Режим цели", "Ответ на ответ", "3 на видео"):
            self.assertIn(text, output)
        self.assertNotIn("Автопоиск", output)  # lives in the «Видео» submenu
        self.assertNotIn("Выберите действие", output)

    def test_mode_picked_with_arrows(self):
        keys = (DOWN, ENTER, DOWN, ENTER, ESC)  # Режим цели -> «Топ-комментарии»
        _, output = self.run_keys(lambda: step_video(self.config_path, 2), keys)
        self.assertEqual(load_config(self.config_path).collector.target_mode, "popular_parent")
        self.assertIn("Режим цели: Топ-комментарии", output)

    def test_mode_menu_space_shows_example_without_selecting(self):
        keys = (DOWN, ENTER, SPACE, DOWN, ESC, ESC)  # open picker, example for the main mode, then for the next
        _, output = self.run_keys(lambda: step_video(self.config_path, 2), keys)
        self.assertIn("ОСНОВНОЙ", output)
        self.assertIn("Депнуть микрозайм", output)
        self.assertIn("отвечаем сюда", output)
        self.assertEqual(load_config(self.config_path).collector.target_mode, "first_reply")

    def test_mode_menu_esc_keeps_mode(self):
        self.run_keys(lambda: step_video(self.config_path, 2), (DOWN, ENTER, UP, ESC, ESC))
        self.assertEqual(load_config(self.config_path).collector.target_mode, "first_reply")

    def test_limit_stays_in_section_and_rejects_bad_input(self):
        keys = (UP, UP, UP, ENTER, ENTER, ESC)  # wraps to «Лимит»: bad value, then 9
        lim, output = self.run_keys(lambda: step_video(self.config_path, 2), keys, ["abc", "9"])
        self.assertEqual(lim, 9)
        self.assertIn("Нужно число от 1 до 50", output)
        self.assertIn("Лимит: 9", output)
        self.assertEqual(load_config(self.config_path).limits.max_iterations_per_session, 9)

    def test_search_set_in_video_submenu_and_shown_in_video_row(self):
        keys = (ENTER, DOWN, DOWN, DOWN, ENTER, ENTER, ESC, ESC)  # Видео -> Автопоиск; reopen the submenu
        _, output = self.run_keys(lambda: step_video(self.config_path, 2), keys, ["python", "1000", ""])
        cfg = load_config(self.config_path)
        self.assertEqual((cfg.collector.search_query, cfg.collector.min_views, cfg.collector.max_views), ("python", 1000, None))
        self.assertIn("автопоиск \"python\"", output)
        self.assertIn("\"python\" · 1000..макс", output)  # views shown in the submenu
        self.assertIn("в очереди: 0 · не используется", output)

    def test_hidden_zero_raises_back(self):
        with self.assertRaises(WizardBack):
            self.run_keys(lambda: step_video(self.config_path, 2), ("0",))

    def test_mode_labels(self):
        self.assertEqual(_target_mode_label("first_sub_comment"), "Ответ на ответ")
        self.assertEqual(_target_mode_label("mixed"), "Смешанный")
        self.assertEqual(_target_mode_label("other"), "other")


class TestLaunchSection(TargetCase):
    def launch(self):
        return step_launch(self.config_path, self.root / "sessions", "sk-test-key-123456", 2)

    def setUp(self):
        super().setUp()
        self.seed_pool()

    def test_summary_and_dry_run(self):
        with patch("youtube_bot.run_video_pool_cli", new_callable=AsyncMock) as bot:
            result, output = self.run_keys(self.launch, (ENTER,))
        self.assertTrue(result)
        self.assertTrue(bot.call_args.kwargs["dry_run"])
        for text in ("Запуск", "Поиск       ⚠ напрямую, с вашего IP", "Публикация  аккаунтов нет", "нет входа", "Ответ на ответ", "2 видео", "Пробный запуск завершён"):
            self.assertIn(text, output)
        self.assertNotIn("sk-test-key-123456", output)

    def test_live_needs_confirmation(self):
        with patch("youtube_bot.run_video_pool_cli", new_callable=AsyncMock) as bot:
            # login warning: yes; final confirmation: no -> back in the menu; then Esc
            with self.assertRaises(WizardBack):
                _, _ = self.run_keys(self.launch, (DOWN, ENTER, ESC), ["y", "n"])
        bot.assert_not_called()

    def test_live_confirmed_runs(self):
        with patch("youtube_bot.run_video_pool_cli", new_callable=AsyncMock) as bot:
            result, output = self.run_keys(self.launch, (DOWN, ENTER), ["y", "y", ""])
        self.assertTrue(result)
        self.assertFalse(bot.call_args.kwargs["dry_run"])
        self.assertIn("Вход в YouTube не выполнен", output)

    def test_stats_returns_to_section(self):
        with patch("modules.wizard._show_stats") as stats, self.assertRaises(WizardBack):
            self.run_keys(self.launch, (DOWN, DOWN, ENTER, ESC))
        stats.assert_called_once()

    def test_dashboard_launch_dry_run(self):
        with patch("youtube_bot.run_video_pool_cli", new_callable=AsyncMock) as bot:
            self.run_keys(lambda: _launch(self.config_path, dry_run=True), ())
        bot.assert_called_once()
        self.assertEqual(self.prompt_mock.call_count, 1)  # only «Нажмите Enter»

    def test_empty_list_without_search_runs_nothing(self):
        self.db(lambda db: db.clear_video_pool())
        with patch("youtube_bot.run_video_pool_cli", new_callable=AsyncMock) as pool, \
             patch("youtube_bot.run_reply_bot", new_callable=AsyncMock) as bot:
            result, output = self.run_keys(self.launch, (ENTER,))
        self.assertTrue(result)
        pool.assert_not_called()
        bot.assert_not_called()
        self.assertIn("нет видео · вставьте список или включите автопоиск", output)
        self.assertIn("Нет видео", output)


class TestVideoPoolTarget(TargetCase):
    """«Цель → Видео» works with the shared video list; launches pick search > list, nothing else."""

    def test_video_submenu_paste_show_clear(self):
        keys = (
            ENTER, ENTER,              # Видео -> Вставить список
            ENTER, DOWN, ENTER,        # Видео -> Показать список
            ENTER, DOWN, DOWN, ENTER,  # Видео -> Очистить список
            ESC,
        )
        answers = ["https://youtu.be/dQw4w9WgXcQ", "abcdefghijk", "bad", "", "", "y"]
        _, output = self.run_keys(lambda: step_video(self.config_path, 3), keys, answers)
        self.assertIn("+2 добавлено · 0 уже были · 1 не распознано", output)
        self.assertIn("2 в очереди", output)
        self.assertIn("abcdefghijk", output)        # shown list
        self.assertIn("0/3", output)                # new videos got the section limit as target
        self.assertIn("Удалено видео: 2", output)
        self.assertEqual(self.db(lambda db: db.count_pool_videos()), 0)

    def test_video_submenu_has_icons_and_no_single_video(self):
        _, output = self.run_keys(lambda: step_video(self.config_path, 2), (ENTER, ESC, ESC))
        for text in ("+ Вставить", "≡ Показать", "× Очистить", "в очереди: 0", "Автопоиск", "выключен"):
            self.assertIn(text, output)
        self.assertNotIn("Одно видео", output)

    def test_freshness_shown_only_with_search_and_picked_with_arrows(self):
        from modules.wizard import update_config_file
        _, output = self.run_keys(lambda: step_video(self.config_path, 2), (ENTER, ESC, ESC))
        self.assertNotIn("Свежесть", output)

        update_config_file(self.config_path, {"collector.search_query": "python"})
        # «Видео», ↓↓↓↓ «Свежесть», Enter; ↓↓ «Не старше 24 часа», Enter; Esc
        keys = (ENTER, DOWN, DOWN, DOWN, DOWN, ENTER, DOWN, DOWN, ENTER, ESC)
        _, output = self.run_keys(lambda: step_video(self.config_path, 2), keys)
        self.assertEqual(load_config(self.config_path).collector.max_age_hours, 24)
        self.assertIn("Свежесть: 24 часа", output)
        self.assertIn("≤24 часа", output)  # the auto-search summary shows the filter

    def test_limit_change_updates_unfinished_pool_videos(self):
        self.seed_pool()
        done_id = self.db(lambda db: db.list_pool_videos("all"))[0].id
        self.db(lambda db: db.update_pool_video_progress(done_id, comments_added=2))  # completed with 2
        keys = (UP, UP, UP, ENTER, ESC)  # «Лимит»
        _, output = self.run_keys(lambda: step_video(self.config_path, 2), keys, ["5"])
        self.assertIn("видео в списке обновлено: 1", output)
        targets = {v.video_url: (v.comments_target, v.status) for v in self.db(lambda db: db.list_pool_videos("all"))}
        self.assertEqual(targets[self.POOL[0]], (2, "completed"))
        self.assertEqual(targets[self.POOL[1]], (5, "pending"))

    def test_launch_runs_pool_for_all_accounts(self):
        self.seed_pool()
        with patch("youtube_bot.run_video_pool_cli", new_callable=AsyncMock) as pools, \
             patch("youtube_bot.run_reply_bot", new_callable=AsyncMock) as bot:
            pools.return_value = 3
            _, output = self.run_keys(lambda: _launch(self.config_path, dry_run=True), ())
        bot.assert_not_called()
        # threads and account mode are read from the config by run_video_pool_cli itself
        self.assertEqual(pools.call_args.kwargs, {"config_path": str(self.config_path), "dry_run": True})
        lim = load_config(self.config_path).limits.max_iterations_per_session
        self.assertIn(f"2 видео · {lim} ответов на каждое", output)
        self.assertIn("Ответов: 3", output)

    def test_search_wins_over_pool(self):
        self.seed_pool()
        from modules.wizard import update_config_file
        update_config_file(self.config_path, {"collector.search_query": "python"})
        with patch("youtube_bot.run_video_pool_cli", new_callable=AsyncMock) as pools, \
             patch("youtube_bot.run_multi_video_pipeline", new_callable=AsyncMock) as search:
            self.run_keys(lambda: _launch(self.config_path, dry_run=True), ())
        pools.assert_not_called()
        search.assert_called_once()

    def test_step_launch_live_on_pool_and_summary(self):
        self.seed_pool()
        launch = lambda: step_launch(self.config_path, self.root / "sessions", "sk-test-key-123456", 2)
        with patch("youtube_bot.run_video_pool_cli", new_callable=AsyncMock) as pools:
            pools.return_value = 0
            _, output = self.run_keys(launch, (DOWN, ENTER), ["y", "y", ""])
        self.assertIn("2 видео · 2 ответов на каждое", output)
        self.assertNotIn("Потоки", output)  # one account: nothing to set
        self.assertFalse(pools.call_args.kwargs["dry_run"])

    def test_step_launch_shows_threads_with_two_accounts(self):
        self.seed_pool()

        async def seed(db):
            for name in ("first", "second"):
                await db.upsert_account(name, str(self.root / "sessions" / name), None, True)
        self.db(seed)
        launch = lambda: step_launch(self.config_path, self.root / "sessions", "sk-test-key-123456", 2)
        # ↓↓ «Потоки» opens the threads screen, then Esc leaves the section
        with patch("modules.wizard.step_threads") as threads, self.assertRaises(WizardBack):
            self.run_keys(launch, (DOWN, DOWN, ENTER, ESC))
        threads.assert_called_once()

    def test_dashboard_row_readiness_and_preview(self):
        from types import SimpleNamespace

        from modules.wizard import _dashboard_rows, _readiness, _target_preview
        self.seed_pool()
        rows = {r[0]: r[2] for r in _dashboard_rows(self.config_path) if r}
        lim = load_config(self.config_path).limits.max_iterations_per_session
        self.assertEqual(rows["4"], f"2 видео · {lim} ответов на каждое · Ответ на ответ")

        with patch("youtube_bot.list_sub_comments", new_callable=AsyncMock) as preview:
            self.run_keys(lambda: _target_preview(self.config_path, 2), ())
        self.assertEqual(preview.call_args.args[0], self.POOL[0])

        empty = SimpleNamespace(
            processor=SimpleNamespace(reply_mode="ai", custom_replies=[]),
            deepseek=SimpleNamespace(api_key="sk-x"),
            browser=SimpleNamespace(session_root=self.root / "sessions"),
            collector=SimpleNamespace(search_query=None, endpoints=[]),
            app=SimpleNamespace(db_path=self.root / "missing.db"),
        )
        self.assertEqual(_readiness(empty)[0], "! нет видео")
        self.assertFalse((self.root / "missing.db").exists())  # read-only check creates no file


class TestStats(TargetCase):
    def test_counts(self):
        words = {"success": "опубликовано", "failed": "ошибок"}
        self.assertEqual(_target_counts({}, words), ("нет", "dim"))
        self.assertEqual(_target_counts({"success": 2}, words), ("опубликовано 2", ""))
        self.assertEqual(_target_counts({"success": 2, "failed": 1, "x": 3}, words), ("опубликовано 2 · ошибок 1 · x 3", "warn"))

    def test_compact_summary(self):
        from database import Database

        async def seed():
            db = Database(self.root / "t.db")
            await db.connect()
            await db.init_schema()
            await db.create_payload(node_id=None, text="Отличное [видео], спасибо", language="ru", source_ref="c1")
            await db.close()

        asyncio.run(seed())
        _, output = self.run_keys(lambda: _show_stats(self.config_path), ())
        for text in ("Статистика", "t.db", "Журнал", "Ответы", "ждут 1", "Последние ответы", "[видео]"):
            self.assertIn(text, output)
        self.assertLessEqual(max(len(line) for line in output.splitlines()), 70)


if __name__ == "__main__":
    unittest.main()
