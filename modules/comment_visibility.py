from __future__ import annotations

import asyncio
import re
from typing import Any

from loguru import logger

_API_KEY_RE = re.compile(r'"INNERTUBE_API_KEY":"([^"]+)"')
_ANY_COMMENT_ID_RE = re.compile(r'"commentId"\s*:\s*"')
_CLIENT_VERSION_RE = re.compile(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"')


class CommentVisibilityChecker:
    """Looks for a posted reply the way an anonymous visitor sees it (InnerTube /next, no cookies).

    Observability only: a miss is logged as a warning and never counts as an error of the account. The check
    can be wrong (propagation delay, YouTube changing the response), so it only ever says "not seen".
    """

    def __init__(self, collector: Any, delay_seconds: float = 45.0) -> None:
        self._collector = collector  # the orchestrator's YouTubeCollector: its curl_cffi session is reused
        self._delay = max(0.0, delay_seconds)

    async def is_visible(self, video_id: str, comment_id: str) -> bool | None:
        """True / False: the reply is / is not in the anonymous thread view. None: could not be checked."""
        try:
            await self._collector.open()
            session = self._collector._session
            # `lc=<id>` makes YouTube pin the linked reply (with its thread open) in the first comments page
            resp = await session.get(f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}")
            if resp.status_code != 200:
                return None
            html = resp.text
            api_key = _API_KEY_RE.search(html)
            token = self._collector._extract_comments_token(html)
            if api_key is None or not token:
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
                return None
            # A real answer is JSON with `onResponseReceivedEndpoints` and (live, 2026-11) carries 20+ commentId keys
            # (`commentEntityPayload.properties.commentId`, `commentViewModel.commentId`); anything else is "unknown".
            if not isinstance(resp.json(), dict) or not _ANY_COMMENT_ID_RE.search(resp.text):
                return None
            # only the /next answer counts: the watch page HTML repeats `lc=` whatever the comment's state
            return re.search(r'"commentId"\s*:\s*"' + re.escape(comment_id) + '"', resp.text) is not None
        except Exception as exc:
            logger.debug("Проверка видимости комментария {} не выполнена: {}", comment_id, exc)
            return None

    async def watch(self, video_id: str, comment_id: str, account_name: str) -> bool | None:
        """Wait, check once and log the outcome; the caller runs it in the background."""
        await asyncio.sleep(self._delay)
        try:
            seen = await self.is_visible(video_id, comment_id)
        except Exception as exc:  # a background task: nothing may escape as "exception was never retrieved"
            logger.debug("Проверка видимости комментария {} прервана: {}", comment_id, exc)
            seen = None
        if seen is True:
            logger.success(
                "[ВИДИМОСТЬ] Ответ {} аккаунта {!r} под видео {} виден всем зрителям",
                comment_id,
                account_name,
                video_id,
            )
        elif seen is False:
            logger.warning(
                "[ВИДИМОСТЬ] Ответ {} аккаунта {!r} под видео {} не найден в анонимной выдаче через {:.0f}с "
                "(не обязательно скрыт: возможна задержка или ошибка проверки)",
                comment_id,
                account_name,
                video_id,
                self._delay,
            )
        else:
            logger.warning(
                "[ВИДИМОСТЬ] Ответ {} аккаунта {!r} под видео {}: не удалось проверить (YouTube не отдал список "
                "комментариев или сеть недоступна)",
                comment_id,
                account_name,
                video_id,
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
    return CommentVisibilityChecker(collector, cfg.limits.visibility_check_delay_seconds)


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
        limit = (self._checker._delay if self._checker is not None else 0.0) + 60.0
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
    "watch_and_record",
]
