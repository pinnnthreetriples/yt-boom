from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import re
import struct
import time

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
DRIFT_GUARD_SECONDS = 4

_BASE32_BODY = re.compile(r"[A-Z2-7]+")


def normalize_secret(secret: str) -> str:
    """Canonical Base32 form of a TOTP secret: upper case, no spaces/dashes/padding. ValueError when it is not Base32."""
    clean = re.sub(r"[\s-]+", "", secret or "").upper().rstrip("=")
    if not clean:
        raise ValueError("TOTP-секрет пуст")
    if not _BASE32_BODY.fullmatch(clean):
        raise ValueError("TOTP-секрет должен быть в Base32 (буквы A-Z и цифры 2-7)")
    return clean


def _decode_secret(secret: str) -> bytes:
    clean = normalize_secret(secret)
    clean += "=" * (-len(clean) % 8)
    try:
        return base64.b32decode(clean)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("TOTP-секрет имеет недопустимую длину для Base32") from exc


def generate_totp(secret: str, at: float | None = None, digits: int = TOTP_DIGITS) -> str:
    """RFC 6238 TOTP (HMAC-SHA1, 30 s window). `secret` is Base32; spaces, dashes and missing `=` padding are allowed.

    Raises ValueError for an empty or non-Base32 secret.
    """
    key = _decode_secret(secret)
    counter = int((time.time() if at is None else at) // TOTP_STEP_SECONDS)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def validate_totp_secret(secret: str) -> str:
    """Normalise the secret and prove it decodes (a trial code is generated); returns the canonical form."""
    clean = normalize_secret(secret)
    generate_totp(clean, at=0)
    return clean


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


__all__ = [
    "generate_fresh_totp",
    "generate_totp",
    "get_remaining_seconds",
    "normalize_secret",
    "validate_totp_secret",
]
