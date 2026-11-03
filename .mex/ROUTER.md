---
name: router
description: Session bootstrap and navigation hub. Read at the start of every session before any task. Contains project state, routing table, and behavioural contract.
edges:
  - target: context/architecture.md
    condition: when working on system design, integrations, or understanding how components connect
  - target: context/stack.md
    condition: when working with specific technologies, libraries, or making tech decisions
  - target: context/conventions.md
    condition: when writing new code, reviewing code, or unsure about project patterns
  - target: context/decisions.md
    condition: when making architectural choices or understanding why something is built a certain way
  - target: context/setup.md
    condition: when setting up the dev environment or running the project for the first time
  - target: context/modules.md
    condition: when inspecting module implementations and code-grounded behaviors
  - target: patterns/INDEX.md
    condition: when looking up available task patterns
  - target: patterns/youtube-reply-flow.md
    condition: when automating YouTube comments or sub-comment replies
  - target: patterns/google-auth-login.md
    condition: when handling Google account sign-in or 2FA TOTP challenges
  - target: patterns/account-management.md
    condition: when importing accounts or managing DPAPI credentials
  - target: patterns/account-warming-heartbeat.md
    condition: when inspecting session health or warming fresh accounts
  - target: patterns/channel-profile.md
    condition: when changing an account's YouTube channel nickname, description or avatar
  - target: patterns/debug-browser-interaction.md
    condition: when diagnosing browser automation or selector issues
last_updated: 2026-11-03
---

# Session Bootstrap

If you haven't already read `AGENTS.md`, read it now — it contains the project identity, non-negotiables, and commands.

Then read this file fully before doing anything else in this session.

## Current Project State

**Working:**
- Automated YouTube comment & nested sub-comment retrieval via InnerTube API (`curl_cffi`)
- Headful & persistent Camoufox browser execution with hardware fingerprint pinning and SOCKS5 remote DNS
- Natural mouse movement via cubic Bezier curves, variable scroll pacing, and typo injection
- Dynamic reply button DOM resolution (`#reply-button-end :is(button, a)`) supporting non-channel accounts
- In-situ YouTube channel creation dialog detection and confirmation
- Automated Google authentication state machine with RFC 6238 TOTP window guard and speedbump dismissal
- Windows DPAPI credential encryption (`protect_secret` / `reveal_secret`) for SQLite persistence
- Multi-account parallel orchestration supporting `each` and `share` routing modes
- Account lifecycle engine (`modules/lifecycle_engine.py`): quarantine 0-24h, aging 24-48h (no comments), probation 48-72h (1/day, 3h cooldown), mature 72h+ (5/day, 45-90 min); 3 consecutive errors restrict an account; `--lifecycle` and `--warm-quarantined` CLI. Per account: `lifecycle_mode` (`auto` by age / `mature` already warmed up / `unlimited` no limits except the breaker), `auto_warmup` on/off, `is_active` in work; CLI `--set-mode`, `--auto-warmup`, `--set-active`, `--reset-restricted`
- Wizard stage 1 «Аккаунты» menu: each account is its own row (Enter/Space opens «Проверить вход» / «Переименовать» / «Выйти из аккаунта» for that account); `1` add/log in, `5` import accounts (file or pasted `login:password:TOTP` lines, then pick who to warm up), `6` warm up all quarantine/aging accounts, `7` bulk mode/in-work/auto-warm-up for ticked accounts, `8` read-only lifecycle table (`modules/account_menu_logic.py`); each account row shows its stage/mode and its menu has a card plus mode, in-work, auto warm-up, warm-up-now and «Снять ограничение»
- Channel profile (`modules/profile_manager.py`): wizard stage 1 `9` «Профиль канала» → account (Space/Enter) → card with nickname / description / avatar edit rows saved as a DB draft (`accounts.profile_*`), «Применить на YouTube» publishes it through YouTube Studio in the account's own Camoufox profile; `--apply-profile <name>` does the same from the CLI
- Comprehensive automated test suite (709 passing unit and integration tests)

**Not yet built:**
- Real-time WebRTC audio stream generation for interactive live events
- Multi-browser cross-engine test runner (Chromium / WebKit) — explicitly restricted to Camoufox/Gecko

**Known issues:**
- Copying `test_metrics.db` across different Windows machines requires re-importing credentials due to DPAPI user binding
- Force-killing Python processes during browser automation can leave stale profile lock files in session folders

## Routing Table

Load the relevant file based on the current task. Always load `context/architecture.md` first if not already in context this session.

| Task type | Load |
|-----------|------|
| Understanding how the system works | `context/architecture.md` |
| Working with a specific technology | `context/stack.md` |
| Writing or reviewing code | `context/conventions.md` |
| Making a design decision | `context/decisions.md` |
| Setting up or running the project | `context/setup.md` |
| Inspecting core module behaviors and code facts | `context/modules.md` |
| Posting YouTube sub-comment replies | `patterns/youtube-reply-flow.md` |
| Authenticating Google accounts | `patterns/google-auth-login.md` |
| Managing credentials and fingerprints | `patterns/account-management.md` |
| Changing a channel nickname / description / avatar | `patterns/channel-profile.md` |
| Debugging browser interactions | `patterns/debug-browser-interaction.md` |
| Any specific task | Check `patterns/INDEX.md` for a matching pattern |

## Behavioural Contract

For every task, follow this loop:

1. **CONTEXT** — Load the relevant context file(s) from the routing table above. Check `patterns/INDEX.md` for a matching pattern. If one exists, follow it.
2. **BUILD** — Do the work. If a pattern exists, follow its Steps. If you are about to deviate from an established pattern, say so before writing any code — state the deviation and why.
3. **VERIFY** — Load `context/conventions.md` and run the Verify Checklist item by item. State each item and whether the output passes. Do not summarise — enumerate explicitly.
4. **DEBUG** — If verification fails or something breaks, check `patterns/INDEX.md` for a debug pattern. Follow it. Fix the issue and re-run VERIFY.
5. **GROW** — After meaningful work, run this binary checklist:
   - **Ground:** What changed in reality? Name the changed behavior, system, command, dependency, or workflow.
   - **Record:** If project state changed, update the "Current Project State" section above. If documented facts changed, update the relevant `context/` file surgically. Record a new behavioral claim about specific code as its own entity (`mex wiki apply` with a `create-entry` operation), grounded to every symbol whose change could make it false and naming the identifiers and literals it depends on. Ground with node ids only; MEX records the fingerprints. Give the entity 3 to 6 `aliases` (short plain-word phrases a developer would type for the symptom or outcome it covers, written from the claim itself, not its title or identifiers).
   - **Connect:** Link section entities to their enclosing knowledge with `refines` using existing ids. Preview missing structural links with `mex wiki link-sections`, then apply explicitly. Review semantic relationships from evidence; do not connect records just because they share keywords or code groundings.
   - **Orient:** If this task can recur and no pattern exists, create one in `patterns/` using `patterns/README.md`, then add it to `patterns/INDEX.md`. If a pattern exists but you learned a gotcha, update it.
   - **Write:** Bump `last_updated` in every scaffold file you changed. Read `mex logging --json` before optional `mex log` notes: `significant` records material rationale, `checkpoints` batches useful notes at task/session boundaries, and `manual` avoids unsolicited notes. Honor explicit user log requests in every mode; mandatory workflow Activity and recovery audits remain required.
