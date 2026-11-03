"""Tab «Прокси» of the tabbed interface (modules/ui/tab_proxies.py), driven through `Controller.press`.

Network checks and DPAPI are mocked: `modules.proxy_check.check_proxy` / `check_direct` answer from a table by host,
`secret_store._blob_call` is a reversible stand-in. A `Run` step is executed by calling its work directly.
"""
from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import yaml

import secret_store
from config import load_config
from database import Database
from modules import proxy_screen
from modules.proxy_check import ProxyCheckResult, _as_parts, browser_proxy_warning
from modules.ui import tab_proxies
from modules.ui.app import AppContext
from modules.ui.shell import Confirm, Controller, Field, Menu, frags_text, render
from modules.wizard import _save_account


def ok(ip: str, country: str | None, ms: int = 140, scheme: str = "http") -> ProxyCheckResult:
    return ProxyCheckResult(True, scheme, ip, country, ms, None, f"Прокси работает: {ip}")


TIMEOUT = ProxyCheckResult(False, None, None, None, None, "timeout", "Прокси не ответил за 10 с")


class ProxyTabCase(unittest.TestCase):
    def setUp(self):
        for target, fake in (
            ("secret_store._blob_call", lambda name, data: bytes(b ^ 0x5A for b in data)),
            ("modules.proxy_check.check_proxy", self.fake_check),
            ("modules.proxy_check.check_direct", self.fake_direct),
            ("modules.wizard.check_youtube_session", lambda path: (Path(path).name in self.logged_in, "")),
            ("modules.ui.tab_proxies._geo_warning", lambda: None),
            ("sys.stdout", io.StringIO()),
        ):
            patcher = patch(target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "cfg.yaml"
        with open("config.yaml", "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["app"]["db_path"] = str(self.root / "t.db")
        data["app"]["log_file"] = str(self.root / "t.log")
        data["browser"]["session_root"] = str(self.root / "sessions")
        data["browser"]["proxy"] = None
        data["browser"]["accounts"] = []
        self.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.checks: dict[str, ProxyCheckResult] = {}
        self.logged_in: set[str] = set()
        proxy_screen._direct = None
        self.addCleanup(setattr, proxy_screen, "_direct", None)
        self.ctx = AppContext(self.config_path)
        self.addCleanup(tab_proxies._PENDING.clear)

    async def fake_check(self, item, timeout=10.0):
        parts = _as_parts(item)
        result = self.checks.get(parts.host, TIMEOUT)
        return replace(result, browser_warning=browser_proxy_warning(result.scheme, parts.login)) if result.ok else result

    async def fake_direct(self, timeout=10.0):
        return ok("85.1.2.3", "RU", 40, None)

    # ----------------------------------------------------------------- data

    def db(self, fn):
        async def run():
            async with Database(load_config(self.config_path).app.db_path) as db:
                return await fn(db)

        return asyncio.run(run())

    def accounts(self, *names: str) -> None:
        for name in names:
            _save_account(self.config_path, name, str(self.root / "sessions" / name), None)

    def add(self, url: str, check: ProxyCheckResult | None = None, country: str | None = None) -> int:
        async def go(db):
            record, _ = await db.add_proxy_url(url, country=country)
            if check is not None:
                await proxy_screen._record(db, record.id, check)
            return record.id

        return self.db(go)

    def proxy_of(self) -> dict[str, int | None]:
        return {acc.name: acc.proxy_id for acc in self.db(lambda db: db.list_accounts())}

    def account_id(self, name: str) -> int:
        return next(acc.id for acc in self.db(lambda db: db.list_accounts()) if acc.name == name)

    def proxies(self):
        return self.db(lambda db: db.list_proxies())

    # ----------------------------------------------------------------- shell

    def shell(self) -> Controller:
        return Controller([tab_proxies.build_tab(self.ctx)])

    def press(self, c: Controller, *keys: str) -> str:
        for key in keys:
            c.press(key)
        return frags_text(render(c))

    def run_pending(self, c: Controller) -> str:
        run = c.pending_run
        self.assertIsNotNone(run, "a run was expected")
        c.finish_run(run.work())
        return frags_text(render(c))

    def select(self, c: Controller, row_id: str) -> None:
        self.assertIn(row_id, [r.id for r in c.selectable_rows()])
        c.select(row_id)


class TestList(ProxyTabCase):
    def test_empty_state_says_everything_goes_from_the_own_ip(self):
        c = self.shell()
        text = frags_text(render(c))
        self.assertIn("прокси нет — всё идёт с вашего IP", text)
        self.assertIn("вставить строку прокси", text)
        self.assertTrue(c.warns[0])
        c.press("enter")
        self.assertIsInstance(c.edit.field, Field)
        self.assertTrue(c.edit.field.multiline)

    def test_row_hint_and_details_never_show_the_password(self):
        self.accounts("vonji")
        pid = self.add("http://user77:secret99@45.130.253.31:8000", ok("45.130.253.31", "PL"))
        self.db(lambda db: db.assign_proxy(pid, ["vonji"]))
        c = self.shell()
        text = frags_text(render(c))
        self.assertIn("PL", text)
        self.assertIn("45.130.253.31:8000", text)
        self.assertIn("На связи · 140 мс · Польша · аккаунтов 1", text)
        text = self.press(c, "space")
        self.assertIn("HTTP · логин user77 · пароль ••••", text)
        self.assertIn("выход 45.130.253.31 Польша", text)
        self.assertIn("vonji", text)
        self.assertNotIn("secret99", text)

    def test_failing_proxy_and_direct_search_mark_the_tab(self):
        self.accounts("vonji")
        pid = self.add("http://u:p@9.9.9.9:80", TIMEOUT)
        self.db(lambda db: db.assign_proxy(pid, ["vonji"]))
        self.db(lambda db: db.set_search_proxy(pid))
        c = self.shell()
        self.assertTrue(c.warns[0])  # the proxy failed its check
        self.assertIn("Нет связи · таймаут", frags_text(render(c)))

    def test_search_on_the_own_ip_is_shown_on_top(self):
        self.add("http://u:p@1.1.1.1:80", ok("1.1.1.1", "DE"))
        text = frags_text(render(self.shell()))
        self.assertIn("поиск видео — с вашего IP", text)


class TestAdd(ProxyTabCase):
    def test_paste_preview_reads_every_line_and_hides_the_password(self):
        c = self.shell()
        c.press("+")
        text = self.press(c, "paste:45.130.253.31:8000:user77:secret99:PL\nnot a proxy\n")
        self.assertIn("45.130.253.31:8000", text)
        self.assertIn("user77", text)
        self.assertIn("••••", text)
        self.assertIn("Строка 2", text)
        self.assertNotIn("secret99", frags_text(render(c)).replace("•", ""))

    def test_add_checks_saves_and_offers_the_accounts_without_a_proxy(self):
        self.accounts("vonji", "kiri")
        self.checks = {"45.130.253.31": ok("45.130.253.31", "PL"), "5.5.5.5": TIMEOUT}
        c = self.shell()
        c.press("+")
        c.press("paste:45.130.253.31:8000:user77:secret99:PL\n5.5.5.5:3128:a:b")
        c.press("enter")
        c.press("enter")
        text = self.run_pending(c)
        self.assertEqual([p.address for p in self.proxies()], ["45.130.253.31:8000"])
        self.assertIn("не работают 1 — не сохранены", text)
        self.assertIn("назначить новые прокси аккаунтам без прокси: 1", text)

        self.select(c, "pending-plan")
        c.press("enter")
        self.assertIsInstance(c.confirm, Confirm)
        self.press(c, "right", "enter")
        self.assertEqual(sum(1 for v in self.proxy_of().values() if v is not None), 1)

        self.select(c, "pending-failing")
        self.press(c, "enter", "right", "enter")
        self.assertEqual(len(self.proxies()), 2)
        self.assertNotIn("pending-failing", [r.id for r in c.view.rows])

    def test_a_country_that_differs_from_the_check_takes_the_exit_country(self):
        self.checks = {"45.130.253.31": ok("45.130.253.31", "DE")}
        c = self.shell()
        c.press("+")
        c.press("paste:45.130.253.31:8000:user77:secret99:PL")
        c.press("enter")
        c.press("enter")
        self.run_pending(c)
        record = self.proxies()[0]
        self.assertEqual(record.country, "DE")  # the seller's PL is dropped, the check's exit country stays
        self.assertEqual(record.last_exit_country, "DE")

    def test_nothing_parsed_is_an_error_without_a_run(self):
        c = self.shell()
        c.press("+")
        c.press("paste:hello world")
        c.press("enter")
        c.press("enter")
        self.assertIsNone(c.pending_run)
        self.assertEqual(c.notice.kind, "err")


class TestProxyActions(ProxyTabCase):
    def setUp(self):
        super().setUp()
        self.accounts("vonji", "kiri")
        self.pid = self.add("http://u:p@45.130.253.31:8000", ok("45.130.253.31", "PL"))
        self.db(lambda db: db.assign_proxy(self.pid, ["vonji"]))

    def test_enter_opens_the_actions(self):
        c = self.shell()
        c.press("enter")
        self.assertIsInstance(c.menu, Menu)
        text = frags_text(render(c))
        for words in ("аккаунты на этом прокси", "проверить", "изменить строку", "искать видео через него", "удалить"):
            self.assertIn(words, text)

    def test_accounts_on_a_proxy_switch_yes_no_at_once(self):
        c = self.shell()
        c.press("enter")
        c.press("enter")  # «аккаунты на этом прокси»
        self.assertIn("› аккаунты", frags_text(render(c)))
        self.select(c, f"a{self.account_id('kiri')}")
        c.press("right")
        self.assertEqual(self.proxy_of()["kiri"], self.pid)
        self.select(c, f"a{self.account_id('vonji')}")
        c.press("left")
        self.assertIsNone(self.proxy_of()["vonji"])
        self.select(c, "search")
        c.press("right")
        self.assertTrue(self.proxies()[0].is_search)
        self.assertEqual(c.notice.kind, "warn")  # accounts share the search IP

    def test_delete_asks_and_names_the_accounts_left_without_a_proxy(self):
        c = self.shell()
        c.press("enter")
        text = self.press(c, "up", "enter")  # the last action: «удалить»
        self.assertIn("vonji останутся без прокси", text)
        c.press("enter")  # «Нет» is selected first
        self.assertEqual(len(self.proxies()), 1)
        c.press("enter")
        c.press("up")
        c.press("enter")
        c.press("right")
        c.press("enter")
        self.assertEqual(self.proxies(), [])
        self.assertIn("теперь с вашего IP: vonji", c.notice.text)

    def test_check_runs_and_records(self):
        self.checks = {"45.130.253.31": ok("45.130.253.31", "PL", ms=99)}
        c = self.shell()
        c.press("enter")
        c.press("down")
        c.press("enter")
        text = self.run_pending(c)
        self.assertEqual(self.proxies()[0].last_latency_ms, 99)
        self.assertIn("99 мс", text)

    def test_edit_line_moves_the_accounts_to_the_new_address(self):
        self.checks = {"7.7.7.7": ok("7.7.7.7", "PL")}
        c = self.shell()
        c.press("enter")
        c.press("down")
        c.press("down")
        c.press("enter")
        self.assertIsNotNone(c.edit)
        c.press("paste:7.7.7.7:9000:u:p")
        c.press("enter")
        self.run_pending(c)
        records = self.proxies()
        self.assertEqual([r.address for r in records], ["7.7.7.7:9000"])
        self.assertEqual(self.proxy_of()["vonji"], records[0].id)

    def test_edit_line_that_fails_changes_nothing(self):
        c = self.shell()
        c.press("enter")
        c.press("down")
        c.press("down")
        c.press("enter")
        c.press("paste:8.8.8.8:9000:u:p")
        c.press("enter")
        self.run_pending(c)
        self.assertEqual([r.address for r in self.proxies()], ["45.130.253.31:8000"])
        self.assertEqual(c.notice.kind, "err")

    def test_check_all(self):
        self.checks = {"45.130.253.31": ok("45.130.253.31", "PL")}
        c = self.shell()
        c.press("C")
        text = self.run_pending(c)
        self.assertIn("работают 1", text)


class TestSearch(ProxyTabCase):
    def test_pick_a_free_proxy_then_back_to_the_own_ip(self):
        free = self.add("http://u:p@1.1.1.1:80", ok("1.1.1.1", "DE"))
        c = self.shell()
        c.press("S")
        self.assertIn("через что искать", frags_text(render(c)))
        self.select(c, f"p{free}")
        c.press("enter")
        self.assertTrue(self.proxies()[0].is_search)
        self.assertEqual(c._depth(), 0)
        self.assertIn("Поиск — через", c.notice.text)

        c.press("S")
        self.select(c, "direct")
        c.press("enter")
        self.assertFalse(self.proxies()[0].is_search)

    def test_a_proxy_with_accounts_asks_first(self):
        self.accounts("vonji")
        pid = self.add("http://u:p@1.1.1.1:80", ok("1.1.1.1", "DE"))
        self.db(lambda db: db.assign_proxy(pid, ["vonji"]))
        c = self.shell()
        c.press("S")
        self.select(c, f"p{pid}")
        c.press("enter")
        self.assertIsInstance(c.confirm, Confirm)
        c.press("enter")  # «Нет»
        self.assertFalse(self.proxies()[0].is_search)
        c.press("enter")
        c.press("right")
        c.press("enter")
        self.assertTrue(self.proxies()[0].is_search)


if __name__ == "__main__":
    unittest.main()
