"""Wizard stage 2 «Прокси» (modules/proxy_screen.py) and the «с вашего IP» lines outside it.

Network checks and DPAPI are mocked: `modules.proxy_check.check_proxy` / `check_direct` answer from a table by host,
`secret_store._blob_call` is a reversible stand-in.
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
from modules import proxy_screen, tui
from modules.proxy_check import ProxyCheckResult, _as_parts, browser_proxy_warning
from modules.wizard import WizardBack, _save_account, load_accounts, step_proxy
from tests.tui_keys import DOWN, ENTER, ESC, SPACE, UP, scripted_keys

SOCKS_WARNING = "SOCKS5 с логином и паролем подходит только для поиска"


def ok(ip: str, country: str | None, ms: int = 210, scheme: str = "http") -> ProxyCheckResult:
    return ProxyCheckResult(True, scheme, ip, country, ms, None, f"Прокси работает: {ip}")


def bad(kind: str, message: str) -> ProxyCheckResult:
    return ProxyCheckResult(False, None, None, None, None, kind, message)


TIMEOUT = bad("timeout", "Прокси не ответил за 10 с: сервер перегружен или недоступен")
AUTH = bad("auth", "Прокси отклонил логин или пароль (ошибка 407): проверьте их у продавца")


class ProxyScreenCase(unittest.TestCase):
    """A temp config + DB; `answers` feed prompt_user (plain menus read their choice through it)."""

    checks: dict[str, ProxyCheckResult] = {}

    def setUp(self):
        patcher = patch.object(secret_store, "_blob_call", lambda name, data: bytes(b ^ 0x5A for b in data))
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
        self.checks = {}
        self.logged_in: set[str] = set()
        proxy_screen._direct = None
        self.addCleanup(setattr, proxy_screen, "_direct", None)
        for target, fake in (
            ("modules.proxy_check.check_proxy", self.fake_check),
            ("modules.proxy_check.check_direct", self.fake_direct),
            ("modules.wizard.check_youtube_session", lambda path: (Path(path).name in self.logged_in, "")),
            ("modules.wizard.clear_terminal", lambda: None),
            ("modules.proxy_screen._geo_hint", lambda: None),
        ):
            patcher = patch(target, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

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

    def accounts(self, *names: str, expected: dict[str, str] | None = None) -> None:
        for name in names:
            _save_account(self.config_path, name, str(self.root / "sessions" / name), None)
        for name, country in (expected or {}).items():
            self.db(lambda db, n=name, c=country: db.connection.execute(
                "UPDATE accounts SET expected_country = ? WHERE name = ?", (c, n)))

    def add(self, url: str, ok_check: ProxyCheckResult | None = None, country: str | None = None) -> int:
        async def go(db):
            record, _ = await db.add_proxy_url(url, country=country)
            if ok_check is not None:
                await proxy_screen._record(db, record.id, ok_check)
            return record.id

        return self.db(go)

    def proxy_of(self) -> dict[str, int | None]:
        return {acc.name: acc.proxy_id for acc in self.db(lambda db: db.list_accounts())}

    def proxies(self):
        return self.db(lambda db: db.list_proxies())

    def state(self):
        return proxy_screen.load_state(self.config_path)

    # ----------------------------------------------------------------- running

    def plain(self, func, *answers: str):
        """Run func() without a console: every prompt_user returns the next answer. Returns (result, output)."""
        out = io.StringIO()
        with patch("modules.wizard.prompt_user", side_effect=list(answers)) as prompt, patch("sys.stdout", out):
            result = func()
        self.prompts = prompt.call_count
        return result, " ".join(out.getvalue().split())  # Rich wraps long lines at 70 columns

    def keys(self, func, *keys: str, answers: list[str] | None = None):
        out = io.StringIO()
        with scripted_keys(*keys) as screen, patch("modules.wizard.prompt_user", side_effect=answers or []), \
             patch("sys.stdout", out):
            try:
                result = func()
            except WizardBack as exc:
                result = exc
        return result, screen.getvalue() + out.getvalue()


class TestStageScreen(ProxyScreenCase):
    def test_empty_library_shows_direct_search_and_accounts_from_your_ip(self):
        self.accounts("alice")
        rows = proxy_screen.main_rows(self.state())
        by_key = {r[0]: r for r in rows if r}
        self.assertEqual(by_key["S"][2], "⚠ напрямую — с вашего IP")
        self.assertEqual(by_key["U"][2], "с прокси 0 из 1 · с вашего IP: alice")
        self.assertFalse(any(key.startswith("P") for key in by_key))
        _, output = self.keys(lambda: step_proxy(self.config_path), ESC)
        self.assertRegex(output, r"▸ .*Добавить прокси")  # nothing to choose yet: the cursor waits on «Добавить»

    def test_rows_of_working_failing_and_unreadable_proxies(self):
        self.accounts("alice", "bob")
        good = self.add("http://u:p@45.130.253.31:8000", ok("45.130.253.31", "PL"), country="PL")
        dead = self.add("socks5://91.12.4.7:1080", TIMEOUT)
        locked = self.add("http://u:p@5.5.5.5:3128")
        self.db(lambda db: db.assign_proxy(good, ["alice"]))
        self.db(lambda db: db.assign_proxy(locked, ["bob"]))
        self.db(lambda db: db.connection.execute(
            "UPDATE proxies SET password = 'dpapi:@@not-base64@@' WHERE id = ?", (locked,)))
        rows = {r[0]: r for r in proxy_screen.main_rows(self.state()) if r}
        self.assertEqual(rows[f"P{good}"][1], "[PL] 45.130.253.31:8000")
        self.assertRegex(rows[f"P{good}"][2], r"^HTTP +✓ 210 мс +1 акк\.$")
        self.assertRegex(rows[f"P{dead}"][2], r"^SOCKS5 +✗ таймаут +—$")
        self.assertEqual(rows[f"P{locked}"][2], "🔒 не расшифрован — вставьте заново")
        self.assertIn("🔒 не читается: bob", rows["U"][2])
        _, output = self.keys(lambda: step_proxy(self.config_path), ESC)
        for text in ("Поиск видео", "[PL] 45.130.253.31:8000", "✗ таймаут", "Проверить все", "Продолжить →"):
            self.assertIn(text, output)
        self.assertNotIn("u:p", output)  # never a password

    def test_continue_warns_once_about_accounts_without_a_working_proxy(self):
        self.accounts("alice", "bob")
        dead = self.add("http://9.9.9.9:80", TIMEOUT)
        self.db(lambda db: db.assign_proxy(dead, ["bob"]))
        _, output = self.plain(lambda: step_proxy(self.config_path), "1", "1")
        self.assertEqual(self.prompts, 2)
        self.assertIn("alice — без прокси, с вашего IP", output)
        self.assertIn("bob — прокси не прошёл проверку", output)
        self.assertIn("Ещё раз «Продолжить» — дальше", output)

    def test_continue_without_problems_and_back(self):
        self.accounts("alice")
        good = self.add("http://45.1.1.1:80", ok("45.1.1.1", "PL"))
        self.db(lambda db: db.assign_proxy(good, ["alice"]))
        self.plain(lambda: step_proxy(self.config_path), "1")
        self.assertEqual(self.prompts, 1)
        with self.assertRaises(WizardBack):
            self.plain(lambda: step_proxy(self.config_path), "0")

    def test_check_all_stores_results_and_shows_your_ip(self):
        self.accounts("alice")
        good = self.add("http://45.1.1.1:80")
        self.add("http://6.6.6.6:80")
        self.checks = {"45.1.1.1": ok("45.1.1.1", "PL")}
        _, output = self.plain(lambda: step_proxy(self.config_path), "4", "1", "1")
        self.assertIn("Проверено 2: работают 1, не работают 1 · ваш IP 85.1.2.3 (RU)", output)
        stored = {p.host: p for p in self.proxies()}
        self.assertTrue(stored["45.1.1.1"].last_ok)
        self.assertEqual(stored["45.1.1.1"].country, "PL")
        self.assertFalse(stored["6.6.6.6"].last_ok)
        self.assertIn("с вашего IP 85.1.2.3 (RU)", proxy_screen.search_value(self.state())[0])
        self.assertEqual(good, stored["45.1.1.1"].id)


class TestPaste(ProxyScreenCase):
    def paste(self, text: str, *answers: str):
        return self.plain(lambda: proxy_screen.add_proxies(self.config_path, self.state()),
                          *text.splitlines(), "", *answers)

    def test_lines_are_shown_checked_saved_and_offered_to_accounts_without_proxy(self):
        self.accounts("alice", "bob", "carol", expected={"bob": "DE"})
        self.checks = {
            "45.130.253.31": ok("45.130.253.31", "PL"),
            "91.12.4.7": ok("91.12.4.7", "DE", 300),
        }
        text = "45.130.253.31:8000:user1:secret1\nfoo bar\n91.12.4.7:1080:user2:secret2:DE\n45.130.253.31:8000:user1:secret1"
        notice, output = self.paste(text, "y", "y")
        self.assertIn("1. 45.130.253.31:8000 · логин user1 · пароль скрыт · тип ? · страна ?", output)
        self.assertIn("Строка 2:", output)
        self.assertIn("повтор строки 1 — пропустим", output)
        self.assertIn("✓ [PL] 45.130.253.31:8000 · HTTP · 210 мс · внешний IP 45.130.253.31", output)
        self.assertIn("Назначить новые прокси аккаунтам без прокси?", output)
        self.assertNotIn("secret1", output)
        self.assertEqual(notice[0], "warn")  # one line was not a proxy
        self.assertIn("Сохранено 2: работают 2 · строк с ошибкой: 1 · назначено аккаунтам: 2", notice[1])
        stored = {p.host: p for p in self.proxies()}
        self.assertEqual(stored["45.130.253.31"].scheme, "http")  # the type found by the check
        self.assertEqual(stored["45.130.253.31"].password, "secret1")
        self.assertEqual((stored["91.12.4.7"].country, stored["91.12.4.7"].country_source), ("DE", "user"))
        by_name = self.proxy_of()
        self.assertEqual(by_name["bob"], stored["91.12.4.7"].id)  # bob expects DE: the German proxy
        self.assertEqual(by_name["alice"], stored["45.130.253.31"].id)
        self.assertIsNone(by_name["carol"])  # one proxy per account, two proxies

    def test_ambiguous_country_is_shown_before_saving_and_can_be_cancelled(self):
        notice, output = self.paste("1.2.3.4:8080:user:pa:PL", "n")
        self.assertIn("страна PL", output)
        self.assertIn("«PL» в конце принято за страну", output)
        self.assertEqual(notice, ("info", "Отменено — ничего не сохранено"))
        self.assertEqual(self.proxies(), [])

    def test_country_mismatch_asks_which_one_to_keep(self):
        self.checks = {"1.2.3.4": ok("1.2.3.4", "DE")}
        notice, output = self.paste("1.2.3.4:8080:user:pass:PL", "y", "2", "n")  # «n»: keep «default» direct
        self.assertIn("в строке страна PL, а выход через прокси — DE", output)
        record = self.proxies()[0]
        self.assertEqual((record.country, record.country_source), ("DE", "check"))
        self.assertEqual(notice[0], "ok")

    def test_failing_proxy_is_saved_only_after_confirmation(self):
        self.checks = {"1.1.1.1": AUTH}
        notice, output = self.paste("1.1.1.1:80:u:p", "y", "n")
        self.assertIn("✗ [??] 1.1.1.1:80 — Прокси отклонил логин или пароль", output)
        self.assertEqual(notice, ("warn", "Ничего не сохранено: рабочих прокси нет"))
        self.assertEqual(self.proxies(), [])
        notice, _ = self.paste("1.1.1.1:80:u:p", "y", "y")
        self.assertIn("Сохранено 1: работают 0, не работают 1", notice[1])
        self.assertFalse(self.proxies()[0].last_ok)

    def test_existing_proxy_is_updated_not_duplicated(self):
        self.accounts("alice")
        old = self.add("http://u:old@1.2.3.4:8080")
        self.db(lambda db: db.assign_proxy(old, ["alice"]))
        self.checks = {"1.2.3.4": ok("1.2.3.4", "PL")}
        _, output = self.paste("1.2.3.4:8080:u:new", "y")
        self.assertIn("уже в списке — обновим", output)
        (record,) = self.proxies()
        self.assertEqual((record.id, record.password, record.accounts_count), (old, "new", 1))

    def test_socks5_with_password_warns_and_is_not_offered_to_accounts(self):
        self.accounts("alice")
        self.checks = {"7.7.7.7": ok("7.7.7.7", "NL", scheme="socks5")}
        notice, output = self.paste("socks5://u:p@7.7.7.7:1080", "y")
        self.assertIn(SOCKS_WARNING, output)
        self.assertNotIn("Назначить новые прокси", output)
        self.assertIsNone(self.proxy_of()["alice"])
        self.assertEqual(notice[0], "ok")

    def test_nothing_recognised(self):
        notice, _ = self.paste("not a proxy")
        self.assertEqual(notice[0], "err")


class TestChecklist(ProxyScreenCase):
    def setUp(self):
        super().setUp()
        self.accounts("a1", "a2", "a3", "a4")
        self.pid = self.add("http://u:p@45.1.1.1:8000", ok("45.1.1.1", "PL"))

    def ticks(self, *keys: str):
        state = self.state()
        return self.keys(lambda: proxy_screen.proxy_accounts_checklist(self.config_path, state, state.proxy(self.pid)),
                         *keys)

    def test_assign_three_accounts_then_untick_one(self):
        notice, output = self.ticks(SPACE, DOWN, SPACE, DOWN, SPACE, DOWN, DOWN, ENTER)
        self.assertEqual(notice, ("ok", "Назначено 3, снято 0"))
        self.assertIn("Сохранить · отмечено 3", output)
        self.assertEqual(self.proxy_of(), {"a1": self.pid, "a2": self.pid, "a3": self.pid, "a4": None})
        notice, _ = self.ticks(DOWN, ENTER, DOWN, DOWN, DOWN, ENTER)
        self.assertEqual(notice, ("warn", "Назначено 0, снято 1 · теперь с вашего IP: a2"))
        self.assertEqual(self.proxy_of()["a2"], None)
        self.assertEqual(self.proxy_of()["a1"], self.pid)

    def test_cancel_changes_nothing(self):
        notice, _ = self.ticks(SPACE, DOWN, SPACE, ESC)
        self.assertEqual(notice[0], "info")
        notice, _ = self.ticks(SPACE, UP, ENTER)  # «Отмена» is the last row
        self.assertEqual(notice[0], "info")
        self.assertEqual(set(self.proxy_of().values()), {None})

    def test_rows_warn_about_country_login_and_socks(self):
        self.db(lambda db: db.connection.execute("UPDATE accounts SET expected_country = 'DE' WHERE name = 'a1'"))
        self.logged_in = {"a2"}
        _, output = self.ticks(ESC)
        self.assertIn("ждёт DE", output)
        self.assertIn("вошёл: смена IP", output)
        socks = self.add("socks5://u:p@8.8.8.8:1080", ok("8.8.8.8", "PL", scheme="socks5"))
        state = self.state()
        _, output = self.keys(
            lambda: proxy_screen.proxy_accounts_checklist(self.config_path, state, state.proxy(socks)), ESC)
        self.assertIn("SOCKS5: браузер не пойдёт", output)

    def test_plain_checklist_toggles_numbers(self):
        with patch("sys.stdout", io.StringIO()):
            picked = tui.checklist([("a", "", ""), ("b", "", ""), ("c", "", "")], {0}, interactive=False,
                                   ask_fn=lambda q, answers=iter(["2 3", "1", ""]): next(answers))
        self.assertEqual(picked, {1, 2})
        with patch("sys.stdout", io.StringIO()):
            self.assertIsNone(tui.checklist([("a", "", "")], (), interactive=False, ask_fn=lambda q: "0"))


class TestCardActions(ProxyScreenCase):
    def test_delete_names_the_accounts_that_go_direct(self):
        self.accounts("alice", "bob")
        pid = self.add("http://1.2.3.4:80", ok("1.2.3.4", "PL"))
        self.db(lambda db: db.assign_proxy(pid, ["alice", "bob"]))
        self.db(lambda db: db.set_search_proxy(pid))
        _, output = self.plain(lambda: proxy_screen.proxy_card(self.config_path, pid), "5", "n", "0")
        self.assertIn("Отвяжутся и пойдут напрямую, с вашего IP: alice, bob", output)
        self.assertEqual(len(self.proxies()), 1)  # «n» keeps it
        notice, _ = self.plain(lambda: proxy_screen.proxy_card(self.config_path, pid), "5", "y")
        self.assertEqual(notice, ("warn", "Удалён 1.2.3.4:80 · теперь с вашего IP: alice, bob · поиск — напрямую"))
        self.assertEqual(self.proxies(), [])
        self.assertEqual(self.proxy_of(), {"alice": None, "bob": None})

    def test_card_shows_details_without_password_and_checks(self):
        self.accounts("alice")
        pid = self.add("http://user:topsecret@1.2.3.4:80", ok("1.2.3.4", "PL"), country="PL")
        self.db(lambda db: db.assign_proxy(pid, ["alice"]))
        self.checks = {"1.2.3.4": ok("1.2.3.4", "DE", 99)}
        _, output = self.plain(lambda: proxy_screen.proxy_card(self.config_path, pid), "2", "0")
        for text in ("Логин", "user", "скрыт", "указана вами", "✓ 210 мс", "alice"):
            self.assertIn(text, output)
        self.assertIn("Указана страна PL, а выход — DE", output)  # after «Проверить связь»
        self.assertNotIn("topsecret", output)
        self.assertEqual(self.proxies()[0].last_latency_ms, 99)

    def test_edit_moves_accounts_and_search_to_the_new_port(self):
        self.accounts("alice")
        pid = self.add("http://u:p@1.2.3.4:80", ok("1.2.3.4", "PL"))
        self.db(lambda db: db.assign_proxy(pid, ["alice"]))
        self.db(lambda db: db.set_search_proxy(pid))
        self.db(lambda db: db.set_account_auth_status(1, "authenticated"))
        self.checks = {"1.2.3.4": ok("1.2.3.4", "PL")}
        _, output = self.plain(lambda: proxy_screen.proxy_card(self.config_path, pid), "3", "1.2.3.4:81:u:p2", "y", "0")
        (record,) = self.proxies()
        self.assertEqual((record.port, record.password, record.accounts_count, record.is_search), (81, "p2", 1, True))
        self.assertIn("Прокси изменён: [PL] 1.2.3.4:81 · аккаунты (1) остались на нём", output)
        account = self.db(lambda db: db.get_account("alice"))
        self.assertEqual(account.auth_status, "authenticated")  # same IP: no new sign-in check needed

    def test_search_proxy_with_accounts_warns_first(self):
        self.accounts("alice")
        pid = self.add("http://1.2.3.4:80", ok("1.2.3.4", "PL"))
        self.db(lambda db: db.assign_proxy(pid, ["alice"]))
        notice, output = self.plain(lambda: proxy_screen.pick_search_proxy(self.config_path), f"P{pid}", "n")
        self.assertIn("Поиск делает много запросов и может навредить аккаунтам на этом IP: alice", output)
        self.assertIsNone(notice)
        self.assertIsNone(self.db(lambda db: db.get_search_proxy()))
        notice, _ = self.plain(lambda: proxy_screen.pick_search_proxy(self.config_path), f"P{pid}", "y")
        self.assertEqual(notice, ("ok", "Поиск — через [PL] 1.2.3.4:80"))
        self.assertEqual(self.db(lambda db: db.get_search_proxy()).id, pid)
        notice, _ = self.plain(lambda: proxy_screen.pick_search_proxy(self.config_path), "D")
        self.assertEqual(notice, ("warn", "Поиск — напрямую, с вашего IP"))

    def test_free_proxy_becomes_search_without_question(self):
        pid = self.add("http://1.2.3.4:80", ok("1.2.3.4", "PL"))
        notice, _ = self.plain(lambda: proxy_screen.pick_search_proxy(self.config_path), f"P{pid}")
        self.assertEqual(notice[0], "ok")
        self.assertEqual(self.prompts, 1)


class TestAccountsAndTheirProxies(ProxyScreenCase):
    def test_pick_proxy_and_go_direct(self):
        self.accounts("alice")
        pid = self.add("http://1.2.3.4:80", ok("1.2.3.4", "PL"))
        _, output = self.plain(lambda: proxy_screen.accounts_and_proxies(self.config_path), "A1", f"P{pid}", "A1", "D", "0")
        self.assertIn("«alice» → [PL] 1.2.3.4:80", output)
        self.assertIn("«alice»: без прокси — пишет с вашего IP", output)
        self.assertIsNone(self.proxy_of()["alice"])

    def test_socks5_with_password_needs_confirmation(self):
        self.accounts("alice")
        pid = self.add("socks5://u:p@1.2.3.4:1080", ok("1.2.3.4", "PL", scheme="socks5"))
        _, output = self.plain(lambda: proxy_screen.accounts_and_proxies(self.config_path), "A1", f"P{pid}", "n", "0")
        self.assertIn(SOCKS_WARNING, output)
        self.assertIsNone(self.proxy_of()["alice"])

    def test_add_new_proxy_for_one_account(self):
        self.accounts("alice", "bob")
        self.checks = {"1.2.3.4": ok("1.2.3.4", "PL")}
        self.plain(lambda: proxy_screen.accounts_and_proxies(self.config_path), "A2", "N", "1.2.3.4:80:u:p", "", "y", "0")
        by_name = self.proxy_of()
        self.assertIsNone(by_name["alice"])  # no «assign to everyone without a proxy» here: it was for bob
        self.assertEqual(by_name["bob"], self.proxies()[0].id)


class TestDistribute(unittest.TestCase):
    def test_country_first_then_in_order(self):
        from types import SimpleNamespace as NS

        pl, de = NS(id=1, country="PL", last_exit_country=None), NS(id=2, country=None, last_exit_country="DE")
        accounts = [NS(name="a", expected_country=None), NS(name="b", expected_country="DE"),
                    NS(name="c", expected_country="US")]
        plan, skipped = proxy_screen.distribute([pl, de], accounts)
        self.assertEqual([(acc.name, p.id) for acc, p in plan], [("b", 2), ("a", 1)])
        self.assertEqual(skipped, ["c"])


class TestYourIpOutsideStage(ProxyScreenCase):
    def test_dashboard_and_account_menu_rows(self):
        from modules.wizard import _dashboard_rows, _proxy_summary, _search_summary

        self.accounts("alice", "bob")
        cfg = load_config(self.config_path)
        self.assertEqual(_search_summary(cfg), ("⚠ с вашего IP", "warn"))
        self.assertEqual(_proxy_summary(cfg), ("нет прокси · все с вашего IP", "warn"))
        pid = self.add("http://45.1.1.1:8000", ok("45.1.1.1", "PL"))
        self.db(lambda db: db.assign_proxy(pid, ["alice"]))
        self.db(lambda db: db.set_search_proxy(pid))
        self.assertEqual(_proxy_summary(cfg), ("1 прокси · у 1 из 2 акк.", "warn"))
        self.assertEqual(_search_summary(cfg), ("через [PL] 45.1.1.1:8000", ""))
        rows = {r[0]: r for r in _dashboard_rows(self.config_path) if r}
        self.assertEqual(rows["2"][1:3], ("Прокси", "1 прокси · у 1 из 2 акк. · поиск через [PL] 45.1.1.1:8000"))

    def test_launch_rows_and_run_log(self):
        from loguru import logger

        from modules.proxy_library import log_proxy_routes
        from modules.wizard import _route_rows

        self.accounts("alice", "bob")
        pid = self.add("http://45.1.1.1:8000", ok("45.1.1.1", "PL"))
        self.db(lambda db: db.assign_proxy(pid, ["alice"]))
        self.assertEqual(_route_rows(self.config_path), [
            ("Поиск", "⚠ напрямую, с вашего IP", "warn"),
            ("Публикация", "1 аккаунт через прокси, 1 с вашего IP (bob)", "warn"),
        ])
        lines: list[str] = []
        sink = logger.add(lambda msg: lines.append(msg.record["message"]), level="INFO")
        try:
            self.db(log_proxy_routes)
        finally:
            logger.remove(sink)
        self.assertEqual(lines, ["Поиск: ⚠ напрямую, с вашего IP", "Ответы: 1 аккаунт через прокси, 1 с вашего IP (bob)"])

    def test_stage_one_shows_short_proxy(self):
        from modules.wizard import _account_proxy_text, _setup_account_rows

        self.accounts("alice", "bob")
        pid = self.add("http://u:p@45.130.253.31:8000", ok("45.130.253.31", "PL"))
        self.db(lambda db: db.assign_proxy(pid, ["alice"]))
        accounts = {acc.name: acc for acc in load_accounts(self.config_path)}
        self.assertEqual(_account_proxy_text(accounts["alice"]), "[PL] 45.130.253.31:8000")
        self.assertEqual(_account_proxy_text(accounts["bob"]), "с вашего IP")
        rows = _setup_account_rows([(accounts["alice"], True)])
        self.assertIn("[PL] 45.130.253.31 ", rows[0][2])  # the stage column follows; no room for the port


if __name__ == "__main__":
    unittest.main()
