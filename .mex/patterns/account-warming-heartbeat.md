---
name: account-warming-heartbeat
description: Fast HTTP session keep-alive checks and behavioral account warm-up workflows.
triggers:
  - "heartbeat"
  - "warm-up"
  - "keep-alive"
  - "session validity"
edges:
  - target: context/modules.md
    condition: when reviewing session heartbeat and warmer module implementations
grounds_to: []
last_updated: 2026-11-03
mex:
  id: mx_01M69X1XBKX43KY14D8945W9XH
  type: pattern
  status: promoted
  revision: 2
  title: account-warming-heartbeat
  relations:
    - type: related_to
      target: mx_01M69V83DQFH77W8JJ4KS4EWP9
      note: when reviewing session heartbeat and warmer module implementations
---

# Pattern: Account Session Heartbeat & Behavioral Warming

Use this pattern when inspecting session cookie validity or establishing initial trust history for freshly authenticated accounts.

## Fast HTTP Heartbeat (`--heartbeat`)
- Inspects `cookies.sqlite` for the target session (`LOGIN_INFO`, `expiry`).
- Only `.youtube.com` domain cookies must be queried (never mix `.google.com` cookies over YouTube-specific `SID`/`HSID`/`SSID`).
- With `--heartbeat-online` dispatches a lightweight HTTP GET to `https://www.youtube.com/` via `curl_cffi` with the Firefox TLS profile (`firefox155`); `fingerprint_headers` takes the `User-Agent` from the account's pinned fingerprint file and rewrites its Firefox version to the installed Camoufox build, so the request matches the browser that owns the cookies. Without a fingerprint the impersonation default UA applies.
- Evaluates whether `"LOGGED_IN": true` is returned.
- Runs across all accounts in seconds without starting browser processes.

```powershell
# Check all accounts
python youtube_bot.py --heartbeat

# Check specific account
python youtube_bot.py --heartbeat bakyt
```

## Behavioral Account Warming (`--warm-up`)
- Launches Camoufox with the account's persistent context, proxy, and fixed fingerprint.
- Loads the YouTube home feed, scrolls it and clicks one visible `ytd-rich-grid-media a#video-title-link` card with a Bezier mouse path. There is no hardcoded fallback link: an empty feed (fresh account, history off) falls back to a neutral search (`/results?search_query=...`, YouTube retired `/feed/trending` in July 2025), and a search without cards fails the warm-up.
- Simulates natural viewing:
  - Video playback for target duration (default 45s); the cursor keeps hovering the `#movie_player` controls bar so `lact` stays fresh in the watchtime beacons.
  - Random player events: theater mode `t` (35%), pause/unpause `k` after 10-25s (20%), skip `ArrowRight` (15%).
  - Wheel scroll to `#comments`; 40% of sessions expand `#replies #more-replies button` and read for 4-7s.
  - 15% chance to like (`#segmented-like-button button`).
- Does NOT post comments or replies.
- Records the session with `Database.record_account_warmup_success` (`warmup_sessions_done`, `last_warmup_at`, `last_used_at`).

## Account Lifecycle (`--lifecycle`, `--warm-quarantined`)
- `mark_account_authenticated` (after every successful sign-in) stamps `authenticated_at`; re-authenticating an already authenticated account keeps the clock.
- `AccountLifecycleEngine.calculate_stage` derives the stage from hours since sign-in; `evaluate_eligibility` adds the daily quota (reset 04:00 UTC), the cooldown between comments and the circuit breaker (`consecutive_errors >= 3` → `restricted`).
- `MultiAccountOrchestrator._usable_accounts` skips accounts that may not comment; a quarantined/aging account that has not warmed up for 4h gets a background warm-up (never in `--dry-run`). The warm-up takes the account's runner lock and closes its open browser first: warm-up and commenting never share one Camoufox profile.
- `_authenticate_account` runs a 60s intro warm-up for accounts still in quarantine/aging.

## Per-account work settings (`accounts.lifecycle_mode`, `auto_warmup`, `is_active`)
- `lifecycle_mode` (migration `Database._ensure_account_mode_columns`, existing rows = `auto`): `auto` «По возрасту» = the age rules above; `mature` «Уже прогрет» skips quarantine/aging/probation (mature limits at once: 5/day, 45-90 min); `unlimited` «Без лимитов» has no stage, daily limit or interval (stage `unlimited`, `quota_limit` None) — only the circuit breaker (3 errors in a row → `restricted`) still applies, in every mode.
- `AccountLifecycleEngine.calculate_stage` is mode-aware; `age_stage` gives the real-age stage regardless of the mode, `LifecycleView.age_stage/mode` carry it to the UI. `warm_candidates` follows `calculate_stage`, so mature/unlimited accounts are never "to warm up" (a manual warm-up via `warm_up_cli` / the menu item still works for any account).
- `auto_warmup` (default on): off → `_schedule_warmup` never starts a background warm-up. `is_active` off → not picked (`list_active_accounts`, `_may_comment_now` re-check) and not warmed. `sync_accounts_from_config` overwrites `is_active` from config.yaml (known issue, fixed by the proxy work); `mirror_active_to_config` writes the switch into config.yaml too.
- «Снять ограничение»: `lifecycle_engine.release_restriction` → `Database.reset_account_restriction` (errors 0, stage re-derived from mode/age).
- The gate log is Russian: `[LIFECYCLE] <имя>: пропущен — <причина>` (`explain_block`), with the way out («Режим» in the account menu, «Снять ограничение», «Автопрогрев выключен»).
- CLI: `--set-mode NAME auto|mature|unlimited`, `--auto-warmup NAME on|off`, `--set-active NAME on|off`, `--reset-restricted NAME`; `--lifecycle` shows Mode / Active / AutoWarm columns (`mature*` = mature by mode).
- Import: `parse_account_line` reads `mode=auto|mature|unlimited` (colon or free form, like `country=`); `register_account_lines(default_mode=...)` applies the wizard's «Эти аккаунты уже прогреты?» answer to lines without their own `mode=`.

```powershell
python youtube_bot.py --warm-up bakyt --warm-up-duration 45
python youtube_bot.py --lifecycle
python youtube_bot.py --warm-quarantined
```

## Wizard menu (`python youtube_bot.py`, stage 1 «Аккаунты»)
- `5` «Импорт аккаунтов»: a file path (default `accounts.txt`) or pasted `login:password:TOTP_SECRET` lines (read with `getpass`, nothing is echoed; an empty line ends the paste). Both go through `youtube_bot.register_account_lines(..., skip_existing=True)`, the same registrar as `--import-accounts-file`: an existing account (same name or same e-mail, so a renamed one too) is skipped, not overwritten; a bad line is reported by number only; a non-UTF-8 file is refused with a hint. The summary never prints a password or TOTP.
- After an import the new accounts are listed with numbers; answer `a`/«все», `n`/Enter, or numbers (`1 3 5`); a wrong answer is asked again. The chosen ones run through `youtube_bot.warm_selected_cli`: a not yet signed-in account is signed in first (`_authenticate_account`, which includes the 60 s intro warm-up of a fresh account), the others use `warm_accounts` like `--warm-quarantined`; past ageing = skipped; the pause between accounts is taken only before an account that really runs.
- `6` «Прогрев»: `warm_candidates` + `warm_accounts` for every authenticated quarantine/aging account, after a y/N confirmation.
- `8` «Жизненный цикл»: read-only table from `AccountLifecycleEngine.summarize` (stage, today's quota, breaker; a switched-off account shows «отключён») plus the session check `check_session_offline` and `last_warmup_at` as "heartbeat"; a dim second line per account: режим · в работе · автопрогрев. Pure row building is `lifecycle_rows` in `modules/account_menu_logic.py`.
- Account row (`_setup_account_rows`): `name · ● вошёл · прокси · <статус>`, status from `account_status_label` (≤15 single-width chars: `◐ карантин 14ч`, `✓ взрослый 2/5`, `✓ прогрет 2/5`, `∞ без лимитов`, `– не в работе`, `✗ ограничен`). Account menu (Enter on the row): card (`account_card_rows`: стадия, возраст, лимит сегодня, следующий коммент, последний прогрев, режим, в работе, автопрогрев) + rows «В работе», «Режим», «Автопрогрев», «Прогреть сейчас», «Снять ограничение» (restricted only, asks y/N). `7` «Режим для группы»: tick accounts, then mode / в работе / автопрогрев for all (`apply_account_settings`).
- `5` import asks once «Эти аккаунты уже прогреты?» (По возрасту / Уже прогрет / Без лимитов; Esc cancels the import) before registering.
- All three run after the arrow menu has returned (`asyncio.run` via `_pools_db`); tests mock `AccountWarmer` / `_authenticate_account` and never open a browser.
