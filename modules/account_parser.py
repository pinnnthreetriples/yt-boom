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
_GEO_TAIL = re.compile(r":(country|locale|mode)=([^:\s]*)$", re.IGNORECASE)  # `...:country=PL:locale=pl-PL:mode=mature`, the very end of the line
_MODES = ("auto", "mature", "unlimited")


class ParsedAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    email: str
    password: str = Field(repr=False, exclude=True)
    totp_secret: str | None = Field(default=None, repr=False, exclude=True)
    recovery_email: str | None = None
    proxy: str | None = None
    expected_country: str | None = None
    locale: str | None = None
    lifecycle_mode: str | None = None  # `mode=auto|mature|unlimited`; None: the line does not say

    @property
    def default_name(self) -> str:
        return self.email.lower()


def looks_like_credentials(value: str) -> bool:
    """`email:...` - tells the new credentials form of --add-account from a legacy plain account name."""
    return bool(_EMAIL_PREFIX.match(value.strip()))


def _split_credentials(rest: str) -> tuple[str, str | None, str | None]:
    """`password[:colons][:totp_secret][:recovery_email]` -> (password, canonical totp secret or None, recovery email).

    The secret is optional: `password` alone or `password:` (empty placeholder) means no TOTP. Without a secret the
    password cannot contain colons, otherwise its last piece is read as the secret.
    """
    pieces = rest.split(":")
    while len(pieces) > 2 and not pieces[-1].strip():  # empty placeholders: `secret::proxy`, trailing ':'
        pieces.pop()
    recovery_email: str | None = None
    if len(pieces) >= 3 and "@" in pieces[-1]:
        recovery_email = pieces.pop().strip() or None
    secret_text = pieces.pop().strip() if len(pieces) >= 2 else ""
    password = ":".join(pieces)
    if not password:
        raise ValueError("пароль не должен быть пустым")
    if not secret_text:
        return password, None, recovery_email
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


def _mode_field(mode: str | None) -> str | None:
    """The account's lifecycle mode from `mode=...`; an unknown word is a ValueError."""
    if mode is None:
        return None
    value = mode.strip().lower()
    if value not in _MODES:
        raise ValueError("mode: ожидается auto, mature или unlimited")
    return value


_PASSWORD_STAND_IN = "x"  # colon-free placeholder: the free-form password is put back after the colon form is parsed


def _free_form_to_colon_line(text: str) -> tuple[str, str]:
    """`email password [totp] [recovery@mail] [proxy] [country=PL] [locale=pl-PL]` (spaces/tabs, any order after the
    password) -> (the colon form with `_PASSWORD_STAND_IN` as the password, the real password). Extra fields are told
    apart by shape: a proxy has `://` or `host:port`, the recovery e-mail has '@', `country=` / `locale=` / `mode=`
    are keys, anything else is the TOTP secret (groups like `JBSW Y3DP` are joined). The password is a whole token,
    so it may hold colons, which the colon form would otherwise split into a "TOTP secret".
    """
    tokens = text.split()
    if len(tokens) < 2 or "@" not in tokens[0]:
        raise ValueError("формат: email пароль [totp_secret] [recovery_email] [proxy]")
    email, password, extras = tokens[0], tokens[1], tokens[2:]
    secret_parts: list[str] = []
    recovery_email = proxy = None
    geo: list[str] = []
    for token in extras:
        proxy_match = _PROXY_TAIL.search(f":{token}")
        if "=" in token and token.split("=", 1)[0].lower() in ("country", "locale", "mode"):
            geo.append(token)
        elif proxy_match is not None and proxy_match.start() == 0:
            if proxy is not None:
                raise ValueError("в строке два прокси")
            proxy = token
        elif "@" in token:
            if recovery_email is not None:
                raise ValueError("в строке две резервные почты")
            recovery_email = token
        else:
            secret_parts.append(token)
    pieces = [email, _PASSWORD_STAND_IN, "".join(secret_parts)]
    if recovery_email is not None or proxy is not None:
        pieces.append(recovery_email or "")
    if proxy is not None:
        pieces.append(proxy)
    return ":".join([*pieces, *geo]), password


def parse_account_line(
    line: str, proxy: str | None = None, country: str | None = None, locale: str | None = None
) -> ParsedAccount:
    """Parse `email:password[:totp_secret][:recovery_email][:proxy][:country=PL][:locale=pl-PL]`.

    The password may contain colons: the email is everything before the first colon, the TOTP secret is the last
    piece before the optional recovery email (the only piece with '@') and the optional proxy. The secret may be
    left out (`email:password`); a secret that is present must be valid
    Base32, so a mis-split line (a proxy mistaken for the secret) is rejected instead of stored.
    Instead of colons the fields may be separated by spaces or tabs (`email password totp ...`), in any order after
    the password; see `_free_form_to_colon_line`.
    The proxy is `scheme://...`, `host:port`, `user:pass@host:port` or `host:port:user:pass` (IPv6 in brackets).
    `country` (the account's expected country: ISO code or name) and `locale` are the last `key=value` pieces of the
    line, like `mode=auto|mature|unlimited` (the account's age mode); the `country` / `locale` arguments are the
    defaults when the line has none.
    """
    text = line.strip()
    if not text:
        raise ValueError("пустая строка аккаунта")
    free_form_password: str | None = None
    if not _EMAIL_PREFIX.match(text) and re.search(r"\s", text):
        text, free_form_password = _free_form_to_colon_line(text)
    email, separator, rest = text.partition(":")
    email = email.strip()
    if not separator or "@" not in email:
        raise ValueError("формат: email:password[:totp_secret][:recovery_email][:proxy]")

    geo: dict[str, str | None] = {"country": country, "locale": locale, "mode": None}
    while (geo_tail := _GEO_TAIL.search(rest)) is not None:
        geo[geo_tail.group(1).lower()] = geo_tail.group(2)
        rest = rest[: geo_tail.start()]
    expected_country, account_locale = _geo_fields(geo["country"], geo["locale"])
    lifecycle_mode = _mode_field(geo["mode"])

    candidates: list[tuple[str, str | None]] = []
    tail = _PROXY_TAIL.search(rest)
    if tail:
        candidates.append((rest[: tail.start()], tail.group(1)))
    if not (tail and free_form_password is not None):  # free form: the proxy token was already told apart by shape
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
            password=free_form_password if free_form_password is not None else password,
            totp_secret=totp_secret,
            recovery_email=recovery_email,
            proxy=effective_proxy,
            expected_country=expected_country,
            locale=account_locale,
            lifecycle_mode=lifecycle_mode,
        )
    assert first_error is not None
    raise first_error


__all__ = ["ParsedAccount", "looks_like_credentials", "parse_account_line"]
