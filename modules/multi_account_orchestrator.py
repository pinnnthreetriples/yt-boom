from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import time
from typing import Any, Callable, NamedTuple

from loguru import logger

from config import AppConfig, BrowserSettings, ProxyUnavailableError, TargetSettings, load_config, resolve_account_proxy
from database import (
    AccountRecord,
    ActivityLogCreate,
    Database,
    ProjectRecord,
    VideoPoolItem,
)
from modules.account_warmer import AccountWarmer
from modules.comment_visibility import CommentVisibilityChecker
from modules.geo_guard import warn_if_exit_mismatch
from modules.interaction_simulator import InteractionSimulator
from modules.lifecycle_engine import AccountLifecycleEngine, explain_block
from modules.pool_manager import PoolManager
from modules.projects import REFERENCE_LINKS, project_config, project_seed
from modules.proxy_library import sync_config_accounts
from modules.processor import DeepSeekProcessor, resolve_reply_text, should_paraphrase
from modules.ui_test_runner import SubmissionResult
from modules.youtube_collector import YouTubeCollector, empty_search_hint, extract_video_id, title_keywords
from modules.youtube_runner import YouTubeRunner


class SearchRunResult(NamedTuple):
    """Outcome of a topic search run: accounts that could post, videos found, replies posted (or simulated)."""

    accounts: int
    found: int
    replies: int


class ProjectRunResult(NamedTuple):
    """Outcome of one project's run: accounts that could post, videos taken (found + own links), replies posted
    (or simulated), and why it was skipped ("" when it ran)."""

    project: str
    accounts: int
    found: int
    replies: int
    skipped: str = ""


class MultiAccountOrchestrator:
    """
    Posts replies under the video pool with several YouTube accounts:
    `concurrency` accounts (or videos) at a time, account_mode "each" / "share",
    error isolation per account and video.
    """

    _project: ProjectRecord | None = None  # set per instance; the class default keeps a run without projects

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
        visibility_checker: CommentVisibilityChecker | None = None,
        project: ProjectRecord | None = None,
        claimed: set[str] | None = None,
    ) -> None:
        """`project`: run only its members, its video pool and its target / replies (`project_config`); None keeps
        the run without projects (every active account, every pool). `claimed`: the comments taken earlier in the
        same launch (shared by the projects run one after another: one comment gets one reply from the whole bot)."""
        base = config_or_path if isinstance(config_or_path, AppConfig) else load_config(config_or_path)
        self._project = project
        self._cfg = project_config(base, project) if project is not None else base

        if headless is not None:
            self._cfg.browser.headless = headless

        self._db = db or Database(self._cfg.app.db_path, project_seed=project_seed(base))
        self._concurrency = max(1, concurrency)
        self._semaphore = asyncio.Semaphore(self._concurrency)
        self._dry_run = dry_run
        self._account_mode = account_mode or self._cfg.limits.account_mode
        self._rng = rng or random.Random()
        # the search proxy lives in the database: `ensure_db` sets it on the collector made here
        self._own_collector = collector is None
        self._collector = collector or YouTubeCollector(self._cfg.collector, None)
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
        self._claimed: set[str] = claimed if claimed is not None else set()
        self._skipped_logged: set[int] = set()
        self._lifecycle_logged: set[tuple[int, str]] = set()
        self._project_logged: set[int] = set()
        self._warmer = warmer or AccountWarmer(self._cfg.browser, simulator=self._simulator, rng=self._rng)
        self._warmup_tasks: dict[int, asyncio.Task[Any]] = {}
        self._warmup_failed_at: dict[int, datetime] = {}
        self._gated_accounts: set[int] = set()  # accounts held back by their lifecycle limits during this run
        self._warmup_slot = asyncio.Semaphore(1)  # one warm-up browser at a time
        # An injected runner_factory means no real YouTube behind the runners (tests): nothing to look for then.
        delay = self._cfg.limits.visibility_check_delay_seconds
        self._visibility = visibility_checker or (
            CommentVisibilityChecker(self._collector, delay) if runner_factory is None and delay > 0 else None
        )
        self._visibility_tasks: set[asyncio.Task[Any]] = set()

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

    @property
    def project(self) -> ProjectRecord | None:
        return self._project

    @property
    def _project_id(self) -> int | None:
        return self._project.id if self._project is not None else None

    async def ensure_db(self) -> None:
        """Ensure database connection and schema initialization."""
        if not self._db.is_connected:
            await self._db.connect()
        await self._db.init_schema()
        if self._own_collector:  # ProxyUnavailableError: a search proxy is set but unreadable, never go direct
            await self._collector.set_proxy(await self._db.search_proxy_url())

    async def sync_accounts_from_config(self) -> list[AccountRecord]:
        """Register the accounts of config.yaml that the database lacks; never overwrites a stored proxy or active
        flag (a yaml proxy is imported only for an account without one)."""
        await self.ensure_db()
        await sync_config_accounts(self._db, self._cfg.browser.accounts)
        return await self._db.list_active_accounts()

    def create_runner(self, acc: AccountRecord) -> Any:
        """Instantiate a YouTubeRunner or custom mock runner for the given account."""
        if self._runner_factory is not None:
            return self._runner_factory(acc)
        return YouTubeRunner(
            browser_settings=self._cfg.browser,
            target=self._yt_target,
            session_dir=acc.session_path,
            proxy=resolve_account_proxy(acc),
            account_name=acc.name,
            simulator=self._simulator,
            rng=self._rng,
            locale=acc.locale,
        )

    async def _get_runner(self, acc: AccountRecord) -> Any:
        """Return the account's long-lived runner, creating it on first use; the pool never exceeds `concurrency`."""
        runner = self._runners.get(acc.id)
        if runner is None:
            if acc.expected_country:  # once per account and process; only a warning, a running account is never blocked
                await warn_if_exit_mismatch(acc.name, acc.expected_country, resolve_account_proxy(acc))
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
            logger.warning("[LIFECYCLE] {}: пропущен — {}", acc.name, explain_block(acc, verdict))
        if verdict.suggested_action == "warm_up":
            self._schedule_warmup(acc)
        return False

    async def _passes_project_gate(self, acc: AccountRecord) -> bool:
        """The project's own daily ceiling for this account (`daily_per_account`); always true without a project.

        It comes on top of the account's lifecycle limits: an account answers only when both let it.
        """
        if self._project is None:
            return True
        quota = await self._db.project_daily_gate(self._project.id, acc.id)
        if quota.allowed:
            return True
        if acc.id not in self._project_logged:
            self._project_logged.add(acc.id)
            reason = (
                f"в проекте сегодня {quota.done}/{quota.limit}" if quota.limit is not None else "не участник проекта"
            )
            logger.warning("[ПРОЕКТ] {} · {}: пропущен — {}", self._project.name, acc.name, reason)
        return False

    def _schedule_warmup(self, acc: AccountRecord) -> None:
        """Start a background warm-up unless one runs already, the last one is under 4 h old, or this is a dry run.

        An account with «Автопрогрев» off or «В работе» off never gets one (a manual warm-up always works).
        """
        if self._dry_run or acc.id in self._warmup_tasks or not acc.auto_warmup or not acc.is_active:
            return
        failed_at = self._warmup_failed_at.get(acc.id)
        if failed_at is not None and datetime.now(timezone.utc) - failed_at <= self.WARMUP_INTERVAL:
            return  # a failed warm-up is not retried on every account pick
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
            async with self._warmup_slot, self._runner_locks[acc.id]:
                # the warm-up opens its own browser on the account's profile: free it from a long-lived runner first
                await self._drop_runner(acc.id)
                result = await self._warmer.warm_up(acc, db=self._db)
            if not getattr(result, "success", True):
                self._warmup_failed_at[acc.id] = datetime.now(timezone.utc)
        except Exception as exc:
            self._warmup_failed_at[acc.id] = datetime.now(timezone.utc)
            logger.error("[LIFECYCLE] Warm-up of account {} failed: {}", acc.name, exc)
        finally:
            self._warmup_tasks.pop(acc.id, None)

    async def _finish_warmups(self) -> None:
        tasks = list(self._warmup_tasks.values())
        if tasks:
            logger.info("[LIFECYCLE] Waiting for {} warm-up session(s) to finish", len(tasks))
            await asyncio.gather(*tasks, return_exceptions=True)

    def _watch_visibility(self, acc: AccountRecord, video: VideoPoolItem, remote_id: str | None) -> None:
        """Check in the background that a live reply shows up for anonymous visitors; never blocks the next comment."""
        if self._visibility is None or self._dry_run or not remote_id:
            return
        task = asyncio.create_task(self._visibility.watch(video.video_id, remote_id, acc.name))
        self._visibility_tasks.add(task)
        task.add_done_callback(self._visibility_tasks.discard)

    async def _finish_visibility_checks(self) -> None:
        tasks = list(self._visibility_tasks)
        if tasks:
            logger.info("[ВИДИМОСТЬ] Ожидание проверки видимости ответов: {}", len(tasks))
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cancel_visibility_checks(self) -> None:
        tasks = list(self._visibility_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _gate_account(self, acc: AccountRecord) -> None:
        """Stop `acc` for this run: its remaining videos stay in the pool for the next one."""
        if acc.id in self._gated_accounts:
            return
        self._gated_accounts.add(acc.id)
        if self._account_mode == "each":  # in "share" mode nothing stops: the next pick takes another account
            logger.info(
                "[LIFECYCLE] Account {} reached its comment limits: a run leaves about one comment per account, "
                "the remaining videos stay pending for the next run",
                acc.name,
            )

    async def _may_comment_now(self, acc: AccountRecord) -> bool:
        """Re-check an account that was picked earlier (mode "each") against its current counters in the database."""
        fresh = await self._db.get_account(acc.id)
        if fresh is not None and not fresh.is_active:  # switched off in the wizard while the run was going
            return False
        if fresh is not None and not await self._passes_lifecycle_gate(fresh):
            return False
        return await self._passes_project_gate(fresh or acc)

    async def _usable_accounts(self) -> list[AccountRecord]:
        """Active accounts that can post: sign-in not failed, not waiting for a first sign-in, allowed by their age
        (and, in a project, its members only, within the project's daily ceiling)."""
        if self._project is not None:
            accounts = await self._db.list_project_accounts(self._project.id, active_only=True)
        else:
            accounts = await self._db.list_active_accounts()
            if not accounts:
                accounts = await self.sync_accounts_from_config()
        for acc in [acc for acc in accounts if acc.proxy_unavailable]:  # fail closed: never post from the own IP
            accounts.remove(acc)
            if acc.id not in self._skipped_logged:
                self._skipped_logged.add(acc.id)
                try:
                    resolve_account_proxy(acc)
                except ProxyUnavailableError as exc:
                    logger.error("ИНФО | {}", exc)
        usable = [
            acc for acc in accounts
            if self._is_usable(acc) and await self._passes_lifecycle_gate(acc) and await self._passes_project_gate(acc)
        ]
        for acc in accounts:
            if acc not in usable and not self._is_usable(acc) and acc.id not in self._skipped_logged:
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
                    self._gate_account(acc)
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
                                project_id=self._project_id,
                                account_id=acc.id,
                            )
                        )
                        continue

                    # Run submission with error isolation. One browser per account stays open
                    # for the whole run; the lock keeps two videos off the same profile.
                    async with self._runner_locks[acc.id]:
                        # picked by parallel workers before any of them posted: re-check against the saved counters
                        if not await self._may_comment_now(acc):
                            self._gate_account(acc)
                            continue
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
                                    project_id=self._project_id,
                                    account_id=acc.id,
                                )
                            )
                            continue
                        if not self._dry_run:  # inside the lock: the next worker of this account sees it
                            if sub_res.success:
                                await self._db.record_account_comment_success(acc.id)
                                if self._project is not None:
                                    await self._db.bump_project_daily(self._project.id, acc.id)
                            else:
                                await self._record_error(acc)

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
                            project_id=self._project_id,
                            account_id=acc.id,
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
                        logger.success(
                            "УСПЕХ | Аккаунт {!r} успешно ответил на комментарий {} к видео {}",
                            acc.name,
                            it.external_id,
                            video.video_id,
                        )
                        video_comments_done += 1
                        posted = True
                        self._watch_visibility(acc, video, sub_res.remote_id)
                    else:
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
                    if account is not None and not await self._may_comment_now(acc):
                        self._gate_account(acc)  # the cooldown refuses the next comment: no point in pausing
                        break
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
        videos: list[VideoPoolItem] | None = None,
    ) -> int:
        """Run the video pool (or the given `videos`), then wait for the warm-up sessions the lifecycle gate started."""
        try:
            return await self._execute_video_pool(max_comments_per_video, comments_target_per_video, videos)
        except BaseException:
            await self._cancel_visibility_checks()  # leaving on an error: do not wait out the check delay
            raise
        finally:
            await self._finish_warmups()
            await self._finish_visibility_checks()

    async def _execute_video_pool(
        self,
        max_comments_per_video: int | None = None,
        comments_target_per_video: int = 2,
        videos: list[VideoPoolItem] | None = None,
    ) -> int:
        """
        Post replies under the pending videos of the user's own links (or under `videos`) with the active accounts.
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

        if videos is None:
            videos = await self._db.list_pool_videos(status="pending", source="links", project_id=self._project_id)
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
                        if acc.id in self._gated_accounts:
                            break  # no comments can be posted any more: do not fetch the remaining videos
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
        if self._account_mode == "each" and not self._gated_accounts:
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
                await self._pool_mgr.load_videos_from_file(
                    v_path, self._project.comments_per_video if self._project else 2, project_id=self._project_id
                )
            else:
                logger.warning("ИНФО | Файл видео не найден: {}", videos_file)

        return await self.execute_video_pool()

    async def run_search(
        self,
        query: str,
        *,
        min_views: int = 0,
        max_views: int | None = None,
        max_age_hours: int | None = None,
        max_videos: int = 5,
        comments_per_video: int = 2,
    ) -> SearchRunResult:
        """Topic search through the accounts: search (via the search proxy), the found videos go into the pool
        (source 'search'), then the same run as the own links: every active account, `concurrency`,
        account_mode and the lifecycle limits. A video found again is taken again; answered comments are skipped.
        """
        await self.ensure_db()
        await self.sync_accounts_from_config()
        accounts = await self._usable_accounts()
        if not accounts:
            logger.warning("ИНФО | Нет аккаунтов, которые могут отвечать сейчас: поиск по теме не запускался")
            await self._finish_warmups()  # the lifecycle gate may have started warm-ups, as in a pool run
            return SearchRunResult(0, 0, 0)

        logger.info(
            "Поиск видео по запросу {!r} [просмотров: {}..{}, не старше: {} ч, видео за запуск: {}]",
            query,
            min_views,
            max_views if max_views is not None else "∞",
            max_age_hours or "∞",
            max_videos,
        )
        found = await self._collector.search_videos(
            query=query,
            min_views=min_views,
            max_views=max_views,
            limit=max_videos,
            max_age_hours=max_age_hours,
        )
        if not found:
            logger.warning(
                "Не найдено видео по запросу {!r} с заданными фильтрами: {}",
                query,
                empty_search_hint(min_views, max_views, max_age_hours),
            )
            return SearchRunResult(len(accounts), 0, 0)

        videos = [
            await self._db.queue_search_video(v["url"], comments_target=comments_per_video, project_id=self._project_id)
            for v in found
        ]
        for v in found:
            logger.info("▶ Найдено: {!r} (ID: {}, просмотров: {:,})", v.get("title", ""), v["video_id"], v.get("view_count", 0))
        replies = await self.execute_video_pool(videos=videos)
        return SearchRunResult(len(accounts), len(videos), replies)

    # ------------------------------------------------------------------ projects

    async def run_project(self, topics: list[str] | None = None) -> ProjectRunResult:
        """One project's run: its members only, its filters, its replies. The videos come, in this order, from every
        topic (`topics` replaces the project's own for this run only), the reference links (similar videos, title
        words) and the own links still waiting; a video is taken once per run. Then the same reply run as the own
        links: `concurrency`, account_mode, the lifecycle limits and the project's daily ceiling."""
        if self._project is None:
            raise RuntimeError("run_project needs a project")
        project = self._project
        await self.ensure_db()
        accounts = await self._usable_accounts()
        if not accounts:
            logger.warning("[ПРОЕКТ] {}: нет аккаунтов, которые могут отвечать сейчас", project.name)
            await self._finish_warmups()  # the lifecycle gate may have started warm-ups, as in a pool run
            return ProjectRunResult(project.name, 0, 0, 0, "нет аккаунтов, которые могут отвечать сейчас")
        logger.info("[ПРОЕКТ] ▶ {} · аккаунтов: {}", project.name, len(accounts))
        videos = await self._collect_project_videos(project.search_topics if topics is None else topics)
        if not videos:
            logger.warning("[ПРОЕКТ] {}: видео не найдено", project.name)
            return ProjectRunResult(project.name, len(accounts), 0, 0, "видео не найдено")
        replies = await self.execute_video_pool(videos=videos)
        return ProjectRunResult(project.name, len(accounts), len(videos), replies)

    async def _collect_project_videos(self, topics: list[str]) -> list[VideoPoolItem]:
        """The run's videos, unique by video id: topics, then reference links, then the own links waiting."""
        assert self._project is not None
        project = self._project
        taken: dict[str, VideoPoolItem] = {}
        own = await self._db.list_pool_videos(status="all", source="links", project_id=project.id)
        own_ids = {video.video_id for video in own}
        for topic in topics:
            await self._take_found(taken, await self._search_topic(topic), f"тема «{topic}»")
        references = sorted(own, key=lambda video: video.id, reverse=True)[:REFERENCE_LINKS]
        if references and project.use_similar:  # the cap counts new videos only: the taken ones are left out
            found = await self._similar_videos(references, own_ids | set(taken))
            await self._take_found(taken, found, "похожие на свои ссылки")
        if references and project.use_title_words:
            found = await self._title_word_videos(references, own_ids | set(taken))
            await self._take_found(taken, found, "слова из названий")
        waiting = await self._db.list_pool_videos(status="pending", source="links", project_id=project.id)
        for video in waiting:
            taken.setdefault(video.video_id, video)
        if waiting:
            logger.info("[ПРОЕКТ] {} · свои ссылки: ждут {}", project.name, len(waiting))
        return list(taken.values())

    def _filters(self) -> dict[str, Any]:
        col = self._cfg.collector
        return {"min_views": col.min_views or 0, "max_views": col.max_views, "max_age_hours": col.max_age_hours}

    async def _search_topic(self, query: str, extra: int = 0) -> list[dict[str, Any]]:
        """A topic search within the project's filters: `max_videos` (+ `extra` that the caller will leave out)."""
        limit = self._cfg.collector.max_videos + extra
        try:
            found = await self._collector.search_videos(query=query, limit=limit, **self._filters())
        except Exception as exc:  # one topic failing must not stop the project
            logger.warning("[ПРОЕКТ] Поиск «{}» не удался: {}", query, exc)
            return []
        if not found:
            logger.warning("Не найдено видео по запросу {!r} с заданными фильтрами: {}", query, empty_search_hint(**self._filters()))
        return found

    async def _similar_videos(self, references: list[VideoPoolItem], exclude: set[str]) -> list[dict[str, Any]]:
        """Recommendations next to the reference links, at most `max_videos` for the whole run."""
        limit = self._cfg.collector.max_videos
        found: list[dict[str, Any]] = []
        for reference in references:
            if len(found) >= limit:
                break
            try:
                related = await self._collector.related_videos(
                    reference.video_id, limit=limit + len(exclude), **self._filters()
                )
            except Exception as exc:
                logger.warning("[ПРОЕКТ] Похожие на {} не получены: {}", reference.video_id, exc)
                continue
            self._add_new(found, related, exclude, limit)
        return found

    async def _title_word_videos(self, references: list[VideoPoolItem], exclude: set[str]) -> list[dict[str, Any]]:
        """A topic search by the significant words of each reference link's title, at most `max_videos` in total."""
        limit = self._cfg.collector.max_videos
        found: list[dict[str, Any]] = []
        queries: set[str] = set()
        for reference in references:
            if len(found) >= limit:
                break
            try:
                query = title_keywords(await self._collector.video_title(reference.video_id))
            except Exception as exc:
                logger.warning("[ПРОЕКТ] Название {} не получено: {}", reference.video_id, exc)
                continue
            if not query or query.casefold() in queries:
                logger.info("[ПРОЕКТ] {}: в названии нет слов для поиска, пропуск", reference.video_id)
                continue
            queries.add(query.casefold())
            logger.info("[ПРОЕКТ] Слова из названия {}: «{}»", reference.video_id, query)
            self._add_new(found, await self._search_topic(query, extra=len(exclude)), exclude, limit)
        return found

    @staticmethod
    def _add_new(found: list[dict[str, Any]], videos: list[dict[str, Any]], exclude: set[str], limit: int) -> None:
        for video in videos:
            if len(found) >= limit:
                return
            if video["video_id"] not in exclude and all(v["video_id"] != video["video_id"] for v in found):
                found.append(video)

    async def _take_found(self, taken: dict[str, VideoPoolItem], found: list[dict[str, Any]], label: str) -> None:
        """Queue found videos into the project's pool for this run (a video already taken is skipped)."""
        assert self._project is not None
        added = 0
        for video in found:
            if video["video_id"] in taken:
                continue
            taken[video["video_id"]] = await self._db.queue_search_video(
                video["url"], comments_target=self._cfg.collector.comments_per_video, project_id=self._project.id
            )
            added += 1
            logger.info("▶ Найдено ({}): {!r} (ID: {}, просмотров: {:,})", label, video.get("title", ""),
                        video["video_id"], video.get("view_count", 0))
        logger.info("[ПРОЕКТ] {} · {}: новых видео {}", self._project.name, label, added)

    async def close(self) -> None:
        """Close account browsers, collector and database connections."""
        for task in list(self._warmup_tasks.values()):
            task.cancel()
        await asyncio.gather(*self._warmup_tasks.values(), return_exceptions=True)
        await self._cancel_visibility_checks()
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
    "ProjectRunResult",
    "SearchRunResult",
]
