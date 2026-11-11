"""Self-audit of the Chrome anti-detect set-up.

Part A is static: it reads the launch configuration (`chrome_launcher`) and the source of the runner, always runs and
needs no browser. Part B starts the real, installed Google Chrome through patchright (headless) and asks the page what a
site would see. Part B is skipped when patchright or Chrome is missing, or when `YT_SKIP_REAL_CHROME=1` is set.

Part B never reaches the internet on purpose: every page is served by a local `http.server` on 127.0.0.1, the exit-IP
lookups are patched, and Chrome itself is pointed at a local proxy that forwards loopback only and answers 502 to every
other host (Chrome's own background chatter included). The only deliberate outside address is the STUN URL of the
WebRTC test; while the UDP lock holds, no packet is ever sent to it.

`YT_AUDIT_VERBOSE=1` prints what the real Chrome reported (candidates, languages, getter source, ...) after the run.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from config import BrowserSettings, TargetSettings
from modules import chrome_launcher
from modules.chrome_launcher import (
    COUNTRY_TO_TIMEZONE,
    WINDOW_HEIGHT_RANGE,
    WINDOW_WIDTH_RANGE,
    accept_languages_for,
    get_chrome_args,
    prepare_chrome_profile,
)
from modules.geo_guard import ExitGeo
from modules.youtube_runner import YouTubeRunner

ROOT = Path(__file__).resolve().parent.parent
PROFILE_PREFIX = "yt_stealth_audit_"  # every Part B profile lives under a folder with this prefix (process check)
FINDINGS: dict[str, object] = {}  # what the real Chrome said; printed with YT_AUDIT_VERBOSE=1


# ===================================================================== Part A: static configuration


class TestChromeArgsConfiguration(unittest.TestCase):
    """`get_chrome_args()`: the command line that is passed on top of what patchright already sets."""

    def setUp(self):
        # The window size is clamped to the work area of the screen: pin it so that the test is machine-independent
        patcher = patch.object(chrome_launcher, "get_screen_work_area", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_exactly_one_disable_features_switch_and_webauthn_is_not_in_it(self):
        # Chrome keeps only the LAST `--disable-features=`; WebAuthentication must stay on (passkey / security-key
        # screens of Google sign-in and `PublicKeyCredential` in the page depend on it).
        for locale, name, proxy in (("en-US", None, None), ("de-DE", "alice", "http://127.0.0.1:8080"),
                                    ("ru-RU", "bob", "socks5://10.0.0.1:1080")):
            with self.subTest(locale=locale, name=name):
                args = get_chrome_args(locale, name, proxy)
                switches = [arg for arg in args if arg.startswith("--disable-features")]
                self.assertEqual(len(switches), 1, switches)
                self.assertTrue(switches[0].startswith("--disable-features="))
                features = switches[0].partition("=")[2].split(",")
                self.assertFalse([f for f in features if "webauthn" in f.lower() or "webauthentication" in f.lower()])
                self.assertEqual(len(features), len(set(features)), "a feature listed twice")
        for arg in args:  # the whole command line, not only the switch above
            self.assertNotIn("WebAuthentication", arg)

    def test_required_switches_are_present(self):
        args = get_chrome_args("de-DE", "alice")
        for required in (
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--disable-quic",
            "--hide-crash-restore-bubble",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
        ):
            self.assertIn(required, args)

    def test_lang_follows_the_locale(self):
        for locale in ("en-US", "de-DE", "ru-RU", "pt-BR"):
            self.assertIn(f"--lang={locale}", get_chrome_args(locale, "alice"))
            self.assertEqual([a for a in get_chrome_args(locale) if a.startswith("--lang=")], [f"--lang={locale}"])

    def test_window_size_is_stable_per_account_and_differs_between_accounts(self):
        def size(name):
            sizes = [a for a in get_chrome_args("en-US", name) if a.startswith("--window-size=")]
            self.assertEqual(len(sizes), 1)
            width, height = map(int, sizes[0].partition("=")[2].split(","))
            return width, height

        names = ["alice", "bob", "carol", "dave", "erin", "frank"]
        sizes = {name: size(name) for name in names}
        for name in names:
            self.assertEqual(size(name), sizes[name], "the same account must always get the same window")
            width, height = sizes[name]
            self.assertTrue(WINDOW_WIDTH_RANGE[0] <= width <= WINDOW_WIDTH_RANGE[1], (name, width))
            self.assertTrue(WINDOW_HEIGHT_RANGE[0] <= height <= WINDOW_HEIGHT_RANGE[1], (name, height))
        self.assertGreater(len(set(sizes.values())), 1, f"all accounts share one window size: {sizes}")
        self.assertEqual(size(None), chrome_launcher.DEFAULT_WINDOW_SIZE)

    def test_host_resolver_rules_only_for_socks5_and_never_hide_the_proxy_host(self):
        socks = get_chrome_args("en-US", "alice", "socks5://203.0.113.9:1080")
        rules = [a for a in socks if a.startswith("--host-resolver-rules=")]
        self.assertEqual(len(rules), 1, socks)
        rule = rules[0].partition("=")[2]
        self.assertIn("MAP * ~NOTFOUND", rule)  # every local DNS lookup is refused ...
        self.assertIn("EXCLUDE 203.0.113.9", rule)  # ... except the proxy itself,
        self.assertIn("EXCLUDE 127.0.0.1", rule)  # and loopback
        named = [a for a in get_chrome_args("en-US", "a", "socks5://proxy.example.net:1080") if "host-resolver" in a]
        self.assertEqual(len(named), 1)
        self.assertIn("EXCLUDE proxy.example.net", named[0])
        for proxy in ("http://203.0.113.9:8080", "203.0.113.9:8080", "http://u:p@203.0.113.9:8080", None):
            with self.subTest(proxy=proxy):
                self.assertFalse([a for a in get_chrome_args("en-US", "alice", proxy) if "host-resolver" in a])


class TestChromeProfilePreferences(unittest.TestCase):
    """`prepare_chrome_profile()`: the profile's `Default/Preferences` is the second lock next to the command line."""

    def _prefs(self, locale: str) -> dict:
        with tempfile.TemporaryDirectory(prefix=PROFILE_PREFIX) as tmp:
            prepare_chrome_profile(Path(tmp), locale)
            return json.loads((Path(tmp) / "Default" / "Preferences").read_text(encoding="utf-8"))

    def test_webrtc_dns_and_languages_are_written(self):
        for locale, languages in (("de-DE", "de-DE,de,en-US,en"), ("ru-RU", "ru-RU,ru,en-US,en"), ("en-GB", "en-GB,en"),
                                  ("en-US", "en-US,en")):
            with self.subTest(locale=locale):
                prefs = self._prefs(locale)
                self.assertEqual(prefs["webrtc"]["ip_handling_policy"], "disable_non_proxied_udp")
                self.assertFalse(prefs["webrtc"]["nonproxied_udp_enabled"])
                self.assertEqual(prefs["intl"]["accept_languages"], languages)
                self.assertEqual(prefs["intl"]["accept_languages"], accept_languages_for(locale))
                self.assertEqual(prefs["net"]["network_prediction_options"], 2)

    def test_the_locale_of_the_host_system_is_never_consulted(self):
        # `ru-RU` host, `de-DE` profile: the list is a pure function of the locale argument
        with patch.dict(os.environ, {"LANG": "ru_RU.UTF-8", "LC_ALL": "ru_RU.UTF-8"}):
            self.assertEqual(self._prefs("de-DE")["intl"]["accept_languages"], "de-DE,de,en-US,en")

    def test_existing_unrelated_keys_survive_and_owned_keys_are_overwritten(self):
        with tempfile.TemporaryDirectory(prefix=PROFILE_PREFIX) as tmp:
            default = Path(tmp) / "Default"
            default.mkdir()
            (default / "Preferences").write_text(
                json.dumps({"keep": {"me": 1}, "webrtc": {"ip_handling_policy": "default"}, "net": {"x": 1}}), encoding="utf-8"
            )
            prepare_chrome_profile(Path(tmp), "de-DE")
            prefs = json.loads((default / "Preferences").read_text(encoding="utf-8"))
        self.assertEqual(prefs["keep"], {"me": 1})
        self.assertEqual(prefs["net"]["x"], 1)
        self.assertEqual(prefs["webrtc"]["ip_handling_policy"], "disable_non_proxied_udp")


class TestRunnerSourceStaysUntouched(unittest.TestCase):
    """AST of `YouTubeRunner._start_chrome`: no script injection, no interception, no identity overrides.

    The whole-code-base scan lives in tests/test_chrome_webauthn_tabs.py (`TestNoInjectionOrInterception`); this is
    the minimal pin on the one function that launches Chrome, so that the audit fails even if that file is edited.
    """

    FORBIDDEN_CALLS = frozenset({"add_init_script", "route", "route_from_har", "set_extra_http_headers", "expose_function",
                                 "expose_binding", "add_script_tag", "set_viewport_size", "emulate_media"})
    FORBIDDEN_KWARGS = frozenset({"user_agent", "extra_http_headers", "viewport", "screen", "device_scale_factor", "locale",
                                  "is_mobile", "has_touch", "permissions", "color_scheme", "ignore_default_args",
                                  "executable_path"})

    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT / "modules" / "youtube_runner.py").read_text(encoding="utf-8"))
        cls.start_chrome = next(
            node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_start_chrome"
        )

    def test_no_injection_or_interception_calls(self):
        calls = {n.func.attr for n in ast.walk(self.start_chrome) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        self.assertFalse(calls & self.FORBIDDEN_CALLS, calls & self.FORBIDDEN_CALLS)
        self.assertNotIn("launch_persistent_context", {n.id for n in ast.walk(self.start_chrome) if isinstance(n, ast.Name)})

    def test_launch_options_carry_no_identity_overrides(self):
        keys: set[str] = set()
        for node in ast.walk(self.start_chrome):
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "launch_options" \
                    and isinstance(node.value, ast.Dict):
                keys |= {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
            if isinstance(node, ast.Assign):  # launch_options["geolocation"] = ...
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and getattr(target.value, "id", None) == "launch_options" \
                            and isinstance(target.slice, ast.Constant):
                        keys.add(target.slice.value)
        self.assertTrue({"user_data_dir", "channel", "headless", "args", "proxy", "timezone_id", "no_viewport"} <= keys, keys)
        self.assertFalse(keys & self.FORBIDDEN_KWARGS, keys & self.FORBIDDEN_KWARGS)
        launches = [n for n in ast.walk(self.start_chrome)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "launch_persistent_context"]
        self.assertEqual(len(launches), 1)
        self.assertEqual([k.arg for k in launches[0].keywords], [None], "only `**launch_options`, no inline keywords")


# ===================================================================== Part B: the real Chrome


def host_ip_addresses() -> set[str]:
    """Addresses of this machine's interfaces, found without sending a packet.

    `getaddrinfo(hostname)` lists the configured ones; a UDP socket that is only `connect()`ed to a TEST-NET address
    (RFC 5737 / 3849) makes the OS pick the outgoing interface, and `getsockname()` tells which. Nothing is sent.
    """
    found: set[str] = set()
    with contextlib.suppress(OSError):
        for info in socket.getaddrinfo(socket.gethostname(), None):
            found.add(str(info[4][0]).split("%")[0])
    for family, probe in ((socket.AF_INET, "192.0.2.1"), (socket.AF_INET6, "2001:db8::1")):
        sock = None
        try:
            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.connect((probe, 9))
            found.add(str(sock.getsockname()[0]).split("%")[0])
        except OSError:
            pass
        finally:
            if sock is not None:
                sock.close()
    return found


# SDP lines that may carry an address; the rest (fingerprints, ice-ufrag, ...) looks like IPv6 to a regex but is not
_ADDRESS_LINE = re.compile(r"^(o=|c=|a=candidate:|a=rtcp:|a=remote-candidates:)")
_SDP_PLACEHOLDERS = {"0.0.0.0", "127.0.0.1", "::"}  # constants Chrome writes into every offer, they identify nothing


def ip_literals(candidate_lines: list[str], sdp: str = "") -> set[str]:
    """Every IPv4 / IPv6 literal in WebRTC candidate lines and in the address-bearing lines of an SDP.

    mDNS names (`*.local`) are not literals and are allowed. The constant placeholders of any SDP are ignored.
    """
    lines = [line.strip() for line in candidate_lines]
    lines += [line.strip() for line in sdp.splitlines() if _ADDRESS_LINE.match(line.strip())]
    found: set[str] = set()
    for line in lines:
        for token in re.split(r"[\s,;]+", line):
            token = token.strip("[]").split("%")[0]
            with contextlib.suppress(ValueError):
                found.add(str(ipaddress.ip_address(token)))
    return found - _SDP_PLACEHOLDERS


# The classic page-side Runtime.enable probe: `console.debug(error)` runs the `stack` getter only when a CDP client has
# enabled the Runtime domain (the browser then serialises console arguments for it). It is kept because it is what
# detection scripts historically used, BUT on Chrome 157 it no longer fires even under stock Playwright with Runtime
# enabled (checked, see `TestStockControls`), so a green result of it alone proves nothing. The authoritative check is
# the CDP traffic itself (`CdpLog`). Other getter probes (error.name, array index, toStringTag, ...) were tried and
# rejected: they fire in a plain Chrome without any CDP client, so they cannot tell anything apart.
STACK_PROBE = """() => {
    const e = new Error(); let fired = false;
    Object.defineProperty(e, 'stack', {get() { fired = true; return ''; }});
    console.debug(e);
    return () => fired;
}"""


class CdpLog:
    """The CDP commands that the patchright (or Playwright) node driver sent to the browser.

    The driver logs every protocol message to its stderr when `DEBUG=pw:protocol` is set; its stderr is pointed at a
    temporary file instead of the console (`<library>._impl._transport._get_stderr_fileno`). This is how the audit sees
    the traffic from the outside: `Runtime.enable` is a CDP command, no page script can be trusted to reveal it.
    """

    SEND = re.compile(r"SEND \u25ba (\{.*\})\s*$")

    def __init__(self) -> None:
        self.file = tempfile.TemporaryFile()

    @contextlib.contextmanager
    def attached(self, library: str):
        """Patch the driver spawn of `patchright` / `playwright`: its stderr goes to this log, protocol debug is on."""
        transport = __import__(f"{library}._impl._transport", fromlist=["_get_stderr_fileno"])
        with patch.object(transport, "_get_stderr_fileno", return_value=self.file.fileno()), \
                patch.dict(os.environ, {"DEBUG": "pw:protocol"}):
            yield

    def commands(self) -> list[dict]:
        self.file.seek(0)
        text = self.file.read().decode("utf-8", "replace")
        found: list[dict] = []
        for line in text.splitlines():
            match = self.SEND.search(line)
            if match:
                with contextlib.suppress(ValueError):
                    found.append(json.loads(match.group(1)))
        return found

    def methods(self) -> list[str]:
        return [str(command.get("method")) for command in self.commands()]

    def close(self) -> None:
        self.file.close()


class GuardProxy:
    """A local HTTP proxy that can only reach this machine: loopback is forwarded, everything else gets a 502.

    Chrome counts it as an active proxy (so the WebRTC proxy policy is exercised) while nothing, not even the browser's
    own background requests, can leave the machine through it. `refused` lists the hosts it turned away.
    """

    def __init__(self) -> None:
        self.refused: list[str] = []
        self.forwarded: list[str] = []
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 3)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_writer = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            request_line, _, header_block = head.decode("latin-1").partition("\r\n")
            method, target, _version = request_line.split(" ", 2)
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
            else:
                parts = urlsplit(target)
                host, port = parts.hostname or "", str(parts.port or 80)
            if host not in ("127.0.0.1", "localhost", "::1"):
                self.refused.append(host)
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            self.forwarded.append(f"{method} {host}:{port}")
            upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", int(port))
            if method.upper() == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                origin = urlsplit(target)
                path = (origin.path or "/") + (f"?{origin.query}" if origin.query else "")
                kept = [h for h in header_block.split("\r\n") if h and h.split(":", 1)[0].lower()
                        not in ("proxy-connection", "connection", "keep-alive", "proxy-authorization")]
                upstream_writer.write(
                    (f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(kept) + "\r\nConnection: close\r\n\r\n").encode("latin-1")
                )
                await upstream_writer.drain()
            await asyncio.gather(self._pipe(reader, upstream_writer), self._pipe(upstream_reader, writer))
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError, ValueError):
            pass
        finally:
            for stream in (writer, upstream_writer):
                if stream is not None:
                    with contextlib.suppress(Exception):
                        stream.close()

    @staticmethod
    async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        try:
            while data := await source.read(65536):
                sink.write(data)
                await sink.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            with contextlib.suppress(Exception):
                if sink.can_write_eof():
                    sink.write_eof()


class PageServer:
    """`http://127.0.0.1:<port>/`: a secure context for the page (`data:` is not one, `PublicKeyCredential` needs it)."""

    PAGES = {
        "/": b"<!doctype html><title>audit</title><body>audit</body>",
        # Runs in the page's own main world at load time, before any automation call could touch the page
        "/leak": (
            "<!doctype html><title>leak</title><script>window.__readLeak = (" + STACK_PROBE + ")();</script><body>leak</body>"
        ).encode("utf-8"),
    }

    def __init__(self) -> None:
        self.accept_language: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.accept_language.append(self.headers.get("Accept-Language", ""))
                body = outer.PAGES.get(self.path.split("?")[0], b"")
                self.send_response(200 if body else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def chrome_processes_using(prefix: str) -> list[str]:
    """`pid name` of the running chrome.exe / node.exe whose command line mentions `prefix` (Windows only)."""
    if sys.platform != "win32":
        return []
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('chrome.exe','node.exe') -and "
        "$_.CommandLine -like '*" + prefix + "*' } | "
        "ForEach-Object { '{0} {1}' -f $_.ProcessId, $_.Name }"
    )
    try:
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _skip_reason() -> str | None:
    if os.environ.get("YT_SKIP_REAL_CHROME") == "1":
        return "YT_SKIP_REAL_CHROME=1"
    try:
        import patchright.async_api  # noqa: F401
    except ImportError:
        return "patchright is not installed"
    if chrome_launcher.find_chrome_executable() is None:
        return "Google Chrome is not installed"
    return None


def _browser_settings(root: Path) -> BrowserSettings:
    target = TargetSettings(
        name="youtube", base_url="https://www.youtube.com",
        item_url_template="https://www.youtube.com/watch?v={external_id}",
        submit_input_selector="#reply-dialog #contenteditable-root",
        submit_button_selector="#reply-dialog #submit-button button",
        confirmation_selector="ytd-comment-view-model",
    )
    return BrowserSettings(headless=True, session_root=root, targets=[target])


DEFAULT_EXIT = ExitGeo(ip="203.0.113.7", country="DE", timezone="Europe/Berlin", latitude=52.52, longitude=13.4)


class RealChromeCase(unittest.IsolatedAsyncioTestCase):
    """Base of Part B: a fresh temporary profile per launch, a local page server and a loopback-only proxy."""

    @classmethod
    def setUpClass(cls):
        reason = _skip_reason()
        if reason:
            raise unittest.SkipTest(reason)
        cls.root = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX))
        cls.pages = PageServer()
        cls.pages.start()

    @classmethod
    def tearDownClass(cls):
        cls.pages.stop()
        shutil.rmtree(cls.root, ignore_errors=True)

    async def asyncSetUp(self):
        self.proxy = GuardProxy()
        await self.proxy.start()
        self.addAsyncCleanup(self.proxy.stop)
        self._launch_index = 0

    @contextlib.asynccontextmanager
    async def chrome(self, *, locale: str | None = None, exit_geo: ExitGeo | None = DEFAULT_EXIT, use_proxy: bool = True,
                     name: str = "audit"):
        """A started headless `YouTubeRunner(engine="chrome")`, closed (and its node driver stopped) on exit.

        Only the network is faked: `ensure_network_ready` and `resolve_exit_geo` answer with `exit_geo`; the profile,
        the command line, the launch and the shutdown are the production code.
        """
        self._launch_index += 1
        self.cdp = CdpLog()
        self.addCleanup(self.cdp.close)
        session_dir = self.root / f"{self.id().rsplit('.', 1)[-1]}_{self._launch_index}"
        cfg = _browser_settings(self.root)
        runner = YouTubeRunner(cfg, cfg.targets[0], session_dir=session_dir, account_name=name, engine="chrome",
                               locale=locale, proxy=self.proxy.url if use_proxy else None)
        try:
            with patch("modules.youtube_runner.ensure_network_ready", AsyncMock(return_value=exit_geo)), \
                    patch("modules.geo_guard.resolve_exit_geo", AsyncMock(return_value=exit_geo)), \
                    self.cdp.attached("patchright"):
                await asyncio.wait_for(runner.start(), 90)
            yield runner
        finally:
            await asyncio.wait_for(runner.close(), 60)

    async def main_world(self, runner: YouTubeRunner, expression: str):
        """Evaluate in the page's MAIN world.

        patchright evaluates in an isolated world by default (`isolated_context=True`: a separate JS world created
        with `Page.createIsolatedWorld`, so that no `Runtime.enable` is needed). A site's own scripts live in the main
        world, so that is where "what a site sees" has to be asked: `isolated_context=False`.
        """
        return await runner._page.evaluate(expression, isolated_context=False)


class TestRealChromeSurface(RealChromeCase):
    """10.1, 10.2, 10.3, 10.5: one launch, the basic automation tells."""

    async def test_navigator_webdriver_is_false_and_the_getter_is_native(self):
        async with self.chrome() as runner:
            await runner._page.goto(self.pages.url + "/", wait_until="domcontentloaded")
            info = await self.main_world(runner, """() => {
                const proto = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver');
                const own = Object.getOwnPropertyDescriptor(navigator, 'webdriver');
                return {
                    value: navigator.webdriver,
                    ownProperty: own === undefined ? null : 'own',
                    protoHasGetter: !!(proto && proto.get),
                    getterSource: proto && proto.get ? Function.prototype.toString.call(proto.get) : null,
                    getterMethod: proto && proto.get ? String(proto.get) : null,
                    protoEnumerable: proto ? proto.enumerable : null,
                };
            }""")
        FINDINGS["webdriver"] = info
        self.assertIs(info["value"], False, info)
        self.assertIsNone(info["ownProperty"], f"navigator carries its own `webdriver` property (patched on the instance): {info}")
        self.assertTrue(info["protoHasGetter"], info)
        self.assertIn("[native code]", info["getterSource"], info)
        self.assertIn("[native code]", info["getterMethod"], info)

    async def test_public_key_credential_exists(self):
        async with self.chrome() as runner:
            await runner._page.goto(self.pages.url + "/", wait_until="domcontentloaded")
            info = await self.main_world(runner, """() => ({
                present: 'PublicKeyCredential' in window,
                secure: window.isSecureContext,
                typeofIt: typeof window.PublicKeyCredential,
                hasAvailabilityApi: typeof (window.PublicKeyCredential || {}).isUserVerifyingPlatformAuthenticatorAvailable,
            })""")
        FINDINGS["publickeycredential"] = info
        self.assertTrue(info["secure"], f"the probe page must be a secure context: {info}")
        self.assertIs(info["present"], True, info)

    async def test_runtime_enable_is_not_leaked(self):
        """No `Runtime.enable` (nor `Console.enable`) is ever sent, and the classic page probe stays silent.

        1. The authoritative check: the CDP commands of the whole run (start, navigation, main-world evaluate, close).
        2. The page probe, twice: an inline script that runs at load in the main world, and `page.evaluate` in the MAIN
           world. patchright evaluates in an isolated world by default (`isolated_context=True`: a separate JS world made
           by `Page.createIsolatedWorld`, so that `Runtime.enable` is not needed); that world has its own console and
           says nothing about what a site's script can observe, hence `isolated_context=False` here. On Chrome 157 this
           probe cannot fire at all (see `STACK_PROBE`), it is a regression tripwire, not the proof.
        """
        async with self.chrome() as runner:
            await runner._page.goto(self.pages.url + "/leak", wait_until="load")
            await asyncio.sleep(0.4)
            inline = await self.main_world(runner, "() => window.__readLeak()")
            evaluated = await self.main_world(runner, "async () => { const read = (" + STACK_PROBE + ")(); "
                                                      "await new Promise((r) => setTimeout(r, 400)); return read(); }")
            methods = self.cdp.methods()
        FINDINGS["runtime_enable"] = {"inline_stack_probe_fired": inline, "main_world_stack_probe_fired": evaluated,
                                      "cdp_commands_sent": len(methods), "Runtime.enable_sent": "Runtime.enable" in methods}
        self.assertGreater(len(methods), 10, "the CDP log is empty: the capture is broken, the check below would pass blindly")
        self.assertIn("Page.navigate", methods)
        self.assertNotIn("Runtime.enable", methods, "patchright sent Runtime.enable: every page script can detect it")
        self.assertNotIn("Console.enable", methods)
        self.assertIs(inline, False, "the stack getter fired for console.debug in an inline script of the page")
        self.assertIs(evaluated, False, "the stack getter fired for console.debug in the main world")

    async def test_cdp_traffic_carries_no_identity_override_and_no_injected_script(self):
        """What the driver tells Chrome during a normal start: only the time zone and the position are overridden.

        A user agent / client-hints / header / viewport / locale override, request interception or a non-empty init
        script would be a second source of truth next to the real browser (and a tell). patchright's own baseline is
        allowed: `setUserAgentOverride` with an EMPTY user agent is a no-op reset, and its utility-world script is empty.
        """
        async with self.chrome() as runner:
            await runner._page.goto(self.pages.url + "/", wait_until="domcontentloaded")
            commands = self.cdp.commands()
        methods = [str(c.get("method")) for c in commands]
        FINDINGS["cdp_methods"] = {m: methods.count(m) for m in sorted(set(methods))}
        self.assertGreater(len(commands), 10, "the CDP log is empty")
        forbidden = {"Emulation.setLocaleOverride", "Emulation.setDeviceMetricsOverride", "Emulation.setNavigatorOverrides",
                     "Emulation.setScriptExecutionDisabled", "Emulation.setTouchEmulationEnabled", "Network.setExtraHTTPHeaders",
                     "Network.setUserAgentOverride", "Network.setRequestInterception", "Fetch.enable", "Page.setBypassCSP",
                     "Emulation.setAutomationOverride", "Runtime.enable", "Console.enable"}
        self.assertEqual(sorted(forbidden & set(methods)), [], "identity override / interception reached the browser")
        self.assertFalse([m for m in methods if m.startswith("WebAuthn.")], "WebAuthn is for the sign-in only")
        for command in commands:
            params = command.get("params") or {}
            if command.get("method") == "Emulation.setUserAgentOverride":
                self.assertEqual(params.get("userAgent"), "", f"a real user agent override: {params}")
                self.assertFalse({"userAgentMetadata", "acceptLanguage", "platform"} & set(params), params)
            if command.get("method") == "Page.addScriptToEvaluateOnNewDocument":
                self.assertEqual(params.get("source"), "", "a script is injected into every new document")
        self.assertIn("Emulation.setTimezoneOverride", methods)  # the one override that is meant to be there

    async def test_the_runner_page_is_the_only_tab(self):
        async with self.chrome() as runner:
            pages = runner.context.pages
            self.assertEqual(len(pages), 1, [p.url for p in pages])
            self.assertIs(runner._page, pages[0])
            await runner._page.goto(self.pages.url + "/", wait_until="domcontentloaded")
            self.assertEqual(len(runner.context.pages), 1)
            self.assertIs(runner._page, runner.context.pages[0])

    @unittest.expectedFailure
    async def test_headless_user_agent_has_no_headless_token(self):
        """KNOWN headless artifact, not a bug of ours: headless Chrome 157 says `HeadlessChrome/157` in its user agent.

        Marked `expectedFailure` on purpose instead of weakening the check: production runs headful
        (`BrowserSettings.headless` is False by default) where the token does not exist. If this ever starts to pass,
        headless Chrome stopped sending the token and the marker has to go.
        """
        async with self.chrome() as runner:
            await runner._page.goto(self.pages.url + "/", wait_until="domcontentloaded")
            info = await self.main_world(runner, "() => ({ua: navigator.userAgent, brands: navigator.userAgentData.brands.map((b) => b.brand)})")
        FINDINGS["headless_user_agent"] = info
        self.assertNotIn("Headless", info["ua"])
        self.assertFalse([brand for brand in info["brands"] if "Headless" in brand], info)


class TestRealChromeLanguages(RealChromeCase):
    """10.4: `navigator.languages` is the profile's list and nothing of the (Russian) host leaks into it."""

    async def test_languages_follow_the_locale_of_the_profile(self):
        for locale in ("de-DE", "ru-RU", "en-GB"):
            with self.subTest(locale=locale):
                self.pages.accept_language.clear()
                async with self.chrome(locale=locale) as runner:
                    await runner._page.goto(self.pages.url + "/", wait_until="domcontentloaded")
                    info = await self.main_world(runner, "() => ({languages: [...navigator.languages], language: navigator.language})")
                expected = accept_languages_for(locale).split(",")
                FINDINGS[f"languages[{locale}]"] = {**info, "accept_language_header": list(self.pages.accept_language)}
                self.assertEqual(info["languages"][0], locale, info)
                self.assertEqual(info["language"], locale, info)
                self.assertEqual(info["languages"], expected, f"extra or missing languages (host OS leak?): {info}")
                header = self.pages.accept_language[-1] if self.pages.accept_language else ""
                self.assertEqual([part.split(";")[0].strip() for part in header.split(",")], expected, header)


class TestRealChromeWebRtc(RealChromeCase):
    """16.1: with an active proxy WebRTC exposes no IP literal: neither a local address nor the public one."""

    OFFER = """async () => {
        const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:stun.l.google.com:19302'}]});
        pc.createDataChannel('probe');
        const candidates = [];
        const finished = new Promise((resolve) => {
            pc.onicecandidate = (event) => {
                if (event.candidate) {
                    candidates.push({line: event.candidate.candidate, address: event.candidate.address || null,
                                     type: event.candidate.type || null});
                } else {
                    resolve('complete');
                }
            };
            setTimeout(() => resolve('timeout'), 6000);
        });
        await pc.setLocalDescription(await pc.createOffer());
        const state = await finished;
        const sdp = pc.localDescription ? pc.localDescription.sdp : '';
        pc.close();
        return {state, candidates, sdp};
    }"""

    def test_the_detector_finds_literals_and_ignores_the_rest(self):
        # Part of the audit's own sanity: a leak must be reported, a mDNS name and SDP placeholders must not
        leaky = ["candidate:1 1 udp 2113937151 192.168.1.20 54400 typ host", "candidate:2 1 udp 1677729535 203.0.113.5 54400 typ srflx",
                 "candidate:3 1 udp 1 2001:db8::7 5 typ host"]
        self.assertEqual(ip_literals(leaky), {"192.168.1.20", "203.0.113.5", "2001:db8::7"})
        clean = ["candidate:1 1 udp 2113937151 0b9f1c0e-1111-4222-8333-444455556666.local 54400 typ host"]
        sdp = "v=0\r\no=- 4611 2 IN IP4 127.0.0.1\r\nc=IN IP4 0.0.0.0\r\na=rtcp:9 IN IP4 0.0.0.0\r\n" \
              "a=fingerprint:sha-256 AB:CD:EF:01:23:45:67:89:AB:CD:EF:01:23:45:67:89\r\n"
        self.assertEqual(ip_literals(clean, sdp), set())
        self.assertEqual(ip_literals([], sdp + "a=candidate:9 1 udp 1 198.51.100.4 1 typ host\r\n"), {"198.51.100.4"})

    async def test_no_ip_literal_in_ice_candidates_or_sdp_with_an_active_proxy(self):
        local_ips = host_ip_addresses()
        async with self.chrome() as runner:
            await runner._page.goto(self.pages.url + "/", wait_until="domcontentloaded")
            result = await self.main_world(runner, self.OFFER)
            self.assertTrue(self.proxy.forwarded, "the page did not go through the proxy: it is not active in this run")
        candidates = [c["line"] for c in result["candidates"]]
        found = ip_literals(candidates + [c["address"] for c in result["candidates"] if c["address"]], result["sdp"])
        FINDINGS["webrtc[proxy]"] = {
            "gathering": result["state"], "candidates": result["candidates"], "ip_literals": sorted(found),
            "host_ips_checked": sorted(local_ips), "proxy_refused_hosts": sorted(set(self.proxy.refused)),
        }
        self.assertNotEqual(result["state"], "timeout", "ICE gathering did not finish in 6 s")
        self.assertEqual(found & local_ips, set(), f"an address of this machine reached WebRTC: {found & local_ips}")
        self.assertEqual(found, set(), f"IP literals in ICE candidates / SDP: {found}; candidates={candidates}")
        self.assertTrue(all(c["type"] != "srflx" for c in result["candidates"]), "a server-reflexive candidate was gathered")


class TestRealChromeTimezone(RealChromeCase):
    """16.2: the time zone is the one of the exit IP's city (geo_guard), then the one of the country, never the host's."""

    async def _timezone(self, exit_geo: ExitGeo | None) -> dict:
        async with self.chrome(exit_geo=exit_geo) as runner:
            await runner._page.goto(self.pages.url + "/", wait_until="domcontentloaded")
            return await self.main_world(runner, """() => ({
                zone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                offsetMinutes: new Date(2026, 6, 1, 12, 0, 0).getTimezoneOffset(),
                locale: Intl.DateTimeFormat().resolvedOptions().locale,
            })""")

    async def test_the_zone_of_the_exit_ip_city_wins_over_the_country_default(self):
        info = await self._timezone(ExitGeo(ip="203.0.113.7", country="US", timezone="America/Chicago", latitude=32.78, longitude=-96.8))
        FINDINGS["timezone[US/Chicago]"] = info
        self.assertEqual(COUNTRY_TO_TIMEZONE["US"], "America/New_York")  # the thing that must NOT be used here
        self.assertEqual(info["zone"], "America/Chicago", info)
        self.assertEqual(info["offsetMinutes"], 300, info)  # CDT in July: UTC-5

    async def test_a_country_without_a_city_zone_falls_back_to_the_country_zone(self):
        info = await self._timezone(ExitGeo(ip="203.0.113.7", country="DE", timezone=None, latitude=None, longitude=None))
        FINDINGS["timezone[DE/None]"] = info
        self.assertEqual(info["zone"], "Europe/Berlin", info)
        self.assertEqual(info["offsetMinutes"], -120, info)  # CEST in July: UTC+2


class TestStockControls(RealChromeCase):
    """Positive controls: stock Playwright DOES leak what the tests above say patchright does not, and the audit sees it.

    Without them a green leak test could mean "the detector is blind". Skipped without `playwright`.
    """

    async def _stock_chrome(self, name: str, *args: str):
        from playwright.async_api import async_playwright

        cdp = CdpLog()
        self.addCleanup(cdp.close)
        with cdp.attached("playwright"):
            driver = await async_playwright().start()
        self.addAsyncCleanup(driver.stop)
        context = await driver.chromium.launch_persistent_context(
            str(self.root / name), channel="chrome", headless=True, no_viewport=True,
            proxy={"server": self.proxy.url}, args=list(args),
        )
        self.addAsyncCleanup(context.close)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(self.pages.url + "/", wait_until="domcontentloaded")
        return page, cdp

    def _need_playwright(self):
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            self.skipTest("playwright is not installed")

    async def test_the_cdp_log_sees_runtime_enable_under_stock_playwright(self):
        self._need_playwright()
        page, cdp = await self._stock_chrome("stock_runtime")
        stack = await page.evaluate("async () => { const read = (" + STACK_PROBE + ")(); "
                                    "await new Promise((r) => setTimeout(r, 400)); return read(); }")
        webdriver = await page.evaluate("() => navigator.webdriver")
        FINDINGS["control[stock playwright]"] = {"Runtime.enable_sent": "Runtime.enable" in cdp.methods(),
                                                 "stack_probe_fired": stack, "navigator.webdriver": webdriver}
        self.assertIn("Runtime.enable", cdp.methods(), "the CDP log cannot see Runtime.enable: the leak test proves nothing")

    async def test_the_webrtc_check_sees_local_addresses_when_the_lock_is_off(self):
        """Chrome without our lock and without mDNS masking exposes the interface addresses: `ip_literals` must find them.

        No STUN server on purpose (nothing leaves the machine): host candidates only.
        """
        self._need_playwright()
        page, _ = await self._stock_chrome("stock_webrtc", "--disable-features=WebRtcHideLocalIpsWithMdns")
        result = await page.evaluate(TestRealChromeWebRtc.OFFER.replace("{urls: 'stun:stun.l.google.com:19302'}", ""))
        lines = [c["line"] for c in result["candidates"]]
        found = ip_literals(lines + [c["address"] for c in result["candidates"] if c["address"]], result["sdp"])
        FINDINGS["control[stock chrome webrtc, mDNS masking off, no proxy policy]"] = {
            "candidates": result["candidates"], "ip_literals": sorted(found)}
        self.assertTrue(found & host_ip_addresses(), f"the control did not expose a host address: {found}; candidates={lines}")


class TestNoLeftoverProcesses(RealChromeCase):
    """The browser and the patchright driver (node.exe) of a closed runner must be gone (Windows: checked by command line)."""

    async def test_a_closed_runner_leaves_no_chrome_or_node_behind(self):
        async with self.chrome() as runner:
            session_dir = str(runner.session_dir)
            alive = chrome_processes_using(session_dir)
            FINDINGS["processes_while_running"] = len(alive)
        await asyncio.sleep(1.5)  # the OS needs a moment to reap the children of a closed Chrome
        left = chrome_processes_using(session_dir)
        if sys.platform == "win32":
            self.assertTrue(alive, "the process check itself finds nothing while Chrome runs: it is blind")
        self.assertEqual(left, [], f"processes survived the close: {left}")


def tearDownModule():
    if os.environ.get("YT_AUDIT_VERBOSE") == "1" and FINDINGS:
        print("\n=== stealth audit findings ===\n" + json.dumps(FINDINGS, indent=2, ensure_ascii=False, default=str), file=sys.stderr)
    if _skip_reason() is None:
        left = chrome_processes_using(PROFILE_PREFIX)
        if left:  # a loud failure of the whole run: a test leaked a browser
            raise AssertionError(f"processes of the audit are still running: {left}")


if __name__ == "__main__":
    unittest.main()
