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
last_updated: 2026-10-31
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
- Dispatches a lightweight HTTP GET to `https://www.youtube.com/?hl=en` via `curl_cffi` (`impersonate="chrome124"`).
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
- Loads YouTube homepage and selects a popular/recommended video or fallback neutral stream.
- Simulates natural viewing:
  - Video playback for target duration (default 45s).
  - Human cursor positioning over `#movie_player`.
  - Natural wheel scrolling towards the comments section.
  - Pausing 3-6s to simulate reading user comments.
  - Smooth wheel scrolling back up.
- Does NOT post comments or replies.
- Updates `accounts.last_used_at` in `test_metrics.db`.

```powershell
python youtube_bot.py --warm-up bakyt --warm-up-duration 45
```
