"""Proxy library: address assembly, paste parsing, DB API, migration, fail-closed resolution, role split, checks."""

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

import secret_store
from config import (
    PROXY_LIBRARY_MARKER,
    AccountSettings,
    ProxyParts,
    ProxyUnavailableError,
    compose_proxy_url,
    parse_proxy,
    resolve_account_proxy,
    split_proxy,
)
from database import Database, RecordNotFoundError
from modules import proxy_check
from modules.proxy_check import ProxyCheckResult, browser_proxy_warning, check_proxies, check_proxy, classify_curl_error
from modules.proxy_library import describe_proxy_routes, migrate_config_proxies, sync_config_accounts
from modules.proxy_paste import parse_proxy_line, parse_proxy_paste
from modules.session_heartbeat import curl_proxy_url

TRICKY_PASSWORDS = ("p@ss", "a:b:c", "sl/ash", "100%", "x@y:z/%41", "pässwörd")


def _fake_blob_call(function_name: str, data: bytes) -> bytes:
    """Reversible stand-in for DPAPI (an SSH session may have no logon key): the real prefix logic still runs."""
    return bytes(b ^ 0x5A for b in data)


class FakeDpapi:
    def setUp(self) -> None:  # noqa: N802 - unittest hook
        patcher = patch.object(secret_store, "_blob_call", _fake_blob_call)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()


class TestAddressAssembly(unittest.TestCase):
    def test_special_characters_survive_parse_proxy_and_curl(self):
        for password in TRICKY_PASSWORDS:
            for login in ("user", "us@er:1"):
                with self.subTest(login=login, password=password):
                    url = compose_proxy_url("http", "1.2.3.4", 8080, login, password)
                    parsed = parse_proxy(url)
                    self.assertEqual((parsed["username"], parsed["password"]), (login, password))
                    self.assertEqual(parsed["server"], "http://1.2.3.4:8080")
                    self.assertEqual(split_proxy(url), ProxyParts("http", "1.2.3.4", 8080, login, password))
                    self.assertEqual(parse_proxy(curl_proxy_url(url))["password"], password)

    def test_ipv6_socks_and_missing_scheme(self):
        self.assertEqual(compose_proxy_url("socks5", "2001:db8::1", 1080), "socks5://[2001:db8::1]:1080")
        self.assertTrue(curl_proxy_url(compose_proxy_url("socks5", "h", 1, "u", "p")).startswith("socks5h://"))
        self.assertEqual(compose_proxy_url(None, "h.example", 80), "http://h.example:80")
        self.assertIsNone(split_proxy("1.2.3.4:80:u:p").scheme)  # no type in the text: the check finds it
        self.assertEqual(split_proxy("socks5h://u:p@H.example:1").scheme, "socks5")
        self.assertEqual(split_proxy("socks5h://u:p@H.example:1").host, "h.example")

    def test_repr_hides_the_password(self):
        self.assertNotIn("secret", repr(ProxyParts("http", "h", 1, "u", "secret")))

    def test_marker_breaks_the_old_runtime(self):
        """An older build reads `accounts.proxy_url` as the proxy: parse_proxy (unchanged from master) must refuse
        the marker, so that build fails instead of going direct."""
        with self.assertRaises(ValueError):
            parse_proxy(PROXY_LIBRARY_MARKER)
        with self.assertRaises(ValueError):
            curl_proxy_url(PROXY_LIBRARY_MARKER)
        with self.assertRaises(ValueError):
            split_proxy(PROXY_LIBRARY_MARKER)


class TestPasteParser(unittest.TestCase):
    def check(self, line, scheme, host, port, login, password, country=None):
        draft = parse_proxy_line(line)
        self.assertEqual(
            (draft.scheme, draft.host, draft.port, draft.login, draft.password, draft.country),
            (scheme, host, port, login, password, country),
        )

    def test_formats(self):
        self.check("1.2.3.4:8080:login:pass", None, "1.2.3.4", 8080, "login", "pass")
        self.check("1.2.3.4:8080:login:pass:PL", None, "1.2.3.4", 8080, "login", "pass", "PL")
        self.check("1.2.3.4:8080:login:pass:Poland", None, "1.2.3.4", 8080, "login", "pass", "PL")
        self.check("login:pass@1.2.3.4:8080", None, "1.2.3.4", 8080, "login", "pass")
        self.check("1.2.3.4:8080@login:pass", None, "1.2.3.4", 8080, "login", "pass")
        self.check("socks5://login:pass@proxy.example.com:1080", "socks5", "proxy.example.com", 1080, "login", "pass")
        self.check("http://1.2.3.4:8080:login:pass", "http", "1.2.3.4", 8080, "login", "pass")
        self.check("1.2.3.4 8080 login pass", None, "1.2.3.4", 8080, "login", "pass")
        self.check("1.2.3.4\t8080\tlogin\tpass\tDE", None, "1.2.3.4", 8080, "login", "pass", "DE")
        self.check("1.2.3.4:8080 login pass United States", None, "1.2.3.4", 8080, "login", "pass", "US")
        self.check("1.2.3.4:8080:login:pass Польша", None, "1.2.3.4", 8080, "login", "pass", "PL")
        self.check("SOCKS5 1.2.3.4:1080:login:pass", "socks5", "1.2.3.4", 1080, "login", "pass")
        self.check("1.2.3.4:8080:login:pass http", "http", "1.2.3.4", 8080, "login", "pass")
        self.check("[2001:db8::1]:8080:login:pass", None, "2001:db8::1", 8080, "login", "pass")
        self.check("1.2.3.4:8080", None, "1.2.3.4", 8080, None, None)

    def test_passwords_with_colon_and_at(self):
        self.check("1.2.3.4:8080:login:pa:ss1", None, "1.2.3.4", 8080, "login", "pa:ss1")
        self.check("login:p@ss@1.2.3.4:8080", None, "1.2.3.4", 8080, "login", "p@ss")
        self.check("login:p:a@ss@1.2.3.4:8080", None, "1.2.3.4", 8080, "login", "p:a@ss")
        self.check("1.2.3.4:8080@login:p@ss", None, "1.2.3.4", 8080, "login", "p@ss")
        self.check("http://login:p%40ss%3A1@1.2.3.4:8080", "http", "1.2.3.4", 8080, "login", "p@ss:1")

    def test_block_with_errors_and_duplicates(self):
        text = "\n".join([
            "# my proxies",
            "1.2.3.4:8080:login:SeCrEt1",
            "",
            "1.2.3.4:99999:login:SeCrEt2",
            "ftp://1.2.3.4:21",
            "1.2.3.4:8080:login:other",
            "garbage SeCrEt3",
        ])
        lines = parse_proxy_paste(text)
        self.assertEqual([line.line_no for line in lines], [2, 4, 5, 6, 7])
        self.assertTrue(lines[0].ok)
        self.assertIn("Строка 4", lines[1].error)
        self.assertIn("порт", lines[1].error)
        self.assertIn("Строка 5", lines[2].error)
        self.assertEqual(lines[3].duplicate_of, 2)
        self.assertIn("Строка 7", lines[4].error)
        for line in lines:
            self.assertNotIn("SeCrEt", line.error or "")


class DbCase(FakeDpapi, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = Database(self.root / "t.db")
        await self.db.connect()
        await self.db.init_schema()

    async def asyncTearDown(self):
        await self.db.close()
        self.tmp.cleanup()

    def raw(self, query, params=()):
        con = sqlite3.connect(self.root / "t.db")
        try:
            return con.execute(query, params).fetchall()
        finally:
            con.close()

    async def accounts(self, *names):
        for name in names:
            await self.db.upsert_account(name, session_path=f"s/{name}")


class TestProxyLibraryDb(DbCase):
    async def test_add_again_updates_instead_of_duplicating(self):
        first, created = await self.db.add_proxy(ProxyParts(None, "1.2.3.4", 8080, "login", "old"), country="pl")
        again, created_again = await self.db.add_proxy(ProxyParts("socks5", "1.2.3.4", 8080, "login", "new"))
        other, _ = await self.db.add_proxy(ProxyParts(None, "1.2.3.4", 8080, "login2", "x"))
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(again.id, first.id)
        self.assertEqual((again.password, again.scheme, again.country), ("new", "socks5", "PL"))
        self.assertNotEqual(other.id, first.id)
        self.assertEqual(len(await self.db.list_proxies()), 2)

    async def test_password_is_encrypted_at_rest_and_never_in_accounts(self):
        await self.accounts("a")
        record, _ = await self.db.add_proxy(ProxyParts("http", "h.example", 3128, "u", "x@y:z/%41"))
        await self.db.assign_proxy(record.id, ["a"])
        (stored,), = self.raw("SELECT password FROM proxies")
        self.assertTrue(stored.startswith(secret_store.DPAPI_PREFIX))
        self.assertNotIn("x@y", stored)
        self.assertEqual(self.raw("SELECT proxy_url FROM accounts"), [(PROXY_LIBRARY_MARKER,)])
        account = await self.db.get_account("a")
        self.assertEqual(parse_proxy(account.proxy_url)["password"], "x@y:z/%41")

    async def test_assign_to_many_accounts_and_clear(self):
        await self.accounts("a", "b", "c", "d")
        for name in ("a", "b", "c", "d"):
            await self.db.set_account_auth_status((await self.db.get_account(name)).id, "authenticated")
        record, _ = await self.db.add_proxy(ProxyParts("http", "1.2.3.4", 8080, "u", "p"))
        self.assertEqual(await self.db.assign_proxy(record.id, ["a", "b", "c"]), ["a", "b", "c"])
        self.assertEqual(await self.db.assign_proxy(record.id, ["a", "b", "c", "d"]), ["d"])  # only real changes
        self.assertEqual((await self.db.get_proxy(record.id)).accounts_count, 4)
        accounts = await self.db.list_accounts()
        self.assertTrue(all(acc.proxy_id == record.id and acc.proxy_url for acc in accounts))
        self.assertTrue(all(acc.auth_status == "pending" for acc in accounts))
        self.assertEqual(await self.db.clear_account_proxy(["b"]), ["b"])
        self.assertEqual(await self.db.clear_account_proxy(["b", "c"]), ["c"])
        with self.assertRaises(RecordNotFoundError):
            await self.db.assign_proxy(record.id, ["nobody"])
        with self.assertRaises(RecordNotFoundError):
            await self.db.assign_proxy(999, ["a"])

    async def test_editing_the_proxy_keeps_the_sign_in_status(self):
        await self.accounts("a")
        record, _ = await self.db.add_proxy(ProxyParts("http", "1.2.3.4", 8080, "u", "old"))
        await self.db.assign_proxy(record.id, ["a"])
        account = await self.db.get_account("a")
        await self.db.mark_account_authenticated(account.id)
        await self.db.update_proxy(record.id, password="new", label="Польша-1")
        await self.db.add_proxy(ProxyParts(None, "1.2.3.4", 8080, "u", "newer"))  # re-pasted from the seller
        account = await self.db.get_account("a")
        self.assertEqual(account.auth_status, "authenticated")
        self.assertEqual(parse_proxy(account.proxy_url)["password"], "newer")
        self.assertEqual((await self.db.get_proxy(record.id)).label, "Польша-1")

    async def test_delete_detaches_accounts_and_the_search(self):
        await self.accounts("a", "b", "c")
        record, _ = await self.db.add_proxy(ProxyParts("http", "1.2.3.4", 8080, "u", "p"))
        await self.db.assign_proxy(record.id, ["a", "b"])
        await self.db.set_search_proxy(record.id)
        self.assertTrue((await self.db.get_proxy(record.id)).is_search)
        result = await self.db.delete_proxy(record.id)
        self.assertEqual(result.detached_accounts, ["a", "b"])
        self.assertTrue(result.was_search)
        self.assertIsNone(await self.db.search_proxy_url())
        for acc in await self.db.list_accounts():
            self.assertIsNone(acc.proxy_url)
            self.assertFalse(acc.proxy_unavailable)
        with self.assertRaises(RecordNotFoundError):
            await self.db.delete_proxy(record.id)

    async def test_upsert_account_with_proxy_string_uses_the_library(self):
        a = await self.db.upsert_account("a", session_path="s/a", proxy_url="1.2.3.4:8080:u:p")
        b = await self.db.upsert_account("b", session_path="s/b", proxy_url="http://u:p@1.2.3.4:8080")
        self.assertEqual(a.proxy_id, b.proxy_id)
        self.assertEqual(len(await self.db.list_proxies()), 1)
        await self.db.mark_account_authenticated(a.id)
        same = await self.db.upsert_account("a", proxy_url="1.2.3.4:8080:u:p")
        self.assertEqual(same.auth_status, "authenticated")  # same proxy: no re-login needed
        moved = await self.db.upsert_account("a", proxy_url="5.6.7.8:80")
        self.assertEqual(moved.auth_status, "pending")
        with self.assertRaises(ValueError):
            await self.db.upsert_account("a", proxy_url="not a proxy")

    async def test_record_check_fills_unknown_type_and_country(self):
        record, _ = await self.db.add_proxy(ProxyParts(None, "1.2.3.4", 8080, "u", "p"))
        checked = await self.db.record_proxy_check(record.id, True, scheme="socks5", exit_ip="9.9.9.9", exit_country="de", latency_ms=120)
        self.assertEqual((checked.scheme, checked.country, checked.country_source), ("socks5", "DE", "check"))
        self.assertTrue(checked.last_ok)
        user_set = await self.db.update_proxy(record.id, country="PL")
        self.assertEqual(user_set.country_source, "user")
        again = await self.db.record_proxy_check(record.id, False, error="таймаут")
        self.assertEqual((again.country, again.last_ok, again.last_error), ("PL", False, "таймаут"))


class TestFailClosed(DbCase):
    async def test_undecryptable_password(self):
        await self.accounts("a")
        record, _ = await self.db.add_proxy(ProxyParts("http", "1.2.3.4", 8080, "u", "p"))
        await self.db.assign_proxy(record.id, ["a"])
        await self.db.set_search_proxy(record.id)
        await self.db.connection.execute("UPDATE proxies SET password = 'dpapi:@@not-base64@@'")
        account = await self.db.get_account("a")
        self.assertTrue(account.proxy_unavailable)
        self.assertIsNone(account.proxy_url)
        with self.assertRaises(ProxyUnavailableError) as ctx:
            resolve_account_proxy(account)
        self.assertIn("«a»", str(ctx.exception))
        with self.assertRaises(ProxyUnavailableError):
            await self.db.search_proxy_url()
        self.assertTrue((await self.db.get_proxy(record.id)).password_unavailable)

    async def test_record_deleted_behind_the_api(self):
        await self.accounts("a")
        record, _ = await self.db.add_proxy(ProxyParts("http", "1.2.3.4", 8080))
        await self.db.assign_proxy(record.id, ["a"])
        await self.db.connection.execute("DELETE FROM proxies")  # FK sets proxy_id NULL, the marker stays
        account = await self.db.get_account("a")
        self.assertTrue(account.proxy_unavailable)
        with self.assertRaises(ProxyUnavailableError):
            resolve_account_proxy(account)

    def test_no_global_fallback(self):
        self.assertIsNone(resolve_account_proxy(MagicMock(proxy_url=None, proxy_unavailable=False)))
        self.assertEqual(resolve_account_proxy(MagicMock(proxy_url=" a:1 ", proxy_unavailable=False)), "a:1")
        with self.assertRaises(ProxyUnavailableError):
            resolve_account_proxy(MagicMock(proxy_url=PROXY_LIBRARY_MARKER, proxy_unavailable=False))


OLD_ACCOUNTS = """
CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, session_path TEXT NOT NULL,
    proxy_url TEXT, is_active INTEGER NOT NULL DEFAULT 1, total_replies INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, last_used_at TEXT, auth_status TEXT DEFAULT 'pending');
INSERT INTO accounts (name, session_path, proxy_url, created_at, auth_status) VALUES
    ('a', 's', 'http://u:p%40ss@1.2.3.4:8080', 'x', 'authenticated'),
    ('b', 's', '1.2.3.4:8080:u:p@ss', 'x', 'authenticated'),
    ('c', 's', 'socks5://5.6.7.8:1080', 'x', 'authenticated'),
    ('d', 's', 'not-a-proxy', 'x', 'authenticated'),
    ('e', 's', NULL, 'x', 'authenticated');
"""


class TestDatabaseMigration(FakeDpapi, unittest.IsolatedAsyncioTestCase):
    async def test_plain_proxies_move_into_the_library_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            con = sqlite3.connect(path)
            con.executescript(OLD_ACCOUNTS)
            con.commit()
            con.close()
            async with Database(path) as db:
                accounts = {acc.name: acc for acc in await db.list_accounts()}
                proxies = await db.list_proxies()
            self.assertEqual(len(proxies), 2)  # a and b are the same proxy
            self.assertEqual(accounts["a"].proxy_id, accounts["b"].proxy_id)
            self.assertEqual(parse_proxy(accounts["a"].proxy_url)["password"], "p@ss")
            self.assertEqual(parse_proxy(accounts["c"].proxy_url)["server"], "socks5://5.6.7.8:1080")
            self.assertTrue(accounts["d"].proxy_unavailable)  # never direct instead of the lost proxy
            self.assertIsNone(accounts["e"].proxy_url)
            self.assertFalse(accounts["e"].proxy_unavailable)
            self.assertTrue(all(acc.auth_status == "authenticated" for acc in accounts.values()))
            raw = sqlite3.connect(path)
            stored = [row[0] for row in raw.execute("SELECT proxy_url FROM accounts ORDER BY id")]
            raw.close()
            self.assertEqual(stored, [PROXY_LIBRARY_MARKER] * 4 + [None])
            backups = sorted(Path(tmp).glob("old.db.bak-proxies-*"))
            self.assertEqual(len(backups), 1)
            con = sqlite3.connect(backups[0])
            self.assertEqual(con.execute("SELECT proxy_url FROM accounts WHERE name='c'").fetchone()[0], "socks5://5.6.7.8:1080")
            con.close()
            async with Database(path) as db:  # second start: nothing to do, no new backup
                self.assertEqual(len(await db.list_proxies()), 2)
            self.assertEqual(len(list(Path(tmp).glob("old.db.bak-proxies-*"))), 1)

    async def test_no_dpapi_keeps_the_old_values_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            con = sqlite3.connect(path)
            con.executescript(OLD_ACCOUNTS)
            con.commit()
            con.close()
            with patch.object(secret_store, "_blob_call", side_effect=OSError("no logon session")):
                async with Database(path) as db:
                    account = await db.get_account("a")
                    self.assertIsNone(account.proxy_id)
                    self.assertEqual(account.proxy_url, "http://u:p%40ss@1.2.3.4:8080")
                    self.assertEqual(await db.list_proxies(), [])


EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "config.example.yaml"


class TestConfigMigration(DbCase):
    def write_config(self, proxy, accounts):
        data = yaml.safe_load(EXAMPLE_CONFIG.read_text(encoding="utf-8"))
        data["app"]["db_path"] = str(self.root / "t.db")
        data["app"]["log_file"] = str(self.root / "t.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["browser"]["proxy"] = proxy
        data["browser"]["accounts"] = accounts
        data.pop("accounts", None)
        path = self.root / "config.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return path

    async def test_yaml_sync_never_wipes_the_db(self):
        record, _ = await self.db.add_proxy(ProxyParts("http", "1.2.3.4", 8080, "u", "p"))
        await self.accounts("a", "b")
        await self.db.assign_proxy(record.id, ["a"])
        settings = [
            AccountSettings(name="a", session_path="s/a", proxy_url=None, is_active=False),  # `proxy_url: null`
            AccountSettings(name="b", session_path="s/b", proxy_url="9.9.9.9:99"),  # imported: b has none
            AccountSettings(name="c", session_path="s/c", is_active=False),  # missing: created as written
        ]
        self.assertEqual(await sync_config_accounts(self.db, settings), ["b"])
        await sync_config_accounts(self.db, settings)  # every start runs it again
        a, b, c = [await self.db.get_account(name) for name in "abc"]
        self.assertEqual(a.proxy_id, record.id)
        self.assertTrue(a.is_active)
        self.assertEqual(parse_proxy(b.proxy_url)["server"], "http://9.9.9.9:99")
        self.assertFalse(c.is_active)
        settings[1] = AccountSettings(name="b", session_path="s/b", proxy_url="7.7.7.7:77")
        await sync_config_accounts(self.db, settings)
        self.assertEqual(parse_proxy((await self.db.get_account("b")).proxy_url)["server"], "http://9.9.9.9:99")

    async def test_global_proxy_becomes_search_proxy_and_is_assigned_visibly(self):
        await self.accounts("own", "plain")
        await self.db.upsert_account("own", proxy_url="5.5.5.5:55")
        await self.db.set_account_auth_status((await self.db.get_account("plain")).id, "authenticated")
        path = self.write_config(
            "http://g:pw@1.1.1.1:3128",
            [
                {"name": "plain", "session_path": "s/plain", "proxy_url": None},
                {"name": "fresh", "session_path": "s/fresh", "proxy_url": "fu:fp@2.2.2.2:22"},
            ],
        )
        report = await migrate_config_proxies(path)
        search = await self.db.get_search_proxy()
        self.assertEqual((search.address, search.login, search.password), ("1.1.1.1:3128", "g", "pw"))
        self.assertEqual(report.imported_accounts, ["fresh"])
        self.assertEqual(sorted(report.assigned_global), ["plain"])
        plain = await self.db.get_account("plain")
        self.assertEqual(plain.proxy_id, search.id)
        self.assertEqual(plain.auth_status, "authenticated")  # same address as before: no new sign-in
        self.assertEqual(parse_proxy((await self.db.get_account("own")).proxy_url)["server"], "http://5.5.5.5:55")
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertIsNone(raw["browser"]["proxy"])
        self.assertTrue(all("proxy_url" not in item for item in raw["browser"]["accounts"]))
        self.assertTrue(report.config_backup.exists())
        self.assertIn("g:pw@1.1.1.1", report.config_backup.read_text(encoding="utf-8"))

        await self.accounts("later")  # added after the migration: stays direct even if browser.proxy reappears
        self.write_config("http://g:pw@1.1.1.1:3128", [{"name": "plain", "session_path": "s/plain"}])
        again = await migrate_config_proxies(path)
        self.assertEqual(again.assigned_global, [])
        self.assertIsNone((await self.db.get_account("later")).proxy_url)
        self.assertEqual(len(await self.db.list_proxies()), 3)


class TestRoles(DbCase):
    async def test_routes_text(self):
        await self.accounts("a", "b", "c")
        record, _ = await self.db.add_proxy(ProxyParts("http", "1.2.3.4", 8080, "u", "p"), country="PL")
        await self.db.assign_proxy(record.id, ["a"])
        await self.db.connection.execute(
            "UPDATE accounts SET proxy_url = ? WHERE name = 'c'", (PROXY_LIBRARY_MARKER,)
        )
        routes = await describe_proxy_routes(self.db)
        self.assertEqual(routes.search.kind, "direct")
        self.assertEqual(routes.search.text, "напрямую, с вашего IP")
        by_name = dict(routes.accounts)
        self.assertEqual(by_name["a"].text, "через прокси 1.2.3.4:8080 (PL)")
        self.assertEqual(by_name["b"].kind, "direct")
        self.assertEqual(by_name["c"].kind, "unavailable")
        await self.db.set_search_proxy(record.id)
        self.assertEqual((await describe_proxy_routes(self.db)).search.text, "через прокси 1.2.3.4:8080 (PL)")

    async def test_search_proxy_goes_to_the_collector_and_never_to_accounts(self):
        from test_multi_account_orchestrator import _make_test_config
        from modules.multi_account_orchestrator import MultiAccountOrchestrator

        search, _ = await self.db.add_proxy(ProxyParts("http", "1.1.1.1", 3128, "s", "sp"))
        own, _ = await self.db.add_proxy(ProxyParts("http", "2.2.2.2", 8080, "o", "op"))
        await self.db.set_search_proxy(search.id)
        await self.accounts("with_own", "without")
        await self.db.assign_proxy(own.id, ["with_own"])
        config = _make_test_config(self.root)
        config.browser.proxy = "http://9.9.9.9:9"  # a leftover global proxy is ignored at runtime
        collector = MagicMock(set_proxy=AsyncMock(), close=AsyncMock())
        with patch("modules.multi_account_orchestrator.YouTubeCollector", return_value=collector) as collector_cls:
            orchestrator = MultiAccountOrchestrator(config, db=self.db, processor=MagicMock())
        collector_cls.assert_called_once_with(config.collector, None)
        await orchestrator.ensure_db()
        collector.set_proxy.assert_awaited_once_with(search.url)
        with patch("modules.multi_account_orchestrator.YouTubeRunner") as runner_cls:
            orchestrator.create_runner(await self.db.get_account("with_own"))
            orchestrator.create_runner(await self.db.get_account("without"))
        proxies = [call.kwargs["proxy"] for call in runner_cls.call_args_list]
        self.assertEqual(proxies, [own.url, None])

    async def test_unreadable_account_is_skipped_by_the_pool(self):
        from test_multi_account_orchestrator import _make_test_config
        from modules.multi_account_orchestrator import MultiAccountOrchestrator

        await self.accounts("ok", "broken")
        await self.db.connection.execute("UPDATE accounts SET proxy_url = ? WHERE name = 'broken'", (PROXY_LIBRARY_MARKER,))
        orchestrator = MultiAccountOrchestrator(
            _make_test_config(self.root), db=self.db, processor=MagicMock(), collector=MagicMock(close=AsyncMock())
        )
        usable = await orchestrator._usable_accounts()
        self.assertEqual([acc.name for acc in usable], ["ok"])


class FakeCurlError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class TestProxyCheck(unittest.IsolatedAsyncioTestCase):
    def test_classify(self):
        cases = [
            (FakeCurlError("curl: (56) CONNECT tunnel failed, response 407", 56), "auth"),
            (FakeCurlError("curl: (97) User was rejected by the SOCKS5 server", 97), "auth"),
            (FakeCurlError("curl: (7) Failed to connect to 1.2.3.4 port 1: Connection refused", 7), "unreachable"),
            (FakeCurlError("curl: (5) Could not resolve proxy: nohost", 5), "unreachable"),
            (FakeCurlError("curl: (28) Operation timed out after 10001 ms", 28), "timeout"),
            (FakeCurlError("curl: (35) SSL connect error", 35), "other"),
        ]
        for exc, kind in cases:
            with self.subTest(kind=kind):
                self.assertEqual(classify_curl_error(exc)[0], kind)

    async def test_unknown_type_tries_http_then_socks5(self):
        calls = []

        async def fake_fetch(url, timeout):
            calls.append(url.split("://")[0])
            if url.startswith("http://"):
                raise proxy_check._FetchError("other", 52)
            return "9.9.9.9", 85

        with patch.object(proxy_check, "_fetch_ip", fake_fetch), patch.object(
            proxy_check, "country_for_ip", AsyncMock(return_value="PL")
        ):
            result = await check_proxy(ProxyParts(None, "1.2.3.4", 1080, "user", "SeCrEt@1"))
        self.assertEqual(calls, ["http", "socks5"])
        self.assertTrue(result.ok)
        self.assertEqual((result.scheme, result.exit_ip, result.exit_country, result.latency_ms), ("socks5", "9.9.9.9", "PL", 85))
        self.assertIn("SOCKS5", result.message)
        self.assertIsNotNone(result.browser_warning)  # SOCKS5 with a login: search only
        self.assertNotIn("SeCrEt", result.message + (result.browser_warning or ""))

    async def test_wrong_password_settles_http_and_says_so(self):
        async def fake_fetch(url, timeout):
            raise proxy_check._FetchError("auth", 56)

        with patch.object(proxy_check, "_fetch_ip", fake_fetch):
            result = await check_proxy("1.2.3.4:8080:user:SeCrEt")
        self.assertFalse(result.ok)
        self.assertEqual((result.error_kind, result.scheme), ("auth", "http"))
        self.assertIn("407", result.message)
        self.assertNotIn("SeCrEt", result.message)

    async def test_many_at_once_keeps_order_and_limit(self):
        running, peak = 0, 0

        async def fake_check(item, timeout):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
            return ProxyCheckResult(True, "http", item, None, 1, None, "ok")

        with patch.object(proxy_check, "check_proxy", fake_check):
            results = await check_proxies([f"ip{i}" for i in range(7)], concurrency=3)
        self.assertEqual([r.exit_ip for r in results], [f"ip{i}" for i in range(7)])
        self.assertLessEqual(peak, 3)

    async def test_direct(self):
        with patch.object(proxy_check, "_fetch_ip", AsyncMock(return_value=("8.8.4.4", 30))), patch.object(
            proxy_check, "country_for_ip", AsyncMock(return_value="RU")
        ):
            result = await proxy_check.check_direct()
        self.assertTrue(result.ok)
        self.assertIn("ваш IP 8.8.4.4", result.message)

    def test_browser_warning_only_for_socks_with_login(self):
        self.assertIsNotNone(browser_proxy_warning("socks5", "u"))
        self.assertIsNone(browser_proxy_warning("socks5", None))
        self.assertIsNone(browser_proxy_warning("http", "u"))


if __name__ == "__main__":
    unittest.main()
