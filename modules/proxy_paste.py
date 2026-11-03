"""Parse proxies pasted from a seller: one line or a block of lines, every common shape.

Pure: no network, no database. `parse_proxy_paste(text)` gives one `PasteLine` per non-empty line, with the
recognised `ProxyDraft` or a Russian error that names the line (the line itself is never echoed: it holds a
password). Accepted shapes (fields may also be separated by spaces or tabs, the country may follow at the end as
ISO-2 or a name, the type may lead or follow as a word):

    1.2.3.4:8080:login:pass            1.2.3.4:8080:login:pass:PL       1.2.3.4 8080 login pass Poland
    login:pass@1.2.3.4:8080            1.2.3.4:8080@login:pass          socks5://login:pass@host.example:1080
    http://1.2.3.4:8080:login:pass     [2001:db8::1]:8080:login:pass    1.2.3.4:8080 (no credentials)

A password may contain `:` and `@`; inside `scheme://` URLs credentials may be percent-encoded. Without a type the
`scheme` stays None: the check (`modules.proxy_check`) finds out whether it is HTTP or SOCKS5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import unquote

from config import ProxyParts
from modules.geo_guard import normalize_country

_SCHEME_WORDS = {"http": "http", "https": "https", "socks5": "socks5", "socks5h": "socks5", "socks": "socks5"}
_HOST_RE = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)$")
_HOST_PORT_RE = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[^:@\s\[\]]+):(\d{1,5})$")
_COUNTRY_WORDS = 3  # "United States of America" is the longest name met in practice after splitting


class ProxyPasteError(ValueError):
    """A line that is not a proxy. The message is in Russian and never contains the line (it may hold a password)."""


@dataclass(frozen=True)
class ProxyDraft:
    """A recognised proxy, not saved yet. `scheme` None = type not given; `country` is ISO-2 or None."""

    scheme: str | None
    host: str
    port: int
    login: str | None = None
    password: str | None = field(default=None, repr=False)
    country: str | None = None

    @property
    def parts(self) -> ProxyParts:
        """What `Database.add_proxy` takes."""
        return ProxyParts(self.scheme, self.host, self.port, self.login, self.password)

    @property
    def key(self) -> tuple[str, int, str]:
        """Identity in the library: (host, port, login)."""
        return self.host.lower(), self.port, self.login or ""

    @property
    def address(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"


@dataclass(frozen=True)
class PasteLine:
    """One non-empty input line: `line_no` (1-based, counted over the whole text), the draft or the error.
    `duplicate_of`: the line number of an earlier line with the same host, port and login (saving merges them)."""

    line_no: int
    draft: ProxyDraft | None = None
    error: str | None = None
    duplicate_of: int | None = None

    @property
    def ok(self) -> bool:
        return self.draft is not None


def _host_port(host: str, port: str) -> tuple[str, int]:
    host = host.strip()
    if not _HOST_RE.match(host):
        raise ProxyPasteError("адрес прокси не похож на IP или домен")
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ProxyPasteError("порт должен быть числом от 1 до 65535")
    return host.strip("[]").lower(), int(port)


def _split_host_port(text: str) -> tuple[str, int] | None:
    match = _HOST_PORT_RE.match(text.strip())
    if match is None:
        return None
    return _host_port(match.group(1), match.group(2))


def _split_credentials(text: str) -> tuple[str | None, str | None]:
    """`login:pass` (the password may contain `:`); a lone word is a login without password."""
    login, sep, password = text.partition(":")
    if not login:
        raise ProxyPasteError("пустой логин")
    return login, (password if sep else None) or None


def _colon_form(text: str) -> tuple[str, int, str | None, str | None, str | None]:
    """`host:port[:login:pass][:country]`, `login:pass@host:port`, `host:port@login:pass`. Returns host, port,
    login, password and a country found as the last colon field."""
    if "@" in text:
        left, _, right = text.rpartition("@")
        hp = _split_host_port(right)
        if hp is not None:  # login:pass@host:port (the password may contain '@')
            return (*hp, *_split_credentials(left), None)
        left, _, right = text.partition("@")
        hp = _split_host_port(left)
        if hp is not None:  # host:port@login:pass
            return (*hp, *_split_credentials(right), None)
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ProxyPasteError("IPv6-адрес должен быть в квадратных скобках: [2001:db8::1]:8080")
        host, rest = text[: end + 1], text[end + 1:]
        if not rest.startswith(":"):
            raise ProxyPasteError("после адреса нужен порт: [2001:db8::1]:8080")
        pieces = [host, *rest[1:].split(":")]
    else:
        pieces = text.split(":")
    if len(pieces) < 2:
        raise ProxyPasteError("нужны адрес и порт (например 1.2.3.4:8080)")
    host, port = _host_port(pieces[0], pieces[1])
    rest = pieces[2:]
    if not rest:
        return host, port, None, None, None
    country = None
    if len(rest) >= 3 and (code := normalize_country(rest[-1])):  # host:port:login:pass:PL
        country, rest = code, rest[:-1]
    if len(rest) == 1:
        raise ProxyPasteError("после логина нужен пароль (адрес:порт:логин:пароль)")
    login, password = rest[0], ":".join(rest[1:])
    if not login:
        raise ProxyPasteError("пустой логин")
    return host, port, login, password or None, country


def _url_form(scheme: str, rest: str) -> tuple[str, int, str | None, str | None, str | None]:
    """After `scheme://`: `[login:pass@]host:port` (percent-encoding decoded) or the seller's `host:port:login:pass`."""
    rest = rest.rstrip("/")
    if "@" in rest:
        creds, _, address = rest.rpartition("@")
        hp = _split_host_port(address)
        if hp is not None:
            login, password = _split_credentials(creds)
            return (*hp, unquote(login) if login else None, unquote(password) if password else None, None)
    return _colon_form(rest)


def _take_country(tokens: list[str]) -> tuple[list[str], str | None]:
    """A trailing country written as separate words (`PL`, `Poland`, `United States`)."""
    for size in range(min(_COUNTRY_WORDS, len(tokens) - 1), 0, -1):
        code = normalize_country(" ".join(tokens[-size:]))
        if code:
            return tokens[:-size], code
    return tokens, None


def parse_proxy_line(line: str) -> ProxyDraft:
    """One proxy from one line; ProxyPasteError (a ValueError, Russian message without the line) otherwise."""
    tokens = line.split()
    if not tokens:
        raise ProxyPasteError("пустая строка")
    scheme: str | None = None
    if tokens[0].lower() in _SCHEME_WORDS and len(tokens) > 1:
        scheme, tokens = _SCHEME_WORDS[tokens[0].lower()], tokens[1:]
    tokens, country = _take_country(tokens)
    if len(tokens) > 1 and tokens[-1].lower() in _SCHEME_WORDS and scheme is None:
        scheme, tokens = _SCHEME_WORDS[tokens[-1].lower()], tokens[:-1]
        if country is None:
            tokens, country = _take_country(tokens)

    if len(tokens) == 1:
        text = tokens[0]
        match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://(.*)$", text)
        if match:
            word = match.group(1).lower()
            if word not in _SCHEME_WORDS:
                raise ProxyPasteError(f"тип прокси «{match.group(1)}» не поддерживается (http, https, socks5)")
            scheme = _SCHEME_WORDS[word]
            host, port, login, password, line_country = _url_form(scheme, match.group(2))
        else:
            host, port, login, password, line_country = _colon_form(text)
        country = country or line_country
    elif len(tokens) == 2:  # host:port login:pass
        hp = _split_host_port(tokens[0])
        if hp is None:
            raise ProxyPasteError("не удалось разобрать строку: ожидается адрес:порт и логин:пароль")
        (host, port), (login, password) = hp, _split_credentials(tokens[1])
    elif len(tokens) == 3:  # host:port login pass  /  host port login:pass
        hp = _split_host_port(tokens[0])
        if hp is not None:
            (host, port), login, password = hp, tokens[1], tokens[2]
        else:
            host, port = _host_port(tokens[0], tokens[1])
            login, password = _split_credentials(tokens[2])
    elif len(tokens) == 4:  # host port login pass
        host, port = _host_port(tokens[0], tokens[1])
        login, password = tokens[2], tokens[3]
    else:
        raise ProxyPasteError("слишком много полей: ожидается адрес, порт, логин, пароль и страна")
    return ProxyDraft(scheme, host, port, login or None, password or None, country)


def parse_proxy_paste(text: str) -> list[PasteLine]:
    """Every non-empty line of `text` (lines starting with `#` are comments) -> PasteLine, in order."""
    results: list[PasteLine] = []
    seen: dict[tuple[str, int, str], int] = {}
    for number, raw in enumerate((text or "").splitlines(), 1):
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        try:
            draft = parse_proxy_line(line)
        except ProxyPasteError as exc:
            results.append(PasteLine(number, error=f"Строка {number}: {exc}"))
            continue
        results.append(PasteLine(number, draft, duplicate_of=seen.get(draft.key)))
        seen.setdefault(draft.key, number)
    return results


__all__ = ["PasteLine", "ProxyDraft", "ProxyPasteError", "parse_proxy_line", "parse_proxy_paste"]
