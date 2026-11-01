from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from config import parse_proxy
from modules.geo_guard import normalize_country, normalize_locale
from modules.totp_helper import validate_totp_secret

_HOST = r"(?:\[[0-9A-Fa-f:.]+\]|[^:/@\s\[\]]+)"
# Proxy at the end of the line: scheme URL, host:port:user:pass, user:pass@host:port or a bare host:port.
# The credentials of `user:pass@host:port` cannot hold ':' or '@' here (otherwise the TOTP secret would be swallowed).
_PROXY_TAIL = re.compile(
    r":("
    r"(?:[a-z][a-z0-9+.-]*://\S+)"
    rf"|(?:{_HOST}:\d{{1,5}}:[^:@\s]+:[^@\s]+)"
    rf"|(?:[^:@\s]+:[^:@\s]*@{_HOST}:\d{{1,5}})"
    rf"|(?:{_HOST}:\d{{1,5}})"
    r")$",
    re.IGNORECASE,
)
_EMAIL_PREFIX = re.compile(r"^[^:@\s]+@[^:\s]+:")
_GEO_TAIL = re.compile(r":(country|locale)=([^:\s]*)$", re.IGNORECASE)  # `...:country=PL:locale=pl-PL`, the very end of the line


class ParsedAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    email: str
    password: str = Field(repr=False, exclude=True)
    totp_secret: str = Field(repr=False, exclude=True)
    recovery_email: str | None = None
    proxy: str | None = None
    expected_country: str | None = None
    locale: str | None = None

    @property
    def default_name(self) -> str:
        return self.email.lower()


def looks_like_credentials(value: str) -> bool:
    """`email:...` - tells the new credentials form of --add-account from a legacy plain account name."""
    return bool(_EMAIL_PREFIX.match(value.strip()))


def _split_credentials(rest: str) -> tuple[str, str, str | None]:
    """`password[:colons]:totp_secret[:recovery_email]` -> (password, canonical totp secret, recovery email)."""
    pieces = rest.split(":")
    while len(pieces) > 2 and not pieces[-1].strip():  # empty placeholders: `secret::proxy`, trailing ':'
        pieces.pop()
    recovery_email: str | None = None
    if len(pieces) >= 3 and "@" in pieces[-1]:
        recovery_email = pieces.pop().strip() or None
    if len(pieces) < 2:
        raise ValueError("не хватает полей: нужны email:password:totp_secret")
    secret_text = pieces.pop().strip()
    password = ":".join(pieces)
    if not password or not secret_text:
        raise ValueError("пароль и TOTP-секрет не должны быть пустыми")
    try:
        totp_secret = validate_totp_secret(secret_text)
    except ValueError as exc:
        raise ValueError(f"{exc}; проверьте формат строки и порядок полей") from None
    return password, totp_secret, recovery_email


def _geo_fields(country: str | None, locale: str | None) -> tuple[str | None, str | None]:
    """Validated ISO country and `ll-RR` locale; a value that is set but not recognised is a ValueError."""
    iso = normalize_country(country) if country and country.strip() else None
    if country and country.strip() and iso is None:
        raise ValueError("country: страна не распознана (ожидается код ISO, например PL, или название, например Poland)")
    tag = normalize_locale(locale) if locale and locale.strip() else None
    if locale and locale.strip() and tag is None:
        raise ValueError("locale: неверный формат, ожидается язык-СТРАНА, например pl-PL")
    return iso, tag


def parse_account_line(
    line: str, proxy: str | None = None, country: str | None = None, locale: str | None = None
) -> ParsedAccount:
    """Parse `email:password:totp_secret[:recovery_email][:proxy][:country=PL][:locale=pl-PL]`.

    The password may contain colons: the email is everything before the first colon, the TOTP secret is the last
    piece before the optional recovery email (the only piece with '@') and the optional proxy. The secret must be
    valid Base32, so a mis-split line (a proxy mistaken for the secret) is rejected instead of stored.
    The proxy is `scheme://...`, `host:port`, `user:pass@host:port` or `host:port:user:pass` (IPv6 in brackets).
    `country` (the account's expected country: ISO code or name) and `locale` are the last `key=value` pieces of the
    line; the `country` / `locale` arguments are the defaults when the line has none.
    """
    text = line.strip()
    if not text:
        raise ValueError("пустая строка аккаунта")
    email, separator, rest = text.partition(":")
    email = email.strip()
    if not separator or "@" not in email:
        raise ValueError("формат: email:password:totp_secret[:recovery_email][:proxy]")

    geo = {"country": country, "locale": locale}
    while (geo_tail := _GEO_TAIL.search(rest)) is not None:
        geo[geo_tail.group(1).lower()] = geo_tail.group(2)
        rest = rest[: geo_tail.start()]
    expected_country, account_locale = _geo_fields(geo["country"], geo["locale"])

    candidates: list[tuple[str, str | None]] = []
    tail = _PROXY_TAIL.search(rest)
    if tail:
        candidates.append((rest[: tail.start()], tail.group(1)))
    candidates.append((rest, None))  # no proxy in the line (or the "proxy" was part of the password)

    first_error: ValueError | None = None
    for body, line_proxy in candidates:
        try:
            password, totp_secret, recovery_email = _split_credentials(body)
            effective_proxy = line_proxy or (proxy.strip() if proxy else None) or None
            if effective_proxy is not None:
                parse_proxy(effective_proxy)  # ValueError on a malformed proxy (the message hides the value)
        except ValueError as exc:
            first_error = first_error or exc
            continue
        return ParsedAccount(
            email=email,
            password=password,
            totp_secret=totp_secret,
            recovery_email=recovery_email,
            proxy=effective_proxy,
            expected_country=expected_country,
            locale=account_locale,
        )
    assert first_error is not None
    raise first_error


__all__ = ["ParsedAccount", "looks_like_credentials", "parse_account_line"]
