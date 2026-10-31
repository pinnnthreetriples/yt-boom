from __future__ import annotations

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


class ValidationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    delivered: bool
    http_status: int | None = None
    checked_url: str | None = None
    duration_seconds: float
    error: str | None = None


def _normalize_for_compare(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().casefold()


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
        remote_id = quote(payload.remote_id, safe="") if payload.remote_id else ""
        url = template.format(remote_id=remote_id, payload_id=payload.id)
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
