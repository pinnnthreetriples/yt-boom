"""Страна выхода прокси и ожидаемая страна аккаунта: нормализация, сверка, кэш (без сети)."""
import unittest
from unittest.mock import MagicMock, patch

from modules import geo_guard
from modules.geo_guard import (
    check_account_exit,
    check_expected_country,
    normalize_country,
    normalize_locale,
    resolve_exit_country,
)


class TestNormalizeCountry(unittest.TestCase):
    def test_iso_names_and_aliases(self):
        cases = {
            "PL": "PL", "pl": "PL", " Pl ": "PL",
            "Poland": "PL", "poland": "PL", "POLAND": "PL",
            "United States": "US", "United Kingdom": "GB", "Russia": "RU", "Russian Federation": "RU",
            "South Korea": "KR", "The Netherlands": "NL", "Czech Republic": "CZ", "Czechia": "CZ",
            "Польша": "PL", "россия": "RU", "США": "US", "Беларусь": "BY", "Белоруссия": "BY",
        }
        for given, expected in cases.items():
            with self.subTest(given=given):
                self.assertEqual(normalize_country(given), expected)

    def test_unknown_is_none_never_a_guess(self):
        for given in (None, "", "   ", "Atlantis", "ZZ", "XX", "EU", "UN", "P", "POL1", "Country version", "12"):
            with self.subTest(given=given):
                self.assertIsNone(normalize_country(given))


class TestNormalizeLocale(unittest.TestCase):
    def test_valid_forms_are_canonicalised(self):
        for given, expected in {"pl-PL": "pl-PL", "pl-pl": "pl-PL", "PL_pl": "pl-PL", " ru-RU ": "ru-RU", "en-GB": "en-GB"}.items():
            with self.subTest(given=given):
                self.assertEqual(normalize_locale(given), expected)

    def test_garbage_is_none(self):
        for given in (None, "", "pl", "polish", "xx-YY", "pl-ZZ", "pl-PL-x", "en US", "pl-"):
            with self.subTest(given=given):
                self.assertIsNone(normalize_locale(given))


class TestCheckExpectedCountry(unittest.TestCase):
    def test_matrix(self):
        self.assertEqual(check_expected_country(None, "PL"), "skipped")
        self.assertEqual(check_expected_country(None, None), "skipped")
        self.assertEqual(check_expected_country("", "PL"), "skipped")
        self.assertEqual(check_expected_country("PL", "PL"), "ok")
        self.assertEqual(check_expected_country("PL", "DE"), "mismatch")
        self.assertEqual(check_expected_country("PL", None), "unknown")


class TestResolveExitCountry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        geo_guard._EXIT_CACHE.clear()

    async def test_looks_up_through_the_proxy_with_remote_dns_and_caches(self):
        with patch("modules.geo_guard._lookup_exit_country", return_value="PL") as lookup:
            first = await resolve_exit_country("socks5://u:p@1.2.3.4:1080")
            second = await resolve_exit_country("socks5://u:p@1.2.3.4:1080")
            other = await resolve_exit_country(None)
        self.assertEqual((first, second, other), ("PL", "PL", "PL"))
        self.assertEqual(lookup.call_count, 2)  # one per distinct proxy; the repeat is served from the cache

    async def test_cache_expires(self):
        with patch("modules.geo_guard._lookup_exit_country", return_value="PL") as lookup, \
             patch("modules.geo_guard._now", side_effect=[0.0, geo_guard.EXIT_COUNTRY_TTL + 1, geo_guard.EXIT_COUNTRY_TTL + 1]):
            await resolve_exit_country(None)
            await resolve_exit_country(None)
        self.assertEqual(lookup.call_count, 2)

    async def test_failure_and_timeout_are_unknown_and_not_cached(self):
        with patch("modules.geo_guard._lookup_exit_country", side_effect=RuntimeError("socks5h://u:SECRETPW@h:1")):
            self.assertIsNone(await resolve_exit_country("socks5://u:p@1.2.3.4:1080"))
        with patch("modules.geo_guard._lookup_exit_country", return_value=None):
            self.assertIsNone(await resolve_exit_country("socks5://u:p@1.2.3.4:1080"))
        with patch("modules.geo_guard._lookup_exit_country", return_value="DE") as lookup:
            self.assertEqual(await resolve_exit_country("socks5://u:p@1.2.3.4:1080"), "DE")
        lookup.assert_called_once()

    async def test_slow_lookup_times_out(self):
        import time

        with patch("modules.geo_guard._lookup_exit_country", side_effect=lambda proxy: time.sleep(0.5) or "PL"):
            self.assertIsNone(await resolve_exit_country(None, timeout=0.05))

    def test_lookup_uses_camoufox_public_ip_with_the_socks5h_proxy_and_its_geoip_database(self):
        public_ip = MagicMock(return_value="203.0.113.5")
        reader = MagicMock()
        reader.__enter__.return_value.get.return_value = {"country": {"iso_code": "pl"}}
        fake_db = MagicMock()
        fake_db.exists.return_value = True
        with patch("camoufox.ip.public_ip", public_ip), \
             patch("camoufox.geolocation.load_geoip_config", return_value={"paths": {"iso_code": "country.iso_code"}}), \
             patch("camoufox.geolocation.get_mmdb_path", return_value=fake_db), \
             patch("maxminddb.open_database", return_value=reader):
            self.assertEqual(geo_guard._lookup_exit_country("socks5://u:p@1.2.3.4:1080"), "PL")
        public_ip.assert_called_once_with("socks5h://u:p@1.2.3.4:1080")

    def test_lookup_without_geoip_database_is_unknown(self):
        fake_db = MagicMock()
        fake_db.exists.return_value = False
        with patch("camoufox.ip.public_ip", MagicMock(return_value="203.0.113.5")), \
             patch("camoufox.geolocation.load_geoip_config", return_value={"paths": {"iso_code": "country.iso_code"}}), \
             patch("camoufox.geolocation.get_mmdb_path", return_value=fake_db):
            self.assertIsNone(geo_guard._lookup_exit_country(None))


class TestCheckAccountExit(unittest.IsolatedAsyncioTestCase):
    async def test_no_expected_country_makes_no_lookup(self):
        with patch("modules.geo_guard.resolve_exit_country") as resolve:
            self.assertEqual(await check_account_exit(None, "socks5://h:1"), ("skipped", None))
        resolve.assert_not_called()

    async def test_expected_country_is_compared_with_the_exit(self):
        for exit_country, verdict in (("PL", "ok"), ("DE", "mismatch"), (None, "unknown")):
            with self.subTest(exit=exit_country), \
                 patch("modules.geo_guard.resolve_exit_country", return_value=exit_country) as resolve:
                self.assertEqual(await check_account_exit("PL", "socks5://h:1"), (verdict, exit_country))
                resolve.assert_awaited_once_with("socks5://h:1")


if __name__ == "__main__":
    unittest.main()
