from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlparse

# What a developer needs to write a handler for an unknown screen: the tag structure, ids, names, types, roles,
# data-* attribute names (values only when they are short numbers, e.g. data-challengetype="6") and the texts of
# buttons, links, headings and labels. Nothing that identifies the account or carries a credential: no values, no query
# strings, no emails, no token-like strings, no scripts.
_DROPPED = frozenset({"script", "style", "noscript", "template", "svg", "link", "meta", "head", "iframe", "object"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_TEXT_HOSTS = frozenset({"button", "a", "h1", "h2", "h3", "label", "legend"})
_KEPT_ATTRS = frozenset({"id", "name", "type", "role", "jsname"})
_URL_ATTRS = frozenset({"href", "action"})

_EMAIL = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_TOKEN = re.compile(r"[A-Za-z0-9_\-+=]{20,}")
_DATA_VALUE = re.compile(r"\d{1,4}")


def _token_or_word(match: re.Match[str]) -> str:
    """A long run is a token when it holds a digit or mixed case; `knowledge-preregistered-email-response` is an id."""
    run = match.group(0)
    mixed = any(c.isupper() for c in run) and any(c.islower() for c in run)
    return "<token>" if mixed or any(c.isdigit() for c in run) else run


def redact(text: str) -> str:
    """Emails and token-like runs replaced by placeholders."""
    return _TOKEN.sub(_token_or_word, _EMAIL.sub("<email>", text))


def _url_without_query(value: str) -> str:
    parsed = urlparse(value)
    return redact(f"{parsed.hostname or ''}{parsed.path}")


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0
        self._text_depth = 0
        self._stack: list[tuple[str, bool, bool]] = []  # (tag, was_dropped, was_text_host)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        dropped = tag in _DROPPED
        text_host = tag in _TEXT_HOSTS or any(k == "role" and v in {"button", "link", "alert"} for k, v in attrs)
        if tag not in _VOID:
            self._stack.append((tag, dropped, text_host))
            self._skip += dropped
            self._text_depth += text_host
        if dropped or self._skip:
            return
        kept: list[str] = []
        for key, raw in attrs:
            value = raw or ""
            if key in _KEPT_ATTRS:
                kept.append(f'{key}="{redact(value)}"')
            elif key.startswith("data-"):
                kept.append(f'{key}="{value if _DATA_VALUE.fullmatch(value) else "…"}"')
            elif key in _URL_ATTRS and value:
                kept.append(f'{key}="{_url_without_query(value)}"')
        self.out.append(f"<{tag}{''.join(' ' + a for a in kept)}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID or not any(open_tag == tag for open_tag, _, _ in self._stack):  # a stray end tag closes nothing
            return
        while self._stack:
            top, dropped, text_host = self._stack.pop()
            self._skip -= dropped
            self._text_depth -= text_host
            if not (dropped or self._skip):
                self.out.append(f"</{top}>")
            if top == tag:
                return

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text and not self._skip and self._text_depth:
            self.out.append(redact(text)[:120])

    def handle_comment(self, data: str) -> None:  # comments are dropped
        return


def sanitize_html(html: str) -> str:
    """Tag skeleton of a page, safe to keep in a log directory; see the module comment for what survives."""
    parser = _Sanitizer()
    parser.feed(html)
    parser.close()
    return "\n".join(parser.out)


__all__ = ["redact", "sanitize_html"]
