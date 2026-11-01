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
        """Wait, check once and log a miss; the caller runs it in the background."""
        await asyncio.sleep(self._delay)
        try:
            seen = await self.is_visible(video_id, comment_id)
        except Exception as exc:  # a background task: nothing may escape as "exception was never retrieved"
            logger.debug("Проверка видимости комментария {} прервана: {}", comment_id, exc)
            return None
        if seen is False:
            logger.warning(
                "[ВИДИМОСТЬ] Комментарий {} аккаунта {!r} под видео {} не найден в анонимной выдаче через {:.0f}с "
                "(не обязательно скрыт: возможна задержка или ошибка проверки)",
                comment_id,
                account_name,
                video_id,
                self._delay,
            )
        elif seen:
            logger.debug("Комментарий {} виден в анонимной выдаче", comment_id)
        return seen


__all__ = ["CommentVisibilityChecker"]
