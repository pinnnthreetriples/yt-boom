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
last_updated: 2026-11-01
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

```powershell
python youtube_bot.py --warm-up bakyt --warm-up-duration 45
python youtube_bot.py --lifecycle
python youtube_bot.py --warm-quarantined
```

## Wizard menu (`python youtube_bot.py`, stage 1 «Аккаунты»)
- `5` «Импорт аккаунтов»: a file path (default `accounts.txt`) or pasted `login:password:TOTP_SECRET` lines (read with `getpass`, nothing is echoed; an empty line ends the paste). Both go through `youtube_bot.register_account_lines(..., skip_existing=True)`, the same registrar as `--import-accounts-file`: existing accounts are skipped (not overwritten), a bad line is reported by number only. The summary never prints a password or TOTP.
- After an import the new accounts are listed with numbers; answer `a`/«все», `n`/Enter, or numbers (`1 3 5`); a wrong answer is asked again. The chosen ones run through `youtube_bot.warm_selected_cli`: a not yet signed-in account is signed in first (`_authenticate_account`, which includes the 60 s intro warm-up), the others use `warm_accounts` like `--warm-quarantined`; past ageing = skipped.
- `6` «Прогрев»: `warm_candidates` + `warm_accounts` for every authenticated quarantine/aging account, after a y/N confirmation.
- `8` «Жизненный цикл»: read-only table from `AccountLifecycleEngine.summarize` (stage, today's quota, breaker) plus the session check `check_session_offline` and `last_warmup_at` as "heartbeat". Pure row building is `lifecycle_rows` in `modules/account_menu_logic.py`.
- All three run after the arrow menu has returned (`asyncio.run` via `_pools_db`); tests mock `AccountWarmer` / `_authenticate_account` and never open a browser.
