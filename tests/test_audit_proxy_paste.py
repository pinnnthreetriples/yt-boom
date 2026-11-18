"""T-08 part 1: ambiguous and edge-case lines of the seller-paste parser (`modules/proxy_paste.py`)."""

from __future__ import annotations

import unittest

from modules.proxy_paste import ProxyPasteError, parse_proxy_line, parse_proxy_paste


class TestPasteAudit(unittest.TestCase):
    def check(self, line, scheme, host, port, login, password, country=None):
        draft = parse_proxy_line(line)
        self.assertEqual(
            (draft.scheme, draft.host, draft.port, draft.login, draft.password, draft.country),
            (scheme, host, port, login, password, country),
        )

    def test_numeric_password_after_at(self):
        self.check("1.2.3.4:8080@myuser:1234", None, "1.2.3.4", 8080, "myuser", "1234")
        self.check("proxy.example.com:8080@myuser:1234", None, "proxy.example.com", 8080, "myuser", "1234")
        self.check("localhost:8080@myuser:1234", None, "localhost", 8080, "myuser", "1234")
        self.check("[2001:db8::1]:8080@myuser:1234", None, "2001:db8::1", 8080, "myuser", "1234")

    def test_password_with_at_and_colon_digits(self):
        self.check("1.2.3.4:8080:user:p@ss:123", None, "1.2.3.4", 8080, "user", "p@ss:123")
        self.check("1.2.3.4:8080:user:p@ss:123:PL", None, "1.2.3.4", 8080, "user", "p@ss:123", "PL")
        self.check("host.example.com:8080:user:p@ss:123", None, "host.example.com", 8080, "user", "p@ss:123")

    def test_user_pass_at_host_still_works(self):
        self.check("user:pass@host.example.com:8080", None, "host.example.com", 8080, "user", "pass")
        self.check("user:1234@1.2.3.4:8080", None, "1.2.3.4", 8080, "user", "1234")
        self.check("user:1234:foo:bar@1.2.3.4:8080", None, "1.2.3.4", 8080, "user", "1234:foo:bar")
        self.check("us.er:p@ss:1@1.2.3.4:8080", None, "1.2.3.4", 8080, "us.er", "p@ss:1")
        self.check("user:pass@proxyhost:8080", None, "proxyhost", 8080, "user", "pass")

    def test_url_form_unchanged(self):
        self.check("socks5://user:p%40ss@1.2.3.4:1080", "socks5", "1.2.3.4", 1080, "user", "p@ss")
        self.check("http://1.2.3.4:8080@myuser:1234", "http", "1.2.3.4", 8080, "myuser", "1234")

    def test_socks_scheme_words(self):
        self.check("socks4 1.2.3.4:1080:u:p", "socks4", "1.2.3.4", 1080, "u", "p")
        self.check("socks4a 1.2.3.4:1080:u:p", "socks4", "1.2.3.4", 1080, "u", "p")
        self.check("socks5h 1.2.3.4:1080:u:p", "socks5", "1.2.3.4", 1080, "u", "p")
        self.check("1.2.3.4:1080:u:p socks4", "socks4", "1.2.3.4", 1080, "u", "p")
        self.check("socks4://u:p@1.2.3.4:1080", "socks4", "1.2.3.4", 1080, "u", "p")
        self.check("socks4a://u:p@1.2.3.4:1080", "socks4", "1.2.3.4", 1080, "u", "p")

    def test_four_tokens_is_never_a_country(self):
        self.check("1.2.3.4 8080 user id", None, "1.2.3.4", 8080, "user", "id")
        self.check("1.2.3.4 8080 user in", None, "1.2.3.4", 8080, "user", "in")
        self.check("1.2.3.4 8080 user pass", None, "1.2.3.4", 8080, "user", "pass")
        self.check("socks5 1.2.3.4 8080 user no", "socks5", "1.2.3.4", 8080, "user", "no")
        self.check("1.2.3.4 8080 user id http", "http", "1.2.3.4", 8080, "user", "id")

    def test_country_with_enough_tokens(self):
        self.check("1.2.3.4 8080 user pass PL", None, "1.2.3.4", 8080, "user", "pass", "PL")
        self.check("1.2.3.4 8080 user pass United States", None, "1.2.3.4", 8080, "user", "pass", "US")
        self.check("1.2.3.4 8080 PL", None, "1.2.3.4", 8080, None, None, "PL")
        self.check("1.2.3.4:8080 user pass DE", None, "1.2.3.4", 8080, "user", "pass", "DE")
        self.check("1.2.3.4:8080:user:pass Польша", None, "1.2.3.4", 8080, "user", "pass", "PL")

    def test_host_and_port_only_with_space(self):
        self.check("1.2.3.4 8080", None, "1.2.3.4", 8080, None, None)
        self.check("proxy.example.com\t8080", None, "proxy.example.com", 8080, None, None)
        self.check("socks5 1.2.3.4 1080", "socks5", "1.2.3.4", 1080, None, None)

    def test_bom_comment_is_skipped(self):
        self.assertEqual(parse_proxy_paste("\ufeff # comment"), [])
        self.assertEqual(parse_proxy_paste("\ufeff#comment\n \n\ufeff\n"), [])
        lines = parse_proxy_paste("\ufeff1.2.3.4:8080:u:p")
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].ok)

    def test_errors_never_hold_the_password(self):
        for line in ("1.2.3.4:99999@SeCrEt:SeCrEt9", "ftp://u:SeCrEt9@1.2.3.4:21", "a b c d e f SeCrEt9 g"):
            with self.subTest(line=line):
                with self.assertRaises(ProxyPasteError) as ctx:
                    parse_proxy_line(line)
                self.assertNotIn("SeCrEt", str(ctx.exception))
                self.assertNotIn("SeCrEt", repr(ctx.exception))


if __name__ == "__main__":
    unittest.main()
