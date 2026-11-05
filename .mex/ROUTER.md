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
last_updated: 2026-11-04
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
- Comments YouTube gives no Reply button (`#reply-button-end` hidden) are detected structurally, skipped without blaming the account and remembered for 7 days (`ReplyUnavailableError`, `SubmissionResult.reply_unavailable`)
- In-situ YouTube channel creation dialog detection and confirmation
- Automated Google authentication state machine with RFC 6238 TOTP window guard and speedbump dismissal
- Windows DPAPI credential encryption (`protect_secret` / `reveal_secret`) for SQLite persistence
- Multi-account parallel orchestration supporting `each` and `share` routing modes
- Account lifecycle engine (`modules/lifecycle_engine.py`): quarantine 0-24h, aging 24-48h (no comments), probation 48-72h (1/day, 3h cooldown), mature 72h+ (5/day, 45-90 min); 3 consecutive errors restrict an account; `--lifecycle` and `--warm-quarantined` CLI. Per account: `lifecycle_mode` (`auto` by age / `mature` already warmed up / `unlimited` no limits except the breaker), `auto_warmup` on/off, `is_active` in work; CLI `--set-mode`, `--auto-warmup`, `--set-active`, `--reset-restricted`
- Console interface (`python youtube_bot.py --wizard`, `start_bot.bat` → `modules/ui/app.py:run_app`): one screen with six tabs «Запуск», «Аккаунты», «Прокси», «Проекты», «Цель», «Ответы» (`modules/ui/tab_*.py`); the old step-by-step wizard, dashboard and submenus are gone. `modules/ui/shell.py` (Controller / render / `run_shell`): rows of icons (`modules/ui/icons.py`: emoji in Windows Terminal, plain symbols elsewhere, `YTBOT_ICONS=emoji|plain`); keys Tab/Shift+Tab/`1`-`6` tabs (`GoTab` by tab name), ↑↓ rows, ←→ change a value, Space details, Enter actions (inline Menu, Field, Confirm, nested Screen, or Run = blocking work outside the menu that returns the next step), Esc back, `X` mark several, `?` help, `Q` quit. Keys are handled in a worker thread because tabs call `asyncio.run`. `modules/wizard.py` keeps only shared helpers (config writes, sessions, accounts, target labels, country question, `run_wizard_app`)
- Projects (`projects` / `project_accounts` tables, `modules/projects.py`): a project = its member accounts (an account may be in several; a new one is in none and does not work) + its own target (topics, own links, reference links: similar videos and title words of the 5 newest own links, filters) + its own replies (AI / templates). The first start seeds «Основной» from config.yaml (all accounts, the whole pool; `app_settings.projects_seeded`); `video_pool` is UNIQUE(project_id, video_url); the journal keeps `project_id` / `account_id`. `youtube_bot.run_projects` runs the projects «в запуске» in queue order, one `MultiAccountOrchestrator(project=...)` each, one comment one reply across them; the account's lifecycle limits stay per account, `daily_per_account` is an extra ceiling per project
- Tabs: «Запуск» (`tab_launch.py`: start dry / live with confirm, the projects «в запуске» (rows once there are several), threads and `account_mode`, window, today's counters, journal with a project filter and record); «Проекты» (`tab_projects.py`: rows, members, in the launch, daily limit per account, order, rename, copy, delete); «Аккаунты» (`tab_accounts.py`, pure helpers `modules/account_menu_logic.py`: one row per account with state / stage / quota / auto-warm-up icons; grouped by project once there are several; Enter menu: proxy, mode, in work, auto warm-up, warm now, channel profile screen, sign-in, rename, projects, reset restriction; `+` add / import `login:password:TOTP` lines then pick who to warm up, `R` refresh, `X` bulk mode / in work / auto warm-up / add to or remove from a project); «Цель» (`tab_target.py`, the current project: topics, videos per topic, freshness, views, whom to answer, replies per video, own links, reference links, preview); «Ответы» (`tab_replies.py`, the current project: AI DeepSeek or own templates, rewording, templates; the DeepSeek key is global)
- Channel profile (`modules/profile_manager.py`): «Аккаунты» tab → account Enter menu → «Профиль канала» screen with nickname / description / avatar edit rows saved as a DB draft (`accounts.profile_*`), «Применить на YouTube» publishes it through YouTube Studio in the account's own Camoufox profile; `--apply-profile <name>` does the same from the CLI
- Proxy library (`proxies` table, `accounts.proxy_id`, DPAPI-encrypted passwords; address assembled only by `config.compose_proxy_url`): one proxy serves any number of accounts; the search proxy (`app_settings.search_proxy_id`) serves only the InnerTube collector; an account uses only its own proxy or goes direct, and an assigned-but-unreadable proxy raises `ProxyUnavailableError` (fail-closed). Paste parsing `modules/proxy_paste.py`, shared check `modules/proxy_check.py`, yaml migration and route texts `modules/proxy_library.py`. SOCKS5 with a login cannot be used by Camoufox (Playwright refuses it)
- «Прокси» tab (`modules/ui/tab_proxies.py`; `modules/proxy_screen.py` is now only proxy logic, no screens; also reached from an account's «Прокси» menu item): rows per proxy with the accounts on it (Enter: accounts on a proxy, check, edit line, search role, delete); `+` add pastes many lines → shown → checked in parallel → saved → offered to proxy-less accounts, `C` check all, `S` search proxy. «с вашего IP» is shown in the «Запуск» and «Аккаунты» tabs and the run log (`proxy_library.log_proxy_routes`)
- Comprehensive automated test suite (unit and integration tests, `python run_tests.py`; UI tests `tests/test_ui_*.py`)

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
