---
name: conventions
description: How code is written in this project — naming, structure, patterns, and style. Load when writing new code or reviewing existing code.
triggers:
  - "convention"
  - "pattern"
  - "naming"
  - "style"
  - "how should I"
  - "what's the right way"
edges:
  - target: context/architecture.md
    condition: when a convention depends on understanding the system structure
  - target: context/modules.md
    condition: when looking up specific module interfaces
grounds_to: []
last_updated: 2026-10-31
mex:
  id: mx_01M69TVZY58165JDM14J2ZVZZM
  type: convention
  status: promoted
  revision: 3
  title: conventions
  relations:
    - type: related_to
      target: mx_01M69TVZXPPER40BE4QGRY2Q4M
      note: when a convention depends on understanding the system structure
    - type: related_to
      target: mx_01M69V83DQFH77W8JJ4KS4EWP9
      note: when looking up specific module interfaces
---

# Conventions

## Naming
- **Files**: `snake_case.py` (e.g. `youtube_runner.py`, `google_authenticator.py`, `totp_helper.py`).
- **Classes**: `PascalCase` (e.g. `YouTubeRunner`, `InteractionSimulator`, `MultiAccountOrchestrator`).
- **Functions & Methods**: `snake_case` with verb-first action intent (e.g. `parse_account_line`, `get_or_create_fingerprint`, `_confirm_channel_creation`).
- **Constants**: `UPPER_SNAKE_CASE` (e.g. `DPAPI_PREFIX`, `YOUTUBE_HOME_URL`, `AUTH_TIMEOUT_SECONDS`).
- **Database Tables & Columns**: `snake_case` plural for tables (`accounts`, `video_pool`, `test_nodes`), `snake_case` for columns (`created_at`, `totp_secret`, `video_id`).

## Structure
- **Asynchronous Everywhere**: All network, browser, and database operations must use `async/await`. Blocking I/O in the main event loop is forbidden.
- **Module Separation**:
  - `modules/` houses business domain runners, collectors, and simulators.
  - Root directory holds entry points (`main.py`, `youtube_bot.py`), test orchestrator (`run_tests.py`), and global services (`database.py`, `config.py`, `secret_store.py`).
  - `tests/` houses isolated unit and integration test fixtures mimicking browser DOM and API endpoints.
- **Selector Robustness**: Never rely on language-specific text or button labels when selecting YouTube DOM elements. Always use structural selectors (e.g. `#reply-button-end :is(button, a)`).

## Patterns
- **Structural Selector Fallbacks**:
  ```python
  # Correct: Structural selector matching both standard buttons and channel-creation anchors in any UI locale
  selectors = [
      f"ytd-comment-view-model:has(a[href*='lc={comment_id}']) #reply-button-end :is(button, a)",
      "#reply-button-end :is(button, a)",
  ]

  # Wrong: Hardcoded English text that breaks on Russian, Kyrgyz, Spanish, etc.
  locator = page.locator("button:has-text('Reply')")
  ```
- **Windows DPAPI Storage**:
  ```python
  # Correct: Encrypt secrets before storing in SQLite
  encrypted_pass = protect_secret(raw_password)
  await db.upsert_account(name, email, password=encrypted_pass, totp_secret=protect_secret(totp))

  # Decrypt on read
  plain_pass = reveal_secret(row["password"])
  ```
- **TOTP Window Rollover Guard**:
  ```python
  # Always ensure fresh TOTP window to avoid submitting expired tokens
  code = await generate_fresh_totp(secret)
  ```

## Verify Checklist
Before presenting or committing code:
- [ ] All database queries are parameterized and run via `database.Database` methods.
- [ ] Account secrets (`password`, `totp_secret`) are wrapped in `protect_secret()` before writing to disk/database.
- [ ] YouTube DOM interactions target structural IDs (`#reply-button-end :is(button, a)`) without locale dependencies.
- [ ] Browser launches use pinned fingerprints (`modules.fingerprint_manager`) and persistent contexts.
- [ ] All automated tests pass: `python run_tests.py` returns 100% OK across all test cases.
