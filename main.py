from __future__ import annotations

import asyncio
import random
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from loguru import logger

from config import AppConfig, ConfigError, TargetSettings, load_config
from database import Database, Payload, QueueItem, TestNode
from modules.collector import CollectedItem, Collector, CollectorError
from modules.interaction_simulator import InteractionSimulator
from modules.process_guard import bind_to_kill_on_close_job
from modules.projects import project_seed
from modules.processor import DeepSeekProcessor, ProcessorError
from modules.reply_deck import ReplyDeck, prepare_reply
from modules.ui_test_runner import UITestRunner
from modules.validator import DeliveryValidator


from modules.logger_config import setup_bot_logging


def setup_logging(config: AppConfig) -> None:
    setup_bot_logging(level=config.app.log_level, log_file=config.app.log_file)
    logger.info("Логирование настроено (уровень={}, файл={})", config.app.log_level, config.app.log_file)


class Orchestrator:
    """Coordinates collection, generation, browser sessions and delayed validation."""

    def __init__(self, config: AppConfig) -> None:
        self._cfg = config
        self._rng = random.Random()
        self._db = Database(config.app.db_path, project_seed=project_seed(config))
        self._deck = ReplyDeck(self._db, self._rng)
        self._collector = Collector(config.collector, config.browser.proxy)
        self._processor = DeepSeekProcessor(config.deepseek, config.processor, rng=self._rng)
        self._validator = DeliveryValidator(config.validator)
        self._simulator = InteractionSimulator(rng=self._rng)
        self._targets: dict[str, TargetSettings] = {target.name: target for target in config.browser.targets}
        self._session_uses: dict[int, int] = {}
        self._browser_slots = asyncio.Semaphore(config.limits.max_concurrent_sessions)
        self._stop = asyncio.Event()
        self._validation_task: asyncio.Task[None] | None = None

    async def run(self) -> dict[str, Any]:
        stats: dict[str, Any] = {}
        completed = 0
        started = time.monotonic()
        try:
            await self._db.connect()
            await self._db.init_schema()
            await self._db.recover_running_jobs()
            await self._seed_nodes()
            self._log_banner()
            self._validation_task = asyncio.create_task(self._validation_loop(), name="validation-loop")

            total = self._cfg.limits.total_iterations
            for iteration in range(1, total + 1):
                if self._stop.is_set():
                    break
                try:
                    if await self._run_iteration(iteration):
                        completed += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Iteration {} crashed", iteration)
                if iteration < total and not self._stop.is_set():
                    await self._pause()
            await self._drain()
        finally:
            self._stop.set()
            await self._stop_validation_task()
            stats = await self._safe_stats()
            await self._close()
            logger.info(
                "Run finished in {:.1f}s: {} of {} iteration(s) submitted, stats={}",
                time.monotonic() - started,
                completed,
                self._cfg.limits.total_iterations,
                stats,
            )
        return stats

    async def _seed_nodes(self) -> None:
        for target in self._cfg.browser.targets:
            node = await self._db.upsert_test_node(target.name, target.base_url)
            logger.info("Registered test node {} (id={}, url={})", node.name, node.id, node.base_url)

    def _log_banner(self) -> None:
        logger.info(
            "Starting {} | targets={} | iterations={} | cooldown={:.0f}s | validation delay={:.0f}s | render={:.0f}-{:.0f}s",
            self._cfg.app.name,
            ",".join(self._targets),
            self._cfg.limits.total_iterations,
            self._cfg.limits.session_cooldown_seconds,
            self._cfg.validator.delay_seconds,
            self._cfg.browser.render_min_seconds,
            self._cfg.browser.render_max_seconds,
        )

    async def _run_iteration(self, index: int) -> bool:
        node = await self._acquire_node()
        if node is None:
            logger.warning("Iteration {} skipped: no test node became available", index)
            return False

        target = self._targets.get(node.name)
        if target is None:
            logger.error("Iteration {}: node {} has no matching browser target", index, node.name)
            await self._release_node(node, cooldown_seconds=0.0)
            return False

        uses = self._session_uses.get(node.id, 0) + 1
        self._session_uses[node.id] = uses
        cooldown = 0.0
        if uses >= self._cfg.limits.max_iterations_per_session:
            cooldown = self._cfg.limits.session_cooldown_seconds
            self._session_uses[node.id] = 0
            logger.info(
                "Node {} reached {} use(s); cooldown {:.0f}s after this iteration",
                node.name,
                uses,
                cooldown,
            )

        deck_card = None  # custom template drawn for this iteration; burned only when the reply was posted
        posted = False
        try:
            items = await self._collector.collect()
            if not items:
                logger.warning("Iteration {} skipped: collector returned no usable items", index)
                return False

            candidate = None
            shuffled = list(items)
            self._rng.shuffle(shuffled)
            for it in shuffled:
                if not await self._db.has_replied_to(it.external_id):
                    candidate = it
                    break
            if candidate is None:
                logger.info("Iteration {}: all {} collected items have already been replied to", index, len(items))
                return False

            item = candidate
            logger.info(
                "Iteration {}: node={} source={}/{} language={} confidence={:.2f}",
                index,
                node.name,
                item.endpoint,
                item.external_id,
                item.language,
                item.confidence,
            )
            if getattr(self._cfg.processor, "reply_mode", "ai") == "custom":
                prepared = await prepare_reply(
                    item,
                    deck=self._deck,
                    account=f"node:{node.name}",
                    text_settings=self._cfg.processor,
                    custom_replies=getattr(self._cfg.processor, "custom_replies", None),
                    rng=self._rng,
                )
                text, deck_card = prepared.text, prepared.card
            else:
                text = await self._processor.generate(item)
            payload = await self._db.create_payload(
                node_id=node.id,
                text=text,
                language=item.language,
                source_ref=item.external_id,
                source_text=item.text,
            )
            posted = await self._submit_payload(node, target, payload, item)
            return posted
        except (CollectorError, ProcessorError) as exc:
            logger.error("Iteration {} aborted: {}", index, exc)
            return False
        finally:
            await self._deck.settle(deck_card, posted=posted)
            await self._release_node(node, cooldown_seconds=cooldown)

    async def _acquire_node(self) -> TestNode | None:
        deadline = time.monotonic() + self._cfg.app.acquire_timeout_seconds
        while time.monotonic() < deadline:
            if self._stop.is_set():
                return None
            try:
                node = await self._db.acquire_test_node()
            except Exception:
                logger.exception("Failed to acquire a test node")
                node = None
            if node is None:
                await asyncio.sleep(self._cfg.app.poll_interval_seconds)
                continue
            uses = self._session_uses.get(node.id, 0)
            if uses >= self._cfg.limits.max_iterations_per_session:
                self._session_uses[node.id] = 0
                await self._db.release_test_node(
                    node.id, cooldown_seconds=self._cfg.limits.session_cooldown_seconds
                )
                logger.info("Node {} is at its per-session limit; cooling down", node.name)
                continue
            return node
        logger.warning("No test node became available within {:.0f}s", self._cfg.app.acquire_timeout_seconds)
        return None

    async def _release_node(self, node: TestNode, cooldown_seconds: float) -> None:
        try:
            await self._db.release_test_node(node.id, cooldown_seconds=cooldown_seconds)
        except Exception:
            logger.exception("Failed to release test node {}", node.name)

    async def _submit_payload(
        self,
        node: TestNode,
        target: TargetSettings,
        payload: Payload,
        item: CollectedItem,
    ) -> bool:
        runner = UITestRunner(
            self._cfg.browser,
            target,
            simulator=self._simulator,
            artifacts_dir=self._cfg.browser.session_root / "_artifacts",
            rng=self._rng,
        )
        job = await self._db.enqueue(
            "submit",
            payload.id,
            node_id=node.id,
            priority=100,
            max_attempts=self._cfg.limits.max_queue_attempts,
        )
        backoff = self._cfg.limits.retry_backoff_seconds

        while True:
            claimed = await self._db.claim_queue_item(queue_id=job.id)
            if claimed is None:
                logger.warning("Submit job {} is not claimable; giving up", job.id)
                await self._db.mark_payload_failed(payload.id, "submit job is not claimable")
                return False

            async with self._browser_slots:
                result = await runner.run(
                    payload.text,
                    external_id=item.external_id,
                    payload_id=payload.id,
                    query=item.text,
                )

            if result.success:
                await self._db.mark_payload_submitted(payload.id, result.remote_id)
                await self._db.complete_queue_item(claimed.id)
                await self._db.enqueue(
                    "validate",
                    payload.id,
                    node_id=node.id,
                    priority=10,
                    delay_seconds=self._cfg.validator.delay_seconds,
                    max_attempts=self._cfg.validator.max_attempts,
                )
                logger.success(
                    "Payload {} submitted (remote_id={}); validation scheduled in {:.0f}s",
                    payload.id,
                    result.remote_id,
                    self._cfg.validator.delay_seconds,
                )
                return True

            error = result.error or "unknown submission error"
            updated = await self._db.reschedule_queue_item(claimed.id, error, delay_seconds=backoff)
            logger.error(
                "Submit attempt {}/{} for payload {} failed: {}",
                claimed.attempts,
                claimed.max_attempts,
                payload.id,
                error,
            )
            if updated.status == "failed":
                await self._db.mark_payload_failed(payload.id, error)
                return False
            await asyncio.sleep(backoff)

    async def _validation_loop(self) -> None:
        logger.debug("Validation loop started")
        while not self._stop.is_set():
            try:
                claimed = await self._db.claim_queue_item(kind="validate")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to claim a validation job")
                await asyncio.sleep(self._cfg.app.poll_interval_seconds)
                continue

            if claimed is None:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._cfg.app.poll_interval_seconds
                    )
                except asyncio.TimeoutError:
                    pass
                continue

            try:
                await self._handle_validation(claimed)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Validation job {} crashed", claimed.id)
                try:
                    updated = await self._db.reschedule_queue_item(
                        claimed.id,
                        "validation job crashed",
                        delay_seconds=self._cfg.validator.poll_interval_seconds,
                    )
                    if updated.status == "failed":
                        await self._db.mark_payload_failed(
                            updated.payload_id, updated.last_error or "validation crashed"
                        )
                except Exception:
                    logger.exception("Failed to recover validation job {}", claimed.id)
        logger.debug("Validation loop stopped")

    async def _handle_validation(self, job: QueueItem) -> None:
        payload = await self._db.get_payload(job.payload_id)
        if payload is None:
            await self._db.fail_queue_item(job.id, f"payload {job.payload_id} not found")
            logger.error("Validation job {} references missing payload {}", job.id, job.payload_id)
            return

        result = await self._validator.validate(payload)
        if result.delivered:
            await self._db.mark_payload_validated(payload.id)
            await self._db.complete_queue_item(job.id)
            logger.success(
                "Payload {} validated in {:.2f}s ({})",
                payload.id,
                result.duration_seconds,
                result.checked_url,
            )
            return

        error = result.error or "payload not rendered"
        if job.attempts >= job.max_attempts:
            await self._db.mark_payload_failed(payload.id, error)
            await self._db.fail_queue_item(job.id, error)
            logger.error(
                "Payload {} failed validation after {} attempt(s): {}", payload.id, job.attempts, error
            )
            return

        await self._db.reschedule_queue_item(
            job.id,
            error,
            delay_seconds=self._cfg.validator.poll_interval_seconds,
        )
        logger.warning(
            "Payload {} not visible yet ({}); retry {}/{} in {:.0f}s",
            payload.id,
            error,
            job.attempts,
            job.max_attempts,
            self._cfg.validator.poll_interval_seconds,
        )

    async def _drain(self) -> None:
        deadline = time.monotonic() + self._cfg.app.drain_timeout_seconds
        while time.monotonic() < deadline and not self._stop.is_set():
            queued = await self._db.count_queue(status="queued", kind="validate")
            running = await self._db.count_queue(status="running", kind="validate")
            pending = queued + running
            if pending == 0:
                return
            logger.info("Waiting for {} validation job(s) to finish (queued={}, running={})", pending, queued, running)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._cfg.app.poll_interval_seconds)
            except asyncio.TimeoutError:
                continue
        remaining = await self._db.count_queue(status="queued", kind="validate")
        remaining += await self._db.count_queue(status="running", kind="validate")
        if remaining:
            logger.warning(
                "Drain timeout reached with {} validation job(s) left; they stay queued for the next run",
                remaining,
            )

    async def _pause(self) -> None:
        delay = self._rng.uniform(self._cfg.limits.pause_min_seconds, self._cfg.limits.pause_max_seconds)
        logger.info("Pausing {:.1f}s before the next iteration", delay)
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _stop_validation_task(self) -> None:
        task, self._validation_task = self._validation_task, None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _safe_stats(self) -> dict[str, Any]:
        try:
            return await self._db.stats()
        except Exception:
            logger.exception("Failed to collect database stats")
            return {}

    async def _close(self) -> None:
        for name, closer in (
            ("collector", self._collector.close),
            ("processor", self._processor.close),
            ("validator", self._validator.close),
            ("database", self._db.close),
        ):
            try:
                await closer()
            except Exception:
                logger.exception("Failed to close {}", name)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    config_path = str(Path(__file__).with_name("config.yaml"))
    for idx, arg in enumerate(arguments):
        if arg in {"--config", "-c"} and idx + 1 < len(arguments):
            config_path = arguments[idx + 1]
            break
        if arg.startswith("--config="):
            config_path = arg.split("=", 1)[1]
            break

    if "--help" in arguments or "-h" in arguments:
        print("Usage: python main.py [--wizard | -i] [--config PATH] [--videos-file PATH] [--concurrency N] [--dry-run]")
        print("Runs the automated E2E pipeline orchestrator, or launches the interactive wizard.")
        print("Options:")
        print("  --videos-file PATH    Run multi-account video pool using URLs/IDs from text file")
        print("  --concurrency N       Maximum concurrent account browser sessions (default: config or 1)")
        print("  --dry-run             Simulate commenting without posting live")
        print("  --headless            Force headless browser mode")
        print("  --no-headless         Force headful browser mode with visible windows")
        return 0

    bind_to_kill_on_close_job()  # browser children die with this process, even on a crash

    if (
        "--wizard" in arguments
        or "-i" in arguments
        or "--interactive" in arguments
        or (not arguments and sys.stdin.isatty())
    ):
        from modules.wizard import run_wizard_app
        run_wizard_app(config_path)
        return 0

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(config)

    # Parse multi-account video pool flags if provided
    videos_file: str | None = None
    concurrency: int = config.limits.max_concurrent_sessions
    dry_run: bool = False
    headless: bool | None = None

    idx = 0
    while idx < len(arguments):
        arg = arguments[idx]
        if arg == "--videos-file" and idx + 1 < len(arguments):
            videos_file = arguments[idx + 1]
            idx += 2
            continue
        if arg.startswith("--videos-file="):
            videos_file = arg.split("=", 1)[1]
            idx += 1
            continue
        if arg in {"--concurrency", "-j"} and idx + 1 < len(arguments):
            with suppress(ValueError):
                concurrency = int(arguments[idx + 1])
            idx += 2
            continue
        if arg.startswith("--concurrency="):
            with suppress(ValueError):
                concurrency = int(arg.split("=", 1)[1])
            idx += 1
            continue
        if arg == "--dry-run":
            dry_run = True
            idx += 1
            continue
        if arg == "--headless":
            headless = True
            idx += 1
            continue
        if arg == "--no-headless":
            headless = False
            idx += 1
            continue
        idx += 1

    if videos_file is not None:
        from modules.multi_account_orchestrator import MultiAccountOrchestrator
        logger.info(
            "Запуск MultiAccountOrchestrator: videos_file={!r}, concurrency={}, dry_run={}",
            videos_file,
            concurrency,
            dry_run,
        )
        multi_orch = MultiAccountOrchestrator(
            config,
            concurrency=concurrency,
            dry_run=dry_run,
            headless=headless,
        )
        try:
            comments_done = asyncio.run(multi_orch.run_video_pool(videos_file=videos_file))
            logger.info("Пул видео обработан: ответов {}", comments_done)
        finally:
            asyncio.run(multi_orch.close())
        return 0
    try:
        asyncio.run(Orchestrator(config).run())
    except KeyboardInterrupt:
        logger.warning("Interrupted by user; shutting down")
        return 130
    except Exception:
        logger.exception("Fatal orchestrator error")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
