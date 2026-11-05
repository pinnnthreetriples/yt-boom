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

    async def _look(self, video_id: str, comment_id: str) -> bool | None:
        try:
            return await self.is_visible(video_id, comment_id)
        except Exception as exc:  # a background task: nothing may escape as "exception was never retrieved"
            logger.debug("Проверка видимости комментария {} прервана: {}", comment_id, exc)
            return None

    async def watch(self, video_id: str, comment_id: str, account_name: str) -> bool | None:
        """Wait, look for the reply (again after each retry delay while it is not seen) and log the verdict.

        The first sighting ends the series. A miss ("False") is only reported after the last look, an unverifiable
        answer ("None") is retried like a miss. Every outcome is logged at INFO or higher, so the log tells
        "confirmed" from "not found" from "could not be checked". The caller runs it in the background.
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

        if seen is False:
            logger.warning(
                "[ВИДИМОСТЬ] Комментарий {} аккаунта {!r} под видео {} не найден в анонимной выдаче: "
                "проверок {}, последняя через {:.0f}с (не обязательно скрыт: возможна задержка или ошибка проверки)",
                comment_id,
                account_name,
                video_id,
                looks,
                waited,
            )
        elif seen is None:
            logger.info(
                "[ВИДИМОСТЬ] Комментарий {} аккаунта {!r} под видео {}: проверить не удалось "
                "(проверок {}, видимость неизвестна)",
                comment_id,
                account_name,
                video_id,
                looks,
            )
        else:
            logger.info(
                "[ВИДИМОСТЬ] Комментарий {} аккаунта {!r} под видео {} виден в анонимной выдаче "
                "(проверка {} через {:.0f}с)",
                comment_id,
                account_name,
                video_id,
                looks,
                waited,
            )
        return seen


__all__ = ["CommentVisibilityChecker"]
