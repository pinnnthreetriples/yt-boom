"""Guard for the non-negotiable "structural selectors only for YouTube / Google UI".

Visible text, `aria-label`, `title` and placeholders are localized: a selector that depends on them breaks per account
UI language (ru-RU, pl-PL, ky...) and is an English-only fingerprint. The scan reads every string literal and every
`get_by_*` call of modules/*.py, so a re-introduced `button:has-text('Reply')` fails here, not in production.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

MODULES = Path(__file__).resolve().parent.parent / "modules"

FORBIDDEN_STRING = re.compile(
    r"""has-text\(|:text-is\(|:text-matches\(|:text\(|:visible-text|internal:(?:text|label|attr)"""
    r"""|(?:^|>>\s*)text\s*=|aria-label(?!ledby)"""
    r"""|\[\s*(?:title|placeholder|alt)\s*[~|^$*]?="""
    # a submit input found by its visible value is text matching too; [value] on other inputs (set_eom=true) is a flag
    r"""|\[\s*type\s*=\s*["']?submit["']?\s*\]\s*\[\s*value\s*[~|^$*]?="""
    r"""|\[\s*value\s*[~|^$*]?=[^\]]*\]\s*\[\s*type\s*=\s*["']?submit"""
    r"""|\btext\(\)|normalize-space\(|contains\(\s*\.\s*,""",
    re.IGNORECASE,
)
FORBIDDEN_CALLS = {"get_by_text", "get_by_label", "get_by_title", "get_by_placeholder", "get_by_alt_text"}

# The ONLY text matchers allowed in modules/*.py: the Google account sign-in / speedbump screens (accounts.google.com,
# gds.google.com). They are not YouTube UI, they have no structural handle, the login must stay fully automatic, and
# the labels cover en/ru only (any other language falls back to the "неизвестный экран" error). file -> (constants, helper).
# Everything else, including every other string in google_authenticator.py, must stay structural.
ALLOWED_TEXT_CONSTANTS = {
    "google_authenticator.py": frozenset(
        {"DECLINE_BUTTONS", "WAS_IT_YOU_BUTTONS", "ACKNOWLEDGE_BUTTONS", "TRY_ANOTHER_WAY", "USE_ANOTHER_ACCOUNT"}
    )
}
ALLOWED_TEXT_HELPERS = {"google_authenticator.py": frozenset({"_buttons"})}  # builds the labels of the constants above


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def _parents(tree: ast.AST) -> dict[int, ast.AST]:
    return {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _is_allowed(node: ast.AST, parents: dict[int, ast.AST], name: str) -> bool:
    constants = ALLOWED_TEXT_CONSTANTS.get(name, frozenset())
    helpers = ALLOWED_TEXT_HELPERS.get(name, frozenset())
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)) and current.name in helpers:
            return True
        if isinstance(current, (ast.Assign, ast.AnnAssign)):
            targets = current.targets if isinstance(current, ast.Assign) else [current.target]
            if any(isinstance(t, ast.Name) and t.id in constants for t in targets):
                return True
        current = parents.get(id(current))
    return False


def find_violations(source: str, name: str = "<source>") -> list[str]:
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    parents = _parents(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if FORBIDDEN_STRING.search(node.value) and not _is_allowed(node, parents, name):
                found.append(f"{name}:{node.lineno}: text-based selector {node.value[:70]!r}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in FORBIDDEN_CALLS:
                found.append(f"{name}:{node.lineno}: {node.func.attr}()")
            elif node.func.attr == "get_by_role" and any(kw.arg == "name" for kw in node.keywords):
                found.append(f"{name}:{node.lineno}: get_by_role(name=...)")
    return found


class TestSelectorGuard(unittest.TestCase):
    def test_modules_have_no_text_based_selectors(self) -> None:
        violations: list[str] = []
        for path in sorted(MODULES.glob("*.py")):
            violations += find_violations(path.read_text(encoding="utf-8"), path.name)
        self.assertEqual(violations, [], "localized selectors break accounts in other UI languages")

    def test_the_google_allowlist_is_exactly_the_five_constants_and_their_helper(self) -> None:
        source = (MODULES / "google_authenticator.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        allowed = ALLOWED_TEXT_CONSTANTS["google_authenticator.py"]
        used_in: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                names = {t.id for t in node.targets if isinstance(t, ast.Name)}
                if any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_buttons" for n in ast.walk(node.value)):
                    used_in |= names
                if names & allowed:
                    used_in |= names & allowed
        self.assertEqual(used_in, set(allowed), "_buttons / text constants may only feed the five allowlisted names")
        # a text selector anywhere else in the same file is still a violation
        sneaky = source + "\nOTHER = ('button:has-text(\"Next\")',)\n"
        self.assertTrue(any("text-based selector" in v for v in find_violations(sneaky, "google_authenticator.py")))
        self.assertTrue(find_violations("X = ('li:has-text(\"Skip\")',)", "google_authenticator.py"))

    def test_the_scan_catches_a_reintroduced_violation(self) -> None:
        bad = (
            'page.locator("button:has-text(\'Reply\')")',
            'page.locator("button[aria-label*=\'Ответить\' i]")',
            'page.locator("text=Sign in")',
            'page.locator("a >> text=Войти")',
            'page.locator("input[placeholder=\'Search\']")',
            'page.locator("//button[contains(text(), \'Next\')]")',
            'page.get_by_text("Next")',
            'page.get_by_role("button", name="Next")',
            'page.locator("input[type=\'submit\'][value=\'Next\']")',
            'page.locator(\'input[value="Next"][type="submit"]\')',
        )
        for source in bad:
            self.assertTrue(find_violations(source), source)

    def test_the_scan_accepts_structural_selectors_and_docstrings(self) -> None:
        good = '''
def f(page):
    """Never use button:has-text() or aria-label here."""
    page.locator("#reply-button-end :is(button, a)")
    page.locator("ytd-comment-view-model:has(a[href*='lc=X']) #reply-button-end :is(button, a)")
    page.locator('form[action*="consent"]:has(input[name="set_eom"][value="true"]) button')
    page.locator("#replies #more-replies-sub-thread button")
    page.locator('form input[type="submit"]')
    page.get_by_role("button")
    return "button text=ok", "aria-expanded"
'''
        self.assertEqual(find_violations(good), [])


if __name__ == "__main__":
    unittest.main()
