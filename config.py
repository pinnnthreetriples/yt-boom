from __future__ import annotations

import os
import re
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, unquote, urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(RuntimeError):
    """Raised when config.yaml cannot be read, expanded or validated."""


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "ui-e2e-framework"
    db_path: Path = Path("test_metrics.db")
    log_level: str = "INFO"
    log_file: Path = Path("logs/framework.log")
    poll_interval_seconds: float = Field(default=5.0, gt=0)
    acquire_timeout_seconds: float = Field(default=120.0, gt=0)
    shutdown_timeout_seconds: float = Field(default=30.0, gt=0)
    drain_timeout_seconds: float = Field(default=1200.0, gt=0)

    @field_validator("log_level")
    @classmethod
    def _normalize_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unsupported log level: {value!r}")
        return level


class DeepSeekSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    temperature: float = Field(default=1.1, ge=0, le=2)
    max_tokens: int = Field(default=256, ge=16, le=8192)
    timeout_seconds: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=3, ge=1, le=10)

    @field_validator("api_key")
    @classmethod
    def _require_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("deepseek.api_key is empty; export DEEPSEEK_API_KEY before running")
        return value


class CollectorEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    url: str
    items_path: str = ""
    id_field: str = "id"
    text_field: str = "text"
    url_field: str | None = None

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"endpoint url must be absolute http(s): {value!r}")
        return value


class CollectorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    impersonate: str = "chrome"
    timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=3, ge=1, le=10)
    retry_backoff_seconds: float = Field(default=2.0, ge=0)
    max_items_per_endpoint: int = Field(default=10, ge=1, le=500)
    languages: list[str] = Field(default_factory=lambda: ["en", "ru"], min_length=1)
    minimum_confidence: float = Field(default=0.75, ge=0, le=1)
    min_text_length: int = Field(default=20, ge=1)
    max_text_length: int = Field(default=600, ge=1)
    require_any_keywords: list[str] = Field(default_factory=list)
    block_keywords: list[str] = Field(default_factory=list)
    endpoints: list[CollectorEndpoint] = Field(min_length=1)
    search_query: str | None = Field(default=None)
    min_views: int = Field(default=0, ge=0)
    max_views: int | None = Field(default=None)
    max_age_hours: int | None = Field(default=None, ge=1)  # auto-search: only videos published within N hours
    max_videos: int = Field(default=5, ge=1, le=50)  # auto-search: videos taken per run
    comments_per_video: int = Field(default=2, ge=1, le=100)
    target_mode: str = Field(default="first_reply")

    @field_validator("languages")
    @classmethod
    def _normalize_languages(cls, values: list[str]) -> list[str]:
        normalized = sorted({value.strip().lower() for value in values if value.strip()})
        if not normalized:
            raise ValueError("collector.languages must contain at least one ISO 639-1 code")
        return normalized

    @model_validator(mode="after")
    def _validate_lengths(self) -> "CollectorSettings":
        if self.min_text_length >= self.max_text_length:
            raise ValueError("collector.min_text_length must be less than max_text_length")
        names = [endpoint.name for endpoint in self.endpoints]
        if len(names) != len(set(names)):
            raise ValueError("collector.endpoints names must be unique")
        return self


class ProcessorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_prompt: str = Field(min_length=1)
    user_prompt_template: str = Field(min_length=1)
    drop_trailing_period_probability: float = Field(default=0.8, ge=0, le=1)
    lowercase_initial_probability: float = Field(default=0.2, ge=0, le=1)
    max_output_length: int = Field(default=400, ge=1)
    max_sentences: int = Field(default=2, ge=1)
    styles: list[str] = Field(
        default_factory=lambda: [
            "casual remark that picks up one specific detail of the comment",
            "thoughtful and insightful follow-up question",
            "humorous, witty remark or light banter",
            "concise acknowledgement or brief nod",
            "relatable personal reaction or perspective",
        ]
    )
    reply_mode: Literal["ai", "custom"] = "ai"
    custom_replies: list[str] = Field(default_factory=list)
    paraphrase_custom: bool = True  # reword custom templates through DeepSeek (needs an API key)

    @field_validator("user_prompt_template")
    @classmethod
    def _require_placeholders(cls, value: str) -> str:
        try:
            fields = {name.split(".")[0].split("[")[0] for _, name, _, _ in string.Formatter().parse(value) if name}
        except ValueError as exc:
            raise ValueError(f"invalid user_prompt_template: {exc}") from exc
        unknown = fields - {"text", "language", "parent_text", "parent_author", "author", "style"}
        if "text" not in fields:
            raise ValueError("processor.user_prompt_template must contain {text}")
        if unknown:
            raise ValueError(f"processor.user_prompt_template has unsupported placeholders: {sorted(unknown)}")
        return value


class TargetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
    base_url: str
    item_url_template: str | None = None
    search_input_selector: str | None = None
    search_button_selector: str | None = None
    result_item_selector: str | None = None
    submit_form_selector: str | None = None
    submit_input_selector: str
    submit_button_selector: str
    confirmation_selector: str | None = None
    remote_id_selector: str | None = None
    remote_id_url_regex: str | None = None
    wait_timeout_ms: int = Field(default=30000, ge=1000)
    comment_thread_selector: str | None = None
    reply_expander_selector: str | None = None
    sub_comment_selector: str | None = None
    reply_button_selector: str | None = None

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"browser target base_url must be absolute http(s): {value!r}")
        return value

    @field_validator("item_url_template")
    @classmethod
    def _validate_item_template(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "{external_id}" not in value and "{payload_id}" not in value:
            raise ValueError("item_url_template must contain {external_id} or {payload_id}")
        return value

    @field_validator("remote_id_url_regex")
    @classmethod
    def _validate_regex(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            compiled = re.compile(value)
        except re.error as exc:
            raise ValueError(f"invalid remote_id_url_regex: {exc}") from exc
        if compiled.groups < 1:
            raise ValueError("remote_id_url_regex must contain a capture group")
        return value

    @model_validator(mode="after")
    def _validate_search_pair(self) -> "TargetSettings":
        if self.search_input_selector is not None and self.search_button_selector is None:
            raise ValueError("search_input_selector requires search_button_selector")
        if self.search_button_selector is not None and self.search_input_selector is None:
            raise ValueError("search_button_selector requires search_input_selector")
        if not any(
            (self.confirmation_selector, self.remote_id_selector, self.remote_id_url_regex)
        ):
            raise ValueError(
                "target needs a success signal: confirmation_selector, remote_id_selector "
                "or remote_id_url_regex"
            )
        return self


_PROXY_HOST = r"(?:\[[0-9A-Fa-f:.]+\]|[^:/@\s\[\]]+)"  # host name, IPv4 or an IPv6 address in brackets
_RAW_PROXY_RE = re.compile(rf"^({_PROXY_HOST}):(\d{{1,5}}):([^:@\s]+):(\S+)$")  # host:port:user:pass
_AUTH_PROXY_RE = re.compile(rf"^([^:/@\s]+):([^/\s]*)@({_PROXY_HOST}):(\d{{1,5}})$")  # user:pass@host:port
_BARE_PROXY_RE = re.compile(rf"^({_PROXY_HOST}):(\d{{1,5}})$")  # host:port
_PROXY_ERROR = "proxy must include host and port (host:port, host:port:user:pass, user:pass@host:port or scheme://...)"


def _proxy_dict(scheme: str, host: str, port: str | int, user: str | None = None, password: str | None = None) -> dict[str, str]:
    number = int(port)
    if not 0 < number < 65536:
        raise ValueError("proxy port must be between 1 and 65535")
    proxy: dict[str, str] = {"server": f"{scheme}://{host}:{number}"}
    if user:
        proxy["username"] = user
    if password:
        proxy["password"] = password
    return proxy


def parse_proxy(proxy_url: str | None) -> dict[str, str] | None:
    """Playwright proxy dict from `scheme://[user:pass@]host:port`, `host:port`, `user:pass@host:port` or `host:port:user:pass`.

    Credentials of the non-URL forms are taken literally (no percent-decoding). IPv6 hosts go in brackets.
    The error message never contains the proxy string: it may hold a password.
    """
    if not proxy_url:
        return None
    text = proxy_url.strip()
    raw = _RAW_PROXY_RE.match(text)
    if raw:
        host, port, user, password = raw.groups()
        return _proxy_dict("http", host, port, user, password)
    auth = _AUTH_PROXY_RE.match(text)
    if auth:
        user, password, host, port = auth.groups()
        return _proxy_dict("http", host, port, user, password)
    bare = _BARE_PROXY_RE.match(text)
    if bare:
        return _proxy_dict("http", bare.group(1), bare.group(2))
    try:
        parsed = urlparse(text)
        hostname, port_number = parsed.hostname, parsed.port
    except ValueError:
        raise ValueError(_PROXY_ERROR) from None
    if not hostname or not port_number:
        raise ValueError(_PROXY_ERROR)
    host = f"[{hostname}]" if ":" in hostname else hostname
    return _proxy_dict(
        parsed.scheme or "http",
        host,
        port_number,
        unquote(parsed.username) if parsed.username else None,
        unquote(parsed.password) if parsed.password else None,
    )


def mask_proxy_url(proxy_url: str | None) -> str:
    """Proxy for logs and tables: `scheme://user:***@host:port`; never contains the password."""
    text = (proxy_url or "").strip()
    if not text:
        return ""
    try:
        parsed = parse_proxy(text)
    except ValueError:
        return "***" if "@" in text or text.count(":") >= 3 else text
    if parsed is None:
        return ""
    user = parsed.get("username")
    if not user:
        return parsed["server"]
    scheme, _, host_port = parsed["server"].partition("://")
    return f"{scheme}://{user}{':***' if parsed.get('password') else ''}@{host_port}"


# Written to `accounts.proxy_url` for every account whose proxy lives in the `proxies` table. Older versions read
# that column as the proxy itself: `parse_proxy` rejects this value, so an old build fails instead of going direct.
PROXY_LIBRARY_MARKER = "library:see-proxies-table"


@dataclass(frozen=True)
class ProxyParts:
    """One proxy split into the fields the `proxies` table stores. `scheme` None: not known yet (a check finds it)."""

    scheme: str | None
    host: str
    port: int
    login: str | None = None
    password: str | None = None

    def __repr__(self) -> str:  # the password never reaches a log line
        return f"ProxyParts(scheme={self.scheme!r}, host={self.host!r}, port={self.port}, login={self.login!r})"


def compose_proxy_url(scheme: str | None, host: str, port: int, login: str | None = None, password: str | None = None) -> str:
    """The one place a proxy address is assembled: `scheme://login:password@host:port`, credentials percent-encoded
    (so `@ : / %` in a password survive `parse_proxy` and libcurl), IPv6 hosts in brackets, no scheme -> http."""
    host = host.strip().strip("[]")
    address = f"[{host}]" if ":" in host else host
    credentials = ""
    if login:
        credentials = quote(login, safe="") + (f":{quote(password, safe='')}" if password else "") + "@"
    return f"{scheme or 'http'}://{credentials}{address}:{int(port)}"


def split_proxy(proxy_url: str) -> ProxyParts:
    """`ProxyParts` from any form `parse_proxy` accepts; `scheme` is None when the text names none. ValueError otherwise."""
    text = (proxy_url or "").strip()
    if not text or text == PROXY_LIBRARY_MARKER:
        raise ValueError(_PROXY_ERROR)
    parsed = parse_proxy(text)
    assert parsed is not None
    scheme, _, address = parsed["server"].partition("://")
    host, _, port = address.rpartition(":")
    if scheme == "socks5h":  # Playwright knows only socks5; DNS on the proxy is curl_proxy_url's job
        scheme = "socks5"
    return ProxyParts(
        scheme.lower() if "://" in text else None,
        host.strip("[]").lower(),
        int(port),
        parsed.get("username") or None,
        parsed.get("password") or None,
    )


class ProxyUnavailableError(RuntimeError):
    """An account (or the search) has a proxy assigned that cannot be read: it must not run at all, never go direct."""


def resolve_account_proxy(account: Any) -> str | None:
    """The proxy one account uses everywhere (sign-in, posting, warm-up, heartbeat): its own one, or None = direct.

    There is no global fallback. An assigned proxy that cannot be read (`AccountRecord.proxy_unavailable`: DPAPI
    could not decrypt it, the record was deleted or damaged) raises ProxyUnavailableError instead of going direct.
    """
    name = getattr(account, "name", "?")
    own = (getattr(account, "proxy_url", None) or "").strip()
    if getattr(account, "proxy_unavailable", False) or own == PROXY_LIBRARY_MARKER:
        raise ProxyUnavailableError(
            f"Аккаунт «{name}»: назначенный ему прокси не читается (запись удалена или повреждена, либо база "
            "перенесена с другого компьютера/пользователя Windows и пароль не расшифровывается). Аккаунт не запущен, "
            "чтобы не выйти в сеть с вашего IP: назначьте прокси заново на этапе «Прокси» мастера"
        )
    return own or None


class AccountSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    session_path: str = Field(default="test_sessions/youtube")
    proxy_url: str | None = None
    is_active: bool = True
    email: str | None = None
    password: str | None = Field(default=None, repr=False, exclude=True)
    totp_secret: str | None = Field(default=None, repr=False, exclude=True)
    recovery_email: str | None = None

    def playwright_proxy(self) -> dict[str, str] | None:
        return parse_proxy(self.proxy_url)


class BrowserSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headless: bool = False
    os: Literal["windows", "macos", "linux"] = "windows"
    geoip: bool = True
    humanize: bool = False
    locale: str = "en-US"
    proxy: str | None = None
    session_root: Path = Path("test_sessions")
    accounts: list[AccountSettings] = Field(default_factory=list)
    navigation_timeout_ms: int = Field(default=45000, ge=5000)
    render_min_seconds: float = Field(default=45.0, gt=0)
    render_max_seconds: float = Field(default=90.0, gt=0)
    render_play_video: bool = True  # click a paused watch-page video (muted) so it really plays during the render wait
    firefox_user_prefs: dict[str, Any] = Field(
        default_factory=lambda: {
            "media.peerconnection.enabled": True,
            "network.http.http3.enable": False,
        }
    )
    targets: list[TargetSettings] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_browser(self) -> "BrowserSettings":
        if self.render_min_seconds > self.render_max_seconds:
            raise ValueError("browser.render_min_seconds must not exceed render_max_seconds")
        names = [target.name for target in self.targets]
        if len(names) != len(set(names)):
            raise ValueError("browser.targets names must be unique")
        _ = self.playwright_proxy()
        if not self.accounts:
            target_name = self.targets[0].name if self.targets else "youtube"
            default_path = (self.session_root / target_name).as_posix()
            self.accounts = [
                AccountSettings(
                    name="default",
                    session_path=default_path,
                    proxy_url=self.proxy,
                    is_active=True,
                )
            ]
        for acc in self.accounts:
            if acc.proxy_url:
                _ = parse_proxy(acc.proxy_url)
        return self

    def playwright_proxy(self) -> dict[str, str] | None:
        return parse_proxy(self.proxy)


class LimitsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_iterations: int = Field(default=6, ge=1, le=1000)
    max_concurrent_sessions: int = Field(default=2, ge=1, le=32)
    # How several accounts share the video pool: "each" - every account passes the whole list,
    # "share" - the videos are split between the accounts.
    account_mode: Literal["each", "share"] = "each"
    max_iterations_per_session: int = Field(default=2, ge=1, le=100)
    session_cooldown_seconds: float = Field(default=120.0, ge=0)
    video_cooldown_seconds: float = Field(default=120.0, ge=0)
    pause_min_seconds: float = Field(default=8.0, ge=0)
    pause_max_seconds: float = Field(default=20.0, ge=0)
    # Wait this long after a posted reply, then look for it as an anonymous visitor would (0 turns the check off).
    visibility_check_delay_seconds: float = Field(default=45.0, ge=0)
    # The switch of the «Запуск» tab; the check runs only when it is on AND the delay above is above 0.
    visibility_check_enabled: bool = True
    retry_backoff_seconds: float = Field(default=45.0, ge=1)
    max_queue_attempts: int = Field(default=3, ge=1, le=10)

    @model_validator(mode="after")
    def _validate_pauses(self) -> "LimitsSettings":
        if self.pause_min_seconds > self.pause_max_seconds:
            raise ValueError("limits.pause_min_seconds must not exceed pause_max_seconds")
        return self


class ValidatorSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    delay_seconds: float = Field(default=300.0, ge=0)
    timeout_seconds: float = Field(default=30.0, gt=0)
    impersonate: str = "chrome"
    poll_interval_seconds: float = Field(default=15.0, ge=1)
    max_attempts: int = Field(default=3, ge=1, le=10)
    endpoint_template: str
    success_marker: str | None = None

    @field_validator("endpoint_template")
    @classmethod
    def _validate_template(cls, value: str) -> str:
        parsed = urlparse(value.replace("{remote_id}", "1").replace("{payload_id}", "1"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("validator.endpoint_template must be an absolute http(s) url")
        if "{remote_id}" not in value and "{payload_id}" not in value:
            raise ValueError("validator.endpoint_template must contain {remote_id} or {payload_id}")
        return value


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: AppSettings
    deepseek: DeepSeekSettings
    collector: CollectorSettings
    processor: ProcessorSettings
    browser: BrowserSettings
    limits: LimitsSettings
    validator: ValidatorSettings
    accounts: list[AccountSettings] = Field(default_factory=list)
    reply_mode: Literal["ai", "custom"] | None = None
    custom_replies: list[str] | None = None

    @model_validator(mode="after")
    def _sync_reply_and_account_settings(self) -> "AppConfig":
        if self.processor.reply_mode == "custom" or self.reply_mode == "custom":
            self.processor.reply_mode = "custom"
            self.reply_mode = "custom"
        else:
            self.processor.reply_mode = "ai"
            self.reply_mode = "ai"

        if self.processor.custom_replies:
            self.custom_replies = self.processor.custom_replies
        elif self.custom_replies:
            self.processor.custom_replies = self.custom_replies
        else:
            self.processor.custom_replies = []
            self.custom_replies = []

        if self.accounts:
            self.browser.accounts = list(self.accounts)
        elif self.browser.accounts:
            self.accounts = list(self.browser.accounts)
        else:
            default_acc = AccountSettings(
                name="default",
                session_path=(self.browser.session_root / "youtube").as_posix(),
                proxy_url=self.browser.proxy,
                is_active=True,
            )
            self.accounts = [default_acc]
            self.browser.accounts = [default_acc]
        return self


def _expand_env(value: Any, location: str, missing: list[str]) -> Any:
    if isinstance(value, dict):
        return {key: _expand_env(item, f"{location}.{key}", missing) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item, f"{location}[{index}]", missing) for index, item in enumerate(value)]
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            resolved = os.environ.get(name)
            if resolved is not None:
                return resolved
            if default is not None:
                return default
            missing.append(f"{location} (${name})")
            return match.group(0)

        return _ENV_PATTERN.sub(replace, value)
    return value


def _resolve_path(base: Path, value: Path) -> Path:
    return value if value.is_absolute() else (base / value).resolve()


def repair_text(value: Any) -> Any:
    """Join UTF-16 surrogate halves into real characters (strings, lists and mappings, recursively).

    The Windows console delivers an emoji as two surrogate halves, and YAML keeps them as "\\uD83D\\uDE33":
    such a string cannot be stored in SQLite or typed into a browser. A half without its pair is dropped.
    """
    if isinstance(value, str):
        if not any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
            return value
        return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "ignore")
    if isinstance(value, list):
        return [repair_text(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_text(item) for key, item in value.items()}
    return value


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"failed to read {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")
    raw = repair_text(raw)

    missing: list[str] = []
    expanded = _expand_env(raw, "$", missing)
    if missing:
        raise ConfigError("missing environment variables: " + ", ".join(sorted(missing)))

    try:
        config = AppConfig.model_validate(expanded)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors()
        )
        raise ConfigError(f"invalid configuration: {details}") from exc

    base_dir = config_path.resolve().parent
    config.app.db_path = _resolve_path(base_dir, config.app.db_path)
    config.app.log_file = _resolve_path(base_dir, config.app.log_file)
    config.browser.session_root = _resolve_path(base_dir, config.browser.session_root)
    for acc in config.accounts:
        acc_path = Path(acc.session_path)
        if not acc_path.is_absolute():
            acc.session_path = str(_resolve_path(base_dir, acc_path))
    for acc in config.browser.accounts:
        acc_path = Path(acc.session_path)
        if not acc_path.is_absolute():
            acc.session_path = str(_resolve_path(base_dir, acc_path))
    return config


__all__ = [
    "AccountSettings",
    "AppConfig",
    "BrowserSettings",
    "CollectorEndpoint",
    "CollectorSettings",
    "ConfigError",
    "DeepSeekSettings",
    "LimitsSettings",
    "PROXY_LIBRARY_MARKER",
    "ProxyParts",
    "ProxyUnavailableError",
    "ProcessorSettings",
    "TargetSettings",
    "ValidatorSettings",
    "compose_proxy_url",
    "load_config",
    "mask_proxy_url",
    "parse_proxy",
    "resolve_account_proxy",
    "split_proxy",
]
