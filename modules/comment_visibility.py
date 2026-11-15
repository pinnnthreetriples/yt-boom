from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from loguru import logger

_API_KEY_RE = re.compile(r'"INNERTUBE_API_KEY":"([^"]+)"')
_ANY_COMMENT_ID_RE = re.compile(r'"commentId"\s*:\s*"')
_CLIENT_VERSION_RE = re.compile(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"')


SessionProvider = Callable[[], Awaitable[Any]]  # returns an opened curl_cffi-like session (get / post)
# (comment_id, video_id, account_name, seen, checked_by) -> stored; failures are the caller's to swallow
CheckRecorder = Callable[[str, str, str, "bool | None", str], Awaitable[None]]


class CommentVisibilityChecker:
    """Looks for a posted reply the way a visitor sees it (InnerTube /next).

    By default the visitor is anonymous (the collector's cookie-less session). An injected `session_provider` (the
    observer account's proxy and cookies) changes only whose point of view it is: the parsing is the same.
    `checked_by` ('anonymous' / 'observer') is stored with the final result when a `recorder` is given.

    Observability only: a miss is logged as a warning and never counts as an error of the account. The check
    can be wrong (propagation delay, YouTube changing the response), so it only ever says "not seen".
    A "seen" is only as good as the id it is given: it must be the new reply's own id, never its parent's.
    """

    def __init__(
        self,
        collector: Any,
        delay_seconds: float = 45.0,
        retry_delays: Sequence[float] = (),
        *,
        session_provider: SessionProvider | None = None,
        checked_by: str = "anonymous",
        recorder: CheckRecorder | None = None,
        auth_headers: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self._collector = collector  # the orchestrator's YouTubeCollector: its token parser (and session) are reused
        self._delay = max(0.0, delay_seconds)
        self._retry_delays = tuple(max(0.0, delay) for delay in retry_delays)
        self._session_provider = session_provider
        self._checked_by = checked_by
        self._recorder = recorder
        self._auth_headers = auth_headers  # signed-in viewer: SAPISIDHASH etc., built per request

    @property
    def checked_by(self) -> str:
        return self._checked_by

    async def _session(self) -> Any:
        if self._session_provider is not None:
            return await self._session_provider()
        await self._collector.open()
        return self._collector._session

    async def is_visible(self, video_id: str, comment_id: str) -> bool | None:
        """True / False: the reply is / is not in the anonymous thread view. None: could not be checked."""
        try:
            session = await self._session()
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
                headers={"Content-Type": "application/json", **(self._auth_headers() if self._auth_headers else {})},
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

        await self._record(video_id, comment_id, account_name, seen)
        observer = self._checked_by == "observer"
        if seen is True:
            logger.success(
                "[ВИДИМОСТЬ] Ответ {} аккаунта {!r} под видео {} "
                + ("виден наблюдателю" if observer else "виден всем зрителям")
                + " (проверка {} через {:.0f}с)",
                comment_id,
                account_name,
                video_id,
                looks,
                waited,
            )
        elif seen is False:
            logger.warning(
                "[ВИДИМОСТЬ] Ответ {} аккаунта {!r} под видео {} не найден в "
                + ("выдаче наблюдателя" if observer else "анонимной выдаче")
                + ": проверок {}, последняя через {:.0f}с (не обязательно скрыт: возможна задержка или ошибка проверки)",
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

    async def _record(self, video_id: str, comment_id: str, account_name: str, seen: bool | None) -> None:
        if self._recorder is None:
            return
        try:
            await self._recorder(comment_id, video_id, account_name, seen, self._checked_by)
        except Exception as exc:  # the log is a bonus: a full disk must not break the run
            logger.debug("Результат проверки комментария {} не сохранён: {}", comment_id, exc)


_STATE_BY_RESULT: dict[bool | None, str] = {True: "seen", False: "unseen", None: "unknown"}


def visibility_enabled(cfg: Any) -> bool:
    """The check is on when the switch is on and the delay is above 0 (0 keeps meaning «off» for old configs)."""
    limits = cfg.limits
    return bool(limits.visibility_check_enabled) and limits.visibility_check_delay_seconds > 0


class ObserverAwareChecker:
    """The checker a run uses when a database is at hand: same interface as `CommentVisibilityChecker`.

    On the first look of the run it asks the database for the observer account. An enabled observer means every check
    runs from its point of view (its own proxy, cookies and SAPISIDHASH; an observer that cannot open its session
    leaves the check «unknown» with one warning, never silently anonymous); otherwise the anonymous checker is used.
    Both record their results in `comment_checks`. `aclose` shuts the observer's HTTP session.
    """

    def __init__(self, anonymous: CommentVisibilityChecker, db: Any, cfg: Any) -> None:
        self._anonymous = anonymous
        self._db = db
        self._cfg = cfg
        self._resolved: CommentVisibilityChecker | None = None
        self._observer_session: Any = None
        self._warned = False

    @property
    def _delay(self) -> float:
        return self._anonymous._delay

    @property
    def _retry_delays(self) -> tuple[float, ...]:
        return self._anonymous._retry_delays

    @property
    def checked_by(self) -> str:
        return (self._resolved or self._anonymous).checked_by

    async def _pick(self) -> CommentVisibilityChecker:
        if self._resolved is not None:
            return self._resolved
        self._resolved = self._anonymous
        try:
            observer = await self._db.get_observer_account()
        except Exception as exc:  # an old DB / a locked file: keep the anonymous check
            logger.debug("[ВИДИМОСТЬ] Наблюдатель не прочитан: {}", exc)
            return self._resolved
        if observer is None or not observer.is_active:
            return self._resolved
        from modules.observer_session import ObserverSession

        self._observer_session = ObserverSession(observer, self._cfg.collector)
        self._resolved = CommentVisibilityChecker(
            self._anonymous._collector,
            self._anonymous._delay,
            self._anonymous._retry_delays,
            session_provider=self._provide,
            checked_by="observer",
            recorder=self._anonymous._recorder,
            auth_headers=self._observer_session.auth_headers,
        )
        logger.info("[ВИДИМОСТЬ] Проверки идут от аккаунта-наблюдателя {!r}", observer.name)
        return self._resolved

    async def _provide(self) -> Any:
        from modules.observer_session import ObserverUnavailable

        try:
            return await self._observer_session.get_session()
        except ObserverUnavailable as exc:
            if not self._warned:
                self._warned = True
                logger.warning("[ВИДИМОСТЬ] {}: проверки пропущены (вкладка «Запуск» → «Наблюдатель»)", exc)
            raise

    async def is_visible(self, video_id: str, comment_id: str) -> bool | None:
        return await (await self._pick()).is_visible(video_id, comment_id)

    async def watch(self, video_id: str, comment_id: str, account_name: str) -> bool | None:
        return await (await self._pick()).watch(video_id, comment_id, account_name)

    async def aclose(self) -> None:
        session, self._observer_session = self._observer_session, None
        self._resolved = None
        if session is not None:
            try:
                await session.close()
            except Exception as exc:
                logger.debug("Сессия наблюдателя не закрыта: {}", exc)


async def close_checker(checker: Any) -> None:
    """Release what a checker holds (the observer's HTTP session); a plain checker has nothing to release."""
    close = getattr(checker, "aclose", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def make_visibility_checker(cfg: Any, collector: Any, db: Any = None) -> Any:
    """A checker for `collector` when the config enables the check, else None; the one place with the condition.

    With `db` the results are stored (`comment_checks`) and an enabled observer account takes over the looking.
    """
    if not visibility_enabled(cfg):
        return None
    checker = CommentVisibilityChecker(
        collector,
        cfg.limits.visibility_check_delay_seconds,
        tuple(cfg.limits.visibility_retry_delays_seconds),
        recorder=db.record_comment_check if db is not None else None,
    )
    return ObserverAwareChecker(checker, db, cfg) if db is not None else checker


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
            await close_checker(self._checker)
            return
        logger.info("[ВИДИМОСТЬ] Ожидание проверки видимости ответов: {}", len(tasks))
        limit = visibility_wait_limit(self._checker)
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=limit)
        except asyncio.TimeoutError:
            logger.warning("[ВИДИМОСТЬ] Проверка не уложилась в {:.0f}с и отменена", limit)
            await self.cancel()
        await close_checker(self._checker)

    async def cancel(self) -> None:
        """Drop the pending checks without waiting for their delay (Ctrl+C, a crash)."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_checker(self._checker)


__all__ = [
    "CommentVisibilityChecker",
    "ObserverAwareChecker",
    "VisibilityWatcher",
    "close_checker",
    "make_visibility_checker",
    "visibility_enabled",
    "visibility_wait_limit",
    "watch_and_record",
]
