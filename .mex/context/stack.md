---
name: stack
description: Technology stack, library choices, and the reasoning behind them. Load when working with specific technologies or making decisions about libraries and tools.
triggers:
  - "library"
  - "package"
  - "dependency"
  - "which tool"
  - "technology"
edges:
  - target: context/decisions.md
    condition: when the reasoning behind a tech choice is needed
  - target: context/conventions.md
    condition: when understanding how to use a technology in this codebase
  - target: context/modules.md
    condition: when inspecting module implementations and code-grounded behaviors
grounds_to: []
last_updated: 2026-10-31
mex:
  id: mx_01M69TVZYPF61YBDP3CQ4X9555
  type: architecture
  status: promoted
  revision: 4
  title: stack
  relations:
    - type: related_to
      target: mx_01M69V8E3R675TR01PVVGBK8EF
      note: when the reasoning behind a tech choice is needed
    - type: related_to
      target: mx_01M69TVZY58165JDM14J2ZVZZM
      note: when understanding how to use a technology in this codebase
    - type: related_to
      target: mx_01M69V83DQFH77W8JJ4KS4EWP9
      note: when inspecting module implementations and code-grounded behaviors
---

# Stack

## Core Technologies
- **Python 3.11+ / 3.12+** — Primary runtime utilizing modern `asyncio`, type union operators (`X | Y`), and strict static typing.
- **Camoufox (`camoufox[geoip]`)** — Anti-detect stealth browser based on Firefox/Gecko with C++-level anti-fingerprinting, WebRTC IP masking, canvas/audio noise, and geo-affinity.
- **SQLite 3 via `aiosqlite`** — Local persistent datastore with WAL mode, parameterized queries, and ACID transaction isolation.
- **Windows DPAPI (`crypt32.dll` via ctypes)** — Native Windows Current-User Data Protection API for symmetric encryption of credentials at rest.

## Key Libraries
- **`curl_cffi`** — High-speed TLS/JA3 impersonating HTTP client for scraping YouTube InnerTube payloads without triggering Cloudflare/Google bot blocks.
- **`lingua-language-detector`** — High-accuracy natural language detection library used to validate comment language before generating AI responses.
- **`openai` SDK** — Interfacing client for DeepSeek Chat API (`deepseek-chat`).
- **`pydantic` v2** — Configuration validation and runtime schema enforcement (`config.py`).
- **`loguru`** — Structured asynchronous logging framework with colorization and rotating file log sinks.
- **`cryptography` / internal RFC 6238** — Time-based One-Time Password generator with safe window rollover handling (`modules/totp_helper.py`).

## What We Deliberately Do NOT Use
- **No Selenium or Puppeteer** — Standard automation frameworks easily detected by Google bot protections; Camoufox with Playwright async API is strictly enforced.
- **No Chromium / Chrome DevTools Protocol (CDP)** — Chromium-based automation leaks CDP runtime domain instrumentation; Gecko-based Camoufox modifies the browser engine directly.
- **No ORM (e.g. SQLAlchemy, Tortoise)** — Asynchronous raw SQL via `aiosqlite` is used directly to maintain minimal footprint, zero migration drift, and maximum performance.
- **No plain text credentials in SQLite** — Sensitive fields (`password`, `totp_secret`) must be encrypted via `secret_store.protect_secret()` before database insertion.

## Version Constraints
- **Python 3.11+** required for native exception groups and enhanced asyncio task management.
- **Camoufox >= 0.4.0** with GeoIP database integration for accurate IP timezone/locale synthesis.
