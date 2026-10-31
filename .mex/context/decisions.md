---
name: decisions
description: Key architectural and technical decisions with reasoning. Load when making design choices or understanding why something is built a certain way.
triggers:
  - "why do we"
  - "why is it"
  - "decision"
  - "alternative"
  - "we chose"
edges:
  - target: context/architecture.md
    condition: when a decision relates to system structure
  - target: context/stack.md
    condition: when a decision relates to technology choice
  - target: context/modules.md
    condition: when inspecting module implementations and code-grounded behaviors
grounds_to: []
last_updated: 2026-10-31
mex:
  id: mx_01M69V8E3R675TR01PVVGBK8EF
  type: architecture
  status: promoted
  revision: 4
  title: decisions
  relations:
    - type: related_to
      target: mx_01M69TVZXPPER40BE4QGRY2Q4M
      note: when a decision relates to system structure
    - type: related_to
      target: mx_01M69TVZYPF61YBDP3CQ4X9555
      note: when a decision relates to technology choice
    - type: related_to
      target: mx_01M69V83DQFH77W8JJ4KS4EWP9
      note: when inspecting module implementations and code-grounded behaviors
---

# Decisions

## Decision Log

<!-- mex:entity
id: mx_01M69V8E0QA79GZ0XQR88R4K8C
type: decision
status: promoted
revision: 2
relations:
  - type: refines
    target: mx_01M69V8E3R675TR01PVVGBK8EF
    note: Section of decisions in context/decisions.md
-->
### Gecko Multi-Process Architecture & Fission Enforcement
**Date:** 2026-10-25
**Status:** Active
**Decision:** Never force single-process Gecko (`dom.ipc.processCount: 1`) or disable Fission (`fission.autostart: false`) in Camoufox user preferences.
**Reasoning:** YouTube's Polymer 3 architecture relies extensively on asynchronous custom elements (`ytd-app`, `ytd-comments`, `ytd-comment-thread-renderer`, `ytd-comment-view-model`) and out-of-process iframes. Under single-process mode or forced non-Fission configurations, Gecko's IPC message queues choke under YouTube's heavy DOM mutation tree, leading to infinite loading skeletons on comments, dropped DOM events, and unclickable buttons. Default multi-process Gecko ensures smooth hydration of comment trees.
**Alternatives considered:** Single-process Gecko to conserve RAM (rejected — completely breaks YouTube dynamic DOM and comment tree hydration).
**Consequences:** Camoufox instances use standard process pools with custom timeout and background throttling tweaks (`dom.min_background_timeout_value: 4`, `dom.timeout.enable_budget_timer_throttling: false`).

<!-- mex:entity
id: mx_01M69V8DXKP236JERT76ZX4R8A
type: decision
status: promoted
revision: 2
relations:
  - type: refines
    target: mx_01M69V8E3R675TR01PVVGBK8EF
    note: Section of decisions in context/decisions.md
-->
### Structural Selectors for YouTube Reply Actions (`#reply-button-end :is(button, a)`)
**Date:** 2026-10-27
**Status:** Active
**Decision:** Target YouTube comment action triggers using the structural selector `#reply-button-end :is(button, a)` rather than `button` tags or textual labels.
**Reasoning:** Deep DOM inspection revealed that for established accounts with existing YouTube channels, YouTube renders an active `<button>` element inside `#reply-button-end`. However, for fresh Google accounts that have never created a channel, YouTube renders an `<a href="/create_channel?..." role="button">` link inside `#reply-button-end`. A selector targeting only `button` completely misses the reply trigger for all new accounts.
**Alternatives considered:** Querying `button` first and falling back to `a` (rejected — `:is(button, a)` provides atomic CSS selector resolution in a single DOM query).
**Consequences:** Both channel and non-channel accounts successfully trigger the reply workflow without branching selector logic.

<!-- mex:entity
id: mx_01M69V8DTHVR942QB3DX8DHGPA
type: decision
status: promoted
revision: 2
relations:
  - type: refines
    target: mx_01M69V8E3R675TR01PVVGBK8EF
    note: Section of decisions in context/decisions.md
-->
### Universal Structural Selectors for Multi-Lingual UI Support (Kyrgyz Locale Evidence)
**Date:** 2026-10-28
**Status:** Active
**Decision:** Eliminate all textual button matching (e.g. `button:has-text("Reply")`, `button:has-text("Ответить")`) as primary selectors, relying on immutable structural component IDs and attributes.
**Reasoning:** When testing Google accounts configured in non-major or regional languages (e.g. Kyrgyz `ky`, where "Reply" is rendered as `Жооп` and "Create Channel" is rendered as `Канал түзүү`), text-based locators catastrophically fail to resolve. Structural identifiers like `#reply-button-end`, `ytd-channel-creation-dialog-renderer`, and `[linked-comment-badge]` remain identical across all language locales worldwide.
**Alternatives considered:** Multi-lingual dictionary lookup maps for 100+ YouTube languages (rejected — extremely brittle and prone to UI copy changes).
**Consequences:** The bot operates deterministically in any language (English, Russian, Kyrgyz, Turkish, Spanish, etc.).

<!-- mex:entity
id: mx_01M69V8DQT3GH6HC3RXH312MP9
type: decision
status: promoted
revision: 2
relations:
  - type: refines
    target: mx_01M69V8E3R675TR01PVVGBK8EF
    note: Section of decisions in context/decisions.md
-->
### In-Situ Channel Creation Flow Handling
**Date:** 2026-10-28
**Status:** Active
**Decision:** Implement automated modal interception for `ytd-channel-creation-dialog-renderer` during the comment reply lifecycle.
**Reasoning:** When a fresh account clicks the reply anchor (`<a href="/create_channel?...">`), YouTube does not immediately navigate to a new page; instead, it pops up an in-situ modal dialog `ytd-channel-creation-dialog-renderer` prompting the user to confirm their handle and profile name. If unhandled, this modal blocks the reply textarea and submission button.
**Alternatives considered:** Pre-creating channels via YouTube Studio before launching the reply bot (rejected — adds friction to account onboarding and can trigger extra verification).
**Consequences:** `YouTubeRunner._confirm_channel_creation` detects the modal, clicks `#create-channel-button button`, waits for modal dismissal, and seamlessly continues the reply workflow.

<!-- mex:entity
id: mx_01M69V8DN1GXXERQA7W5RA4S7P
type: decision
status: promoted
revision: 2
relations:
  - type: refines
    target: mx_01M69V8E3R675TR01PVVGBK8EF
    note: Section of decisions in context/decisions.md
-->
### Robust Google Authenticator State Machine & Material 3 Alert Filtering
**Date:** 2026-10-29
**Status:** Active
**Decision:** Build a multi-step Google authentication state machine that explicitly filters non-error informative banners inside `[role="alert"]` / `[aria-live="assertive"]`, handles RFC 6238 TOTP window safety, and dismisses security interstitials with "Cancel" / "Not now".
**Reasoning:**
1. Google's Material 3 redesign renders normal page headers and assistive descriptions (e.g. "To continue to YouTube", "Enter your password") inside `[role="alert"]` or `[aria-live="assertive"]`. Blind error scrapers flag these as authentication failures.
2. If an authentication code is submitted near the end of the 30-second TOTP window, network latency causes server rejection. If retried immediately with the same token, Google rejects it again.
3. Recovery interstitials and passkey upgrade screens must be declined ("Cancel", "Not now", `DECLINE_BUTTONS`) rather than accepted, which would trigger native Windows Hello prompts.
**Alternatives considered:** Manual login for all accounts (rejected — prevents automated scaling and headless headless verification).
**Consequences:** Robust, hands-free authentication with zero false-positive error flags on standard Google login screens.

<!-- mex:entity
id: mx_01M69V8DJ8BDQTD1V1999XAXTY
type: decision
status: promoted
revision: 2
relations:
  - type: refines
    target: mx_01M69V8E3R675TR01PVVGBK8EF
    note: Section of decisions in context/decisions.md
-->
### Windows DPAPI for Local SQLite Credential Encryption
**Date:** 2026-10-29
**Status:** Active
**Decision:** Encrypt all stored account passwords and TOTP secrets in SQLite using Windows DPAPI (`CryptProtectData` / `CryptUnprotectData`).
**Reasoning:** Storing raw account credentials in `test_metrics.db` presents a critical security vulnerability if the database file is backed up, inspected, or leaked. Windows DPAPI provides hardware- and OS-account-bound encryption with zero secret management overhead: only the current Windows user session can decrypt the data.
**Alternatives considered:** Symmetric AES encryption with a hardcoded application key (rejected — hardcoded keys are easily extracted; DPAPI ties encryption to the Windows logon credential).
**Consequences:** Database exports or copies onto other machines or Windows profiles cannot decrypt stored Google credentials.

<!-- mex:entity
id: mx_01M69V8DDVEDW64F8538AAFY27
type: decision
status: promoted
revision: 2
relations:
  - type: refines
    target: mx_01M69V8E3R675TR01PVVGBK8EF
    note: Section of decisions in context/decisions.md
-->
### Anti-Fraud, WebRTC Masking, and SOCKS5 Remote DNS Configuration
**Date:** 2026-10-30
**Status:** Active
**Decision:** Force `network.proxy.socks_remote_dns: true`, disable WebRTC local IP leakage, and pin persistent hardware fingerprints per account session directory.
**Reasoning:**
1. Default SOCKS5 proxy configurations leak DNS requests to the local network adapter, exposing the user's real ISP location and DNS servers to Google.
2. WebRTC STUN/TURN packets bypass standard proxy routing, revealing local and public LAN IPs. Camoufox WebRTC masking binds IP resolution to the active proxy.
3. Constantly regenerating device fingerprints (canvas, WebGL, audio buffer) on every browser launch triggers Google security challenges for suspicious session anomalies. Pinning the account hardware fingerprint configuration ensures hardware identity continuity.
**Alternatives considered:** Dynamic proxy rotation on every request (rejected — triggers immediate Google account suspension due to geo-hopping).
**Consequences:** Sessions maintain stable geo-affinity and zero network fingerprint leaks.
