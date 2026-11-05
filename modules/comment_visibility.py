from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from typing import Any

from loguru import logger

_API_KEY_RE = re.compile(r'"INNERTUBE_API_KEY":"([^"]+)"')
_ANY_COMMENT_ID_RE = re.compile(r'"commentId"\s*:\s*"')
_CLIENT_VERSION_RE = re.compile(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"')


class CommentVisibilityChecker:
    """Looks for a posted reply the way an anonymous visitor sees it (InnerTube /next, no cookies).

    Observability only: a miss is logged as a warning and never counts as an error of the account. The check
    can be wrong (propagation delay, YouTube changing the response), so it only ever says "not seen".
    A "seen" is only as good as the id it is given: it must be the new reply's own id, never its parent's.
    """

    def __init__(
        self,
        collector: Any,
        delay_seconds: float = 45.0,
        retry_delays: Sequence[float] = (),
    ) -> None:
        self._collector = collector  # the orchestrator's YouTubeCollector: its curl_cffi session is reused
        self._delay = max(0.0, delay_seconds)
        self._retry_delays = tuple(max(0.0, delay) for delay in retry_delays)

    async def is_visible(self, video_id: str, comment_id: str) -> bool | None:
        """True / False: the reply is / is not in the anonymous thread view. None: could not be checked."""
        try:
            await self._collector.open()
            session = self._collector._session
            # `lc=<id>` makes YouTube pin the linked reply (with its thread open) in the first comments page
            resp = await session.get(f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}")
            if resp.status_code != 200:
                logger.debug("Видимость {}: страница видео ответила HTTP {}", comment_id, resp.status_code)
                return None
            html = resp.text
            api_key = _API_KEY_RE.search(html)
            token = self._collector._extract_comments_token(html)
            if api_key is None or not token:
                logger.debug("Видимость {}: на странице нет ключа API или токена комментариев", comment_id)
                return None
            version = _CLIENT_VERSION_RE.search(html)
            payload = {
                "context": {
                    "client": {
                        "clientName": "WEB",
                        "clientVersion": version.group(1) if version else "2.20261029.01.00",
                    }
                },
                "continuation": token,
            }
            resp = await session.post(
                f"https://www.youtube.com/youtubei/v1/next?key={api_key.group(1)}",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                logger.debug("Видимость {}: /next ответил HTTP {}", comment_id, resp.status_code)
                return None
            # A real answer is JSON with `onResponseReceivedEndpoints` and (live, 2026-11) carries 20+ commentId keys
            # (`commentEntityPayload.properties.commentId`, `commentViewModel.commentId`); anything else is "unknown".
            if not isinstance(resp.json(), dict) or not _ANY_COMMENT_ID_RE.search(resp.text):
                logger.debug("Видимость {}: /next не отдал список комментариев", comment_id)
                return None
            # only the /next answer counts: the watch page HTML repeats `lc=` whatever the comment's state
            return re.search(r'"commentId"\s*:\s*"' + re.escape(comment_id) + '"', resp.text) is not None
        except Exception as exc:
            logger.debug("Проверка видимости комментария {} не выполнена: {}", comment_id, exc)
            return None

    async def _look(self, video_id: str, comment_id: str) -> bool | None:
        try:
            return await self.is_visible(video_id, comment_id)
        except Exception as exc:  # a background task: nothing may escape as "exception was never retrieved"
            logger.debug("Проверка видимости комментария {} прервана: {}", comment_id, exc)
            return None

    async def watch(self, video_id: str, comment_id: str, account_name: str) -> bool | None:
        """Wait, look for the reply (again after each retry delay while it is not seen) and log the outcome.

        The first sighting ends the series. A miss ("False") is only reported after the last look, an unverifiable
        answer ("None") is retried like a miss. Every outcome is logged, so the log tells "confirmed" from
        "not found" from "could not be checked". The caller runs it in the background.
        """
        waited = self._delay
        await asyncio.sleep(self._delay)
        seen = await self._look(video_id, comment_id)
        looks = 1
        for pause in self._retry_delays:
            if seen:
                break
            await asyncio.sleep(pause)
            waited += pause
            seen = await self._look(video_id, comment_id)
            looks += 1

        if seen is True:
            logger.success(
                "[ВИДИМОСТЬ] Ответ {} аккаунта {!r} под видео {} виден всем зрителям (проверка {} через {:.0f}с)",
                comment_id,
                account_name,
                video_id,
                looks,
                waited,
            )
        elif seen is False:
            logger.warning(
                "[ВИДИМОСТЬ] Ответ {} аккаунта {!r} под видео {} не найден в анонимной выдаче: проверок {}, "
                "последняя через {:.0f}с (не обязательно скрыт: возможна задержка или ошибка проверки)",
                comment_id,
                account_name,
                video_id,
                looks,
                waited,
            )
        else:
            logger.warning(
                "[ВИДИМОСТЬ] Ответ {} аккаунта {!r} под видео {}: не удалось проверить за {} проверок(ки) "
                "(YouTube не отдал список комментариев или сеть недоступна)",
                comment_id,
                account_name,
                video_id,
                looks,
            )
        return seen


_STATE_BY_RESULT: dict[bool | None, str] = {True: "seen", False: "unseen", None: "unknown"}


def visibility_enabled(cfg: Any) -> bool:
    """The check is on when the switch is on and the delay is above 0 (0 keeps meaning «off» for old configs)."""
    limits = cfg.limits
    return bool(limits.visibility_check_enabled) and limits.visibility_check_delay_seconds > 0


def make_visibility_checker(cfg: Any, collector: Any) -> CommentVisibilityChecker | None:
    """A checker for `collector` when the config enables the check, else None; the one place with the condition."""
    if not visibility_enabled(cfg):
        return None
    return CommentVisibilityChecker(
        collector, cfg.limits.visibility_check_delay_seconds, tuple(cfg.limits.visibility_retry_delays_seconds)
    )


async def watch_and_record(
    checker: CommentVisibilityChecker,
    db: Any,
    log_id: int,
    video_id: str,
    remote_id: str,
    account_name: str,
) -> str | None:
    """Run the check and store seen / unseen / unknown in the journal record `log_id`; never raises."""
    try:
        result = await checker.watch(video_id, remote_id, account_name)
        state = _STATE_BY_RESULT[result if result is None else bool(result)]
        await db.set_activity_visibility(log_id, state)
        return state
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # a background task: nothing may escape as "exception was never retrieved"
        logger.debug("Результат проверки видимости {} не записан: {}", remote_id, exc)
        return None


def visibility_wait_limit(checker: Any) -> float:
    """The longest a run may wait for `checker`'s background look: its delay, every retry pause and a minute of slack.

    Reads the numbers defensively: a stand-in checker without them counts as 0 seconds.
    """
    delay = getattr(checker, "_delay", 0.0)
    retries = getattr(checker, "_retry_delays", ())
    total = float(delay) if isinstance(delay, (int, float)) else 0.0
    if isinstance(retries, (tuple, list)):
        total += sum(float(pause) for pause in retries if isinstance(pause, (int, float)))
    return total + 60.0


class VisibilityWatcher:
    """Background checks of one run: schedules `watch_and_record`, waits for them (bounded) or cancels them.

    Shared by the CLI loops of `youtube_bot.py`; the orchestrator keeps its own task set with the same helpers.
    """

    def __init__(self, checker: CommentVisibilityChecker | None, db: Any) -> None:
        self._checker = checker
        self._db = db
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def tasks(self) -> set[asyncio.Task[Any]]:
        return self._tasks

    def schedule(
        self,
        log_id: int | None,
        video_id: str,
        remote_id: str | None,
        account_name: str,
        *,
        dry_run: bool = False,
    ) -> asyncio.Task[Any] | None:
        """Start a background check after a live reply; None (and one debug line) when it must not run."""
        if self._checker is None:
            logger.debug("[ВИДИМОСТЬ] Проверка выключена: ответ не проверяется")
            return None
        if dry_run:
            logger.debug("[ВИДИМОСТЬ] Пробный режим: ничего не опубликовано, проверять нечего")
            return None
        if not remote_id or log_id is None:
            logger.debug("[ВИДИМОСТЬ] У ответа нет id комментария: проверка пропущена")
            return None
        task = asyncio.create_task(watch_and_record(self._checker, self._db, log_id, video_id, remote_id, account_name))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def finish(self) -> None:
        """Wait for the pending checks (never forever); call before the collector and the database are closed."""
        tasks = list(self._tasks)
        if not tasks:
            return
        logger.info("[ВИДИМОСТЬ] Ожидание проверки видимости ответов: {}", len(tasks))
        limit = visibility_wait_limit(self._checker)
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=limit)
        except asyncio.TimeoutError:
            logger.warning("[ВИДИМОСТЬ] Проверка не уложилась в {:.0f}с и отменена", limit)
            await self.cancel()

    async def cancel(self) -> None:
        """Drop the pending checks without waiting for their delay (Ctrl+C, a crash)."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


__all__ = [
    "CommentVisibilityChecker",
    "VisibilityWatcher",
    "make_visibility_checker",
    "visibility_enabled",
    "visibility_wait_limit",
    "watch_and_record",
]
