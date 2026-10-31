from __future__ import annotations

import html
import re
import time
from typing import Any
from urllib.parse import quote, urlparse

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException
from loguru import logger
from pydantic import BaseModel, ConfigDict

from config import ValidatorSettings
from database import Payload

_WHITESPACE = re.compile(r"\s+")
_META_TAG = re.compile(r"<meta\s[^>]*>", re.IGNORECASE)
_META_ATTR = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_YOUTUBE_COMMENT_URL = "https://www.youtube.com/watch?v={video_id}&lc={remote_id}"


class ValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    delivered: bool
    http_status: int | None = None
    checked_url: str | None = None
    duration_seconds: float
    error: str | None = None


def _normalize_for_compare(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().casefold()


def _video_id_of(payload: Payload) -> str:
    """Video id from payload.source_ref (`video_id#parent_id[#comment_id]`)."""
    return (payload.source_ref or "").split("#", 1)[0].strip()


def _is_youtube_comment(payload: Payload) -> bool:
    return bool(_video_id_of(payload)) and (payload.remote_id or "").startswith("Ug")


def _meta_properties(page: str) -> dict[str, str]:
    """`<meta property=... content=...>` pairs of an HTML page (first occurrence wins)."""
    found: dict[str, str] = {}
    for tag in _META_TAG.findall(page):
        attrs = {m.group(1).lower(): html.unescape(m.group(2) if m.group(2) is not None else m.group(3))
                 for m in _META_ATTR.finditer(tag)}
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs:
            found.setdefault(key, attrs["content"])
    return found


class DeliveryValidator:
    """Read-only HTTP verification that a submitted fixture is rendered publicly."""

    def __init__(self, settings: ValidatorSettings) -> None:
        self._cfg = settings
        self._session: AsyncSession | None = None

    async def open(self) -> None:
        if self._session is not None:
            return
        self._session = AsyncSession(
            impersonate=self._cfg.impersonate,
            timeout=self._cfg.timeout_seconds,
            headers={"Accept": "application/json, text/html;q=0.9"},
        )
        logger.debug("Validator HTTP session opened (impersonate={})", self._cfg.impersonate)

    async def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()
            logger.debug("Validator HTTP session closed")

    async def __aenter__(self) -> "DeliveryValidator":
        await self.open()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    def build_url(self, payload: Payload) -> str:
        template = self._cfg.endpoint_template
        if "{remote_id}" in template and not payload.remote_id:
            raise ValueError(f"payload {payload.id} has no remote_id")
        video_id = quote(_video_id_of(payload), safe="")
        if "{video_id}" in template and not video_id:
            raise ValueError(f"payload {payload.id} has no video id in source_ref")
        remote_id = quote(payload.remote_id, safe="") if payload.remote_id else ""
        url = template.format(video_id=video_id, remote_id=remote_id, payload_id=payload.id)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"validation url is not absolute http(s): {url!r}")
        return url

    async def validate(self, payload: Payload) -> ValidationResult:
        started = time.monotonic()
        if not payload.text.strip():
            return ValidationResult(
                delivered=False,
                duration_seconds=0.0,
                error="payload text is empty",
            )
        if _is_youtube_comment(payload):
            return await self._validate_youtube_comment(payload, started)
        try:
            url = self.build_url(payload)
        except ValueError as exc:
            return ValidationResult(
                delivered=False,
                duration_seconds=time.monotonic() - started,
                error=f"cannot build validation url: {exc}",
            )

        await self.open()
        assert self._session is not None
        try:
            response = await self._session.get(url)
        except RequestException as exc:
            duration = time.monotonic() - started
            logger.warning("Validation request for payload {} failed: {}", payload.id, exc)
            return ValidationResult(
                delivered=False,
                checked_url=url,
                duration_seconds=duration,
                error=f"{type(exc).__name__}: {exc}",
            )

        duration = time.monotonic() - started
        status = int(response.status_code)
        if status != 200:
            logger.warning("Validation of payload {} returned HTTP {}", payload.id, status)
            return ValidationResult(
                delivered=False,
                http_status=status,
                checked_url=url,
                duration_seconds=duration,
                error=f"HTTP {status}",
            )

        haystack = self._haystack(response)
        if not self._contains(haystack, payload.text):
            return ValidationResult(
                delivered=False,
                http_status=status,
                checked_url=url,
                duration_seconds=duration,
                error="payload text not found in response",
            )
        if self._cfg.success_marker and not self._contains(haystack, self._cfg.success_marker):
            return ValidationResult(
                delivered=False,
                http_status=status,
                checked_url=url,
                duration_seconds=duration,
                error=f"success marker {self._cfg.success_marker!r} not found",
            )

        logger.debug("Validation of payload {} succeeded in {:.2f}s", payload.id, duration)
        return ValidationResult(
            delivered=True,
            http_status=status,
            checked_url=url,
            duration_seconds=duration,
        )

    async def _validate_youtube_comment(self, payload: Payload, started: float) -> ValidationResult:
        """A reply is public when its permalink renders `og:title` 'Comment from...' with our text in `og:description`."""
        url = _YOUTUBE_COMMENT_URL.format(
            video_id=quote(_video_id_of(payload), safe=""),
            remote_id=quote(payload.remote_id or "", safe=""),
        )
        await self.open()
        assert self._session is not None
        try:
            response = await self._session.get(url, headers={"Accept": "text/html"})
        except RequestException as exc:
            logger.warning("Validation request for payload {} failed: {}", payload.id, exc)
            return ValidationResult(
                delivered=False,
                checked_url=url,
                duration_seconds=time.monotonic() - started,
                error=f"{type(exc).__name__}: {exc}",
            )

        duration = time.monotonic() - started
        status = int(response.status_code)

        def verdict(delivered: bool, error: str | None = None) -> ValidationResult:
            return ValidationResult(
                delivered=delivered, http_status=status, checked_url=url, duration_seconds=duration, error=error
            )

        if status != 200:
            logger.warning("Validation of payload {} returned HTTP {}", payload.id, status)
            return verdict(False, f"HTTP {status}")

        meta = _meta_properties(getattr(response, "text", "") or "")
        if not meta.get("og:title", "").casefold().startswith("comment from"):
            return verdict(False, "og:title 'Comment from...' not found")
        description = _normalize_for_compare(meta.get("og:description", ""))
        needle = _normalize_for_compare(payload.text)
        truncated = description.endswith(("…", "..."))
        prefix = description.rstrip("….").rstrip()
        if needle not in description and not (truncated and len(prefix) >= 8 and needle.startswith(prefix)):
            return verdict(False, "payload text not found in og:description")
        if self._cfg.success_marker and not self._contains([description], self._cfg.success_marker):
            return verdict(False, f"success marker {self._cfg.success_marker!r} not found")

        logger.debug("Validation of payload {} succeeded in {:.2f}s", payload.id, duration)
        return verdict(True)

    @staticmethod
    def _haystack(response: Any) -> list[str]:
        chunks: list[str] = [getattr(response, "text", "") or ""]
        try:
            data = response.json()
        except (ValueError, TypeError):
            return chunks

        stack: list[Any] = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, str):
                chunks.append(node)
            elif isinstance(node, dict):
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        return chunks

    @staticmethod
    def _contains(haystack: list[str], needle: str) -> bool:
        normalized_needle = _normalize_for_compare(needle)
        if not normalized_needle:
            return False
        return any(normalized_needle in _normalize_for_compare(chunk) for chunk in haystack)


__all__ = ["DeliveryValidator", "ValidationResult"]
