---
name: setup
description: Dev environment setup and commands. Load when setting up the project for the first time or when environment issues arise.
triggers:
  - "setup"
  - "install"
  - "environment"
  - "getting started"
  - "how do I run"
  - "local development"
edges:
  - target: context/stack.md
    condition: when specific technology versions or library details are needed
  - target: context/architecture.md
    condition: when understanding how components connect during setup
grounds_to: []
last_updated: 2026-11-03
mex:
  id: mx_01M69TVZYDJAF96PJC4A94TX3T
  type: guide
  status: promoted
  revision: 3
  title: setup
  relations:
    - type: related_to
      target: mx_01M69TVZYPF61YBDP3CQ4X9555
      note: when specific technology versions or library details are needed
    - type: related_to
      target: mx_01M69TVZXPPER40BE4QGRY2Q4M
      note: when understanding how components connect during setup
---

# Setup

## Prerequisites
- **Python 3.11+ / 3.12+** on Windows (or virtual environment in `.venv/`).
- **Camoufox browser binaries**: Installed via `camoufox fetch` during setup.
- **Node.js 20+ & npm**: Required for `mex-agent` memory tools.
- **DeepSeek API Key**: Set in `config.yaml` or via environment variable `DEEPSEEK_API_KEY`.

## First-time Setup
1. **Activate virtual environment**:
   ```powershell
   .\.venv\Scripts\Activate.ps1
   ```
2. **Install dependencies**:
   ```powershell
   pip install -r requirements.txt
   camoufox fetch
   ```
3. **Configure environment & keys**:
   Copy or edit `config.yaml` with your DeepSeek API key and proxy settings if applicable.
4. **Run the console interface** (five tabs: «Запуск», «Аккаунты», «Прокси», «Цель», «Ответы»):
   ```powershell
   python youtube_bot.py --wizard
   ```
5. **Verify setup with unit tests**:
   ```powershell
   python run_tests.py
   ```

## Environment Variables
- `DEEPSEEK_API_KEY` (required if not set in `config.yaml`) — API authentication key for DeepSeek LLM generation.
- `CAMOUFOX_HEADLESS` (optional, default `false`) — Forces headless browser mode for testing.

## Common Commands
- **Run all automated tests**: `python run_tests.py`
- **Launch the console interface**: `python youtube_bot.py` (or `python youtube_bot.py --wizard`)
- **Authenticate Google account**: `python youtube_bot.py --auth-account <name>`
- **Batch import accounts**: `python youtube_bot.py --import-accounts-file <path>`
- **List stored accounts**: `python youtube_bot.py --list-accounts`
- **Search videos & reply**: `python youtube_bot.py --search "<topic>" --min-views 10000 --max-views 500000 --dry-run`
- **Inspect activity audit history**: `python youtube_bot.py --history`
- **Inspect database metrics**: `python youtube_bot.py --status`
- **MEX memory health check**: `mex doctor` and `mex check`

## Common Issues
- **Camoufox profile lock error (`Process already running`)**: Ensure no orphaned Firefox/Camoufox processes remain running in the background. Terminate via Task Manager or `Get-Process camoufox* | Stop-Process`.
- **DPAPI decryption failure (`SecretStoreError`)**: Occurs if `test_metrics.db` was copied from another machine or different Windows user profile. Re-import credentials for that account.
- **YouTube captcha or IP challenge**: Ensure high-quality residential SOCKS5 proxies are configured and DNS remote routing is active (`network.proxy.socks_remote_dns: true`).
