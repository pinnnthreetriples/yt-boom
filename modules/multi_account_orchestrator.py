from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import time
from typing import Any, Callable

from loguru import logger

from config import AppConfig, BrowserSettings, TargetSettings, load_config, resolve_account_proxy
from database import (
    AccountRecord,
    ActivityLogCreate,
    Database,
    VideoPoolItem,
)
from modules.account_warmer import AccountWarmer
from modules.interaction_simulator import InteractionSimulator
from modules.lifecycle_engine import AccountLifecycleEngine
from modules.pool_manager import PoolManager
from modules.processor import DeepSeekProcessor, resolve_reply_text, should_paraphrase
from modules.ui_test_runner import SubmissionResult
from modules.youtube_collector import YouTubeCollector, extract_video_id
from modules.youtube_runner import YouTubeRunner


class MultiAccountOrchestrator:
    """
    Posts replies under the video pool with several YouTube accounts:
    `concurrency` accounts (or videos) at a time, account_mode "each" / "share",
    error isolation per account and video.
    """

    def __init__(
        self,
        config_or_path: AppConfig | str | Path,
        db: Database | None = None,
        concurrency: int = 1,
        dry_run: bool = False,
        account_mode: str | None = None,
        headless: bool | None = None,
        rng: random.Random | None = None,
        *,
        collector: YouTubeCollector | None = None,
        processor: DeepSeekProcessor | None = None,
        runner_factory: Callable[[AccountRecord], Any] | None = None,
        warmer: AccountWarmer | None = None,
    ) -> None:
        if isinstance(config_or_path, AppConfig):
            self._cfg = config_or_path
        else:
            self._cfg = load_config(config_or_path)

        if headless is not None:
            self._cfg.browser.headless = headless

        self._db = db or Database(self._cfg.app.db_path)
        self._concurrency = max(1, concurrency)
        self._semaphore = asyncio.Semaphore(self._concurrency)
        self._dry_run = dry_run
        self._account_mode = account_mode or self._cfg.limits.account_mode
        self._rng = rng or random.Random()
        self._collector = collector or YouTubeCollector(self._cfg.collector)
        self._processor = processor or (
            DeepSeekProcessor(self._cfg.deepseek, self._cfg.processor, rng=self._rng)
            if getattr(self._cfg.processor, "reply_mode", "ai") == "ai"
            or should_paraphrase(self._cfg.deepseek, self._cfg.processor)
            else None
        )
        self._runner_factory = runner_factory
        self._runners: dict[int, Any] = {}
        self._runner_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._simulator = InteractionSimulator(rng=self._rng)

        # Locate or initialize YouTube target settings
        yt_target = None
        for t in self._cfg.browser.targets:
            if t.name == "youtube" or "youtube.com" in t.base_url:
                yt_target = t
                break
        if yt_target is None:
            yt_target = TargetSettings(
                name="youtube",
                base_url="https://www.youtube.com",
                item_url_template="https://www.youtube.com/watch?v={external_id}",
                submit_input_selector="#reply-dialog #contenteditable-root",
                submit_button_selector="#reply-dialog #submit-button button",
                confirmation_selector="ytd-comment-view-model",
            )
        self._yt_target = yt_target

        self._pool_mgr = PoolManager(self._db, collector=self._collector, rng=self._rng)
        self._in_flight: dict[int, int] = defaultdict(int)
        self._account_offset: int = 0
        # Comments taken in this run: parallel accounts must not pick the same one before its payload exists.
        self._claimed: set[str] = set()
        self._skipped_logged: set[int] = set()
        self._lifecycle_logged: set[tuple[int, str]] = set()
        self._warmer = warmer or AccountWarmer(self._cfg.browser, simulator=self._simulator, rng=self._rng)
        self._warmup_tasks: dict[int, asyncio.Task[Any]] = {}
        self._warmup_slot = asyncio.Semaphore(1)  # one warm-up browser at a time

    @property
    def db(self) -> Database:
        return self._db

    @property
    def config(self) -> AppConfig:
        return self._cfg

    @property
    def concurrency(self) -> int:
        return self._concurrency

    @property
    def account_mode(self) -> str:
        return self._account_mode

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def pool_manager(self) -> PoolManager:
        return self._pool_mgr

    async def ensure_db(self) -> None:
        """Ensure database connection and schema initialization."""
        if not self._db.is_connected:
            await self._db.connect()
        await self._db.init_schema()

    async def sync_accounts_from_config(self) -> list[AccountRecord]:
        """Ensure all accounts defined in config are registered in the database."""
        await self.ensure_db()
        for acc in self._cfg.browser.accounts:
            await self._db.upsert_account(
                name=acc.name,
                session_path=acc.session_path,
                proxy_url=acc.proxy_url,
                is_active=acc.is_active,
            )
        return await self._db.list_active_accounts()

    def create_runner(self, acc: AccountRecord) -> Any:
        """Instantiate a YouTubeRunner or custom mock runner for the given account."""
        if self._runner_factory is not None:
            return self._runner_factory(acc)
        return YouTubeRunner(
            browser_settings=self._cfg.browser,
            target=self._yt_target,
            session_dir=acc.session_path,
            proxy=resolve_account_proxy(acc.proxy_url, self._cfg.browser.proxy),
            account_name=acc.name,
            simulator=self._simulator,
            rng=self._rng,
        )

    async def _get_runner(self, acc: AccountRecord) -> Any:
        """Return the account's long-lived runner, creating it on first use; the pool never exceeds `concurrency`."""
        runner = self._runners.get(acc.id)
        if runner is None:
            while len(self._runners) >= self._concurrency:
                idle_acc_id = next(
                    (
                        acc_id
                        for acc_id in self._runners
                        if acc_id != acc.id
                        and not self._runner_locks[acc_id].locked()
                        and self._in_flight[acc_id] == 0
                    ),
                    None,
                )
                if idle_acc_id is None:
                    break  # every other browser is busy: nothing safe to unload
                await self._drop_runner(idle_acc_id)
            # re-check: _drop_runner awaits, another task may have created this account's runner meanwhile
            runner = self._runners.get(acc.id)
            if runner is None:
                runner = self._runners[acc.id] = self.create_runner(acc)
        return runner

    async def _drop_runner(self, acc_id: int) -> None:
        runner = self._runners.pop(acc_id, None)
        if runner is not None and hasattr(runner, "close"):
            try:
                await runner.close()
            except Exception:
                pass

    async def _close_runners(self) -> None:
        for acc_id in list(self._runners):
            await self._drop_runner(acc_id)

    @staticmethod
    def _is_usable(acc: AccountRecord) -> bool:
        """A failed sign-in is out; a `pending` account with credentials needs a saved session (cookies.sqlite).

        Accounts without credentials (`default`, manual `--login`) keep working whatever their status says.
        """
        if acc.auth_status == "failed":
            return False
        if acc.auth_status == "pending" and acc.email and not (Path(acc.session_path) / "cookies.sqlite").exists():
            return False
        return True

    WARMUP_INTERVAL = timedelta(hours=4)

    async def _passes_lifecycle_gate(self, acc: AccountRecord) -> bool:
        """Age, daily quota, cooldown and circuit breaker of a signed-in account.

        Accounts that were never signed in through the bot (`default`, manual `--login` without a stamp, still
        `pending`) have no lifecycle clock and are left to `_is_usable`.
        """
        if acc.auth_status != "authenticated":
            return True
        verdict = await AccountLifecycleEngine.evaluate_eligibility(acc, db=self._db)
        if verdict.can_comment:
            return True
        if (acc.id, verdict.reason) not in self._lifecycle_logged:
            self._lifecycle_logged.add((acc.id, verdict.reason))
            logger.warning("[LIFECYCLE] Account {} skipped: {}", acc.name, verdict.reason)
        if verdict.suggested_action == "warm_up":
            self._schedule_warmup(acc)
        return False

    def _schedule_warmup(self, acc: AccountRecord) -> None:
        """Start a background warm-up unless one runs already, the last one is under 4 h old, or this is a dry run."""
        if self._dry_run or acc.id in self._warmup_tasks:
            return
        if acc.last_warmup_at:
            try:
                last = datetime.fromisoformat(acc.last_warmup_at.replace("Z", "+00:00"))
            except ValueError:
                last = None
            if last is not None:
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - last <= self.WARMUP_INTERVAL:
                    return
        self._warmup_tasks[acc.id] = asyncio.create_task(self._run_warmup(acc))

    async def _run_warmup(self, acc: AccountRecord) -> None:
        try:
            async with self._warmup_slot:
                await self._warmer.warm_up(acc, db=self._db)
        except Exception as exc:
            logger.error("[LIFECYCLE] Warm-up of account {} failed: {}", acc.name, exc)
        finally:
            self._warmup_tasks.pop(acc.id, None)

    async def _finish_warmups(self) -> None:
        tasks = list(self._warmup_tasks.values())
        if tasks:
            logger.info("[LIFECYCLE] Waiting for {} warm-up session(s) to finish", len(tasks))
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _may_comment_now(self, acc: AccountRecord) -> bool:
        """Re-check an account that was picked earlier (mode "each") against its current counters in the database."""
        fresh = await self._db.get_account(acc.id)
        return fresh is None or await self._passes_lifecycle_gate(fresh)

    async def _usable_accounts(self) -> list[AccountRecord]:
        """Active accounts that can post: sign-in not failed, not waiting for a first sign-in, allowed by their age."""
        accounts = await self._db.list_active_accounts()
        if not accounts:
            accounts = await self.sync_accounts_from_config()
        usable = [acc for acc in accounts if self._is_usable(acc) and await self._passes_lifecycle_gate(acc)]
        for acc in accounts:
            if acc not in usable and acc.id not in self._skipped_logged:
                self._skipped_logged.add(acc.id)
                logger.warning(
                    "ИНФО | Аккаунт {!r} пропущен (вход: {}): выполните `youtube_bot.py --auth-account {}`",
                    acc.name,
                    acc.auth_status,
                    acc.name,
                )
        return usable

    async def _pick_account(self) -> AccountRecord | None:
        """Select next active account with least total replies, rotated round-robin."""
        accounts = await self._usable_accounts()
        if not accounts:
            return None
        n = len(accounts)
        start = self._account_offset % n
        self._account_offset += 1
        rotated = accounts[start:] + accounts[:start]
        rotated.sort(key=lambda a: a.total_replies + self._in_flight[a.id])
        return rotated[0]

    async def _record_error(self, acc: AccountRecord) -> None:
        """A failed live submission counts toward the account's circuit breaker (3 in a row restrict it)."""
        if self._dry_run:
            return
        try:
            await self._db.record_account_error(acc.id)
        except Exception as exc:
            logger.debug("Не удалось записать ошибку аккаунта {}: {}", acc.name, exc)

    async def _process_video(
        self,
        video: VideoPoolItem,
        target: int,
        account: AccountRecord | None = None,
    ) -> int:
        """
        Post up to `target` replies under one pool video.
        - account=None ("share"): the video's progress counts toward `target`, each reply goes to
          the least-loaded account.
        - account given ("each"): that account writes `target` replies of its own; the video is
          marked completed by execute_video_pool once every account has passed it.
        """
        needed = target if account is not None else max(0, target - video.comments_done)
        if needed <= 0:
            await self._db.update_pool_video_progress(video.id, comments_added=0, completed=True)
            return 0

        logger.info(
            "ИНФО | ▶ Обработка видео {} ({}), требуется ответов: {}",
            video.video_id,
            video.video_url,
            needed,
        )
        video_comments_done = 0

        try:
            target_mode = getattr(self._cfg.collector, "target_mode", "first_reply")
            items = []
            try:
                items = await self._collector.fetch_popular_comments(
                    video.video_url,
                    limit=needed * 4,
                    target_mode=target_mode,
                )
            except Exception as exc:
                logger.warning(
                    "ИНФО | Ошибка fetch_popular_comments для {}: {}",
                    video.video_url,
                    exc,
                )

            if not items and hasattr(self._collector, "collect_from_videos"):
                try:
                    items = await self._collector.collect_from_videos([video.video_url], max_items=needed * 4)
                except Exception as exc:
                    logger.warning("ИНФО | Ошибка collect_from_videos: {}", exc)

            if not items:
                logger.warning("ИНФО | Не найдено подходящих комментариев для видео {}", video.video_id)
                return 0

            for it in items:
                if video_comments_done >= needed:
                    break

                if it.external_id in self._claimed:
                    continue
                self._claimed.add(it.external_id)  # before any await: a parallel account must skip it
                if await self._db.has_replied_to(it.external_id):
                    logger.debug("ИНФО | Пропуск уже обработанного комментария {}", it.external_id)
                    continue

                acc = account or await self._pick_account()
                if acc is not None and account is not None and not await self._may_comment_now(acc):
                    self._claimed.discard(it.external_id)
                    break
                if acc is None:
                    self._claimed.discard(it.external_id)
                    logger.warning("ИНФО | Нет доступных аккаунтов для комментария")
                    break

                self._in_flight[acc.id] += 1
                posted = False
                try:
                    logger.info(
                        "🎯 Цель: {} — {!r} (аккаунт: {!r})",
                        getattr(it, "author", "unknown"),
                        getattr(it, "text", "")[:60],
                        acc.name,
                    )

                    # Prepare response
                    effective_mode = getattr(self._cfg.processor, "reply_mode", "ai")
                    try:
                        if effective_mode == "custom":
                            reply_text = resolve_reply_text(
                                it,
                                custom_reply=None,
                                custom_replies=getattr(self._cfg.processor, "custom_replies", None),
                                rng=self._rng,
                            )
                            if self._processor is not None:
                                reply_text = await self._processor.paraphrase(reply_text)
                        else:
                            if self._processor is not None:
                                reply_text = await self._processor.generate(it)
                            else:
                                reply_text = resolve_reply_text(it, rng=self._rng)
                        logger.info("💬 НАШ ОТВЕТ ({!r}): «{}»", acc.name, reply_text)
                    except Exception as exc:
                        logger.error("ОШИБКА | Сбой подготовки ответа: {}", exc)
                        await self._db.log_activity(
                            ActivityLogCreate(
                                video_id=video.video_id,
                                video_url=video.video_url,
                                target_id=it.external_id,
                                target_author=getattr(it, "author", None),
                                target_text=getattr(it, "text", None),
                                reply_text=None,
                                mode="dry_run" if self._dry_run else "live",
                                status="failed",
                                error=f"Reply generation failed: {exc}",
                            )
                        )
                        continue

                    # Run submission with error isolation. One browser per account stays open
                    # for the whole run; the lock keeps two videos off the same profile.
                    async with self._runner_locks[acc.id]:
                        runner = await self._get_runner(acc)
                        try:
                            sub_res = await runner.run(
                                reply_text,
                                external_id=it.external_id,
                                payload_id=None,
                                dry_run=self._dry_run,
                            )
                        except Exception as exc:
                            await self._drop_runner(acc.id)  # next comment starts a fresh browser
                            await self._record_error(acc)
                            logger.error(
                                "ОШИБКА | Сбой браузера/раннера для аккаунта {!r}: {}",
                                acc.name,
                                exc,
                            )
                            await self._db.log_activity(
                                ActivityLogCreate(
                                    video_id=video.video_id,
                                    video_url=video.video_url,
                                    target_id=it.external_id,
                                    target_author=getattr(it, "author", None),
                                    target_text=getattr(it, "text", None),
                                    reply_text=reply_text,
                                    mode="dry_run" if self._dry_run else "live",
                                    status="failed",
                                    error=str(exc),
                                )
                            )
                            continue

                    mode = "dry_run" if self._dry_run else "live"
                    status = (
                        "simulated"
                        if (self._dry_run and sub_res.success)
                        else ("success" if sub_res.success else "failed")
                    )
                    await self._db.log_activity(
                        ActivityLogCreate(
                            video_id=video.video_id,
                            video_url=video.video_url,
                            target_id=it.external_id,
                            target_author=getattr(it, "author", None),
                            target_text=getattr(it, "text", None),
                            reply_text=reply_text,
                            mode=mode,
                            status=status,
                            error=sub_res.error if not sub_res.success else None,
                            duration_sec=getattr(sub_res, "duration_seconds", None),
                        )
                    )

                    if sub_res.success:
                        # Track payload for deduplication if live
                        if not self._dry_run:
                            try:
                                node = await self._db.upsert_test_node(
                                    self._yt_target.name,
                                    self._yt_target.base_url,
                                )
                                p = await self._db.reserve_payload(
                                    node_id=node.id,
                                    source_ref=it.external_id,
                                    language=getattr(it, "language", "en"),
                                    source_text=it.text,
                                )
                                if p:
                                    await self._db.update_payload_text(p.id, reply_text)
                                    await self._db.mark_payload_submitted(p.id, sub_res.remote_id)
                            except Exception as exc:
                                logger.debug("Payload tracking: {}", exc)

                        is_video_complete = account is None and (
                            video.comments_done + video_comments_done + 1 >= target
                        )
                        await self._db.update_pool_video_progress(
                            pool_video_id=video.id,
                            comments_added=1,
                            completed=is_video_complete,
                        )
                        if self._dry_run:
                            await self._db.increment_account_stats(acc.id, replies=1)
                        else:
                            await self._db.record_account_comment_success(acc.id)
                        logger.success(
                            "УСПЕХ | Аккаунт {!r} успешно ответил на комментарий {} к видео {}",
                            acc.name,
                            it.external_id,
                            video.video_id,
                        )
                        video_comments_done += 1
                        posted = True
                    else:
                        await self._record_error(acc)
                        logger.error(
                            "ОШИБКА | Не удалось отправить ответ аккаунтом {!r} к видео {}: {}",
                            acc.name,
                            video.video_id,
                            sub_res.error,
                        )
                finally:
                    self._in_flight[acc.id] -= 1
                    if not posted:  # a failed comment stays free for another account
                        self._claimed.discard(it.external_id)
                if posted and video_comments_done < needed and not self._dry_run:
                    await asyncio.sleep(
                        self._rng.uniform(self._cfg.limits.pause_min_seconds, self._cfg.limits.pause_max_seconds)
                    )

        except Exception as exc:
            logger.error(
                "ОШИБКА | Непредвиденный сбой при обработке видео {}: {}",
                video.video_id,
                exc,
            )

        return video_comments_done

    async def execute_video_pool(
        self,
        max_comments_per_video: int | None = None,
        comments_target_per_video: int = 2,
    ) -> int:
        """Run the video pool, then wait for the warm-up sessions the lifecycle gate started."""
        try:
            return await self._execute_video_pool(max_comments_per_video, comments_target_per_video)
        finally:
            await self._finish_warmups()

    async def _execute_video_pool(
        self,
        max_comments_per_video: int | None = None,
        comments_target_per_video: int = 2,
    ) -> int:
        """
        Post replies under the pending pool videos with the active accounts.
        - "each": every account passes the whole list, `concurrency` accounts at a time
          (1 = one account after another).
        - "share": the videos are spread across the accounts (least replies / round-robin),
          `concurrency` videos at a time.
        - Error isolation: exceptions in one account/video don't break others.
        """
        await self.ensure_db()
        accounts = await self._usable_accounts()
        if not accounts:
            logger.warning("ИНФО | Нет активных аккаунтов для выполнения пула видео")
            return 0

        videos = await self._db.list_pool_videos(status="pending")
        if not videos:
            logger.info("ИНФО | Нет видео в пуле для обработки")
            return 0

        logger.info(
            "ИНФО | Найдено {} видео в пуле для обработки (аккаунтов: {}, потоков: {}, режим: {})",
            len(videos),
            len(accounts),
            self._concurrency,
            self._account_mode,
        )

        def target_of(video: VideoPoolItem) -> int:
            if max_comments_per_video is not None:
                return max_comments_per_video
            return video.comments_target or comments_target_per_video

        async def _run_account(acc: AccountRecord) -> int:
            async with self._semaphore:
                done = 0
                try:
                    for video in videos:
                        done += await self._process_video(video, target_of(video), acc)
                finally:
                    await self._drop_runner(acc.id)  # the account's browser closes when its turn ends
                return done

        async def _run_video(video: VideoPoolItem) -> int:
            async with self._semaphore:
                return await self._process_video(video, target_of(video))

        if self._account_mode == "each":
            tasks = [_run_account(acc) for acc in accounts]
        else:
            tasks = [_run_video(video) for video in videos]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await self._close_runners()
        if self._account_mode == "each":
            for video in videos:
                await self._db.update_pool_video_progress(video.id, comments_added=0, completed=True)
        total_comments = sum(r for r in results if isinstance(r, int))
        logger.info(
            "ИНФО | Обработка пула видео завершена: успешно отправлено {} ответов",
            total_comments,
        )
        return total_comments

    async def run_video_pool(self, videos_file: str | Path | None = None) -> int:
        """Load videos_file (if given) into the video pool, then run execute_video_pool; returns replies posted."""
        await self.ensure_db()
        await self.sync_accounts_from_config()

        if videos_file is not None:
            v_path = Path(videos_file).expanduser()
            if v_path.exists():
                await self._pool_mgr.load_videos_from_file(v_path)
            else:
                logger.warning("ИНФО | Файл видео не найден: {}", videos_file)

        return await self.execute_video_pool()

    async def close(self) -> None:
        """Close account browsers, collector and database connections."""
        for task in list(self._warmup_tasks.values()):
            task.cancel()
        await asyncio.gather(*self._warmup_tasks.values(), return_exceptions=True)
        await self._close_runners()
        if hasattr(self._collector, "close"):
            try:
                await self._collector.close()
            except Exception:
                pass
        if self._processor is not None and hasattr(self._processor, "close"):
            try:
                await self._processor.close()
            except Exception:
                pass
        if hasattr(self._db, "close"):
            try:
                await self._db.close()
            except Exception:
                pass


__all__ = [
    "MultiAccountOrchestrator",
]
