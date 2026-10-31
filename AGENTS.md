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
- `tests/` — comprehensive automated test suite (103 unit and integration tests)
- `run_tests.py` — test discovery and test suite runner
- `test_sessions/` — local browser session storage directories

## 4. Technical & Implementation Rules
- **Browser Automation Setup:** Use `camoufox.async_api.AsyncCamoufox` with `persistent_context=True`, `os="windows"`, `geoip=True`.
- **Network Routing:** Configure `firefox_user_prefs={"media.peerconnection.enabled": False, "network.http.http3.enable": False}` for isolated proxy testing.
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
- Multi-video search & reply: `python youtube_bot.py --search "python" --min-views 10000 --max-views 500000 --comments-per-video 10 --dry-run`
- View activity history / audit log: `python youtube_bot.py --history`
- List YouTube sub-comments: `python youtube_bot.py --video "https://www.youtube.com/watch?v=..." --list-replies --limit 5`
- Video list with all accounts: `python youtube_bot.py --videos-file videos.txt --concurrency 2 --account-mode each --dry-run`
- Dry run bot on single video: `python youtube_bot.py --video "https://www.youtube.com/watch?v=..." --limit 2 --dry-run`
- Interactive YouTube login: `python youtube_bot.py --login`
- Check database metrics: `python youtube_bot.py --status`
- Run orchestrator: `python main.py` (or `python main.py --wizard`)