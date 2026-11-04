from __future__ import annotations

import asyncio
from pathlib import Path
import random
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from database import Database, VideoPoolItem
from modules.youtube_collector import YouTubeCollector, extract_video_id


class PoolAddResult(BaseModel):
    """Outcome of adding a pasted / loaded list to a pool."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    added: int = 0       # new records in the DB
    existing: int = 0    # already were in the pool
    invalid: int = 0     # lines that are not a video link / ID
    items: list[Any] = Field(default_factory=list)  # every unique valid entry, in input order


_LINK_LIKE = re.compile(r"(?:https?://|www\.|youtu|@)", re.IGNORECASE)


def _pool_entries(text: str) -> list[str]:
    """Entries without '#' comments (whole-line or inline): one per line, or several per line.

    Commas and semicolons always separate entries; spaces only when every part is a link or @handle,
    so a line of plain words stays one (invalid) entry instead of becoming several bare handles.
    """
    entries = []
    for raw_line in text.splitlines():
        clean = raw_line.split("#")[0].strip()
        for piece in re.split(r"[,;]", clean):
            piece = piece.strip()
            if not piece:
                continue
            parts = piece.split()
            if len(parts) > 1 and all(_LINK_LIKE.match(p) for p in parts):
                entries.extend(parts)
            else:
                entries.append(piece)
    return entries


def parse_video_text(text: str) -> tuple[list[str], int]:
    """Canonical watch URLs (unique, input order) and the number of unrecognised lines."""
    urls: list[str] = []
    seen: set[str] = set()
    invalid = 0
    for entry in _pool_entries(text):
        vid = extract_video_id(entry)
        if not vid:
            logger.warning("Skipping invalid video entry {!r}", entry)
            invalid += 1
        elif vid not in seen:
            seen.add(vid)
            urls.append(f"https://www.youtube.com/watch?v={vid}")
    return urls, invalid


class PoolManager:
    """Manages the video pool: pasted / loaded video lists and their reply progress."""

    def __init__(
        self,
        db: Database,
        collector: YouTubeCollector | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._db = db
        self._collector = collector
        self._rng = rng if rng is not None else random.Random()

    # ------------------------------------------------------------- Video Pool

    async def load_videos_from_file(
        self,
        file_path: str | Path,
        comments_target: int = 2,
    ) -> list[VideoPoolItem]:
        """
        Load video URLs or IDs from a text file into the video pool.
        Skips comments (#), empty lines, and duplicates.
        """
        path = Path(file_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"Video pool file not found: {path}")

        if comments_target < 1:
            raise ValueError("comments_target must be positive")

        result = await self.add_videos_from_text(path.read_text(encoding="utf-8"), comments_target)
        logger.info("Loaded {} unique video(s) into pool from {}", len(result.items), path.name)
        return result.items

    async def add_videos_from_text(self, text: str, comments_target: int = 2) -> PoolAddResult:
        """Add pasted video links / IDs (one per line) to the pool; counts new, existing and invalid."""
        if comments_target < 1:
            raise ValueError("comments_target must be positive")
        urls, invalid = parse_video_text(text)
        known = {v.video_url for v in await self._db.list_pool_videos(status="all", source="links")}
        result = PoolAddResult(invalid=invalid)
        for url in urls:
            if url in known:
                result.existing += 1
            else:
                result.added += 1
            result.items.append(await self._db.add_video_to_pool(video_url=url, comments_target=comments_target))
        return result

    async def get_next_video_task(self) -> VideoPoolItem | None:
        """Fetch next pending or in-progress video requiring comments from the pool."""
        return await self._db.get_next_pool_video()

    async def record_video_progress(
        self,
        video_id: int,
        comments_added: int = 1,
        completed: bool = False,
    ) -> None:
        """Update comment progress for a pool video and mark completed when target is met."""
        await self._db.update_pool_video_progress(
            pool_video_id=video_id,
            comments_added=comments_added,
            completed=completed,
        )


__all__ = [
    "PoolAddResult",
    "PoolManager",
    "parse_video_text",
]
