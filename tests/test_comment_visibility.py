from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from curl_cffi.requests.exceptions import RequestException

from modules.comment_visibility import CommentVisibilityChecker, make_visibility_checker, visibility_wait_limit

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

    async def test_watch_logs_success_when_visible_and_a_distinct_warning_when_unverifiable(self) -> None:
        body = '{"commentId":"Ugx.reply"}'
        for collector, expected in ((_collector(body), True), (_collector("{}", token=None), None)):
            with patch("modules.comment_visibility.asyncio.sleep", AsyncMock()), patch(
                "modules.comment_visibility.logger"
            ) as log:
                self.assertEqual(await CommentVisibilityChecker(collector).watch("v", "Ugx.reply", "a"), expected)
            if expected:
                log.success.assert_called_once()
                log.warning.assert_not_called()
            else:
                log.success.assert_not_called()
                self.assertIn("не удалось проверить", log.warning.call_args.args[0])

    async def test_retries_stop_at_the_first_sighting(self) -> None:
        checker = CommentVisibilityChecker(_collector("{}"), delay_seconds=45, retry_delays=(120, 300))
        checker.is_visible = AsyncMock(side_effect=[False, True])
        sleep = AsyncMock()
        with patch("modules.comment_visibility.asyncio.sleep", sleep), patch(
            "modules.comment_visibility.logger"
        ) as log:
            self.assertIs(await checker.watch("vid", "Ugx.reply", "acc"), True)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [45, 120])  # no third look after a hit
        self.assertEqual(checker.is_visible.await_count, 2)
        log.warning.assert_not_called()
        log.success.assert_called_once()
        self.assertEqual(log.success.call_args.args[4], 2)  # the sighting came on the second look

    async def test_a_miss_is_reported_only_after_the_last_look(self) -> None:
        checker = CommentVisibilityChecker(_collector("{}"), delay_seconds=45, retry_delays=(120, 300))
        checker.is_visible = AsyncMock(return_value=False)
        sleep = AsyncMock()
        with patch("modules.comment_visibility.asyncio.sleep", sleep), patch(
            "modules.comment_visibility.logger"
        ) as log:
            self.assertIs(await checker.watch("vid", "Ugx.reply", "acc"), False)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [45, 120, 300])
        self.assertEqual(checker.is_visible.await_count, 3)
        log.warning.assert_called_once()
        self.assertEqual(log.warning.call_args.args[4], 3)  # looks made
        self.assertEqual(log.warning.call_args.args[5], 465)  # seconds after the reply of the last look

    async def test_unverifiable_answers_are_retried_and_reported_when_they_persist(self) -> None:
        checker = CommentVisibilityChecker(_collector("{}"), delay_seconds=45, retry_delays=(120,))
        checker.is_visible = AsyncMock(side_effect=[None, False])
        with patch("modules.comment_visibility.asyncio.sleep", AsyncMock()), patch(
            "modules.comment_visibility.logger"
        ):
            self.assertIs(await checker.watch("vid", "Ugx.reply", "acc"), False)  # the retry turned unknown into a verdict
        checker.is_visible = AsyncMock(return_value=None)
        with patch("modules.comment_visibility.asyncio.sleep", AsyncMock()), patch(
            "modules.comment_visibility.logger"
        ) as log:
            self.assertIsNone(await checker.watch("vid", "Ugx.reply", "acc"))
        log.success.assert_not_called()
        self.assertIn("не удалось проверить", log.warning.call_args.args[0])  # unknown is never silent

    def test_factory_hands_the_retry_delays_to_the_checker(self) -> None:
        limits = SimpleNamespace(
            visibility_check_enabled=True, visibility_check_delay_seconds=30.0, visibility_retry_delays_seconds=[60.0, 90.0]
        )
        checker = make_visibility_checker(SimpleNamespace(limits=limits), _collector("{}"))
        self.assertEqual((checker._delay, checker._retry_delays), (30.0, (60.0, 90.0)))
        limits.visibility_check_enabled = False
        self.assertIsNone(make_visibility_checker(SimpleNamespace(limits=limits), _collector("{}")))

    def test_wait_limit_covers_every_retry_pause_and_tolerates_stand_ins(self) -> None:
        checker = CommentVisibilityChecker(_collector("{}"), delay_seconds=45, retry_delays=(120, 300))
        self.assertEqual(visibility_wait_limit(checker), 45 + 120 + 300 + 60)
        self.assertEqual(visibility_wait_limit(CommentVisibilityChecker(_collector("{}"), delay_seconds=7)), 67)
        self.assertEqual(visibility_wait_limit(SimpleNamespace(_delay=0.0)), 60.0)  # no retry list
        self.assertEqual(visibility_wait_limit(None), 60.0)
        self.assertEqual(visibility_wait_limit(MagicMock()), 60.0)  # attributes that are not numbers count as 0

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
