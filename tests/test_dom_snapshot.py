"""The unknown-screen snapshot keeps the structure a handler needs and nothing that identifies the account."""
import unittest

from modules.dom_snapshot import redact, sanitize_html

TOKEN = "AUjHKweuSomBFRuaKgv9h3DQj_NHnIaohPwW0l"

PAGE = f"""<!doctype html><html lang="en"><head><title>Sign in alice@example.com</title>
<meta name="csrf" content="{TOKEN}"><style>.x {{ color: red }}</style></head>
<body data-flow="{TOKEN}" data-challengetype="6" data-identifier="alice@example.com" data-name="alice">
<script nonce="{TOKEN}">var token = "{TOKEN}"; window.email = "alice@example.com";</script>
<!-- alice@example.com -->
<form id="challenge" action="https://accounts.google.com/v3/signin/challenge?TL={TOKEN}&continue=x" method="post">
  <h1 id="headingText">Verify it's you</h1>
  <p>alice@example.com</p>
  <input id="identifierId" name="identifier" type="email" value="alice@example.com" autocomplete="username" aria-label="alice@example.com">
  <input type="password" name="Passwd" value="hunter2">
  <textarea name="note">my secret note</textarea>
  <div role="button" jsname="Cuz2Ue" data-secret="{TOKEN}">Try another way <span>for alice@example.com</span></div>
  <a href="/signin/recovery?hl=en&token={TOKEN}" id="forgot">Forgot password?</a>
  <button type="button" id="next" jsname="LgbsSe"><span>Next</span></button>
  <svg><text>svg text</text></svg><iframe src="https://x.test/?t={TOKEN}">frame text</iframe>
</form></body></html>"""


class TestSanitizeHtml(unittest.TestCase):
    def setUp(self):
        self.out = sanitize_html(PAGE)

    def test_emails_values_tokens_and_scripts_are_gone(self):
        for secret in ("alice", "example.com", TOKEN, "hunter2", "secret note", "csrf", "color: red", "svg text", "frame text"):
            self.assertNotIn(secret, self.out, secret)

    def test_structure_ids_names_types_and_data_attribute_names_survive(self):
        for kept in (
            '<form id="challenge"', '<input id="identifierId" name="identifier" type="email">',
            '<input type="password" name="Passwd">', '<h1 id="headingText">', 'jsname="Cuz2Ue"', 'jsname="LgbsSe"',
            "data-flow=", "data-identifier=", "data-secret=",
        ):
            self.assertIn(kept, self.out)

    def test_button_link_and_heading_texts_survive_with_emails_redacted(self):
        for text in ("Verify it's you", "Try another way", "Next", "Forgot password?", "for <email>"):
            self.assertIn(text, self.out)

    def test_data_values_are_dropped_except_short_numbers(self):
        self.assertIn('data-challengetype="6"', self.out)
        self.assertIn('data-flow="…"', self.out)
        self.assertIn('data-identifier="…"', self.out)

    def test_urls_lose_their_query_string(self):
        self.assertIn('action="accounts.google.com/v3/signin/challenge"', self.out)
        self.assertIn('href="/signin/recovery"', self.out)

    def test_stray_end_tags_do_not_swallow_the_open_elements(self):
        out = sanitize_html('<div id="a"></span></p><button id="b">Go</button></div>')
        self.assertEqual(out, '<div id="a">\n<button id="b">\nGo\n</button>\n</div>')

    def test_redact_replaces_emails_and_long_token_like_runs_only(self):
        self.assertEqual(redact("mail bob+x@mail.example.org now"), "mail <email> now")
        self.assertEqual(redact(f"id {TOKEN}"), "id <token>")
        self.assertEqual(redact("Try another way"), "Try another way")
        self.assertEqual(redact("knowledge-preregistered-email-response"), "knowledge-preregistered-email-response")


if __name__ == "__main__":
    unittest.main()
