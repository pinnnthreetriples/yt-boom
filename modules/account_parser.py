from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

from config import parse_proxy

# Proxy at the end of the line: scheme URL, raw ip:port:user:pass, or bare host.name:port
_PROXY_TAIL = re.compile(
    r":((?:[a-z][a-z0-9+.-]*://\S+)|(?:[^:/@\s]+:\d{2,5}:[^:@\s]+:[^@\s]+)|(?:[^:/@\s]+\.[^:/@\s]+:\d{2,5}))$",
    re.IGNORECASE,
)
_EMAIL_PREFIX = re.compile(r"^[^:@\s]+@[^:\s]+:")


class ParsedAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    email: str
    password: str
    totp_secret: str
    recovery_email: str | None = None
    proxy: str | None = None

    @property
    def default_name(self) -> str:
        return self.email.lower()


def looks_like_credentials(value: str) -> bool:
    """`email:...` - tells the new credentials form of --add-account from a legacy plain account name."""
    return bool(_EMAIL_PREFIX.match(value.strip()))


def parse_account_line(line: str, proxy: str | None = None) -> ParsedAccount:
    """Parse `email:password:totp_secret[:recovery_email][:proxy]`.

    The password may contain colons: the email is everything before the first colon, the TOTP secret is the last
    piece before the optional recovery email (the only piece with '@') and the optional proxy.
    """
    text = line.strip()
    if not text:
        raise ValueError("пустая строка аккаунта")
    email, separator, rest = text.partition(":")
    email = email.strip()
    if not separator or "@" not in email:
        raise ValueError("формат: email:password:totp_secret[:recovery_email][:proxy]")

    line_proxy: str | None = None
    tail = _PROXY_TAIL.search(rest)
    if tail:
        line_proxy = tail.group(1)
        rest = rest[: tail.start()]
    pieces = rest.split(":")

    recovery_email: str | None = None
    if len(pieces) >= 3 and "@" in pieces[-1]:
        recovery_email = pieces.pop().strip() or None
    if len(pieces) < 2:
        raise ValueError("не хватает полей: нужны email:password:totp_secret")
    totp_secret = pieces.pop().strip()
    password = ":".join(pieces)
    if not password or not totp_secret:
        raise ValueError("пароль и TOTP-секрет не должны быть пустыми")

    effective_proxy = line_proxy or (proxy.strip() if proxy else None) or None
    if effective_proxy is not None:
        parse_proxy(effective_proxy)  # ValueError on a malformed proxy
    return ParsedAccount(
        email=email,
        password=password,
        totp_secret=totp_secret,
        recovery_email=recovery_email,
        proxy=effective_proxy,
    )


__all__ = ["ParsedAccount", "looks_like_credentials", "parse_account_line"]
