"""Герметичность сети: ensure_network_ready, точка выхода (страна/таймзона/координаты), гео-замок профиля (без сети)."""
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfoNotFoundError

from loguru import logger

from config import BrowserSettings, TargetSettings
from modules import geo_guard
from modules.geo_guard import (
    ExitGeo,
    GeoLockError,
    NetworkGuardError,
    check_geo_lock,
    ensure_network_ready,
    read_session_geo,
    resolve_exit_country,
    resolve_exit_geo,
    resolve_host_ip,
    session_geo_path,
    write_session_geo,
)

PROXY = "http://user:SECRETPW@203.0.113.5:8080"
HOST_IP = "198.51.100.7"
PROXY_IP = "203.0.113.5"
GEO = ExitGeo(PROXY_IP, "US", "America/Chicago", 41.85, -87.65)
AIO_PATHS = {"iso_code": "country.iso_code", "longitude": "location.longitude",
             "latitude": "location.latitude", "timezone": "location.time_zone"}


def _clear_caches() -> None:
    geo_guard._EXIT_CACHE.clear()
    geo_guard._GEO_CACHE.clear()
    geo_guard._READY_CACHE.clear()
    geo_guard._HOST_IP_CACHE.clear()


@contextlib.contextmanager
def captured_logs():
    """Every loguru message of the block (level, text)."""
    records: list[tuple[str, str]] = []
    sink = logger.add(lambda message: records.append((message.record["level"].name, message.record["message"])), level="DEBUG")
    try:
        yield records
    finally:
        logger.remove(sink)


class _Case(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        _clear_caches()
        self.addCleanup(_clear_caches)


class TestEnsureNetworkReady(_Case):
    async def _ready(self, proxy=PROXY, *, geo=GEO, host_ip=HOST_IP, headers=None, echo_error=None, **kwargs):
        echo = MagicMock(side_effect=echo_error) if echo_error else MagicMock(return_value=headers if headers is not None else {})
        with patch("modules.geo_guard.resolve_exit_geo", AsyncMock(return_value=geo)) as resolve_geo, \
             patch("modules.geo_guard.resolve_host_ip", AsyncMock(return_value=host_ip)) as resolve_host, \
             patch("modules.geo_guard._fetch_echo_headers", echo):
            try:
                result = await ensure_network_ready(proxy, **kwargs)
            finally:
                self.calls = (resolve_geo, resolve_host, echo)
        return result

    async def test_no_proxy_checks_nothing_by_default(self):
        for proxy in (None, "", "   "):
            with self.subTest(proxy=proxy):
                self.assertIsNone(await self._ready(proxy))
                for call in self.calls:
                    call.assert_not_called()

    async def test_require_proxy_without_proxy_fails_before_any_network_call(self):
        for proxy in (None, "", "  "):
            with self.subTest(proxy=proxy), self.assertRaises(NetworkGuardError):
                await self._ready(proxy, require_proxy=True)
            for call in self.calls:
                call.assert_not_called()

    async def test_require_proxy_with_a_proxy_is_fine(self):
        self.assertEqual(await self._ready(require_proxy=True), GEO)

    async def test_malformed_proxy_is_refused_without_its_text(self):
        with captured_logs() as records, self.assertRaises(NetworkGuardError) as raised:
            await self._ready("not a proxy SECRETPW")
        self.assertNotIn("SECRETPW", str(raised.exception))
        self.assertFalse(any("SECRETPW" in text for _, text in records))
        for call in self.calls:
            call.assert_not_called()

    async def test_unreachable_proxy_fails_closed(self):
        with self.assertRaises(NetworkGuardError) as raised:
            await self._ready(geo=None)
        self.assertNotIn("SECRETPW", str(raised.exception))
        self.assertNotIn("203.0.113.5", str(raised.exception))
        self.calls[1].assert_not_called()  # no point comparing IPs of a proxy that does not answer

    async def test_proxy_exiting_from_the_host_ip_is_refused(self):
        with self.assertRaises(NetworkGuardError) as raised:
            await self._ready(host_ip=PROXY_IP)
        self.assertNotIn("SECRETPW", str(raised.exception))
        self.calls[2].assert_not_called()

    async def test_ipv6_notation_differences_still_match(self):
        geo = ExitGeo("2001:db8:0:0:0:0:0:1", "DE", None, None, None)
        with self.assertRaises(NetworkGuardError):
            await self._ready(geo=geo, host_ip="2001:DB8::1")

    async def test_forwarding_headers_with_the_host_ip_make_the_proxy_transparent(self):
        leaks = {
            "x-forwarded-for": "10.0.0.1, 198.51.100.7",
            "forwarded": 'for=198.51.100.7;proto=http',
            "via": "1.1 gateway (198.51.100.7)",
            "x-real-ip": "198.51.100.7",
            "client-ip": "198.51.100.7:51234",
        }
        for name, value in leaks.items():
            with self.subTest(header=name), captured_logs() as records, self.assertRaises(NetworkGuardError) as raised:
                await self._ready(headers={name: value, "host": "httpbin.org"})
            self.assertNotIn("SECRETPW", str(raised.exception))
            self.assertNotIn(HOST_IP, str(raised.exception))
            self.assertFalse(any(HOST_IP in text or "SECRETPW" in text for _, text in records))

    async def test_ipv6_host_ip_in_the_forwarded_header(self):
        with self.assertRaises(NetworkGuardError):
            await self._ready(host_ip="2001:db8::1", headers={"forwarded": 'for="[2001:db8::1]:4711";proto=http'})

    async def test_headers_without_the_host_ip_pass(self):
        headers = {"x-forwarded-for": "203.0.113.5", "via": "1.1 proxy", "x-real-ip": "198.51.100.70", "accept": "*/*"}
        self.assertEqual(await self._ready(headers=headers), GEO)
        self.calls[2].assert_called_once_with(PROXY)

    async def test_unknown_host_ip_warns_and_lets_the_session_through(self):
        with captured_logs() as records:
            self.assertEqual(await self._ready(host_ip=None), GEO)
        self.assertTrue(any(level == "WARNING" for level, _ in records))
        self.calls[2].assert_not_called()  # nothing to look for without the host IP

    async def test_failed_echo_request_warns_and_lets_the_session_through(self):
        with captured_logs() as records:
            self.assertEqual(await self._ready(echo_error=RuntimeError("curl: proxy http://u:SECRETPW@h:1 failed")), GEO)
        warnings = [text for level, text in records if level == "WARNING"]
        self.assertTrue(warnings)
        self.assertFalse(any("SECRETPW" in text for _, text in records))

    async def test_success_is_cached_per_proxy_and_failure_is_not(self):
        with patch("modules.geo_guard.resolve_exit_geo", AsyncMock(side_effect=[None, GEO, GEO])) as resolve_geo, \
             patch("modules.geo_guard.resolve_host_ip", AsyncMock(return_value=HOST_IP)), \
             patch("modules.geo_guard._fetch_echo_headers", MagicMock(return_value={})) as echo:
            with self.assertRaises(NetworkGuardError):
                await ensure_network_ready(PROXY)
            self.assertEqual(await ensure_network_ready(PROXY), GEO)  # the failure left nothing behind
            self.assertEqual(await ensure_network_ready(PROXY), GEO)  # served from the cache
            self.assertEqual(resolve_geo.await_count, 2)
            self.assertEqual(echo.call_count, 1)
            await ensure_network_ready("http://other:pw@203.0.113.6:8080")  # another proxy is another key
            self.assertEqual(resolve_geo.await_count, 3)

    async def test_refusals_are_not_cached(self):
        with patch("modules.geo_guard.resolve_exit_geo", AsyncMock(return_value=GEO)), \
             patch("modules.geo_guard.resolve_host_ip", AsyncMock(side_effect=[PROXY_IP, HOST_IP])), \
             patch("modules.geo_guard._fetch_echo_headers", MagicMock(return_value={})):
            with self.assertRaises(NetworkGuardError):
                await ensure_network_ready(PROXY)
            self.assertEqual(await ensure_network_ready(PROXY), GEO)

    async def test_cache_expires(self):
        with patch("modules.geo_guard.resolve_exit_geo", AsyncMock(return_value=GEO)) as resolve_geo, \
             patch("modules.geo_guard.resolve_host_ip", AsyncMock(return_value=HOST_IP)), \
             patch("modules.geo_guard._fetch_echo_headers", MagicMock(return_value={})), \
             patch("modules.geo_guard._now", side_effect=[0.0, geo_guard.EXIT_COUNTRY_TTL + 1, geo_guard.EXIT_COUNTRY_TTL + 1]):
            await ensure_network_ready(PROXY)
            await ensure_network_ready(PROXY)
        self.assertEqual(resolve_geo.await_count, 2)

    async def test_no_network_call_blocks_the_event_loop(self):
        """The blocking lookups run through asyncio.to_thread: the loop stays responsive during a slow echo request."""
        import asyncio
        import time

        ticks = []

        async def ticker():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        with patch("modules.geo_guard.resolve_exit_geo", AsyncMock(return_value=GEO)), \
             patch("modules.geo_guard.resolve_host_ip", AsyncMock(return_value=HOST_IP)), \
             patch("modules.geo_guard._fetch_echo_headers", side_effect=lambda proxy: time.sleep(0.2) or {}):
            await ensure_network_ready(PROXY)
        task.cancel()
        self.assertGreater(len(ticks), 5)


class TestMentionsIp(unittest.TestCase):
    def test_exact_address_only(self):
        self.assertTrue(geo_guard._mentions_ip("10.0.0.1, 198.51.100.7", HOST_IP))
        self.assertFalse(geo_guard._mentions_ip("198.51.100.70, 198.51.100.", HOST_IP))
        self.assertFalse(geo_guard._mentions_ip("", HOST_IP))
        self.assertFalse(geo_guard._mentions_ip("for=unknown;proto=http", HOST_IP))


class TestHostIp(_Case):
    async def test_direct_lookup_is_cached_and_failure_is_not(self):
        with patch("modules.geo_guard._lookup_host_ip", side_effect=[RuntimeError("down"), HOST_IP]) as lookup:
            self.assertIsNone(await resolve_host_ip())
            self.assertEqual(await resolve_host_ip(), HOST_IP)
            self.assertEqual(await resolve_host_ip(), HOST_IP)
        self.assertEqual(lookup.call_count, 2)

    async def test_garbage_answer_is_unknown(self):
        with patch("modules.geo_guard._lookup_host_ip", return_value="<html>"):
            self.assertIsNone(await resolve_host_ip())

    async def test_cached_longer_than_a_proxy_exit(self):
        self.assertGreater(geo_guard.HOST_IP_TTL, geo_guard.EXIT_COUNTRY_TTL)

    def test_lookup_is_direct(self):
        public_ip = MagicMock(return_value=HOST_IP)
        with patch("camoufox.ip.public_ip", public_ip):
            self.assertEqual(geo_guard._lookup_host_ip(), HOST_IP)
        public_ip.assert_called_once_with(None)

    async def test_slow_lookup_times_out(self):
        import time

        with patch("modules.geo_guard._lookup_host_ip", side_effect=lambda: time.sleep(0.5) or HOST_IP):
            self.assertIsNone(await resolve_host_ip(timeout=0.05))


class TestExitGeoParsing(unittest.TestCase):
    def test_valid_record(self):
        record = {"country": {"iso_code": "us"}, "location": {"time_zone": "America/Chicago", "latitude": 41.85, "longitude": -87.65}}
        self.assertEqual(geo_guard._geo_from_record(PROXY_IP, record, AIO_PATHS), GEO)

    def test_flat_paths_of_the_deprecated_geolite_source(self):
        paths = {"iso_code": "country_code", "longitude": "longitude", "latitude": "latitude", "timezone": "timezone"}
        record = {"country_code": "DE", "timezone": "Europe/Berlin", "latitude": "52.5", "longitude": "13.4"}
        self.assertEqual(geo_guard._geo_from_record("203.0.113.9", record, paths), ExitGeo("203.0.113.9", "DE", "Europe/Berlin", 52.5, 13.4))

    def test_invalid_timezone_is_dropped(self):
        for bad in (None, "", "UTC", "Chicago", "America/", "/America/Chicago", "../etc/passwd", "America/Chi cago", 123, ["a/b"], {}):
            with self.subTest(zone=bad):
                record = {"country": {"iso_code": "US"}, "location": {"time_zone": bad}}
                self.assertIsNone(geo_guard._geo_from_record(PROXY_IP, record, AIO_PATHS).timezone)

    def test_timezone_unknown_to_the_database_is_dropped_but_kept_without_a_database(self):
        record = {"location": {"time_zone": "Mars/Olympus"}}
        with patch("modules.geo_guard.ZoneInfo", side_effect=ZoneInfoNotFoundError("Mars/Olympus")):
            with patch("modules.geo_guard._tz_database_available", return_value=True):
                self.assertIsNone(geo_guard._geo_from_record(PROXY_IP, record, AIO_PATHS).timezone)
            with patch("modules.geo_guard._tz_database_available", return_value=False):  # no tzdata on this machine
                self.assertEqual(geo_guard._geo_from_record(PROXY_IP, record, AIO_PATHS).timezone, "Mars/Olympus")

    def test_timezone_known_to_the_database_is_kept(self):
        with patch("modules.geo_guard.ZoneInfo", return_value=object()):
            self.assertEqual(geo_guard._valid_timezone(" Europe/Warsaw "), "Europe/Warsaw")
            self.assertEqual(geo_guard._valid_timezone("America/Argentina/Buenos_Aires"), "America/Argentina/Buenos_Aires")

    def test_out_of_range_or_garbage_coordinates_are_dropped_as_a_pair(self):
        cases = [(91, 10), (-90.5, 10), (10, 181), (10, -180.1), ("x", 10), (10, None), (float("nan"), 10),
                 (float("inf"), 10), (True, 10), (None, None)]
        for lat, lon in cases:
            with self.subTest(lat=lat, lon=lon):
                record = {"location": {"latitude": lat, "longitude": lon}}
                geo = geo_guard._geo_from_record(PROXY_IP, record, AIO_PATHS)
                self.assertEqual((geo.latitude, geo.longitude), (None, None))

    def test_boundary_coordinates_are_valid(self):
        geo = geo_guard._geo_from_record(PROXY_IP, {"location": {"latitude": -90, "longitude": 180}}, AIO_PATHS)
        self.assertEqual((geo.latitude, geo.longitude), (-90.0, 180.0))

    def test_unknown_country_and_empty_record(self):
        self.assertEqual(geo_guard._geo_from_record(PROXY_IP, None, AIO_PATHS), ExitGeo(PROXY_IP, None, None, None, None))
        self.assertIsNone(geo_guard._geo_from_record(PROXY_IP, {"country": {"iso_code": "ZZ"}}, AIO_PATHS).country)

    def test_missing_path_entries_are_tolerated(self):
        geo = geo_guard._geo_from_record(PROXY_IP, {"country": {"iso_code": "pl"}}, {"iso_code": "country.iso_code"})
        self.assertEqual(geo, ExitGeo(PROXY_IP, "PL", None, None, None))


class TestLookupExitGeo(unittest.TestCase):
    def _lookup(self, record, *, db_exists=True, paths=AIO_PATHS, proxy="socks5://u:p@1.2.3.4:1080"):
        public_ip = MagicMock(return_value=PROXY_IP)
        reader = MagicMock()
        reader.__enter__.return_value.get.return_value = record
        fake_db = MagicMock()
        fake_db.exists.return_value = db_exists
        with patch("camoufox.ip.public_ip", public_ip), \
             patch("camoufox.geolocation.load_geoip_config", return_value={"paths": paths}), \
             patch("camoufox.geolocation.get_mmdb_path", return_value=fake_db), \
             patch("maxminddb.open_database", return_value=reader):
            geo = geo_guard._lookup_exit_geo(proxy)
        public_ip.assert_called_once_with("socks5h://u:p@1.2.3.4:1080")
        return geo

    def test_reads_country_zone_and_coordinates_by_the_camoufox_config_paths(self):
        record = {"country": {"iso_code": "US"}, "location": {"time_zone": "America/Chicago", "latitude": 41.85, "longitude": -87.65}}
        self.assertEqual(self._lookup(record), GEO)

    def test_country_wrapper_still_returns_just_the_country(self):
        with patch("modules.geo_guard._lookup_exit_geo", return_value=GEO):
            self.assertEqual(geo_guard._lookup_exit_country(None), "US")
        with patch("modules.geo_guard._lookup_exit_geo", return_value=None):
            self.assertIsNone(geo_guard._lookup_exit_country(None))

    def test_missing_database_keeps_the_ip_but_not_the_geo(self):
        self.assertEqual(self._lookup({}, db_exists=False), ExitGeo(PROXY_IP, None, None, None, None))

    def test_unreadable_database_keeps_the_ip(self):
        public_ip = MagicMock(return_value=PROXY_IP)
        fake_db = MagicMock()
        fake_db.exists.return_value = True
        with patch("camoufox.ip.public_ip", public_ip), \
             patch("camoufox.geolocation.load_geoip_config", return_value={"paths": AIO_PATHS}), \
             patch("camoufox.geolocation.get_mmdb_path", return_value=fake_db), \
             patch("maxminddb.open_database", side_effect=ValueError("corrupt")):
            self.assertEqual(geo_guard._lookup_exit_geo(None), ExitGeo(PROXY_IP, None, None, None, None))

    def test_failing_ip_lookup_raises(self):
        with patch("camoufox.ip.public_ip", MagicMock(side_effect=RuntimeError("proxy down"))), \
             patch("camoufox.geolocation.load_geoip_config", return_value={"paths": AIO_PATHS}):
            with self.assertRaises(RuntimeError):
                geo_guard._lookup_exit_geo(None)


class TestResolveExitGeo(_Case):
    async def test_caches_a_located_exit_and_feeds_the_country_cache(self):
        with patch("modules.geo_guard._lookup_exit_geo", return_value=GEO) as lookup, \
             patch("modules.geo_guard._lookup_exit_country") as country_lookup:
            first = await resolve_exit_geo(PROXY)
            second = await resolve_exit_geo(PROXY)
            country = await resolve_exit_country(PROXY)
        self.assertEqual((first, second, country), (GEO, GEO, "US"))
        lookup.assert_called_once()
        country_lookup.assert_not_called()

    async def test_failure_and_timeout_are_none_and_not_cached(self):
        import time

        with patch("modules.geo_guard._lookup_exit_geo", side_effect=RuntimeError("socks5h://u:SECRETPW@h:1")), \
             captured_logs() as records:
            self.assertIsNone(await resolve_exit_geo(PROXY))
        self.assertFalse(any("SECRETPW" in text for _, text in records))
        with patch("modules.geo_guard._lookup_exit_geo", side_effect=lambda proxy: time.sleep(0.5) or GEO):
            self.assertIsNone(await resolve_exit_geo(PROXY, timeout=0.05))
        with patch("modules.geo_guard._lookup_exit_geo", return_value=GEO) as lookup:
            self.assertEqual(await resolve_exit_geo(PROXY), GEO)
        lookup.assert_called_once()

    async def test_an_exit_without_geo_is_returned_but_not_cached(self):
        bare = ExitGeo(PROXY_IP, None, None, None, None)
        with patch("modules.geo_guard._lookup_exit_geo", return_value=bare) as lookup:
            self.assertEqual(await resolve_exit_geo(PROXY), bare)
            self.assertEqual(await resolve_exit_geo(PROXY), bare)
        self.assertEqual(lookup.call_count, 2)  # the database may be downloaded a minute later


class TestSessionGeoFile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "profile"

    def test_path(self):
        self.assertEqual(session_geo_path(self.dir), self.dir / "session_geo.json")

    def test_write_then_read_round_trip_without_secrets(self):
        write_session_geo(self.dir, "pl", "Europe/Warsaw")
        data = read_session_geo(self.dir)
        self.assertEqual(set(data), {"country", "timezone", "updated_at"})
        self.assertEqual((data["country"], data["timezone"]), ("PL", "Europe/Warsaw"))
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["session_geo.json"])  # no tmp file left

    def test_overwrite_replaces_the_record(self):
        write_session_geo(self.dir, "PL", "Europe/Warsaw")
        write_session_geo(self.dir, "DE", None)
        data = read_session_geo(self.dir)
        self.assertEqual((data["country"], data["timezone"]), ("DE", None))

    def test_nothing_is_written_when_everything_is_unknown(self):
        write_session_geo(self.dir, None, None)
        self.assertFalse(session_geo_path(self.dir).exists())

    def test_failed_replace_keeps_the_old_file_and_leaves_no_tmp(self):
        write_session_geo(self.dir, "PL", "Europe/Warsaw")
        with patch("modules.geo_guard.os.replace", side_effect=OSError("locked")), captured_logs() as records:
            write_session_geo(self.dir, "DE", "Europe/Berlin")  # never raises
        self.assertEqual(read_session_geo(self.dir)["country"], "PL")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["session_geo.json"])
        self.assertTrue(any(level == "WARNING" for level, _ in records))

    def test_write_goes_through_a_tmp_file_and_os_replace(self):
        with patch("modules.geo_guard.os.replace", wraps=geo_guard.os.replace) as replace:
            write_session_geo(self.dir, "PL", None)
        (source, target), _ = replace.call_args
        self.assertNotEqual(Path(source), Path(target))
        self.assertEqual(Path(target), session_geo_path(self.dir))

    def test_read_of_missing_broken_or_non_object_is_none(self):
        self.assertIsNone(read_session_geo(self.dir))
        self.dir.mkdir(parents=True)
        for text in ("{broken", "", "[1, 2]", "null", "\"PL\""):
            with self.subTest(text=text):
                session_geo_path(self.dir).write_text(text, encoding="utf-8")
                self.assertIsNone(read_session_geo(self.dir))


class TestCheckGeoLock(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "profile"

    def _saved(self, country, timezone="Europe/Warsaw"):
        self.dir.mkdir(parents=True, exist_ok=True)
        session_geo_path(self.dir).write_text(json.dumps({"country": country, "timezone": timezone}), encoding="utf-8")

    def test_no_file_is_new_and_writes_nothing(self):
        for current in ("PL", None):
            with self.subTest(current=current):
                self.assertEqual(check_geo_lock(self.dir, current), "new")
        self.assertFalse(self.dir.exists())

    def test_same_country_is_ok_whatever_the_case_or_spelling(self):
        self._saved("PL")
        for current in ("PL", "pl", "Poland"):
            with self.subTest(current=current):
                self.assertEqual(check_geo_lock(self.dir, current), "ok")

    def test_unknown_current_or_saved_country(self):
        self._saved("PL")
        self.assertEqual(check_geo_lock(self.dir, None), "unknown")
        self.assertEqual(check_geo_lock(self.dir, "Atlantis"), "unknown")
        for saved in (None, "", "ZZ"):
            with self.subTest(saved=saved):
                self._saved(saved)
                self.assertEqual(check_geo_lock(self.dir, "PL"), "unknown")

    def test_broken_file_is_unknown_not_an_error(self):
        self.dir.mkdir(parents=True)
        session_geo_path(self.dir).write_text("{not json", encoding="utf-8")
        with captured_logs() as records:
            self.assertEqual(check_geo_lock(self.dir, "PL", mode="block"), "unknown")
        self.assertFalse(any(level == "CRITICAL" for level, _ in records))

    def test_changed_country_warns_with_a_critical_log_naming_both_countries(self):
        self._saved("PL")
        with captured_logs() as records:
            self.assertEqual(check_geo_lock(self.dir, "US"), "changed")
        critical = [text for level, text in records if level == "CRITICAL"]
        self.assertEqual(len(critical), 1)
        self.assertIn("PL", critical[0])
        self.assertIn("US", critical[0])
        self.assertIn("кража сессии", critical[0])
        self.assertEqual(read_session_geo(self.dir)["country"], "PL")  # the check never rewrites the record

    def test_changed_country_in_block_mode_raises_with_the_same_text(self):
        self._saved("PL")
        with captured_logs() as records, self.assertRaises(GeoLockError) as raised:
            check_geo_lock(self.dir, "us", mode="block")
        critical = [text for level, text in records if level == "CRITICAL"]
        self.assertEqual(critical, [str(raised.exception)])

    def test_block_mode_lets_the_same_country_and_new_profiles_through(self):
        self.assertEqual(check_geo_lock(self.dir, "PL", mode="block"), "new")
        self._saved("PL")
        self.assertEqual(check_geo_lock(self.dir, "PL", mode="block"), "ok")


class TestConfigFields(unittest.TestCase):
    def _browser(self, **extra):
        target = TargetSettings(name="youtube", base_url="https://www.youtube.com", submit_input_selector="i",
                                submit_button_selector="b", confirmation_selector="c")
        return BrowserSettings(targets=[target], **extra)

    def test_defaults_keep_the_old_behaviour(self):
        browser = self._browser()
        self.assertFalse(browser.require_proxy)
        self.assertEqual(browser.geo_lock, "warn")

    def test_values_are_accepted_and_validated(self):
        browser = self._browser(require_proxy=True, geo_lock="block")
        self.assertTrue(browser.require_proxy)
        self.assertEqual(browser.geo_lock, "block")
        with self.assertRaises(ValueError):
            self._browser(geo_lock="strict")


if __name__ == "__main__":
    unittest.main()
