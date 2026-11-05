---
name: cookie-robot
description: Pre-warming a fresh account's browser profile with cookies (Google NID/AEC, analytics, ads) BEFORE the Google sign-in, so the sign-in does not meet a captcha.
triggers:
  - "cookie robot"
  - "куки-робот"
  - "нагулять куки"
  - "captcha at sign-in"
  - "sterile profile"
edges:
  - target: context/modules.md
    condition: when reviewing the cookie robot implementation details
  - target: patterns/google-auth-login.md
    condition: when the sign-in itself fails or asks for a captcha
  - target: patterns/account-warming-heartbeat.md
    condition: when the account needs the YouTube warm-up (after sign-in), not the pre-sign-in cookies
grounds_to: []
last_updated: 2026-11-05
---

# Pattern: Cookie Robot (pre-sign-in profile warming)

## Context
A new account starts with a sterile Camoufox profile (0 cookies, no history); Google then tends to show a captcha at sign-in. `modules/cookie_robot.py` visits 4-5 trusted sites in the account's own persistent profile first. It is **not** the YouTube warm-up (`account_warmer.py`), which needs a signed-in account.

## Flow
1. `plan_sites(rng)`: 4-5 random sites from `SITE_POOL` (14 trusted sites) in random order; `google.com` with an everyday search (`GOOGLE_QUERIES`) is always first or second (native `NID` / `AEC`).
2. `CookieRobot.run(account, on_progress)`: `resolve_account_proxy(account)` (fails closed on `ProxyUnavailableError`, never goes out from the own IP) → `YouTubeRunner.start()` (saved fingerprint via `get_or_create_fingerprint`, the account's proxy and locale) → per site: `goto` (10-12 s), accept the cookie banner (`CONSENT_ACCEPT_SELECTORS + CMP_ACCEPT_SELECTORS` from `modules/page_helpers.py`), 1-2 `InteractionSimulator.scroll` calls over a 5-8 s stay → `finally: runner.close()` (flushes cookies to the profile, releases the profile lock; capped by `CLOSE_TIMEOUT`).
3. Every site is its own `try/except` with a hard `asyncio.wait_for` cap: a blocked or slow site is counted in `CookieRobotResult.failed` and the walk goes on; `success` = at least one site visited. `on_progress(step, total, site)` fires as each site starts; a broken callback is ignored.
4. Entry points: `youtube_bot.run_cookie_robot(config_path, names, on_progress, ...)`; UI «Аккаунты»: after adding/importing accounts the question «Нагулять профиль перед входом (куки-робот)?» (then the usual «Войти и прогреть сейчас?»), and «вход» → «нагулять куки» (`login_menu`, row `l-cookies`) for one account. The bar is `format_progress_bar` → `[=====>    ] 3/5 wikipedia.org`, one printed line per step (`tab_accounts.cookie_progress_printer`; no `\r` redraw, the robot's log lines go to the same terminal). The Google search counts only when the page reaches `/search?` (`wait_for_url`), else the search URL is opened. An account without a proxy gets a warning in the question: the cookies would carry the home IP, so assign a proxy first.

## Gotchas
- `InteractionSimulator.smooth_scroll` does not exist; the smooth wheel scroll is `InteractionSimulator.scroll(page, pixels)`.
- Banner selectors are ids/classes of the consent platforms (OneTrust, Didomi, Cookiebot, ...): `tests/test_selector_guard.py` forbids text/aria/title selectors. A miss just leaves the banner.
- `parent.lock` stays as a file after a clean close on Windows; what matters is that nothing holds it (the profile folder can be deleted).
- One profile, one browser: do not run the robot while the orchestrator, a warm-up or the sign-in holds the account's profile.
- A UI test helper must not be named `run` in a `TestCase` (it hides `TestCase.run`, the tests silently do not execute).

## Verify
- [ ] `python run_tests.py` passes (`tests/test_cookie_robot.py`, cookie tests in `tests/test_ui_accounts.py`).
- [ ] Real smoke (headless, throwaway profile): 4 of 4 sites visited, `cookies.sqlite` holds Google `NID` / `AEC`, the profile folder deletes (no held lock).
