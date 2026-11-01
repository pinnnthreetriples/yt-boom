from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from curl_cffi.requests.exceptions import RequestException

from modules.comment_visibility import CommentVisibilityChecker

HTML = '"INNERTUBE_API_KEY":"key123" "INNERTUBE_CLIENT_VERSION":"2.20261101.00.00" lc=Ugx.reply'


def _collector(next_body: str, *, html: str = HTML, token: str | None = "tok", next_status: int = 200):
    session = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(status_code=200, text=html)),
        post=AsyncMock(
            return_value=SimpleNamespace(status_code=next_status, text=next_body, json=lambda: json.loads(next_body))
        ),
    )
    return SimpleNamespace(open=AsyncMock(), _session=session, _extract_comments_token=lambda _html: token)


class TestCommentVisibilityChecker(unittest.IsolatedAsyncioTestCase):
    async def test_reply_present_in_the_next_response_is_visible(self) -> None:
        body = json.dumps({"payload": {"commentEntityPayload": {"properties": {"commentId": "Ugx.reply"}}}})
        collector = _collector(body)
        checker = CommentVisibilityChecker(collector)
        self.assertIs(await checker.is_visible("vid", "Ugx.reply"), True)
        self.assertEqual(
            collector._session.get.await_args.args[0], "https://www.youtube.com/watch?v=vid&lc=Ugx.reply"
        )
        self.assertEqual(collector._session.post.await_args.kwargs["json"]["continuation"], "tok")

    async def test_reply_missing_from_the_next_response_is_not_seen(self) -> None:
        # the watch page HTML repeats lc= whatever happened to the comment: only the /next answer counts
        body = json.dumps({"payload": {"commentEntityPayload": {"properties": {"commentId": "Ugx.other"}}}})
        self.assertIs(await CommentVisibilityChecker(_collector(body)).is_visible("vid", "Ugx.reply"), False)

    async def test_unverifiable_cases_return_none(self) -> None:
        self.assertIsNone(await CommentVisibilityChecker(_collector("{}", token=None)).is_visible("v", "Ugx.r"))
        self.assertIsNone(await CommentVisibilityChecker(_collector("{}", html="no key")).is_visible("v", "Ugx.r"))
        self.assertIsNone(await CommentVisibilityChecker(_collector("{}", next_status=429)).is_visible("v", "Ugx.r"))
        collector = _collector("{}")
        collector._session.post = AsyncMock(side_effect=RequestException("boom"))
        self.assertIsNone(await CommentVisibilityChecker(collector).is_visible("v", "Ugx.r"))

    async def test_watch_waits_then_warns_without_claiming_a_ban(self) -> None:
        checker = CommentVisibilityChecker(_collector('{"commentId": "Ugx.other"}'), delay_seconds=45)
        sleep = AsyncMock()
        with patch("modules.comment_visibility.asyncio.sleep", sleep), patch(
            "modules.comment_visibility.logger"
        ) as log:
            self.assertIs(await checker.watch("vid", "Ugx.reply", "acc"), False)
        sleep.assert_awaited_once_with(45)
        message = log.warning.call_args.args[0]
        self.assertIn("не найден", message)
        self.assertNotIn("бан", message.lower())

    async def test_watch_stays_quiet_when_visible_or_unverifiable(self) -> None:
        body = '{"commentId":"Ugx.reply"}'
        for collector, expected in ((_collector(body), True), (_collector("{}", token=None), None)):
            with patch("modules.comment_visibility.asyncio.sleep", AsyncMock()), patch(
                "modules.comment_visibility.logger"
            ) as log:
                self.assertEqual(await CommentVisibilityChecker(collector).watch("v", "Ugx.reply", "a"), expected)
            log.warning.assert_not_called()

    async def test_unexpected_errors_never_escape_the_background_task(self) -> None:
        collector = _collector("{}")
        collector._session.get = AsyncMock(side_effect=RuntimeError("boom"))
        checker = CommentVisibilityChecker(collector)
        self.assertIsNone(await checker.is_visible("v", "Ugx.r"))
        checker.is_visible = AsyncMock(side_effect=KeyError("odd"))
        with patch("modules.comment_visibility.asyncio.sleep", AsyncMock()):
            self.assertIsNone(await checker.watch("v", "Ugx.r", "a"))

    async def test_real_shaped_response_matches_the_exact_id_not_its_parent(self) -> None:
        # live shape (2026-11): pretty-printed, the id sits in commentEntityPayload.properties and commentViewModel
        parent, reply = "UgyaEE4RK3LmE06FeJp4AaABAg", "UgyaEE4RK3LmE06FeJp4AaABAg.91bmow5-ynA929G-qC6oWd"
        body = json.dumps(
            {
                "onResponseReceivedEndpoints": [{"commentViewModel": {"commentId": parent}}],
                "frameworkUpdates": {
                    "entityBatchUpdate": {"mutations": [{"payload": {"commentEntityPayload": {"properties": {"commentId": parent}}}}]}
                },
            },
            indent=2,
        )
        checker = CommentVisibilityChecker(_collector(body))
        self.assertIs(await checker.is_visible("vid", parent), True)
        self.assertIs(await checker.is_visible("vid", reply), False)  # a visible parent says nothing about the reply

    async def test_answers_without_comment_data_are_unknown_not_a_miss(self) -> None:
        for body in ("{}", '{"onResponseReceivedEndpoints": []}', "[]", "<html>", ""):
            collector = _collector("{}")
            collector._session.post = AsyncMock(
                return_value=SimpleNamespace(status_code=200, text=body, json=lambda body=body: json.loads(body))
            )
            self.assertIsNone(await CommentVisibilityChecker(collector).is_visible("v", "Ugx.r"), body)


if __name__ == "__main__":
    unittest.main()
