from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException
from lingua import IsoCode639_1, Language, LanguageDetectorBuilder
from loguru import logger
from pydantic import BaseModel, ConfigDict

from config import CollectorEndpoint, CollectorSettings

_WHITESPACE = re.compile(r"\s+")


class CollectorError(RuntimeError):
    """Raised when an endpoint cannot be fetched or parsed."""


class CollectedItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    endpoint: str
    external_id: str
    text: str
    language: str
    confidence: float
    source_url: str | None = None
    parent_id: str | None = None
    parent_text: str | None = None
    parent_author: str | None = None
    author: str | None = None
    video_id: str | None = None
    is_reply: bool = False
    like_count: int = 0


class Collector:
    """Fetches candidate fixtures from public endpoints and filters them by metrics and language."""

    def __init__(self, settings: CollectorSettings) -> None:
        self._cfg = settings
        self._session: AsyncSession | None = None
        self._languages = set(settings.languages)
        self._detector = self._build_detector(settings.languages)

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
        except Exception as exc:  # lingua raises RuntimeError/ValueError on bad detector setup
            raise CollectorError(f"failed to build language detector: {exc}") from exc

    async def open(self) -> None:
        if self._session is not None:
            return
        self._session = AsyncSession(
            impersonate=self._cfg.impersonate,
            timeout=self._cfg.timeout_seconds,
            headers={"Accept": "application/json"},
        )
        logger.debug("Collector HTTP session opened (impersonate={})", self._cfg.impersonate)

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()
            logger.debug("Collector HTTP session closed")

    async def __aenter__(self) -> "Collector":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def collect(self) -> list[CollectedItem]:
        await self.open()
        items: list[CollectedItem] = []
        seen: set[str] = set()

        for endpoint in self._cfg.endpoints:
            if "youtube.com" in endpoint.url or "youtu.be" in endpoint.url:
                from modules.youtube_collector import YouTubeCollector
                yt_col = YouTubeCollector(self._cfg)
                yt_items = await yt_col.collect_from_videos([endpoint.url], max_items=self._cfg.max_items_per_endpoint)
                for item in yt_items:
                    if item.external_id not in seen:
                        seen.add(item.external_id)
                        items.append(item)
                continue

            try:
                payload = await self._fetch_json(endpoint)
            except CollectorError as exc:
                logger.error("Collector endpoint {} failed: {}", endpoint.name, exc)
                continue

            raw_items = self._extract_items(payload, endpoint)
            if not raw_items:
                logger.warning("Collector endpoint {} returned no usable items", endpoint.name)
                continue

            accepted = 0
            for raw in raw_items:
                if accepted >= self._cfg.max_items_per_endpoint:
                    break
                candidate = self._build_candidate(endpoint, raw)
                if candidate is None:
                    continue
                if candidate["external_id"] in seen:
                    logger.trace("Skipping duplicate item {}", candidate["external_id"])
                    continue
                if not self._passes_filters(candidate["text"]):
                    logger.trace("Skipping item {} by metric/keyword filters", candidate["external_id"])
                    continue
                detection = await asyncio.to_thread(self._detect_language, candidate["text"])
                if detection is None:
                    logger.trace("Skipping item {}: language undetected", candidate["external_id"])
                    continue
                language, confidence = detection
                if language not in self._languages:
                    logger.trace("Skipping item {}: language {} not allowed", candidate["external_id"], language)
                    continue
                if confidence < self._cfg.minimum_confidence:
                    logger.trace(
                        "Skipping item {}: confidence {:.2f} below {:.2f}",
                        candidate["external_id"],
                        confidence,
                        self._cfg.minimum_confidence,
                    )
                    continue

                seen.add(candidate["external_id"])
                items.append(
                    CollectedItem(
                        endpoint=candidate["endpoint"],
                        external_id=candidate["external_id"],
                        text=candidate["text"],
                        language=language,
                        confidence=confidence,
                        source_url=candidate["source_url"],
                    )
                )
                accepted += 1

            logger.info(
                "Collector endpoint {}: {} item(s) accepted ({} raw)",
                endpoint.name,
                accepted,
                len(raw_items),
            )

        logger.info("Collector accepted {} item(s) in total", len(items))
        return items

    async def _fetch_json(self, endpoint: CollectorEndpoint) -> Any:
        session = self._session
        if session is None:
            raise CollectorError("collector session is not open")
        last_error: str = "unknown error"
        for attempt in range(1, self._cfg.max_retries + 1):
            try:
                response = await session.get(endpoint.url)
            except RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "GET {} attempt {}/{} failed: {}", endpoint.url, attempt, self._cfg.max_retries, last_error
                )
            else:
                status = int(response.status_code)
                if status == 200:
                    try:
                        return response.json()
                    except (json.JSONDecodeError, ValueError) as exc:
                        last_error = f"invalid JSON: {exc}"
                        logger.warning(
                            "GET {} attempt {}/{} returned invalid JSON",
                            endpoint.url,
                            attempt,
                            self._cfg.max_retries,
                        )
                elif 400 <= status < 500 and status not in (408, 429):
                    raise CollectorError(f"{endpoint.name}: HTTP {status} is not retryable")
                else:
                    last_error = f"HTTP {status}"
                    logger.warning(
                        "GET {} attempt {}/{} returned {}", endpoint.url, attempt, self._cfg.max_retries, last_error
                    )
            if attempt < self._cfg.max_retries:
                await asyncio.sleep(self._cfg.retry_backoff_seconds * attempt)
        raise CollectorError(f"{endpoint.name}: giving up after {self._cfg.max_retries} attempts ({last_error})")

    @staticmethod
    def _dig(data: Any, path: str) -> Any:
        node = data
        for part in path.split("."):
            if not part:
                continue
            if isinstance(node, dict):
                node = node.get(part)
            elif isinstance(node, list) and part.lstrip("-").isdigit():
                index = int(part)
                node = node[index] if -len(node) <= index < len(node) else None
            else:
                return None
            if node is None:
                return None
        return node

    def _extract_items(self, payload: Any, endpoint: CollectorEndpoint) -> list[dict[str, Any]]:
        node = self._dig(payload, endpoint.items_path) if endpoint.items_path else payload
        if node is None:
            logger.warning("Collector endpoint {}: items_path {!r} not found", endpoint.name, endpoint.items_path)
            return []
        if isinstance(node, dict):
            node = [node]
        if not isinstance(node, list):
            logger.warning("Collector endpoint {}: items_path {!r} is not a list", endpoint.name, endpoint.items_path)
            return []
        return [entry for entry in node if isinstance(entry, dict)]

    def _build_candidate(self, endpoint: CollectorEndpoint, raw: dict[str, Any]) -> dict[str, Any] | None:
        text = self._dig(raw, endpoint.text_field)
        if not isinstance(text, str):
            logger.trace("Item in {} has no string field {!r}", endpoint.name, endpoint.text_field)
            return None
        text = text.strip()
        if not text:
            return None

        external_id = self._dig(raw, endpoint.id_field)
        if external_id is None or str(external_id).strip() == "":
            external_id = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
        else:
            external_id = str(external_id).strip()

        source_url = self._dig(raw, endpoint.url_field) if endpoint.url_field else None
        return {
            "endpoint": endpoint.name,
            "external_id": external_id,
            "text": text,
            "source_url": str(source_url) if source_url else None,
        }

    def _passes_filters(self, text: str) -> bool:
        normalized = _WHITESPACE.sub(" ", text).strip()
        if not (self._cfg.min_text_length <= len(normalized) <= self._cfg.max_text_length):
            return False
        lowered = normalized.lower()
        if any(keyword.lower() in lowered for keyword in self._cfg.block_keywords if keyword.strip()):
            return False
        required = [keyword.lower() for keyword in self._cfg.require_any_keywords if keyword.strip()]
        if required and not any(keyword in lowered for keyword in required):
            return False
        return True

    def _detect_language(self, text: str) -> tuple[str, float] | None:
        try:
            values = self._detector.compute_language_confidence_values(text)
        except Exception as exc:  # lingua can raise on pathological input
            logger.warning("Language detection failed: {}", exc)
            return None
        if not values:
            return None
        top = values[0]
        return top.language.iso_code_639_1.name.lower(), float(top.value)


__all__ = ["CollectedItem", "Collector", "CollectorError"]
