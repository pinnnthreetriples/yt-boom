"""Проверка купленных прокси вживую: настоящие запросы libcurl к локальным серверам (эхо-сервис IP и HTTP-прокси на
127.0.0.1), без интернета и без подмены `_fetch_ip`. Переменные окружения прокси выставлены во всех тестах: проверка
обязана их игнорировать (trust_env=False)."""
import asyncio
import http.client
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from database import proxy_country, proxy_country_label
from modules import geo_guard, proxy_check, proxy_screen
from modules.proxy_check import check_direct, check_proxies, check_proxy, classify_curl_error

EXIT_IP = "203.0.113.9"  # what the local echo service answers
HOST_IP = "198.51.100.7"  # "the machine's own IP" in these tests
SECRET = "SeCrEt-pw"
DEAD = "127.0.0.1:1"  # nothing listens on port 1


class _Echo(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", headers=None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/ip":
            self._send(200, EXIT_IP.encode())
        elif path == "/ip-json":
            self._send(200, json.dumps({"ip": EXIT_IP}).encode())
        elif path == "/denied":
            self._send(200, b"Access denied")
        elif path == "/bad-json":
            self._send(200, b'{"ip":"not-an-ip"}')
        elif path == "/html":
            self._send(200, b"<h1>403</h1>")
        elif path == "/redirect":
            self._send(302, b"", {"Location": "/ip"})
        elif path == "/bad-gateway":
            self._send(502, b"bad gateway")
        elif path == "/headers":
            self._send(200, json.dumps({"headers": dict(self.headers.items())}).encode())
        else:
            self._send(404)


class _ForwardProxy(BaseHTTPRequestHandler):
    """A plain HTTP proxy: forwards absolute-URI GETs (adding `server.extra` headers), answers CONNECT with
    `server.connect_status` (no tunnels)."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        url = urlsplit(self.path)
        conn = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in ("proxy-connection", "proxy-authorization")}
        headers.update(self.server.extra)
        conn.request("GET", url.path + (f"?{url.query}" if url.query else ""), headers=headers)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        self.send_response(response.status)
        for name, value in response.getheaders():
            if name.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):
        self.server.connects += 1
        self.send_response(self.server.connect_status)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _serve(handler, **attrs):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    for name, value in attrs.items():
        setattr(server, name, value)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class LiveCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.echo = _serve(_Echo)
        cls.proxy = _serve(_ForwardProxy, extra={}, connect_status=403, connects=0)

    @classmethod
    def tearDownClass(cls):
        for server in (cls.echo, cls.proxy):
            server.shutdown()
            server.server_close()

    def setUp(self):
        self.proxy.extra = {}
        self.proxy.connect_status = 403
        self.proxy.connects = 0
        env = {"NO_PROXY": "*", "no_proxy": "*", "HTTP_PROXY": f"http://{DEAD}", "http_proxy": f"http://{DEAD}",
               "HTTPS_PROXY": f"http://{DEAD}", "https_proxy": f"http://{DEAD}", "ALL_PROXY": f"http://{DEAD}"}
        for patcher in (
            patch.dict(os.environ, env),
            patch.object(proxy_check, "IP_ECHO_URLS", (self.url("/ip"),)),
            patch.object(geo_guard, "HEADER_ECHO_URL", self.url("/headers")),
            patch.object(geo_guard, "resolve_host_ip", AsyncMock(return_value=HOST_IP)),
            patch.object(proxy_check, "country_for_ip", AsyncMock(return_value=None)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def url(self, path, scheme="http"):
        return f"{scheme}://127.0.0.1:{self.echo.server_port}{path}"

    @property
    def proxy_url(self):
        return f"http://user:{SECRET}@127.0.0.1:{self.proxy.server_port}"

    def echo_urls(self, *paths):
        return patch.object(proxy_check, "IP_ECHO_URLS", tuple(self.url(p) for p in paths))

    def assert_no_secret(self, result):
        self.assertNotIn(SECRET, f"{result.message} {result.leak_warning} {result.browser_warning} {result!r}")


class TestEnvironmentIsIgnored(LiveCase):
    async def test_dead_proxy_fails_even_with_no_proxy_star(self):
        result = await check_proxy(f"http://user:{SECRET}@{DEAD}", timeout=3)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "unreachable")
        self.assert_no_secret(result)

    async def test_direct_check_ignores_http_proxy_variables(self):
        os.environ.pop("NO_PROXY", None)  # restored by patch.dict; NO_PROXY=* would hide the bug here
        os.environ.pop("no_proxy", None)
        result = await check_direct(timeout=3)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.exit_ip, EXIT_IP)

    async def test_working_proxy_passes(self):
        result = await check_proxy(self.proxy_url, timeout=3)
        self.assertTrue(result.ok, result.message)
        self.assertEqual((result.scheme, result.exit_ip, result.error_kind, result.leak_warning), ("http", EXIT_IP, None, None))
        self.assert_no_secret(result)

    def test_geo_guard_lookups_ignore_the_environment(self):
        with patch.object(geo_guard, "IP_ECHO_URLS", (self.url("/ip-json"),)):
            self.assertEqual(geo_guard._fetch_public_ip(None, timeout=3), EXIT_IP)  # direct despite HTTP_PROXY
            self.assertEqual(geo_guard._lookup_host_ip(), EXIT_IP)
            with self.assertRaises(RuntimeError) as raised:
                geo_guard._fetch_public_ip(f"http://user:{SECRET}@{DEAD}", timeout=3)  # NO_PROXY=* must not bypass it
            self.assertNotIn(SECRET, str(raised.exception))
            self.assertEqual(geo_guard._fetch_public_ip(self.proxy_url, timeout=3), EXIT_IP)
        self.proxy.extra = {"Via": "1.1 test"}
        self.assertEqual(geo_guard._fetch_echo_headers(self.proxy_url).get("via"), "1.1 test")  # through the proxy, not direct


class TestAnswerMustBeAnIp(LiveCase):
    async def test_non_ip_answers_never_pass(self):
        for path in ("/denied", "/bad-json", "/html", "/redirect"):
            with self.subTest(path=path), self.echo_urls(path):
                result = await check_proxy(self.proxy_url, timeout=3)
                self.assertFalse(result.ok)
                self.assertEqual(result.error_kind, "bad_response")
                self.assertIsNone(result.exit_ip)
                self.assert_no_secret(result)

    async def test_json_answer_is_read(self):
        with self.echo_urls("/ip-json"):
            self.assertEqual((await check_proxy(self.proxy_url, timeout=3)).exit_ip, EXIT_IP)

    async def test_bad_gateway_falls_back_to_the_next_service(self):
        with self.echo_urls("/bad-gateway", "/ip"):
            result = await check_proxy(self.proxy_url, timeout=3)
        self.assertTrue(result.ok, result.message)

    async def test_unresolvable_service_falls_back_to_the_next_one(self):
        """curl code 6 (the echo service's name, not the proxy's) is not "proxy unreachable"."""
        with patch.object(proxy_check, "IP_ECHO_URLS", ("http://no-such-host.invalid/ip", self.url("/ip"))):
            result = await check_direct(timeout=3)
        self.assertTrue(result.ok, result.message)


class TestProxyMustHideTheHostIp(LiveCase):
    async def test_exit_from_the_host_ip_fails(self):
        for host_ip in (EXIT_IP, f"::ffff:{EXIT_IP}"):
            with self.subTest(host_ip=host_ip):
                result = await check_proxy(self.proxy_url, timeout=3, host_ip=host_ip)
                self.assertFalse(result.ok)
                self.assertEqual(result.error_kind, "host_ip_leak")
                self.assertIn("не скрывает ваш IP", result.message)
                self.assert_no_secret(result)

    async def test_host_ip_in_a_forwarding_header_fails(self):
        for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP", "Via"):
            value = f"for={HOST_IP}" if header == "Forwarded" else (f"1.1 gw ({HOST_IP})" if header == "Via" else HOST_IP)
            with self.subTest(header=header):
                self.proxy.extra = {header: value}
                result = await check_proxy(self.proxy_url, timeout=3)
                self.assertFalse(result.ok)
                self.assertEqual(result.error_kind, "header_leak")
                self.assertNotIn(HOST_IP, result.message)
                self.assert_no_secret(result)

    async def test_proxy_headers_without_the_host_ip_only_warn(self):
        self.proxy.extra = {"Via": "1.1 squid", "X-Forwarded-For": "10.0.0.5"}
        result = await check_proxy(self.proxy_url, timeout=3)
        self.assertTrue(result.ok, result.message)
        self.assertIn("via", result.leak_warning)

    async def test_unknown_host_ip_warns_instead_of_passing_silently(self):
        result = await check_proxy(self.proxy_url, timeout=3, resolve_host=False)
        self.assertTrue(result.ok)
        self.assertIn("пропущена", result.leak_warning)

    async def test_failed_header_echo_warns(self):
        with patch.object(geo_guard, "HEADER_ECHO_URL", self.url("/bad-gateway")):
            result = await check_proxy(self.proxy_url, timeout=3)
        self.assertTrue(result.ok)
        self.assertIn("заголовки", result.leak_warning)

    async def test_batch_resolves_the_host_ip_once_and_isolates_bad_items(self):
        results = await check_proxies([self.proxy_url, "not a proxy", self.proxy_url], timeout=3)
        self.assertEqual([r.ok for r in results], [True, False, True])
        geo_guard.resolve_host_ip.assert_awaited_once()

    async def test_check_all_records_a_proxy_that_exits_from_your_ip_as_dead(self):
        """«Проверить все»: the direct check runs first and every proxy is compared with it."""
        record = SimpleNamespace(id=7, password_unavailable=False, scheme="http", host="127.0.0.1",
                                 port=self.proxy.server_port, login="user", password=SECRET)
        stored = []

        class FakeDb:
            async def record_proxy_check(self, proxy_id, ok, **fields):
                stored.append((proxy_id, ok, fields))

        state = proxy_screen.ProxyState([record], [])
        with patch("modules.wizard._pools_db", lambda path, fn: asyncio.run(fn(FakeDb()))), \
             patch("modules.tui.status_line"), patch("modules.wizard.safe_print"):
            kind, text = await asyncio.to_thread(proxy_screen.check_all, None, state)
        self.assertEqual(stored[0][:2], (7, False))  # direct exit == proxy exit (both 203.0.113.9)
        self.assertIn("не скрывает", stored[0][2]["error"])
        self.assertEqual(kind, "warn")
        self.assertNotIn(SECRET, text)


class TestProxyRefusals(LiveCase):
    async def test_connect_403_is_forbidden_not_unreachable(self):
        with patch.object(proxy_check, "IP_ECHO_URLS", (self.url("/ip", "https"), self.url("/ip", "https"))):
            result = await check_proxy(self.proxy_url, timeout=3)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, "forbidden")
        self.assertNotIn("недоступен", result.message)
        self.assertEqual(self.proxy.connects, 1)  # the proxy refused: the second service is not asked
        self.assert_no_secret(result)

    async def test_connect_407_is_a_login_error(self):
        self.proxy.connect_status = 407
        with patch.object(proxy_check, "IP_ECHO_URLS", (self.url("/ip", "https"),)):
            result = await check_proxy(self.proxy_url, timeout=3)
        self.assertEqual((result.ok, result.error_kind), (False, "auth"))

    async def test_connect_400_on_unknown_type_tries_socks5(self):
        self.proxy.connect_status = 400
        tried = []
        real_try = proxy_check._try

        async def spy(parts, scheme, timeout):
            tried.append(scheme)
            return await real_try(parts, scheme, timeout)

        with patch.object(proxy_check, "IP_ECHO_URLS", (self.url("/ip", "https"),)), patch.object(proxy_check, "_try", spy):
            result = await check_proxy(f"127.0.0.1:{self.proxy.server_port}:user:{SECRET}", timeout=3)
        self.assertEqual(tried, ["http", "socks5"])
        self.assertFalse(result.ok)
        self.assert_no_secret(result)


class FakeCurlError(Exception):
    def __init__(self, text, code):
        super().__init__(text)
        self.code = code


class TestClassify(unittest.TestCase):
    def test_curl_messages(self):
        cases = [
            ("curl: (56) CONNECT tunnel failed, response 407", 56, "auth"),
            ("curl: (56) CONNECT tunnel failed, response 403", 56, "forbidden"),
            ("curl: (7) CONNECT tunnel failed, response 401", 7, "forbidden"),
            ("curl: (56) CONNECT tunnel failed, response 400", 56, "protocol"),
            ("curl: (56) CONNECT tunnel failed, response 502", 56, "upstream"),
            ("curl: (56) CONNECT tunnel failed, response 503", 56, "upstream"),
            ("curl: (5) Could not resolve proxy: nohost", 5, "unreachable"),
            ("curl: (6) Could not resolve host: api.ipify.org", 6, "upstream"),
            ("curl: (7) Failed to connect to 1.2.3.4 port 1: Connection refused", 7, "unreachable"),
            ("curl: (97) User was rejected by the SOCKS5 server (1 1).", 97, "auth"),
            ("curl: (28) Operation timed out after 10001 ms", 28, "timeout"),
        ]
        for text, code, kind in cases:
            with self.subTest(text=text):
                self.assertEqual(classify_curl_error(FakeCurlError(text, code))[0], kind)

    def test_socks_login_error_does_not_claim_http_407(self):
        self.assertNotIn("407", proxy_check._failure_message("auth", 97, 10, True, "socks5"))
        self.assertIn("407", proxy_check._failure_message("auth", 56, 10, True, "http"))


class TestCheckedCountryWins(unittest.TestCase):
    def test_exit_country_beats_the_sellers_word(self):
        record = SimpleNamespace(id=1, country="US", country_source="user", last_exit_country="RU", address="1.2.3.4:80")
        self.assertEqual(proxy_country(record), "RU")
        self.assertEqual(proxy_screen.country_of(record), "RU")
        self.assertEqual(proxy_country_label(record), "RU≠US")
        self.assertEqual(proxy_screen.proxy_name(record), "[RU≠US] 1.2.3.4:80")
        account = SimpleNamespace(proxy_id=None, expected_country="US")
        with patch("modules.proxy_screen.browser_warning", return_value=None):
            self.assertIn("ждёт US", proxy_screen.account_warnings(account, False, record))

    def test_seller_country_is_shown_until_a_check(self):
        record = SimpleNamespace(country="US", last_exit_country=None)
        self.assertEqual((proxy_country(record), proxy_country_label(record)), ("US", "US"))
        same = SimpleNamespace(country="pl", last_exit_country="PL")
        self.assertEqual(proxy_country_label(same), "PL")


if __name__ == "__main__":
    unittest.main()
