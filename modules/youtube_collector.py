from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException
from lingua import IsoCode639_1, Language, LanguageDetectorBuilder
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from config import CollectorSettings
from modules.collector import CollectedItem, CollectorError
from modules.session_heartbeat import curl_proxy_url

_WHITESPACE = re.compile(r"\s+")
_VIDEO_ID_REGEX = re.compile(r"(?:v=|\/shorts\/|youtu\.be\/|embed\/)([A-Za-z0-9_-]{11})")


def extract_video_id(url_or_id: str) -> str | None:
    """Extract 11-character YouTube video ID from various URL formats or plain ID."""
    url_or_id = url_or_id.strip()
    if len(url_or_id) == 11 and re.fullmatch(r"[A-Za-z0-9_-]{11}", url_or_id):
        return url_or_id
    match = _VIDEO_ID_REGEX.search(url_or_id)
    if match:
        return match.group(1)
    parsed = urlparse(url_or_id)
    if parsed.query:
        params = parse_qs(parsed.query)
        if "v" in params and params["v"]:
            return params["v"][0]
    return None


def _find_keys(obj: Any, target_key: str) -> list[Any]:
    """Recursively search for all values matching a specific key in nested JSON."""
    results: list[Any] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == target_key:
                results.append(v)
            results.extend(_find_keys(v, target_key))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(_find_keys(item, target_key))
    return results


class YouTubeSubComment(BaseModel):
    """Represents a reply under a comment (comment under comment) on YouTube."""
    model_config = ConfigDict(frozen=True)

    video_id: str
    comment_id: str
    parent_comment_id: str
    author: str
    text: str
    parent_author: str | None = None
    parent_text: str | None = None
    permalink: str
    like_count: int = 0


# YouTube «Upload date» search filter: max video age in hours -> protobuf value
# (1 last hour, 2 today, 3 this week, 4 this month, 5 this year). The exact cut is done by parse_age_hours.
_UPLOAD_DATE_FILTERS: list[tuple[int, int]] = [(1, 1), (24, 2), (168, 3), (720, 4), (8760, 5)]


def upload_date_filter(max_age_hours: int) -> int:
    """Narrowest YouTube upload date filter that still covers max_age_hours."""
    return next((value for hours, value in _UPLOAD_DATE_FILTERS if max_age_hours <= hours), 5)


def build_search_params(sort_by: str = "views", video_only: bool = True, upload_date: int | None = None) -> str:
    """
    Build YouTube InnerTube search protobuf filter parameter.
    'CAMSAhAB' is the canonical protobuf token for:
      - Field 1 (sort_by): 3 (view count)
      - Field 2 (filter): video only (type = 1)
    upload_date (see _UPLOAD_DATE_FILTERS) adds filter field 1, e.g. views + today = 'CAMSBAgCEAE='.
    """
    s = (sort_by or "views").lower()
    if upload_date is not None:
        sort_code = {"views": 3, "view_count": 3, "popularity": 3, "popular": 3,
                     "date": 2, "upload_date": 2, "newest": 2, "rating": 1, "likes": 1}.get(s, 0)
        filters = bytes([0x08, upload_date]) + (b"" if video_only else b"")
        raw = (bytes([0x08, sort_code]) if sort_code else b"") + bytes([0x12, len(filters)]) + filters
        return base64.b64encode(raw).decode("ascii")
    if video_only:
        if s in {"views", "view_count", "popularity", "popular"}:
            return "CAMSAhAB"
        elif s in {"date", "upload_date", "newest"}:
            return "CAISAhAB"
        elif s in {"rating", "likes"}:
            return "CAESAhAB"
        else:
            return "EgIQAQ=="
    else:
        if s in {"views", "view_count", "popularity", "popular"}:
            return "CAMSAg=="
        elif s in {"date", "upload_date"}:
            return "CAISAg=="
        return ""


# "5 hours ago", "Streamed 2 days ago", "3 дня назад" -> hours per unit (matched by prefix).
_AGE_REGEX = re.compile(
    r"(\d+)\s*(second|sec|minute|min|hour|day|week|month|year|сек|мин|час|дн|ден|недел|месяц|год|лет)",
    re.IGNORECASE,
)
_AGE_UNIT_HOURS: dict[str, float] = {
    "sec": 1 / 3600, "second": 1 / 3600, "сек": 1 / 3600,
    "min": 1 / 60, "minute": 1 / 60, "мин": 1 / 60,
    "hour": 1, "час": 1,
    "day": 24, "дн": 24, "ден": 24,
    "week": 168, "недел": 168,
    "month": 720, "месяц": 720,
    "year": 8760, "год": 8760, "лет": 8760,
}


def parse_age_hours(published_text: str) -> float | None:
    """Video age in hours from a relative publish time ("5 hours ago"); None if not recognised."""
    match = _AGE_REGEX.search(published_text or "")
    if not match:
        return None
    return int(match.group(1)) * _AGE_UNIT_HOURS[match.group(2).lower()]


def _parse_compact_number(raw: str) -> int:
    """Parse international number string with suffixes (K, M, B, тыс, млн, млрд) and varied separators."""
    if not raw:
        return 0
    # Normalize all unicode whitespace (non-breaking space \xa0, narrow no-break space \u202f, etc.)
    text = re.sub(r"[\s\xa0\u2000-\u200b\u202f\u205f\u3000]+", " ", str(raw)).strip()
    if not text:
        return 0

    def _clean_float(s: str) -> float | None:
        cand = re.sub(r"\s+", "", s)
        if not cand:
            return None
        if "." in cand and "," in cand:
            if cand.rfind(",") > cand.rfind("."):
                cand = cand.replace(".", "").replace(",", ".")
            else:
                cand = cand.replace(",", "")
        elif "," in cand:
            if re.search(r",\d{1,2}$", cand):
                cand = cand.replace(",", ".")
            else:
                cand = cand.replace(",", "")
        elif "." in cand:
            if cand.count(".") > 1 or re.search(r"^\d{1,3}\.\d{3}$", cand):
                cand = cand.replace(".", "")
        try:
            return float(cand)
        except ValueError:
            return None

    # Billions / млрд / миллиард / B
    b_match = re.search(r"([\d\s.,]+)\s*(?:[bB]|млрд|миллиард|billion)", text, re.IGNORECASE)
    if b_match:
        val = _clean_float(b_match.group(1))
        if val is not None:
            return int(val * 1_000_000_000)

    # Millions / млн / миллион / M
    m_match = re.search(r"([\d\s.,]+)\s*(?:[mM]|млн|миллион|million)", text, re.IGNORECASE)
    if m_match:
        val = _clean_float(m_match.group(1))
        if val is not None:
            return int(val * 1_000_000)

    # Thousands / тыс / тысяча / K
    k_match = re.search(r"([\d\s.,]+)\s*(?:[kK]|тыс|тысяч|thousand)", text, re.IGNORECASE)
    if k_match:
        val = _clean_float(k_match.group(1))
        if val is not None:
            return int(val * 1_000)

    # Plain digits with separators
    plain_match = re.search(r"[\d\s.,]+", text)
    if plain_match:
        cand = plain_match.group(0).strip()
        cleaned = re.sub(r"[^\d.,]", "", cand)
        if re.match(r"^\d+[.,]\d{1,2}$", cleaned):
            return int(float(cleaned.replace(",", ".")))
        digits = re.sub(r"\D", "", cand)
        if digits:
            return int(digits)

    return 0


def _extract_text_from_node(node: Any) -> str:
    """Extract plain text from InnerTube structures (simpleText, runs, dict, int, str)."""
    if node is None:
        return ""
    if isinstance(node, (int, float)):
        return str(node)
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if "simpleText" in node and isinstance(node["simpleText"], str):
            return node["simpleText"]
        if "runs" in node and isinstance(node["runs"], list):
            return "".join(r.get("text", "") for r in node["runs"] if isinstance(r, dict))
        if "content" in node:
            return _extract_text_from_node(node["content"])
        if "text" in node:
            return _extract_text_from_node(node["text"])
        if "accessibility" in node:
            return _extract_text_from_node(node["accessibility"])
        if "accessibilityData" in node:
            return _extract_text_from_node(node["accessibilityData"])
        if "label" in node:
            return _extract_text_from_node(node["label"])
        if "videoViewCountRenderer" in node:
            return _extract_text_from_node(node["videoViewCountRenderer"])
        for key in ("viewCount", "views", "likeCount", "voteCount", "shortViewCountText", "viewCountText"):
            if key in node:
                return _extract_text_from_node(node[key])
    return ""


def parse_view_count(node: Any) -> int:
    """Robustly parse YouTube view counts across formats, locales, and InnerTube structures."""
    if isinstance(node, int):
        return node
    raw_text = _extract_text_from_node(node)
    return _parse_compact_number(raw_text)


def parse_like_count(like_val: Any) -> int:
    """Parse YouTube like/vote counts from InnerTube objects or localized strings."""
    if isinstance(like_val, int):
        return like_val
    raw_text = _extract_text_from_node(like_val)
    return _parse_compact_number(raw_text)


class YouTubeCollector:
    """Fetches YouTube comments and nested replies (comments under comments) via InnerTube and HTTP."""

    def __init__(self, settings: CollectorSettings, proxy: str | None = None) -> None:
        self._cfg = settings
        self._proxy = proxy  # None: requests go out directly
        self._session: AsyncSession | None = None
        self._languages = set(settings.languages)
        self._detector = self._build_detector(settings.languages)
        self._api_key: str | None = None
        self._client_version: str | None = None

    @staticmethod
    def _build_detector(codes: list[str]) -> Any:
        languages: list[Language] = []
        for code in codes:
            try:
                languages.append(Language.from_iso_code_639_1(IsoCode639_1.from_str(code.lower())))
            except (TypeError, ValueError) as exc:
                raise CollectorError(f"unsupported collector language code: {code!r}") from exc
        try:
            return LanguageDetectorBuilder.from_languages(*languages).with_preloaded_language_models().build()
        except Exception as exc:
            raise CollectorError(f"failed to build language detector: {exc}") from exc

    async def open(self) -> None:
        if self._session is not None:
            return
        self._session = AsyncSession(
            impersonate=self._cfg.impersonate,
            proxy=curl_proxy_url(self._proxy),
            timeout=self._cfg.timeout_seconds,
            headers={
                "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            },
        )
        logger.debug("YouTube collector HTTP session opened")

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()
            logger.debug("YouTube collector HTTP session closed")

    async def __aenter__(self) -> "YouTubeCollector":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def fetch_video_sub_comments(
        self,
        video_url_or_id: str,
        *,
        max_sub_comments: int | None = None,
    ) -> list[YouTubeSubComment]:
        """Fetch comments under comments (replies to comments) for a given YouTube video."""
        video_id = extract_video_id(video_url_or_id)
        if not video_id:
            raise CollectorError(f"invalid YouTube video URL or ID: {video_url_or_id!r}")

        await self.open()
        assert self._session is not None

        video_url = f"https://www.youtube.com/watch?v={video_id}"
        logger.debug("Сбор комментариев и веток ответов для видео {}", video_id)

        try:
            resp = await self._session.get(video_url)
        except RequestException as exc:
            raise CollectorError(f"Ошибка загрузки страницы видео {video_url}: {exc}") from exc

        if resp.status_code != 200:
            raise CollectorError(f"Страница видео вернула код HTTP {resp.status_code}")

        html = resp.text
        api_key_match = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
        if not api_key_match:
            raise CollectorError("Не удалось извлечь INNERTUBE_API_KEY из страницы YouTube")
        api_key = api_key_match.group(1)

        version_match = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
        client_version = version_match.group(1) if version_match else "2.20261029.01.00"

        # Extract initial continuation token for comments section
        token = self._extract_comments_token(html)
        if not token:
            logger.warning("Токен продолжения комментариев не найден для видео {} (комментарии отключены?)", video_id)
            return []

        # Request initial comment threads
        next_url = f"https://www.youtube.com/youtubei/v1/next?key={api_key}"
        threads_payload = {
            "context": {"client": {"clientName": "WEB", "clientVersion": client_version}},
            "continuation": token,
        }

        try:
            threads_resp = await self._session.post(
                next_url,
                json=threads_payload,
                headers={"Content-Type": "application/json"},
            )
        except RequestException as exc:
            raise CollectorError(f"failed to fetch YouTube comment threads: {exc}") from exc

        if threads_resp.status_code != 200:
            raise CollectorError(f"InnerTube comment threads request returned HTTP {threads_resp.status_code}")

        threads_data = threads_resp.json()
        parent_comments, reply_tokens = self._parse_comment_threads(threads_data, video_id)
        logger.debug(
            "Found {} top-level comments, {} threads with replies for video {}",
            len(parent_comments),
            len(reply_tokens),
            video_id,
        )

        limit = max_sub_comments or self._cfg.max_items_per_endpoint
        sub_comments: list[YouTubeSubComment] = []

        # Fetch replies under comments
        for parent_id, r_token in reply_tokens:
            if len(sub_comments) >= limit:
                break
            parent_info = parent_comments.get(parent_id, {})
            parent_text = parent_info.get("text", "")
            parent_author = parent_info.get("author", "")

            r_payload = {
                "context": {"client": {"clientName": "WEB", "clientVersion": client_version}},
                "continuation": r_token,
            }
            try:
                r_resp = await self._session.post(
                    next_url,
                    json=r_payload,
                    headers={"Content-Type": "application/json"},
                )
                if r_resp.status_code == 200:
                    replies_data = r_resp.json()
                    parsed_replies = self._parse_replies(
                        replies_data,
                        video_id=video_id,
                        parent_id=parent_id,
                        parent_text=parent_text,
                        parent_author=parent_author,
                    )
                    sub_comments.extend(parsed_replies)
            except Exception as exc:
                logger.warning("Не удалось получить ответы для ветки комментариев {}: {}", parent_id, exc)

        logger.debug("Извлечено {} ответов под комментариями для видео {}", len(sub_comments), video_id)
        return sub_comments[:limit]

    def _extract_comments_token(self, html: str) -> str | None:
        """Find the comment section continuation token from ytInitialData in HTML."""
        data_match = re.search(r"var ytInitialData\s*=\s*({.*?});(?:</script>|\n|$)", html)
        if not data_match:
            data_match = re.search(r"window\[\"ytInitialData\"\]\s*=\s*({.*?});(?:</script>|\n|$)", html)
        if not data_match:
            return None
        try:
            data = json.loads(data_match.group(1))
            commands = _find_keys(data, "continuationCommand")
            for cmd in commands:
                if isinstance(cmd, dict) and "token" in cmd:
                    return cmd["token"]
        except Exception as exc:
            logger.debug("Failed to parse ytInitialData: {}", exc)
        return None

    def _parse_comment_threads(
        self,
        threads_data: dict[str, Any],
        video_id: str,
    ) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str]]]:
        """Parse parent comments (including like counts) and their reply continuation tokens."""
        parent_comments: dict[str, dict[str, Any]] = {}
        reply_tokens: list[tuple[str, str]] = []

        # 1. Parse mutations (modern frameworkUpdates)
        mutations = threads_data.get("frameworkUpdates", {}).get("entityBatchUpdate", {}).get("mutations", [])
        for mut in mutations:
            p = mut.get("payload", {})
            if "commentEntityPayload" in p:
                cep = p["commentEntityPayload"]
                cid = cep.get("properties", {}).get("commentId", "")
                author = cep.get("author", {}).get("displayName", "")
                text = cep.get("properties", {}).get("content", {}).get("content", "")
                toolbar = cep.get("toolbar", {})
                like_val = toolbar.get("likeCountNotliked") or toolbar.get("likeCount") or cep.get("likeCount")
                like_cnt = parse_like_count(like_val)
                if cid:
                    parent_comments[cid] = {
                        "author": author,
                        "text": text,
                        "like_count": like_cnt,
                        "comment_id": cid,
                    }

        # 2. Parse thread renderers for reply tokens
        threads = _find_keys(threads_data, "commentThreadRenderer")
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            # Also fallback parse commentRenderer if mutations were absent
            comment_renderer = thread.get("comment", {}).get("commentRenderer")
            if comment_renderer:
                cid = comment_renderer.get("commentId", "")
                author = comment_renderer.get("authorText", {}).get("simpleText", "")
                runs = comment_renderer.get("contentText", {}).get("runs", [])
                text = "".join(r.get("text", "") for r in runs if isinstance(r, dict))
                like_cnt = parse_like_count(comment_renderer.get("voteCount") or comment_renderer.get("likeCount"))
                if cid and cid not in parent_comments:
                    parent_comments[cid] = {
                        "author": author,
                        "text": text,
                        "like_count": like_cnt,
                        "comment_id": cid,
                    }

            # Find reply token
            replies = thread.get("replies")
            if replies:
                tokens = _find_keys(replies, "token")
                top_id = None
                if comment_renderer and "commentId" in comment_renderer:
                    top_id = comment_renderer["commentId"]
                else:
                    cvm = thread.get("commentViewModel") or thread.get("comment", {}).get("commentViewModel")
                    if isinstance(cvm, dict):
                        if "commentId" in cvm:
                            top_id = cvm["commentId"]
                        elif "commentViewModel" in cvm and isinstance(cvm["commentViewModel"], dict):
                            top_id = cvm["commentViewModel"].get("commentId")
                if not top_id:
                    comment_part = {k: v for k, v in thread.items() if k != "replies"}
                    cids = _find_keys(comment_part, "commentId")
                    if cids:
                        top_id = str(cids[0])
                if top_id and tokens:
                    reply_tokens.append((top_id, str(tokens[0])))
                elif tokens:
                    reply_tokens.append(("unknown", str(tokens[0])))

        return parent_comments, reply_tokens

    def _parse_replies(
        self,
        replies_data: dict[str, Any],
        video_id: str,
        parent_id: str,
        parent_text: str | None,
        parent_author: str | None,
    ) -> list[YouTubeSubComment]:
        """Extract sub-comments (comments under comments) from replies response."""
        results: list[YouTubeSubComment] = []

        # 1. Framework mutations
        mutations = replies_data.get("frameworkUpdates", {}).get("entityBatchUpdate", {}).get("mutations", [])
        for mut in mutations:
            p = mut.get("payload", {})
            if "commentEntityPayload" in p:
                cep = p["commentEntityPayload"]
                cid = cep.get("properties", {}).get("commentId", "")
                author = cep.get("author", {}).get("displayName", "")
                text = cep.get("properties", {}).get("content", {}).get("content", "")
                toolbar = cep.get("toolbar", {})
                like_val = toolbar.get("likeCountNotliked") or toolbar.get("likeCount") or cep.get("likeCount")
                like_cnt = parse_like_count(like_val)
                if cid and text.strip():
                    permalink = f"https://www.youtube.com/watch?v={video_id}&lc={cid}"
                    results.append(
                        YouTubeSubComment(
                            video_id=video_id,
                            comment_id=cid,
                            parent_comment_id=parent_id,
                            author=author,
                            text=text.strip(),
                            parent_author=parent_author,
                            parent_text=parent_text,
                            permalink=permalink,
                            like_count=like_cnt,
                        )
                    )

        # 2. Classic commentRenderer fallback
        if not results:
            comment_renderers = _find_keys(replies_data, "commentRenderer")
            for cr in comment_renderers:
                if not isinstance(cr, dict):
                    continue
                cid = cr.get("commentId", "")
                author = cr.get("authorText", {}).get("simpleText", "")
                runs = cr.get("contentText", {}).get("runs", [])
                text = "".join(r.get("text", "") for r in runs if isinstance(r, dict)).strip()
                like_cnt = parse_like_count(cr.get("voteCount") or cr.get("likeCount"))
                if cid and text:
                    permalink = f"https://www.youtube.com/watch?v={video_id}&lc={cid}"
                    results.append(
                        YouTubeSubComment(
                            video_id=video_id,
                            comment_id=cid,
                            parent_comment_id=parent_id,
                            author=author,
                            text=text,
                            parent_author=parent_author,
                            parent_text=parent_text,
                            permalink=permalink,
                            like_count=like_cnt,
                        )
                    )

        return results

    async def collect_from_videos(
        self,
        video_urls: list[str],
        *,
        max_items: int | None = None,
    ) -> list[CollectedItem]:
        """Collect sub-comments across multiple videos and filter them according to CollectorSettings."""
        limit = max_items or self._cfg.max_items_per_endpoint
        items: list[CollectedItem] = []
        seen: set[str] = set()

        for url in video_urls:
            if len(items) >= limit:
                break
            try:
                sub_comments = await self.fetch_video_sub_comments(url, max_sub_comments=limit - len(items))
            except Exception as exc:
                logger.error("Failed to collect YouTube replies from {}: {}", url, exc)
                continue

            for sc in sub_comments:
                if len(items) >= limit:
                    break
                if sc.comment_id in seen:
                    continue
                seen.add(sc.comment_id)

                if not self._passes_filters(sc.text):
                    logger.trace("Sub-comment {} skipped by metric/keyword filters", sc.comment_id)
                    continue

                detection = await asyncio.to_thread(self._detect_language, sc.text)
                if detection is None:
                    continue
                language, confidence = detection
                if language not in self._languages:
                    logger.trace("Sub-comment {} skipped: language {} not in allowed", sc.comment_id, language)
                    continue
                if confidence < self._cfg.minimum_confidence:
                    logger.trace("Sub-comment {} skipped: confidence {:.2f} below minimum", sc.comment_id, confidence)
                    continue

                ext_id = f"{sc.video_id}#{sc.parent_comment_id}#{sc.comment_id}"
                items.append(
                    CollectedItem(
                        endpoint=f"youtube:{sc.video_id}",
                        external_id=ext_id,
                        text=sc.text,
                        language=language,
                        confidence=confidence,
                        source_url=sc.permalink,
                        parent_id=sc.parent_comment_id,
                        parent_text=sc.parent_text,
                        parent_author=sc.parent_author,
                        author=sc.author,
                        video_id=sc.video_id,
                        is_reply=True,
                        like_count=sc.like_count,
                    )
                )

        logger.debug("Собрано {} валидных комментариев из {} видео YouTube", len(items), len(video_urls))
        return items

    async def _ensure_innertube_credentials(self, fallback_url: str = "https://www.youtube.com") -> tuple[str, str]:
        """Ensure YouTube InnerTube API key and client version are available."""
        if self._api_key and self._client_version:
            return self._api_key, self._client_version
        await self.open()
        assert self._session is not None
        try:
            resp = await self._session.get(fallback_url)
            html = resp.text
            api_key_match = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
            if api_key_match:
                self._api_key = api_key_match.group(1)
            version_match = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
            if version_match:
                self._client_version = version_match.group(1)
        except Exception as exc:
            logger.debug("Failed to extract InnerTube credentials from {}: {}", fallback_url, exc)

        if not self._api_key:
            self._api_key = ""
        if not self._client_version:
            self._client_version = "2.20261029.01.00"

        return self._api_key, self._client_version

    async def search_videos(
        self,
        query: str,
        *,
        min_views: int = 0,
        max_views: int | None = None,
        sort_by: str = "views",
        limit: int = 10,
        max_pages: int = 3,
        max_age_hours: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search YouTube videos via InnerTube, sorted by views (or other sort_by)
        and filtered by min_views / max_views thresholds.

        max_age_hours keeps only videos published at most that many hours ago: YouTube's
        upload date filter narrows the search, the publish time text gives the exact cut.
        """
        await self.open()
        assert self._session is not None

        api_key, client_version = await self._ensure_innertube_credentials()
        search_url = f"https://www.youtube.com/youtubei/v1/search?key={api_key}"
        params_token = build_search_params(
            sort_by=sort_by,
            video_only=True,
            upload_date=upload_date_filter(max_age_hours) if max_age_hours else None,
        )

        def too_old(published: str) -> bool:
            if not max_age_hours:
                return False
            age = parse_age_hours(published)
            return age is None or age > max_age_hours

        payload: dict[str, Any] = {
            "context": {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": client_version,
                    "hl": "en",
                    "gl": "US",
                }
            },
            "query": query,
            "params": params_token,
        }

        matched_videos: list[dict[str, Any]] = []
        continuation_token: str | None = None

        for page in range(max_pages):
            if len(matched_videos) >= limit:
                break

            req_payload = payload if page == 0 else {
                "context": {"client": {"clientName": "WEB", "clientVersion": client_version}},
                "continuation": continuation_token,
            }

            try:
                resp = await self._session.post(
                    search_url,
                    json=req_payload,
                    headers={"Content-Type": "application/json"},
                )
            except RequestException as exc:
                logger.error("InnerTube search request failed on page {}: {}", page + 1, exc)
                break

            if resp.status_code != 200:
                logger.error("InnerTube search returned HTTP {}", resp.status_code)
                break

            data = resp.json()
            renderers = _find_keys(data, "videoRenderer")
            lockups = _find_keys(data, "lockupViewModel")
            if not renderers and not lockups:
                logger.debug("No videoRenderer or lockupViewModel items found on search page {}", page + 1)
                break

            for vr in renderers:
                if not isinstance(vr, dict):
                    continue
                vid = vr.get("videoId")
                if not vid:
                    continue

                title_node = vr.get("title")
                title = _extract_text_from_node(title_node).strip()

                view_node = vr.get("viewCountText") or vr.get("shortViewCountText")
                views_text = _extract_text_from_node(view_node).strip()
                views = parse_view_count(view_node)

                byline = vr.get("ownerText") or vr.get("longBylineText")
                channel = _extract_text_from_node(byline).strip()

                published = _extract_text_from_node(vr.get("publishedTimeText")).strip()

                if views < min_views or too_old(published):
                    continue
                if max_views is not None and views > max_views:
                    continue

                video_entry = {
                    "video_id": vid,
                    "title": title or f"Video {vid}",
                    "url": f"https://www.youtube.com/watch?v={vid}",
                    "view_count": views,
                    "views_text": views_text,
                    "channel": channel,
                    "published_time": published,
                }
                matched_videos.append(video_entry)
                if len(matched_videos) >= limit:
                    break

            if len(matched_videos) < limit:
                for lu in lockups:
                    if not isinstance(lu, dict):
                        continue
                    if lu.get("contentType") != "LOCKUP_CONTENT_TYPE_VIDEO":
                        continue
                    vid = lu.get("contentId")
                    if not vid or any(v["video_id"] == vid for v in matched_videos):
                        continue
                    meta = lu.get("metadata", {}).get("lockupMetadataViewModel", {})
                    title = _extract_text_from_node(meta.get("title")).strip()
                    rows = meta.get("metadataRows", [])
                    views_text = ""
                    views = 0
                    channel = ""
                    published = ""
                    for row in rows:
                        parts = row.get("metadataParts", [])
                        for part in parts:
                            part_text = _extract_text_from_node(part.get("text")).strip()
                            if "view" in part_text.lower() or "просмотр" in part_text.lower():
                                views_text = part_text
                                views = parse_view_count(part_text)
                            elif not channel:
                                channel = part_text
                            else:
                                published = part_text
                    if views < min_views or too_old(published):
                        continue
                    if max_views is not None and views > max_views:
                        continue
                    matched_videos.append({
                        "video_id": vid,
                        "title": title or f"Video {vid}",
                        "url": f"https://www.youtube.com/watch?v={vid}",
                        "view_count": views,
                        "views_text": views_text,
                        "channel": channel,
                        "published_time": published,
                    })
                    if len(matched_videos) >= limit:
                        break

            commands = _find_keys(data, "continuationCommand")
            continuation_token = None
            for cmd in commands:
                if isinstance(cmd, dict) and "token" in cmd:
                    continuation_token = str(cmd["token"])
                    break
            if not continuation_token:
                tokens = _find_keys(data, "token")
                continuation_token = str(tokens[0]) if tokens else None
            if not continuation_token:
                break

        logger.info(
            "Поиск InnerTube по запросу {!r}: найдено {} видео с критериями просмотров [{}..{}], не старше {} ч",
            query,
            len(matched_videos),
            min_views,
            max_views,
            max_age_hours or "∞",
        )
        return matched_videos[:limit]

    async def fetch_popular_comments(
        self,
        video_url_or_id: str,
        *,
        limit: int = 10,
        target_mode: str = "popular_parent",
    ) -> list[CollectedItem]:
        """
        Fetch comments for a video, sorted by popularity (like count DESC).
        Supported target modes:
          - 'popular_parent': Top-level popular comments (most upvoted comments on the video)
          - 'sub_comment': Replies under comments (sub-comments), sorted by likes
          - 'mixed': Combined top-level comments and replies, sorted by likes
        """
        video_id = extract_video_id(video_url_or_id)
        if not video_id:
            raise CollectorError(f"invalid YouTube video URL or ID: {video_url_or_id!r}")

        await self.open()
        assert self._session is not None

        video_url = f"https://www.youtube.com/watch?v={video_id}"
        logger.debug("Сбор комментариев для видео {} (режим={})", video_id, target_mode)

        try:
            resp = await self._session.get(video_url)
        except RequestException as exc:
            raise CollectorError(f"failed to fetch YouTube video page {video_url}: {exc}") from exc

        if resp.status_code != 200:
            raise CollectorError(f"YouTube video page returned HTTP {resp.status_code}")

        html = resp.text
        api_key_match = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
        if not api_key_match:
            raise CollectorError("could not extract INNERTUBE_API_KEY from YouTube page")
        api_key = api_key_match.group(1)
        self._api_key = api_key

        version_match = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
        client_version = version_match.group(1) if version_match else "2.20261029.01.00"
        self._client_version = client_version

        token = self._extract_comments_token(html)
        if not token:
            logger.warning("Токен продолжения комментариев не найден для видео {}", video_id)
            return []

        next_url = f"https://www.youtube.com/youtubei/v1/next?key={api_key}"
        threads_payload = {
            "context": {"client": {"clientName": "WEB", "clientVersion": client_version}},
            "continuation": token,
        }

        try:
            threads_resp = await self._session.post(
                next_url,
                json=threads_payload,
                headers={"Content-Type": "application/json"},
            )
        except RequestException as exc:
            raise CollectorError(f"failed to fetch YouTube comment threads: {exc}") from exc

        if threads_resp.status_code != 200:
            raise CollectorError(f"InnerTube comment threads request returned HTTP {threads_resp.status_code}")

        threads_data = threads_resp.json()
        parent_comments, reply_tokens = self._parse_comment_threads(threads_data, video_id)

        candidates: list[dict[str, Any]] = []

        # 1. Include parent comments if mode is popular_parent or mixed
        if target_mode in ("popular_parent", "mixed"):
            for cid, p_info in parent_comments.items():
                text = p_info.get("text", "")
                author = p_info.get("author", "")
                like_cnt = p_info.get("like_count", 0)
                if cid and text.strip():
                    candidates.append({
                        "video_id": video_id,
                        "comment_id": cid,
                        "parent_id": None,
                        "author": author,
                        "text": text.strip(),
                        "parent_author": None,
                        "parent_text": None,
                        "is_reply": False,
                        "like_count": like_cnt,
                        "permalink": f"https://www.youtube.com/watch?v={video_id}&lc={cid}",
                    })

        # 2. Include replies if mode is sub_comment or mixed
        if target_mode in ("sub_comment", "mixed"):
            max_reply_threads = max(limit * 2, 10)
            for parent_id, r_token in reply_tokens[:max_reply_threads]:
                parent_info = parent_comments.get(parent_id, {})
                parent_text = parent_info.get("text", "")
                parent_author = parent_info.get("author", "")

                r_payload = {
                    "context": {"client": {"clientName": "WEB", "clientVersion": client_version}},
                    "continuation": r_token,
                }
                try:
                    r_resp = await self._session.post(
                        next_url,
                        json=r_payload,
                        headers={"Content-Type": "application/json"},
                    )
                    if r_resp.status_code == 200:
                        replies_data = r_resp.json()
                        parsed_replies = self._parse_replies(
                            replies_data,
                            video_id=video_id,
                            parent_id=parent_id,
                            parent_text=parent_text,
                            parent_author=parent_author,
                        )
                        for r in parsed_replies:
                            candidates.append({
                                "video_id": r.video_id,
                                "comment_id": r.comment_id,
                                "parent_id": r.parent_comment_id,
                                "author": r.author,
                                "text": r.text,
                                "parent_author": r.parent_author,
                                "parent_text": r.parent_text,
                                "is_reply": True,
                                "like_count": r.like_count,
                                "permalink": r.permalink,
                            })
                except Exception as exc:
                    logger.debug("Failed to fetch replies for thread {}: {}", parent_id, exc)

        # 3. Include first reply under popular parent comments if mode is first_reply / first_sub_comment
        if target_mode in ("first_reply", "first_sub_comment"):
            sorted_parents = sorted(
                parent_comments.items(),
                key=lambda x: x[1].get("like_count", 0),
                reverse=True,
            )
            reply_token_map = dict(reply_tokens)
            max_threads = max(limit * 3, 15)
            for parent_id, p_info in sorted_parents[:max_threads]:
                if len(candidates) >= limit * 2:
                    break
                r_token = reply_token_map.get(parent_id)
                if not r_token:
                    continue

                parent_text = p_info.get("text", "")
                parent_author = p_info.get("author", "")
                parent_likes = p_info.get("like_count", 0)

                r_payload = {
                    "context": {"client": {"clientName": "WEB", "clientVersion": client_version}},
                    "continuation": r_token,
                }
                try:
                    r_resp = await self._session.post(
                        next_url,
                        json=r_payload,
                        headers={"Content-Type": "application/json"},
                    )
                    if r_resp.status_code == 200:
                        replies_data = r_resp.json()
                        parsed_replies = self._parse_replies(
                            replies_data,
                            video_id=video_id,
                            parent_id=parent_id,
                            parent_text=parent_text,
                            parent_author=parent_author,
                        )
                        valid_replies = [r for r in parsed_replies if self._passes_filters(r.text)]
                        # Sort replies by likes descending so the most upvoted / prominent top reply (as seen in YouTube UI) is chosen
                        valid_replies.sort(key=lambda r: r.like_count, reverse=True)
                        if valid_replies:
                            r = valid_replies[0]
                            candidates.append({
                                "video_id": r.video_id,
                                "comment_id": r.comment_id,
                                "parent_id": r.parent_comment_id,
                                "author": r.author,
                                "text": r.text,
                                "parent_author": r.parent_author,
                                "parent_text": r.parent_text,
                                "is_reply": True,
                                "like_count": r.like_count,
                                "parent_likes": parent_likes,
                                "permalink": r.permalink,
                            })
                except Exception as exc:
                    logger.debug("Failed to fetch first reply for thread {}: {}", parent_id, exc)

        if target_mode in ("first_reply", "first_sub_comment"):
            candidates.sort(key=lambda c: (c.get("parent_likes", 0), c["like_count"]), reverse=True)
        else:
            candidates.sort(key=lambda c: c["like_count"], reverse=True)

        results: list[CollectedItem] = []
        seen: set[str] = set()

        for c in candidates:
            if len(results) >= limit:
                break
            cid = c["comment_id"]
            if cid in seen:
                continue
            seen.add(cid)

            if not self._passes_filters(c["text"]):
                continue

            detection = await asyncio.to_thread(self._detect_language, c["text"])
            if detection is None:
                continue
            language, confidence = detection
            if language not in self._languages:
                continue
            if confidence < self._cfg.minimum_confidence:
                continue

            if c["is_reply"]:
                ext_id = f"{video_id}#{c['parent_id']}#{cid}"
            else:
                ext_id = f"{video_id}#{cid}"

            results.append(
                CollectedItem(
                    endpoint=f"youtube:{video_id}",
                    external_id=ext_id,
                    text=c["text"],
                    language=language,
                    confidence=confidence,
                    source_url=c["permalink"],
                    parent_id=c["parent_id"],
                    parent_text=c["parent_text"],
                    parent_author=c["parent_author"],
                    author=c["author"],
                    video_id=video_id,
                    is_reply=c["is_reply"],
                    like_count=c["like_count"],
                )
            )

        logger.debug(
            "Найдено {} комментариев для видео {} (режим={}, максимум лайков={})",
            len(results),
            video_id,
            target_mode,
            results[0].like_count if results else 0,
        )
        return results[:limit]

    def _passes_filters(self, text: str) -> bool:
        normalized = _WHITESPACE.sub(" ", text).strip()
        length = len(normalized)
        if length < self._cfg.min_text_length or length > self._cfg.max_text_length:
            return False
        lowered = normalized.lower()
        for blocked in self._cfg.block_keywords:
            if blocked.lower() in lowered:
                return False
        if self._cfg.require_any_keywords:
            matched = any(kw.lower() in lowered for kw in self._cfg.require_any_keywords)
            if not matched:
                return False
        return True

    def _detect_language(self, text: str) -> tuple[str, float] | None:
        cleaned = _WHITESPACE.sub(" ", text).strip()
        if not cleaned:
            return None
        confidence_values = self._detector.compute_language_confidence_values(cleaned)
        if not confidence_values:
            return None
        best = confidence_values[0]
        iso = best.language.iso_code_639_1.name.lower()
        return iso, float(best.value)


__all__ = [
    "YouTubeCollector",
    "YouTubeSubComment",
    "build_search_params",
    "parse_age_hours",
    "upload_date_filter",
    "extract_video_id",
    "parse_like_count",
    "parse_view_count",
]
