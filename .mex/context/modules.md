---
name: modules
description: Core subsystems and modules implementing browser automation, authentication, InnerTube collection, and telemetry.
triggers:
  - "modules"
  - "youtube runner"
  - "google authenticator"
  - "totp"
  - "fingerprint"
  - "collector"
edges:
  - target: context/architecture.md
    condition: when understanding how modules fit into system design
  - target: context/decisions.md
    condition: when reviewing architectural decisions affecting modules
  - target: context/conventions.md
    condition: when extending or modifying module code
grounds_to: []
last_updated: 2026-11-03
mex:
  id: mx_01M69V83DQFH77W8JJ4KS4EWP9
  type: component
  status: promoted
  revision: 4
  title: modules
  relations:
    - type: related_to
      target: mx_01M69TVZXPPER40BE4QGRY2Q4M
      note: when understanding how modules fit into system design
    - type: related_to
      target: mx_01M69V8E3R675TR01PVVGBK8EF
      note: when reviewing architectural decisions affecting modules
    - type: related_to
      target: mx_01M69TVZY58165JDM14J2ZVZZM
      note: when extending or modifying module code
---

# Modules Subsystem

## Overview
The `modules/` directory contains the core implementation layer of the YouTube comment reply and telemetry pipeline:
- **`youtube_runner.py` & `ui_test_runner.py`**: Browser automation engine managing Camoufox lifecycles, DOM traversal, permalink navigation, and response interception.
- **`google_authenticator.py` & `totp_helper.py`**: Automated Google account authentication, 2FA challenge handling, and security speedbump resolution.
- **`account_parser.py`, `fingerprint_manager.py` & `secret_store.py`**: Account credential parsing, hardware fingerprint generation/pinning, and Windows DPAPI encryption.
- **`youtube_collector.py` & `collector.py`**: High-performance HTTP-level InnerTube search and nested comment thread extraction.
- **`processor.py` & `interaction_simulator.py`**: DeepSeek AI text payload generation, typography formatting, and human-like Bezier cursor/keyboard simulation.
- **`multi_account_orchestrator.py` & `pool_manager.py`**: Target queue coordination across multiple browser contexts and accounts.
- **`modules/ui/`, `wizard.py`, `account_menu_logic.py`, `proxy_screen.py` & `tui.py`**: Terminal UI. `ui/app.py:run_app` opens one screen with five tabs (`ui/tab_launch.py`, `tab_accounts.py`, `tab_proxies.py`, `tab_target.py`, `tab_replies.py`) rendered by `ui/shell.py` (Controller / render / `run_shell`; keys handled in a worker thread because tabs call `asyncio.run`; `ui/icons.py` picks emoji or plain symbols). `wizard.py` is now only shared helpers (config writes, sessions, accounts, target plan, country question, `run_wizard_app`); `account_menu_logic.py` holds the pure parts of the «Аккаунты» tab (warm-up pick, import summary, card rows, profile pending fields); `proxy_screen.py` is proxy logic for the «Прокси» tab (no screens).

<!-- mex:entity
id: mx_01M69V83D1FY43YN45W86J7DN9
type: fact
status: promoted
revision: 2
metadata:
  aliases:
    - reply button selector
    - channel creation anchor
    - reply not clickable
    - youtube locale reply
    - is button a
relations:
  - type: refines
    target: mx_01M69V83DQFH77W8JJ4KS4EWP9
    note: Part of modules in context/modules.md
provenance:
  createdBy:
    kind: agent
    id: claude
  createdAt: 2026-10-31T20:43:00Z
grounds_to:
  - file: modules/youtube_runner.py
    node: class:85ef316144e0d4344be7913a9cee5599
    fingerprint: mh2:64:8B0ACCC6AJLGjAAjMu0ANhf9AJ2IhwCJo2MAGsYCAILzvAANINUAZ2LrAEL38AA6Ly4BCbx_AGUTMgAnEbwAC1V2ACz60gBzStIAm1_-ABe5FgEiNqQAAFmgABAKDQCBl2sAcycWADZV3gBf6R4AoWVhAHY6tgAZISIAK97AAQwfWAAGsfkAKe9tAAonowAY3IcBJs3fAAz3cwACj6QAV0JzADnwXgBYL7kADJzBABNTjwBViK0AS7HsADim4QAy5ckAanIyAU1lUgDq91IACsL3ACEF6AB1baAAK2e3ABB75wD607UABo7jAIoZCgB-ffsAECwLAV1jqwAtEfoAPyvlAAA
    bodyHash: 4add449f78da828b3cb9405beb660d06c31a6d764a966463648a29e2cc8aae79
-->
## Dynamic Reply Button Structural Selector

YouTube renders a standard button tag inside #reply-button-end for accounts with an active channel, but renders an anchor tag with href=/create_channel for non-channel accounts. The structural selector #reply-button-end :is(button, a) matches both variants across all locales without requiring text-based matching.

<!-- mex:entity
id: mx_01M69V83EEQ4MW6R1704RH4P3T
type: fact
status: promoted
revision: 2
metadata:
  aliases:
    - first reply modal
    - create channel dialog
    - channel creation popup
    - ytd-channel-creation-dialog
    - dismiss channel prompt
relations:
  - type: refines
    target: mx_01M69V83DQFH77W8JJ4KS4EWP9
    note: Part of modules in context/modules.md
provenance:
  createdBy:
    kind: agent
    id: claude
  createdAt: 2026-10-31T20:43:00Z
grounds_to:
  - file: modules/youtube_runner.py
    node: class:85ef316144e0d4344be7913a9cee5599
    fingerprint: mh2:64:8B0ACCC6AJLGjAAjMu0ANhf9AJ2IhwCJo2MAGsYCAILzvAANINUAZ2LrAEL38AA6Ly4BCbx_AGUTMgAnEbwAC1V2ACz60gBzStIAm1_-ABe5FgEiNqQAAFmgABAKDQCBl2sAcycWADZV3gBf6R4AoWVhAHY6tgAZISIAK97AAQwfWAAGsfkAKe9tAAonowAY3IcBJs3fAAz3cwACj6QAV0JzADnwXgBYL7kADJzBABNTjwBViK0AS7HsADim4QAy5ckAanIyAU1lUgDq91IACsL3ACEF6AB1baAAK2e3ABB75wD607UABo7jAIoZCgB-ffsAECwLAV1jqwAtEfoAPyvlAAA
    bodyHash: 4add449f78da828b3cb9405beb660d06c31a6d764a966463648a29e2cc8aae79
-->
## In-Situ Channel Creation Dialog Confirmation

When an account without a channel triggers a reply, YouTube displays an in-situ modal dialog ytd-channel-creation-dialog-renderer. _confirm_channel_creation detects this modal, clicks #create-channel-button button, and waits for dialog disappearance before proceeding with reply submission.

<!-- mex:entity
id: mx_01M69V83EVJS28SR4738FTRSWZ
type: fact
status: promoted
revision: 2
metadata:
  aliases:
    - gecko multi-process
    - fission autostart
    - polymer blank page
    - socks remote dns
    - background timer throttling
relations:
  - type: refines
    target: mx_01M69V83DQFH77W8JJ4KS4EWP9
    note: Part of modules in context/modules.md
provenance:
  createdBy:
    kind: agent
    id: claude
  createdAt: 2026-10-31T20:43:00Z
grounds_to:
  - file: modules/youtube_runner.py
    node: class:85ef316144e0d4344be7913a9cee5599
    fingerprint: mh2:64:8B0ACCC6AJLGjAAjMu0ANhf9AJ2IhwCJo2MAGsYCAILzvAANINUAZ2LrAEL38AA6Ly4BCbx_AGUTMgAnEbwAC1V2ACz60gBzStIAm1_-ABe5FgEiNqQAAFmgABAKDQCBl2sAcycWADZV3gBf6R4AoWVhAHY6tgAZISIAK97AAQwfWAAGsfkAKe9tAAonowAY3IcBJs3fAAz3cwACj6QAV0JzADnwXgBYL7kADJzBABNTjwBViK0AS7HsADim4QAy5ckAanIyAU1lUgDq91IACsL3ACEF6AB1baAAK2e3ABB75wD607UABo7jAIoZCgB-ffsAECwLAV1jqwAtEfoAPyvlAAA
    bodyHash: 4add449f78da828b3cb9405beb660d06c31a6d764a966463648a29e2cc8aae79
-->
## Gecko Multi-Process and Background Performance Preferences

Camoufox browser launch options in _launch_options explicitly set dom.min_background_timeout_value to 4, dom.timeout.enable_budget_timer_throttling to False, network.proxy.socks_remote_dns to True, and preserve native multi-process Gecko execution to prevent Polymer DOM rendering deadlocks and DNS leakage.

<!-- mex:entity
id: mx_01M69V83F787NRQF2BY9CP7PFJ
type: fact
status: promoted
revision: 2
metadata:
  aliases:
    - aria-live false error
    - google login error false positive
    - material 3 alert filter
    - role alert banner
    - sign in stuck error
relations:
  - type: refines
    target: mx_01M69V83DQFH77W8JJ4KS4EWP9
    note: Part of modules in context/modules.md
provenance:
  createdBy:
    kind: agent
    id: claude
  createdAt: 2026-10-31T20:43:00Z
grounds_to:
  - file: modules/google_authenticator.py
    node: class:c0c2d2ee349a3dee74942def60291483
    fingerprint: mh2:64:5RsAiTAnAJLGjAAjMu0ANhf9AJ2IhwAvuhQAGsYCAILzvAACLoYAU7PCAF6OnACr5c4ABd5_AI6e6QAnEbwAicbSAYxy0QAcio0Bu5WOABe5FgCDROgAAFmgABAKDQCV-t8Bi3h5ADZV3gBf6R4AoWVhABMbaAAZISIApav2AQwfWAAoDkcAKe9tAD2wZQATjUcBnxoTAAz3cwACj6QAV0JzAPZ2egB73HoADJzBABNTjwBViK0ANKY-AIPpDgD9iq8AanIyAD5FVADNt_sAGNgkALsctAAHvq4AK2e3ABB75wAN8k0AGeHYAIoZCgELVL8AECwLAGgbXgAJHdYAHRaTAAA
    bodyHash: a2bbbcbad5126a0b9e2f8d4e6c563776c537af6a1e4d9cabf0f44b93b3f4707e
-->
## Material 3 Informative Banner Filtering

_visible_error_text checks aria-live=assertive and role=alert elements while filtering out benign Material 3 headers and banners defined in _NON_ERROR_STRINGS such as Welcome, Enter your password, and 2-step verification to avoid false-positive authentication error detection.

<!-- mex:entity
id: mx_01M69V83FJG4PY67ZHZJJT34H9
type: fact
status: promoted
revision: 2
metadata:
  aliases:
    - google passkey prompt
    - decline recovery speedbump
    - skip phone verification
    - windows hello bypass
    - login interstitial cancel
relations:
  - type: refines
    target: mx_01M69V83DQFH77W8JJ4KS4EWP9
    note: Part of modules in context/modules.md
provenance:
  createdBy:
    kind: agent
    id: claude
  createdAt: 2026-10-31T20:43:00Z
grounds_to:
  - file: modules/google_authenticator.py
    node: class:c0c2d2ee349a3dee74942def60291483
    fingerprint: mh2:64:5RsAiTAnAJLGjAAjMu0ANhf9AJ2IhwAvuhQAGsYCAILzvAACLoYAU7PCAF6OnACr5c4ABd5_AI6e6QAnEbwAicbSAYxy0QAcio0Bu5WOABe5FgCDROgAAFmgABAKDQCV-t8Bi3h5ADZV3gBf6R4AoWVhABMbaAAZISIApav2AQwfWAAoDkcAKe9tAD2wZQATjUcBnxoTAAz3cwACj6QAV0JzAPZ2egB73HoADJzBABNTjwBViK0ANKY-AIPpDgD9iq8AanIyAD5FVADNt_sAGNgkALsctAAHvq4AK2e3ABB75wAN8k0AGeHYAIoZCgELVL8AECwLAGgbXgAJHdYAHRaTAAA
    bodyHash: a2bbbcbad5126a0b9e2f8d4e6c563776c537af6a1e4d9cabf0f44b93b3f4707e
-->
## Google Sign-in Security Speedbump Interstitial Dismissal

_handle_screen inspects candidate actions for security speedbumps and interstitials before processing login fields, clicking DECLINE_BUTTONS (Not now, Skip, Cancel) to prevent native Windows Hello passkey prompts or phone verification locks.

<!-- mex:entity
id: mx_01M69V83FYDGV8V9N720KQEVDP
type: fact
status: promoted
revision: 1
metadata:
  aliases:
    - totp rejected in transit
    - 2fa time window
    - totp code expired
    - rfc 6238 rollover
    - google 2fa retry
relations:
  - type: refines
    target: mx_01M69V83DQFH77W8JJ4KS4EWP9
    note: Part of modules in context/modules.md
provenance:
  createdBy:
    kind: agent
    id: claude
  createdAt: 2026-10-31T20:43:00Z
grounds_to:
  - file: modules/totp_helper.py
    node: function:771578f496e43623d561797fde79603d
    fingerprint: mh2:64:Jg1FxmwFN1HPChJgBAs62QgDd5lbBdYm2AkGZvUPw1WJBrP_UwMLqzcBEUxwA8trqgKEkM4HSEE-F7nfLQGm_XIFrYDvENXWrwxJbcQAIMPuDc-ABAp_GqwAEAoNAJX63wir1lkFBwarD6pGBQt1FisLKQTjAppWVwHkpb4TsAmIAENuRw-u3YwFSnBnB7bA-AHlTlIDl-yNA2UvmQocznIE9t9WCKLF0w8PY8sKuGaCHVJtFwezOl4BkIJCB1oL_gj7g1oFicDaBPnEvgVzGlYPyUYsC5hsARNa05wEL0wOBtUADBSjElsCzn09BlnC0QP6ZwwANB8XEFvd_gCy8E0CCGZ1bmN0aW9uBm1ldGhvZAQABZK2d1JuQMm5DFFmK3VpgACA5cAwg0SZ3wujVyfIR_1WAP9kvBQo9FhIpe79azygcfYBd0-4qE-B3icoDzPh5MkO4g
    bodyHash: de4050b009b6cdfd0584fda79fbbd66e715eefe903ed763d1f35e1f6c86705f1
-->
## RFC 6238 TOTP Time Window Rollover Guard

generate_fresh_totp checks remaining time in the current 30-second RFC 6238 window and sleeps if less than 2.0 seconds remain, preventing code expiry in transit while ensuring subsequent retries wait out the current window.

<!-- mex:entity
id: mx_01M69V83G98FV8QFYQ5BZE6WZD
type: fact
status: promoted
revision: 2
metadata:
  aliases:
    - session fingerprint reuse
    - pin camoufox fingerprint
    - browser identity drift
    - fingerprint json
    - hardware profile lock
relations:
  - type: refines
    target: mx_01M69V83DQFH77W8JJ4KS4EWP9
    note: Part of modules in context/modules.md
provenance:
  createdBy:
    kind: agent
    id: claude
  createdAt: 2026-10-31T20:43:00Z
grounds_to:
  - file: modules/fingerprint_manager.py
    node: function:395aae1edefb6a66fa1262fbd0678998
    fingerprint: mh2:64:4AEBETRmA1BViQPBgCIAYcNMADitfwHL0vAAZUJaAQMmtQD7tsIDC6s3ARFMcAAIJ50ChJDOApdzOgBJZz0Bpv1yALJDigEWGqoCO9WzAVCoDQCmqrMB6BhkABuabACV-t8B2FU8A1kiEwLwxawA_X64ATWQzQFmOe4AK97ABQI_eABDbkcCj9O4AFsP2AFioVwClhKsAAz3cwNlL5kAV0JzAcS_ugQ6czoADJzBBjc6wABViK0AS7HsAP2N5gKCzWUBKHU_AV4XVQDq91IBpc_cALsctAB7AUYA_vFbASlVdwM68x0AG33ZAMpWpQMGFEgBCgpqAXsehAFfY3kAHRaTAghmdW5jdGlvbgZtZXRob2QOAD_MefwjYjq5gXgNwiu1zTgAUjv8APXEa7XKQE3l5aMfyQC9E-jCKizVslyenTCrSePwATavbYzSkO1o5nuZg545sPQBN3v7YlSNqOgTk4c1v2gjxQFHtbXncurKuZB4EHv07dueAU_npnhytWH50_LWKG9uhB4BXSt0bN8zOrTVzulccMoZLQFiIo_en1e4B-jiVOsSmFOMAZjQxHqw4qfrZe6Rf3AMHJAB45gY6LJikwJWN3Z6hRzatwHqqU0ej5fW-ytqx3ByJ4fzAfnvR87WP8dNwIAET5_maDUB-t0XXX7HzXlA4EO600xEtA
    bodyHash: 31b0556c265976798c91866abe9060cb872167237769caeaa5d6871f71c28136
-->
## Persistent Hardware Fingerprint Pinning

get_or_create_fingerprint generates a hardware fingerprint via Camoufox on initial account session creation, atomically stores it in fingerprint.json with the target OS tag, and reuses it on subsequent launches to prevent session identity drift.

<!-- mex:entity
id: mx_01M69V83GPKNH4J72XZZQGK9D3
type: fact
status: promoted
revision: 1
metadata:
  aliases:
    - encrypt password sqlite
    - windows dpapi store
    - cryptprotectdata credentials
    - decrypt dpapi secret
    - sqlite credential security
relations:
  - type: refines
    target: mx_01M69V83DQFH77W8JJ4KS4EWP9
    note: Part of modules in context/modules.md
provenance:
  createdBy:
    kind: agent
    id: claude
  createdAt: 2026-10-31T20:43:00Z
grounds_to:
  - file: secret_store.py
    node: function:d8de541a79387882c604db3f0ddf7724
    fingerprint: mh2:64:RwEU28cBCC8WAcAfewA2F_0Gj27IBdYm2AkGZvUA3fPIABlAcwVJ3pcABfw3AKvlzgKEkM4D2eK8AHR3vQGm_XIHETztCA7FAwH-jr0H2y33DJpBKgp_GqwHh8ADCAAfSQOhi3AFFnqNC_7MCQD9frgFY6CkAcAGcADwYnED0z6lAENuRwQ_45oBxIE2DzJFZwXRkx8ApdqNA2UvmQcfyzoLl1OuBJd-pAVdj3EEXD5AGi4xqwezOl4FCT7cB1oL_gD9h_EHMIugANrCiAZ6BEIUqSedCArqgQFHrRcEL0wOAzrzHQu26PwIP0laBF9cLwAfYHIE0A2uCLWPwACy8E0CCGZ1bmN0aW9uBm1ldGhvZAQA84KVQIdFX66e0KOhbULuJQGWHhO3-iNeiSHTgRJNtGnVAaa-RVQcBZiGXoZx0wsGHSIB8G9csUy-e5MXzL0kdgyYiA
    bodyHash: a051015286928797b60531c7306017959999d132630d2db73bef614dfca45646
-->
## Windows DPAPI Credential Encryption

protect_secret uses ctypes to invoke Windows CryptProtectData in current-user scope, prefixing output with dpapi: and base64-encoding the ciphertext for safe local SQLite storage.

<!-- mex:entity
id: mx_01M69V83H3XVCQBTA70NYP4ACS
type: fact
status: promoted
revision: 1
metadata:
  aliases:
    - import accounts file format
    - email pass totp string
    - parse account proxy
    - account import delimiter
    - recovery email line format
relations:
  - type: refines
    target: mx_01M69V83DQFH77W8JJ4KS4EWP9
    note: Part of modules in context/modules.md
provenance:
  createdBy:
    kind: agent
    id: claude
  createdAt: 2026-10-31T20:43:00Z
grounds_to:
  - file: modules/account_parser.py
    node: function:ea8bb37b531e01939dee3e12e1ba71b9
    fingerprint: mh2:64:6wEAiTAnAQgvFgBASEwCDf59AHyy7QMGwIEAZUJaAILzvAD7tsIDC6s3ARFMcACVUg4ChJDOAlcM6wBJZz0Ay3RvA1Sz5gBzStIBxAetACDD7gHWcwwBmO9_AH9LSgCV-t8CYO8RAG1iwgBf6R4AoWVhB791MgAZISIAN1WUBEPpiABDbkcCbkXoAAtbAwSEW3oClhKsAAz3cwI6F34B9DaSBPbfVgSTL_0ADJzBAEgyyABViK0B9-Z9AUW-4ALKD1gHU3N4AV4XVQGDthgDjsj0Anv-dwB1baAAg8P7BC9MDgAaD6UAG33ZA4MR0wRfXC8E_WTaAXsehAAtEfoAsvBNAghmdW5jdGlvbgZtZXRob2QTAGVig5ke7Vlh1tZufLEbIhsAkV4F3HHaBDTG6i8Fkd8ERQDoq7glSJNVtVaeqS6zn5srAQ3CdH0ejr2NPuCAv2UAiFYBTkRJWAnNUjWUroTUTxQ1DAFPsI13BS_Cqr6ze9Vk8yH_AV2Ls7HxGzmeNEdHqTvfuugBZGnG2InmdNZx-jwac3Vh0wF-NwOqWAxlDnH90hVqMA9ZAZdskGK2oyDRVtK2vB_jsFQBocCa5sEpahvESl2QjMZlkgG84j3R_LV3GYVJ5UQHXAE4Ab7RNS9A9zirLkBncpi8AJwBy4wyVkw-YgfDjjWOBCDPhwHeAmo3cqymp_iTuRlC16gwAeWdYsYt3HjK2hOC2807fPsB8bXZrm7vp2M9jkh28blhawH4YpKJpc1tb9ixe4-QDJaRAf9F0zPD3cmzT8b1JmX7N_8
    bodyHash: 51609aec9e0122e885748d461b98acf3cd4c8a1c15f10ae3df112c4d9abdf48f
-->
## Multi-Field Account Credential String Parsing

parse_account_line splits colon-separated account strings into email, password, totp_secret, optional recovery_email, and optional proxy, handling colons in passwords and normalising whitespace.

<!-- mex:entity
id: mx_01M69V83HGBPRPMNPJY0A2P93M
type: fact
status: promoted
revision: 2
metadata:
  aliases:
    - bezier mouse movement
    - human typing cadence
    - bot detection mouse curve
    - anti-bot interaction
    - typo simulation
relations:
  - type: refines
    target: mx_01M69V83DQFH77W8JJ4KS4EWP9
    note: Part of modules in context/modules.md
provenance:
  createdBy:
    kind: agent
    id: claude
  createdAt: 2026-10-31T20:43:00Z
grounds_to:
  - file: modules/interaction_simulator.py
    node: class:44282ff915cd75804344cb644948bd70
    fingerprint: mh2:64:xxQAiTAnAJLGjAAjMu0ANhf9AKWZnwCTUcMAGsYCAIOsugAGBCYAU7PCARFMcACr5c4ACJr0ARWM9wAnEbwAC1V2AYxy0QAh0FsBFGhLACDD7gBkzbUAAFmgABAKDQCV-t8AcycWAI8CgABlYmEAoWVhALXd3QAVlAMA8GJxAnBCUwBDbkcAKe9tAD2wZQATjUcBJs3fAAz3cwACj6QAV0JzAD0_ugB73HoADJzBABNTjwAMECUAdqjpACkmIwEiNhEAanIyAN6XYAGkXUkACsL3ACEF6ACMdGUApwA_ALyhcgBFGVwAG33ZAB4bowAWwL8AEGZ6ATlobwAhuxQAsvBNAAA
    bodyHash: 9ceed63b414402405fd65c32965851140ee685cc80f6c43a06b924f3784634ac
-->
## Humanized Bezier Curve Mouse Movement and Typing Simulation

InteractionSimulator implements cubic Bezier mouse curves with non-linear pacing, mouse wheel acceleration, and character-by-character typing simulation with configurable typo injection and backspace correction.

<!-- mex:entity
id: mx_01M69V83HXK3D5H7HJSX9XP6X3
type: fact
status: promoted
revision: 1
metadata:
  aliases:
    - strip em dash reply
    - normalize ai output
    - humanize comment text
    - lowercase initial letter
    - drop trailing period
relations:
  - type: refines
    target: mx_01M69V83DQFH77W8JJ4KS4EWP9
    note: Part of modules in context/modules.md
provenance:
  createdBy:
    kind: agent
    id: claude
  createdAt: 2026-10-31T20:43:00Z
grounds_to:
  - file: modules/processor.py
    node: function:63433454e2436fca3ebd7720380011d0
    fingerprint: mh2:64:wwIAiTAnB2XgtAAjMu0ANhf9AfI7vAHL0vAAqX-lAtRyOAD7tsIDC6s3AMb_igCr5c4ChJDOAydYeQGTtPoAwuuDAuGmtwCACPgB_o69ACDD7gEiNqQFBnsGABCpCQW6qOQBqIf6AwC27ABf6R4AoWVhBmg6tAAZISIA0e7YAX3clwBDbkcCIpnUAcSBNgM5wHgDk1h9AKXajQNlL5kB9DaSACKByANbBdAADJzBAKcMuABViK0AS7HsAsEtbQIU0tEGGfRVAV4XVQHPVwcCUkO7BAOMPgM4tKkBF-IJAYHyrAAN8k0CNSrQBa3PRQELVL8AH2ByAcm9CQUJFQAAsvBNAghmdW5jdGlvbgZtZXRob2QGALD0mwo6XwCMx1RZuwdV13EBLgpPtwqE_rmFaYt7yl5z4AFUCGqEwpZsB9tshk3s6rfQAd_aZkpJA0rD3c2nmezHbQMB74A5t-6cvVtKwkcs1KieYQH0JvQDWHSabDqL9qrSm6p7
    bodyHash: 29f6450a0660b846043bab197afdec2348e3ee5db96836ce7f2d70ef8600eb5d
-->
## Typography Normalization and Sentence Truncation

normalize_text transforms AI-generated reply payloads by replacing em/en-dashes with standard hyphens, substituting curly quotes, optionally omitting trailing periods (80% probability), lowercasing initial letters (20% probability), and limiting text to max 2 sentences.

<!-- mex:entity
id: mx_01M69V83JBDF0G7P1FQ3N7GYYT
type: fact
status: promoted
revision: 2
metadata:
  aliases:
    - multi account concurrency
    - account mode each share
    - parallel youtube sessions
    - split videos accounts
    - account rotation queue
relations:
  - type: refines
    target: mx_01M69V83DQFH77W8JJ4KS4EWP9
    note: Part of modules in context/modules.md
provenance:
  createdBy:
    kind: agent
    id: claude
  createdAt: 2026-10-31T20:43:00Z
grounds_to:
  - file: modules/multi_account_orchestrator.py
    node: class:9f36d77b1dd835ea96e34e5417aa8189
    fingerprint: mh2:64:qx4ACCC6AJLGjAB8NIgANhf9AJ2IhwAjdmUAGsYCAILzvAACLoYAU7PCAInIMAA6Ly4ABd5_AI6e6QBJZz0AicbSADgz6wBmH7wBFGhLABe5FgC4L80AU7KSABAKDQBXiaIAcycWAEISzwBf6R4AoWVhALXd3QAVlAMAK97AAW5k4wBDbkcAKe9tAA2ThQFioVwAFbWSAAz3cwACj6QAV0JzADnwXgBYL7kADJzBABNTjwA6M2gAS7HsADim4QD9iq8AeSW1AGQeYADq91IBEhruALsctAB1baAAK2e3AFkUCwAaD6UAG33ZADNBbAClF9cAH2ByAHex5gAtEfoAhMkDAAA
    bodyHash: a75fc99182321442bf4b9a472bc85f200bd3b8ba2b3aec25f8143d1c50b6f6b3
-->
## Multi-Account Parallel Execution Modes

MultiAccountOrchestrator manages parallel execution across active Google accounts bounded by max_concurrent_sessions, supporting each mode where every account processes all target videos and share mode where target videos are divided among accounts.

<!-- mex:entity
id: mx_01M69V83JTY6Z0M6ZMKNHGT9YS
type: fact
status: promoted
revision: 1
metadata:
  aliases:
    - video pool deduplication
    - target video queue
    - track video replies
    - video pool table
    - resume video queue
relations:
  - type: refines
    target: mx_01M69V83DQFH77W8JJ4KS4EWP9
    note: Part of modules in context/modules.md
provenance:
  createdBy:
    kind: agent
    id: claude
  createdAt: 2026-10-31T20:43:00Z
grounds_to:
  - file: modules/pool_manager.py
    node: class:bcf4c7c4c38abf7417306c8787b1a962
    fingerprint: mh2:64:7gICQK18AJLGjAB4a9QANhf9A3eZWwHL0vAFDbohA3gb4ADCLh4A82y_AAX8NwCr5c4ChJDOARtj7wC7d3kAicbSAuGmtwYeIVsFt8jXACDD7gEiNqQBmO9_ABuabACV-t8EXVuZArhziwWWbicAoWVhAxh28QAZISIAK97AAYlpqwBDbkcEP-OaAAonowAY3IcBnxoTANEtOwE3jLwBukRqAcS_ugB73HoADJzBAaXjkQBViK0AjL7PAZCCQgLKD1gHJb7bAU1lUgDq91IBpc_cBPz-ngcbNVIApwA_BC9MDgM68x0DIZJuAq0_oQClF9cAH2ByAcm9CQAxnL0AsvBNAAA
    bodyHash: 222f57cbdb8c357d95b41cb5dc20563528513e58f3d4c1c1e4ee2109657aa40e
-->
## Video Pool Target Queue Tracking

PoolManager coordinates video URL ingestion from files or search results into the video_pool database table, tracking processed status, reply counts, and deduplicating entries across runs.
