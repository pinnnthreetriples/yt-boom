---
name: architecture
description: How the major pieces of this project connect and flow. Load when working on system design, integrations, or understanding how components interact.
triggers:
  - "architecture"
  - "system design"
  - "how does X connect to Y"
  - "integration"
  - "flow"
edges:
  - target: context/stack.md
    condition: when specific technology details are needed
  - target: context/decisions.md
    condition: when understanding why the architecture is structured this way
  - target: context/modules.md
    condition: when inspecting module implementations and code-grounded behaviors
grounds_to: []
last_updated: 2026-11-03
mex:
  id: mx_01M69TVZXPPER40BE4QGRY2Q4M
  type: architecture
  status: promoted
  revision: 4
  title: architecture
  relations:
    - type: related_to
      target: mx_01M69TVZYPF61YBDP3CQ4X9555
      note: when specific technology details are needed
    - type: related_to
      target: mx_01M69V8E3R675TR01PVVGBK8EF
      note: when understanding why the architecture is structured this way
    - type: related_to
      target: mx_01M69V83DQFH77W8JJ4KS4EWP9
      note: when inspecting module implementations and code-grounded behaviors
---

# Architecture

## System Overview
The pipeline operates as an automated, multi-account YouTube comment-reply system with strict anti-fraud safeguards:
1. **Target Ingestion & Discovery**: Video URLs or search terms are supplied via CLI, text files, or the interactive console interface (`modules/ui/app.py`, «Цель» tab; shared helpers in `modules/wizard.py`). YouTube InnerTube API queries (`modules/youtube_collector.py`) find candidate videos and harvest popular parent comments and nested sub-comments without requiring official Google Data API credentials. A topic search (`collector.search_query`, «Цель» tab or `--search`) runs through the accounts like the own links: `MultiAccountOrchestrator.run_search` searches via the search proxy (by views without a view cap, by relevance with `max_views`; the next page comes from `continuationItemRenderer`, not the header chips), puts up to `collector.max_videos` finds into `video_pool` (`source = 'search'`, a video found again starts a new round; answered comments are skipped by the payload check) and runs them with every active account, `max_concurrent_sessions`, `account_mode` and the lifecycle limits. `--search --account NAME` keeps the single-account `run_multi_video_pipeline`.
2. **Text Filtering & Payload Generation**: Fetched sub-comments pass through `modules/collector.py` for language validation (`lingua-language-detector`) and text metrics. Qualifying comments are dispatched to DeepSeek API via `modules/processor.py` for context-aware reply generation, followed by typography normalization (`normalize_text`).
3. **Queueing & Multi-Account Orchestration**: Targets and generated payloads are queued in SQLite (`test_metrics.db`) via `database.py`. `modules/multi_account_orchestrator.py` assigns targets across active Google accounts according to concurrency limits and routing modes (`each` or `share`).
4. **Browser Automation & Natural Interaction**: For each target, `modules/youtube_runner.py` drives a persistent `AsyncCamoufox` session with dedicated hardware fingerprints (`modules/fingerprint_manager.py`), pinned proxies, and remote SOCKS5 DNS. Mouse movement follows Bezier curves with speed jitter, human pauses, and natural keystroke timings (`modules/interaction_simulator.py`).
5. **State Handling & In-Situ Dialogs**: The runner detects dynamic reply button DOM states (`<button>` vs `<a href="/create_channel">`), handles first-time channel creation dialogs (`ytd-channel-creation-dialog-renderer`), and verifies comment submission via network interception.

## Key Components
- **`modules.youtube_runner.YouTubeRunner`** — Manages persistent Camoufox browser lifecycles, navigation to `&lc={id}` permalinks, multi-lingual UI interaction, in-situ channel creation, and network response interception for comment verification.
- **`modules.google_authenticator.GoogleAuthenticator`** — Full Google sign-in state machine supporting Material 3 dialogs, 2FA TOTP (RFC 6238), security speedbump dismissal, and automated recovery email verification.
- **`modules.multi_account_orchestrator.MultiAccountOrchestrator`** — Coordinates parallel browser worker sessions across authenticated Google accounts, applying rate limits and target partitioning.
- **`modules.youtube_collector.YouTubeCollector`** — Headless InnerTube client using `curl_cffi` for fast video search and nested comment tree extraction.
- **`modules.processor.DeepSeekProcessor`** — Asynchronous AI reply generator using DeepSeek chat models with fallback support and typo/typography normalizer.
- **`secret_store` & `modules.fingerprint_manager`** — Windows DPAPI credential encryption for local SQLite persistence and persistent hardware profile isolation per account.
- **`database.Database`** — High-concurrency async SQLite storage (`aiosqlite`) tracking nodes, payloads, execution queues, and account credentials.

## External Dependencies
- **YouTube Web & InnerTube API** — Primary target interface accessed via HTTP (`curl_cffi` impersonation) and headful Camoufox browser automation.
- **DeepSeek API (`openai` SDK)** — LLM inference service generating human-like context-aware replies to YouTube comments.
- **Google Accounts Service** — Authenticates accounts across `accounts.google.com` using credentials, TOTP secrets, and recovery emails.
- **Windows DPAPI (`crypt32.dll`)** — OS-level encryption securing account passwords and 2FA secrets on disk.

## What Does NOT Exist Here
- **No official Google Cloud YouTube Data API v3 integration** — All comment extraction uses direct InnerTube client emulation to avoid quota starvation.
- **No external centralized proxy manager or cloud orchestrator** — Proxies and accounts are configured locally in SQLite or YAML.
- **No single-process or Fission-disabled Gecko browser mode** — Browser sessions strictly run multi-process Gecko to prevent Polymer UI deadlocks.
