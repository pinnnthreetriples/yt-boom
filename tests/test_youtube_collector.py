import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from config import CollectorEndpoint, CollectorSettings
from modules.collector import Collector
from modules.youtube_collector import (
    YouTubeCollector,
    YouTubeSubComment,
    extract_video_id,
)


class TestYouTubeCollector(unittest.TestCase):
    def setUp(self):
        self.settings = CollectorSettings(
            impersonate="chrome",
            timeout_seconds=10.0,
            max_items_per_endpoint=5,
            languages=["en", "ru"],
            minimum_confidence=0.7,
            min_text_length=5,
            max_text_length=500,
            require_any_keywords=[],
            block_keywords=["spam", "casino"],
            endpoints=[CollectorEndpoint(name="test", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")],
        )
        self.collector = YouTubeCollector(self.settings)

    def test_extract_video_id_valid_formats(self):
        cases = [
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&lc=Ugx123", "dQw4w9WgXcQ"),
            ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ]
        for url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(extract_video_id(url), expected)

    def test_extract_video_id_invalid(self):
        invalid_cases = [
            "",
            "   ",
            "https://example.com/video",
            "https://www.youtube.com/feed/subscriptions",
            "short",
            "toolongvideoid123456789",
        ]
        for url in invalid_cases:
            with self.subTest(url=url):
                self.assertIsNone(extract_video_id(url))

    def test_filter_text_metrics(self):
        # Too short (< 5)
        self.assertFalse(self.collector._passes_filters("hi"))
        # Blocked keyword
        self.assertFalse(self.collector._passes_filters("Visit our casino for big wins!"))
        # Valid text
        self.assertTrue(self.collector._passes_filters("This is a great comment under a video."))

    def test_detect_language(self):
        res_en = self.collector._detect_language("This is a fantastic explanation of the topic.")
        self.assertIsNotNone(res_en)
        lang, conf = res_en
        self.assertEqual(lang, "en")
        self.assertGreaterEqual(conf, 0.7)

        res_ru = self.collector._detect_language("Это действительно очень полезный и интересный комментарий.")
        self.assertIsNotNone(res_ru)
        lang_ru, conf_ru = res_ru
        self.assertEqual(lang_ru, "ru")
        self.assertGreaterEqual(conf_ru, 0.7)

    def test_parse_replies_framework_updates(self):
        mock_data = {
            "frameworkUpdates": {
                "entityBatchUpdate": {
                    "mutations": [
                        {
                            "payload": {
                                "commentEntityPayload": {
                                    "properties": {
                                        "commentId": "Ugz123.reply456",
                                        "content": {"content": "I completely agree with your previous point!"},
                                    },
                                    "author": {"displayName": "User123"},
                                }
                            }
                        }
                    ]
                }
            }
        }
        sub_comments = self.collector._parse_replies(
            mock_data,
            video_id="dQw4w9WgXcQ",
            parent_id="Ugz123",
            parent_text="Original top comment",
            parent_author="OriginalAuthor",
        )
        self.assertEqual(len(sub_comments), 1)
        sc = sub_comments[0]
        self.assertEqual(sc.comment_id, "Ugz123.reply456")
        self.assertEqual(sc.parent_comment_id, "Ugz123")
        self.assertEqual(sc.author, "User123")
        self.assertEqual(sc.text, "I completely agree with your previous point!")
        self.assertEqual(sc.parent_text, "Original top comment")
        self.assertEqual(sc.permalink, "https://www.youtube.com/watch?v=dQw4w9WgXcQ&lc=Ugz123.reply456")

    def test_parse_replies_classic_fallback(self):
        mock_data = {
            "commentRenderer": {
                "commentId": "UgzClassic.reply789",
                "authorText": {"simpleText": "ClassicAuthor"},
                "contentText": {"runs": [{"text": "Classic reply format text."}]},
            }
        }
        sub_comments = self.collector._parse_replies(
            mock_data,
            video_id="dQw4w9WgXcQ",
            parent_id="UgzClassic",
            parent_text="Parent text",
            parent_author="ParentAuthor",
        )
        self.assertEqual(len(sub_comments), 1)
        self.assertEqual(sub_comments[0].comment_id, "UgzClassic.reply789")
        self.assertEqual(sub_comments[0].text, "Classic reply format text.")


    def test_parse_comment_threads_modern_comment_view_model(self):
        mock_data = {
            "frameworkUpdates": {
                "entityBatchUpdate": {
                    "mutations": [
                        {
                            "payload": {
                                "commentEntityPayload": {
                                    "properties": {
                                        "commentId": "UgzModernTop123",
                                        "content": {"content": "Top-level modern comment text"},
                                    },
                                    "author": {"displayName": "@TopAuthor"},
                                }
                            }
                        }
                    ]
                }
            },
            "contents": [
                {
                    "commentThreadRenderer": {
                        "commentViewModel": {
                            "commentViewModel": {
                                "commentId": "UgzModernTop123"
                            }
                        },
                        "replies": {
                            "commentRepliesRenderer": {
                                "contents": [
                                    {
                                        "continuationItemRenderer": {
                                            "continuationEndpoint": {
                                                "continuationCommand": {
                                                    "token": "token_for_replies_123"
                                                }
                                            }
                                        }
                                    }
                                ]
                            }
                        }
                    }
                }
            ]
        }
        parent_comments, reply_tokens = self.collector._parse_comment_threads(mock_data, "test_vid")
        self.assertIn("UgzModernTop123", parent_comments)
        self.assertEqual(parent_comments["UgzModernTop123"]["author"], "@TopAuthor")
        self.assertEqual(parent_comments["UgzModernTop123"]["text"], "Top-level modern comment text")
        self.assertEqual(len(reply_tokens), 1)
        self.assertEqual(reply_tokens[0][0], "UgzModernTop123")
        self.assertEqual(reply_tokens[0][1], "token_for_replies_123")

    def test_extract_comments_token_missing(self):
        html_without_data = "<html><head><title>No data</title></head><body>Nothing here</body></html>"
        self.assertIsNone(self.collector._extract_comments_token(html_without_data))

    def test_fetch_video_sub_comments_comments_disabled(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        with patch.object(self.collector, "_session", new_callable=AsyncMock) as mock_session, \
             patch.object(self.collector, "_extract_comments_token", return_value=None):
            mock_resp = AsyncMock()
            mock_resp.status_code = 200
            mock_resp.text = '{"INNERTUBE_API_KEY":"dummy_key"}'
            mock_session.get.return_value = mock_resp

            result = asyncio.run(self.collector.fetch_video_sub_comments("dQw4w9WgXcQ"))
            self.assertEqual(result, [])

    def test_build_search_params(self):
        from modules.youtube_collector import build_search_params
        # Views sort with video_only -> canonical CAMSAhAB
        self.assertEqual(build_search_params("views", video_only=True), "CAMSAhAB")
        self.assertEqual(build_search_params("view_count", video_only=True), "CAMSAhAB")
        self.assertEqual(build_search_params("popularity", video_only=True), "CAMSAhAB")
        # Date sort
        self.assertEqual(build_search_params("date", video_only=True), "CAISAhAB")
        # Rating sort
        self.assertEqual(build_search_params("rating", video_only=True), "CAESAhAB")
        # Relevance
        self.assertEqual(build_search_params("relevance", video_only=True), "EgIQAQ==")
        # Not video only
        self.assertEqual(build_search_params("views", video_only=False), "CAMSAg==")
        # Upload date filter (YouTube's own tokens: views + today / this week)
        self.assertEqual(build_search_params("views", video_only=True, upload_date=2), "CAMSBAgCEAE=")
        self.assertEqual(build_search_params("views", video_only=True, upload_date=3), "CAMSBAgDEAE=")
        self.assertEqual(build_search_params("relevance", video_only=True, upload_date=2), "EgQIAhAB")

    def test_upload_age_parsing_and_filter_choice(self):
        from modules.youtube_collector import parse_age_hours, upload_date_filter

        self.assertEqual(parse_age_hours("5 hours ago"), 5)
        self.assertEqual(parse_age_hours("Streamed 2 days ago"), 48)
        self.assertEqual(parse_age_hours("1 week ago"), 168)
        self.assertEqual(parse_age_hours("30 minutes ago"), 0.5)
        self.assertEqual(parse_age_hours("3 дня назад"), 72)
        self.assertEqual(parse_age_hours("1 день назад"), 24)
        self.assertEqual(parse_age_hours("5 часов назад"), 5)
        self.assertEqual(parse_age_hours("2 года назад"), 2 * 8760)
        self.assertIsNone(parse_age_hours(""))
        self.assertIsNone(parse_age_hours("Premiere"))
        # 12 h and 24 h -> «today», 2 and 7 days -> «this week»
        self.assertEqual([upload_date_filter(h) for h in (12, 24, 48, 168)], [2, 2, 3, 3])

    def test_parse_view_count(self):
        from modules.youtube_collector import parse_view_count

        # English suffixes
        self.assertEqual(parse_view_count("1.5M views"), 1_500_000)
        self.assertEqual(parse_view_count("2.4B views"), 2_400_000_000)
        self.assertEqual(parse_view_count("350K views"), 350_000)
        self.assertEqual(parse_view_count("1,234,567 views"), 1_234_567)
        self.assertEqual(parse_view_count("42 views"), 42)

        # Russian suffixes and comma decimals
        self.assertEqual(parse_view_count("1,5 млн просмотров"), 1_500_000)
        self.assertEqual(parse_view_count("1,5\u202fмлн просмотров"), 1_500_000)
        self.assertEqual(parse_view_count("1\u202f500 тыс. просмотров"), 1_500_000)
        self.assertEqual(parse_view_count("2,4 млрд просмотров"), 2_400_000_000)
        self.assertEqual(parse_view_count("350 тыс. просмотров"), 350_000)
        self.assertEqual(parse_view_count("12 тыс."), 12_000)
        self.assertEqual(parse_view_count("1 234 567 просмотров"), 1_234_567)
        self.assertEqual(parse_view_count("1.234.567 просмотров"), 1_234_567)

        # Dict structures
        self.assertEqual(parse_view_count({"simpleText": "500K views"}), 500_000)
        self.assertEqual(parse_view_count({"runs": [{"text": "1.2M"}, {"text": " views"}]}), 1_200_000)
        self.assertEqual(
            parse_view_count({"accessibility": {"accessibilityData": {"label": "1.2 million views"}}}),
            1_200_000,
        )
        self.assertEqual(
            parse_view_count({"videoViewCountRenderer": {"viewCount": {"simpleText": "1,234,567"}}}),
            1_234_567,
        )

        # Edge cases
        self.assertEqual(parse_view_count(None), 0)
        self.assertEqual(parse_view_count(""), 0)
        self.assertEqual(parse_view_count("No views"), 0)
        self.assertEqual(parse_view_count(9999), 9999)

    def test_parse_like_count(self):
        from modules.youtube_collector import parse_like_count

        self.assertEqual(parse_like_count("1.2K"), 1200)
        self.assertEqual(parse_like_count("12 тыс."), 12000)
        self.assertEqual(parse_like_count("1,5 млн"), 1500000)
        self.assertEqual(parse_like_count("1,5\u202fмлн"), 1500000)
        self.assertEqual(parse_like_count("42"), 42)
        self.assertEqual(parse_like_count(150), 150)
        self.assertEqual(parse_like_count(None), 0)
        self.assertEqual(parse_like_count(""), 0)
        self.assertEqual(parse_like_count({"simpleText": "3.5K"}), 3500)
        self.assertEqual(parse_like_count({"runs": [{"text": "850"}]}), 850)
        self.assertEqual(
            parse_like_count({"accessibility": {"accessibilityData": {"label": "2.5K likes"}}}),
            2500,
        )

    def test_search_videos_mocked(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        mock_search_data = {
            "contents": {
                "twoColumnSearchResultsRenderer": {
                    "primaryContents": {
                        "sectionListRenderer": {
                            "contents": [
                                {
                                    "itemSectionRenderer": {
                                        "contents": [
                                            {
                                                "videoRenderer": {
                                                    "videoId": "vid_high_11",
                                                    "title": {"runs": [{"text": "High Views Video"}]},
                                                    "viewCountText": {"simpleText": "2.5M views"},
                                                    "ownerText": {"runs": [{"text": "Awesome Channel"}]},
                                                    "publishedTimeText": {"simpleText": "3 days ago"},
                                                }
                                            },
                                            {
                                                "videoRenderer": {
                                                    "videoId": "vid_low_222",
                                                    "title": {"simpleText": "Low Views Video"},
                                                    "viewCountText": {"simpleText": "500 views"},
                                                    "ownerText": {"runs": [{"text": "Small Channel"}]},
                                                }
                                            },
                                            {
                                                "lockupViewModel": {
                                                    "contentId": "vid_modern_33",
                                                    "contentType": "LOCKUP_CONTENT_TYPE_VIDEO",
                                                    "metadata": {
                                                        "lockupMetadataViewModel": {
                                                            "title": {"content": "Modern Lockup Video"},
                                                            "metadataRows": [
                                                                {
                                                                    "metadataParts": [
                                                                        {"text": {"content": "Lockup Creator"}},
                                                                        {"text": {"content": "150K views"}},
                                                                    ]
                                                                }
                                                            ],
                                                        }
                                                    },
                                                }
                                            },
                                        ]
                                    }
                                }
                            ]
                        }
                    }
                }
            }
        }

        with patch.object(self.collector, "_ensure_innertube_credentials", new_callable=AsyncMock) as mock_creds, \
             patch.object(self.collector, "open", new_callable=AsyncMock):
            from unittest.mock import MagicMock
            mock_creds.return_value = ("fake_key", "fake_ver")
            mock_session = AsyncMock()
            mock_resp = AsyncMock()
            mock_resp.status_code = 200
            mock_resp.json = MagicMock(return_value=mock_search_data)
            mock_session.post.return_value = mock_resp
            self.collector._session = mock_session

            # Query with min_views=1000 should include both vid_high_11 and vid_modern_33
            videos = asyncio.run(
                self.collector.search_videos("python tutorial", min_views=1000, limit=5)
            )
            self.assertEqual(len(videos), 2)
            self.assertEqual(videos[0]["video_id"], "vid_high_11")
            self.assertEqual(videos[0]["title"], "High Views Video")
            self.assertEqual(videos[0]["view_count"], 2_500_000)
            self.assertEqual(videos[0]["channel"], "Awesome Channel")

            # Check lockupViewModel parsing
            self.assertEqual(videos[1]["video_id"], "vid_modern_33")
            self.assertEqual(videos[1]["title"], "Modern Lockup Video")
            self.assertEqual(videos[1]["view_count"], 150_000)
            self.assertEqual(videos[1]["channel"], "Lockup Creator")

    def test_search_videos_max_age(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        def video(vid, published=None):
            node = {"videoId": vid, "title": {"simpleText": vid}, "viewCountText": {"simpleText": "10K views"}}
            if published:
                node["publishedTimeText"] = {"simpleText": published}
            return {"videoRenderer": node}

        data = {"contents": [video("fresh_vid_1", "5 hours ago"), video("old_vid_222", "2 days ago"),
                             video("no_date_333")]}
        with patch.object(self.collector, "_ensure_innertube_credentials", new_callable=AsyncMock) as creds, \
             patch.object(self.collector, "open", new_callable=AsyncMock):
            creds.return_value = ("fake_key", "fake_ver")
            resp = AsyncMock()
            resp.status_code = 200
            resp.json = MagicMock(return_value=data)
            self.collector._session = AsyncMock()
            self.collector._session.post.return_value = resp

            videos = asyncio.run(self.collector.search_videos("news", limit=5, max_pages=1, max_age_hours=24))
            self.assertEqual([v["video_id"] for v in videos], ["fresh_vid_1"])  # unknown age is skipped too
            self.assertEqual(self.collector._session.post.call_args.kwargs["json"]["params"], "CAMSBAgCEAE=")

            videos = asyncio.run(self.collector.search_videos("news", limit=5, max_pages=1))
            self.assertEqual(len(videos), 3)  # no filter: everything passes
            self.assertEqual(self.collector._session.post.call_args.kwargs["json"]["params"], "CAMSAhAB")

    def test_fetch_popular_comments_modes(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_html = '<script>var ytInitialData = {"continuationCommand": {"token": "comm_token_1"}};</script><script>"INNERTUBE_API_KEY":"dummy_key"</script>'

        # Threads with 2 parent comments: one with 500 likes, one with 2000 likes
        mock_threads_data = {
            "contents": [
                {
                    "commentThreadRenderer": {
                        "comment": {
                            "commentRenderer": {
                                "commentId": "parent_low_likes",
                                "authorText": {"simpleText": "UserLow"},
                                "contentText": {"runs": [{"text": "This is a great parent comment with low likes."}]},
                                "voteCount": {"simpleText": "500"},
                            }
                        },
                        "replies": {
                            "commentRepliesRenderer": {
                                "contents": [
                                    {
                                        "continuationItemRenderer": {
                                            "continuationEndpoint": {
                                                "continuationCommand": {"token": "r_token_1"}
                                            }
                                        }
                                    }
                                ]
                            }
                        },
                    }
                },
                {
                    "commentThreadRenderer": {
                        "comment": {
                            "commentRenderer": {
                                "commentId": "parent_high_likes",
                                "authorText": {"simpleText": "UserHigh"},
                                "contentText": {"runs": [{"text": "This is a fantastic parent comment with high likes!"}]},
                                "voteCount": {"simpleText": "2K"},
                            }
                        }
                    }
                },
            ]
        }

        # Replies under parent_low_likes: has a sub-comment with 5000 likes
        mock_replies_data = {
            "frameworkUpdates": {
                "entityBatchUpdate": {
                    "mutations": [
                        {
                            "payload": {
                                "commentEntityPayload": {
                                    "properties": {
                                        "commentId": "sub_super_popular",
                                        "content": {"content": "This is a super popular reply under a comment!"},
                                    },
                                    "author": {"displayName": "SubStar"},
                                    "toolbar": {"likeCountNotliked": "5K"},
                                }
                            }
                        }
                    ]
                }
            }
        }

        with patch.object(self.collector, "open", new_callable=AsyncMock), \
             patch.object(self.collector, "_detect_language", return_value=("en", 0.95)):
            mock_session = AsyncMock()

            # GET returns video page HTML
            get_resp = AsyncMock()
            get_resp.status_code = 200
            get_resp.text = mock_html
            mock_session.get.return_value = get_resp

            # POST returns threads or replies based on continuation token in payload
            async def fake_post(url, json=None, headers=None):
                resp = AsyncMock()
                resp.status_code = 200
                if json.get("continuation") == "comm_token_1":
                    resp.json = MagicMock(return_value=mock_threads_data)
                else:
                    resp.json = MagicMock(return_value=mock_replies_data)
                return resp

            mock_session.post.side_effect = fake_post
            self.collector._session = mock_session

            # 1. Mode: popular_parent
            parents = asyncio.run(
                self.collector.fetch_popular_comments("dQw4w9WgXcQ", limit=5, target_mode="popular_parent")
            )
            self.assertEqual(len(parents), 2)
            # Most liked parent first
            self.assertEqual(parents[0].external_id, "dQw4w9WgXcQ#parent_high_likes")
            self.assertEqual(parents[0].like_count, 2000)
            self.assertEqual(parents[1].external_id, "dQw4w9WgXcQ#parent_low_likes")
            self.assertEqual(parents[1].like_count, 500)

            # 2. Mode: sub_comment
            replies = asyncio.run(
                self.collector.fetch_popular_comments("dQw4w9WgXcQ", limit=5, target_mode="sub_comment")
            )
            self.assertEqual(len(replies), 1)
            self.assertEqual(replies[0].external_id, "dQw4w9WgXcQ#parent_low_likes#sub_super_popular")
            self.assertEqual(replies[0].like_count, 5000)

            # 3. Mode: mixed -> highest likes overall first (5000 > 2000 > 500)
            mixed = asyncio.run(
                self.collector.fetch_popular_comments("dQw4w9WgXcQ", limit=5, target_mode="mixed")
            )
            self.assertEqual(len(mixed), 3)
            self.assertEqual(mixed[0].like_count, 5000)
            self.assertEqual(mixed[1].like_count, 2000)
            self.assertEqual(mixed[2].like_count, 500)

            # 4. Mode: first_reply -> returns first reply under popular parent threads
            first_replies = asyncio.run(
                self.collector.fetch_popular_comments("dQw4w9WgXcQ", limit=5, target_mode="first_reply")
            )
            self.assertEqual(len(first_replies), 1)
            self.assertEqual(first_replies[0].external_id, "dQw4w9WgXcQ#parent_low_likes#sub_super_popular")
            self.assertEqual(first_replies[0].parent_id, "parent_low_likes")
            self.assertTrue(first_replies[0].is_reply)


if __name__ == "__main__":
    unittest.main()


class TestCollectorProxy(unittest.IsolatedAsyncioTestCase):
    """Collectors reach YouTube through a proxy when one is given, and directly (as before) when not."""

    def setUp(self):
        self.settings = CollectorSettings(
            languages=["en"],
            endpoints=[CollectorEndpoint(name="test", url="https://www.youtube.com/watch?v=dQw4w9WgXcQ")],
        )

    async def _session_kwargs(self, module: str, collector) -> dict:
        with patch(f"{module}.AsyncSession") as session_cls:
            await collector.open()
        return session_cls.call_args.kwargs

    async def test_youtube_collector_uses_the_proxy_as_a_curl_url(self):
        collector = YouTubeCollector(self.settings, "http://us er:p@ss@1.2.3.4:1080")
        kwargs = await self._session_kwargs("modules.youtube_collector", collector)
        self.assertEqual(kwargs["proxy"], "http://us%20er:p%40ss@1.2.3.4:1080")

    async def test_socks5_proxy_resolves_dns_on_the_proxy(self):
        collector = YouTubeCollector(self.settings, "socks5://u:p@1.2.3.4:1080")
        kwargs = await self._session_kwargs("modules.youtube_collector", collector)
        self.assertEqual(kwargs["proxy"], "socks5h://u:p@1.2.3.4:1080")

    async def test_youtube_collector_without_a_proxy_goes_direct(self):
        for proxy in ("default", None, ""):
            with self.subTest(proxy=proxy):
                collector = YouTubeCollector(self.settings) if proxy == "default" else YouTubeCollector(self.settings, proxy)
                kwargs = await self._session_kwargs("modules.youtube_collector", collector)
                self.assertIsNone(kwargs["proxy"])

    async def test_generic_collector_uses_the_proxy(self):
        kwargs = await self._session_kwargs("modules.collector", Collector(self.settings, "socks5://1.2.3.4:1080"))
        self.assertEqual(kwargs["proxy"], "socks5h://1.2.3.4:1080")
        kwargs = await self._session_kwargs("modules.collector", Collector(self.settings))
        self.assertIsNone(kwargs["proxy"])

    async def test_generic_collector_hands_its_proxy_to_the_youtube_collector(self):
        collector = Collector(self.settings, "socks5://1.2.3.4:1080")
        collector.open = AsyncMock()
        with patch("modules.youtube_collector.YouTubeCollector") as youtube_collector:
            youtube_collector.return_value.collect_from_videos = AsyncMock(return_value=[])
            await collector.collect()
        youtube_collector.assert_called_once_with(self.settings, "socks5://1.2.3.4:1080")
