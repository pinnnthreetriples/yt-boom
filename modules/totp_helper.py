from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import struct
import time

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
DRIFT_GUARD_SECONDS = 4


def _decode_secret(secret: str) -> bytes:
    clean = re.sub(r"\s+", "", secret).upper()
    missing = len(clean) % 8
    if missing:
        clean += "=" * (8 - missing)
    return base64.b32decode(clean, casefold=True)


def generate_totp(secret: str, at: float | None = None, digits: int = TOTP_DIGITS) -> str:
    """RFC 6238 TOTP (HMAC-SHA1, 30 s window). `secret` is Base32, spaces and missing `=` padding are allowed."""
    key = _decode_secret(secret)
    counter = int((time.time() if at is None else at) // TOTP_STEP_SECONDS)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def get_remaining_seconds(at: float | None = None) -> int:
    """Seconds left in the current 30-second TOTP window."""
    now = time.time() if at is None else at
    return TOTP_STEP_SECONDS - int(now % TOTP_STEP_SECONDS)


async def generate_fresh_totp(secret: str) -> str:
    """Drift guard: when fewer than 4 seconds are left in the window, wait for the next one so the code does not expire in flight."""
    remaining = get_remaining_seconds()
    if remaining < DRIFT_GUARD_SECONDS:
        await asyncio.sleep(remaining + 0.5)
    return generate_totp(secret)


__all__ = ["generate_fresh_totp", "generate_totp", "get_remaining_seconds"]
