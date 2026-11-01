# Project: Automated Web UI Testing & Content Aggregation Pipeline (YouTube Comment Replier)

## 1. Architecture Overview
A modular Python 3.11+ automated testing and telemetry pipeline for Windows with YouTube comments-under-comments AI reply bot:
1. **Data Collection Module (Headless HTTP & YouTube InnerTube):** Uses `curl_cffi` to fetch public YouTube comment threads and nested comments under comments (sub-comments / replies), filters items by text metrics and language via `lingua-language-detector`, and processes text payloads via `DeepSeek API` (`openai` SDK).
2. **Browser Test Runner (Headful/Persistent UI Session):** Uses `AsyncCamoufox` with a local test session directory (`./test_sessions/youtube`) and configured stealth parameters to execute automated end-to-end browser workflows (navigating to linked comments `&lc={id}`, dismissing cookie consent, scrolling, expanding replies under comments, and typing natural replies with Bezier mouse movement and human typing cadences).
3. **Telemetry & Deduplication Module:** Tracks processed comments and posted replies in SQLite (`test_metrics.db`) via `aiosqlite` with transactional queueing, state machines, and deduplication (`has_replied_to`).

## 2. Tech Stack & Environment (Windows)
- Python 3.11+ / 3.12+
- Dependencies: `camoufox[geoip]`, `playwright`, `curl_cffi`, `lingua-language-detector`, `openai`, `pydantic`, `pyyaml`, `aiosqlite`, `loguru`.
- Database: SQLite (`test_metrics.db`) via `aiosqlite`.

## 3. Directory Structure
- `config.yaml` — test suite configurations, keyword filters, throttling limits, API keys, execution timeouts
- `config.py` — Pydantic models and config validator
- `database.py` — async SQLite CRUD (`test_nodes`, `payloads`, `execution_queue`, `activity_log` tables) + `has_replied_to` deduplication
- `main.py` — async test orchestrator and rate/throttle manager
- `youtube_bot.py` — standalone CLI entry point for YouTube comment-reply bot (`--video`, `--search`, `--min-views`, `--max-views`, `--max-age-hours`, `--comments-per-video`, `--target-mode`, `--limit`, `--dry-run`, `--login`, `--list-replies`, `--status`, `--history`)
- `modules/collector.py` — public endpoint data fetcher (`curl_cffi`) + language validation filter
- `modules/youtube_collector.py` — YouTube InnerTube topic video search with view count filter & popular/nested comments scraper
- `modules/processor.py` — DeepSeek API text integration + `normalize_text()` typography formatter
- `modules/interaction_simulator.py` — UI interaction helpers: smooth Bezier curve mouse movement with variable frame pacing, smooth wheel scrolling, and natural text input simulation (including typo injection and Backspace correction)
- `modules/ui_test_runner.py` — Camoufox browser automation runner (with YouTube runner delegation)
- `modules/youtube_runner.py` — specialized YouTube sub-comment runner (cookie dismissal, lazy-load comments, expand replies, contenteditable typing, submission)
- `modules/multi_account_orchestrator.py` — replies under the video list with all active accounts: `limits.max_concurrent_sessions` accounts at a time; `limits.account_mode` `each` (every account passes the whole list) or `share` (replies under each video are split between accounts)
- `modules/pool_manager.py` — video list (`video_pool` table): pasted / file links and reply progress
- `modules/validator.py` — read-only DOM/cache rendering verification module
- `modules/wizard.py` — minimalist interactive CMD setup wizard & dashboard (5-step onboarding, search setup, and interactive Activity Audit Log)
- `modules/google_authenticator.py` — automated Google sign-in state machine (TOTP 2FA, speedbump handling, in-situ channel creation)
- `modules/totp_helper.py` — RFC 6238 TOTP engine with time window rollover protection
- `modules/account_parser.py` — single account and batch account file parser (`email:pass:totp[:recovery][:proxy]`)
- `modules/secret_store.py` — Windows DPAPI credential encryption layer for local SQLite storage
- `modules/fingerprint_manager.py` — stable hardware fingerprint manager per browser session directory
- `modules/session_heartbeat.py` — lightweight HTTP session token keep-alive & validity verifier (`curl_cffi` reading `cookies.sqlite`)
- `modules/account_warmer.py` — organic warm-up runner (home-feed video discovery, player telemetry, comments reading, rare likes)
- `modules/lifecycle_engine.py` — 4-day account ageing state machine (quarantine → aging → probation → mature) with daily quota, cooldown and circuit breaker
- `modules/account_menu_logic.py` — pure helpers of the wizard «Аккаунты» screen (warm-up pick, import/warm-up summaries, lifecycle table rows)
- `modules/profile_manager.py` — channel profile: avatar file checks/copies (`profiles/avatars/`) and the YouTube Studio editor (nickname, description, avatar) that publishes the draft stored in the DB
- `tests/` — comprehensive automated test suite (709 unit and integration tests)
- `run_tests.py` — test discovery and test suite runner
- `test_sessions/` — local browser session storage directories

## 4. Technical & Implementation Rules
- **Browser Automation Setup:** Use `camoufox.async_api.AsyncCamoufox` with `persistent_context=True`, `os="windows"`, `geoip=True`.
- **Selector Robustness:** Target YouTube action buttons by structural IDs (e.g. `#reply-button-end :is(button, a)`) rather than locale-dependent strings to support any UI language (English, Russian, Kyrgyz, etc.).
- **Channel State:** Automated handling for initial channel creation dialogs (`ytd-channel-creation-dialog-renderer`) when new Google accounts reply for the first time.
- **Typography Normalizer (`normalize_text`):**
  - Replace em-dashes (`—`, `–`) with standard hyphens `-`.
  - Replace guillemets (`«»`) with standard quotes `""`.
  - Omit trailing periods in 80% of test data payloads.
  - Lowercase initial characters in 20% of test data payloads.
  - Keep test strings short (1-2 sentences).
- **Code Style:** Async/await everywhere (`asyncio`), strict type hints, `pydantic` config validation, `loguru` logging.

## 5. Commands
- Run all automated tests: `python run_tests.py`
- Launch interactive setup wizard: `python youtube_bot.py` (or `python youtube_bot.py --wizard`)
- Authenticate single account: `python youtube_bot.py --auth-account <name>`
- Import accounts from file: `python youtube_bot.py --import-accounts-file <path>`
- List all stored accounts: `python youtube_bot.py --list-accounts`
- Check account session heartbeat (lightweight HTTP): `python youtube_bot.py --heartbeat [account]`
- Run account warm-up: `python youtube_bot.py --warm-up <name> [--warm-up-duration 45]`
- Publish the saved channel profile: `python youtube_bot.py --apply-profile <name>` (the draft is edited in the wizard: Accounts → Channel profile)
- Multi-video search & reply: `python youtube_bot.py --search "python" --min-views 10000 --max-views 500000 --comments-per-video 10 --dry-run`
- View activity history / audit log: `python youtube_bot.py --history`
- List YouTube sub-comments: `python youtube_bot.py --video "https://www.youtube.com/watch?v=..." --list-replies --limit 5`
- Video list with all accounts: `python youtube_bot.py --videos-file videos.txt --concurrency 2 --account-mode each --dry-run`
- Dry run bot on single video: `python youtube_bot.py --video "https://www.youtube.com/watch?v=..." --account <name> --limit 1 --dry-run`
- Interactive YouTube login: `python youtube_bot.py --login`
- Check database metrics: `python youtube_bot.py --status`
- Run orchestrator: `python main.py` (or `python main.py --wizard`)

## 6. MEX Agent Memory Integration
All AI agents (Antigravity, Claude Code, Cursor, etc.) working on this repository must interact with MEX memory (`mex-agent`):
- **Bootstrap Context:** At the beginning of each session or task, read `.mex/ROUTER.md` and load appropriate context files (`.mex/context/`).
- **Architectural Decisions:** Automatically log major architectural choices via:
  ```powershell
  mex log --type decision "<decision and rationale>" --file <file-path>
  ```
- **Critical Findings & Edge Cases:** Record non-obvious quirks, bug resolutions, or DOM discoveries via:
  ```powershell
  mex log --type note "<finding details>" --file <file-path>
  ```
- **Risks & Limits:** Record technical limitations or operational risks via:
  ```powershell
  mex log --type risk "<risk text>"
  ```
- **Sync & Integrity:** Ensure post-commit hooks (`mex watch`) remain enabled, verify health with `mex doctor` and `mex check`, and keep logging cadence at `mex logging significant`.
