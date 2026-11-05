"""Wizard stage 2 «Прокси» (modules/proxy_screen.py) and the «с вашего IP» lines outside it.

Network checks and DPAPI are mocked: `modules.proxy_check.check_proxy` / `check_direct` answer from a table by host,
`secret_store._blob_call` is a reversible stand-in.
"""
from __future__ import annotations

import unittest


from modules import proxy_screen
from modules.proxy_check import ProxyCheckResult

SOCKS_WARNING = "SOCKS5 с логином и паролем подходит только для поиска"


def ok(ip: str, country: str | None, ms: int = 210, scheme: str = "http") -> ProxyCheckResult:
    return ProxyCheckResult(True, scheme, ip, country, ms, None, f"Прокси работает: {ip}")


def bad(kind: str, message: str) -> ProxyCheckResult:
    return ProxyCheckResult(False, None, None, None, None, kind, message)


TIMEOUT = bad("timeout", "Прокси не ответил за 10 с: сервер перегружен или недоступен")
AUTH = bad("auth", "Прокси отклонил логин или пароль (ошибка 407): проверьте их у продавца")


class TestDistribute(unittest.TestCase):
    def test_country_first_then_in_order(self):
        from types import SimpleNamespace as NS

        pl, de = NS(id=1, country="PL", last_exit_country=None), NS(id=2, country=None, last_exit_country="DE")
        accounts = [NS(name="a", expected_country=None), NS(name="b", expected_country="DE"),
                    NS(name="c", expected_country="US")]
        plan, skipped = proxy_screen.distribute([pl, de], accounts)
        self.assertEqual([(acc.name, p.id) for acc, p in plan], [("b", 2), ("a", 1)])
        self.assertEqual(skipped, ["c"])


if __name__ == "__main__":
    unittest.main()
