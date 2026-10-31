---
name: debug-browser-interaction
description: Debugging checklist and procedure for diagnosing Camoufox browser automation, DOM selector, and network errors.
triggers:
  - "debug browser"
  - "selector timeout"
  - "profile lock"
  - "browser error"
edges:
  - target: context/modules.md
    condition: when inspecting runner implementation
  - target: context/decisions.md
    condition: when checking Gecko multi-process and proxy preferences
grounds_to: []
last_updated: 2026-10-31
mex:
  id: mx_01M69VBZX8ZWDBY8SNECHP9K53
  type: pattern
  status: promoted
  revision: 3
  title: debug-browser-interaction
  relations:
    - type: related_to
      target: mx_01M69V83DQFH77W8JJ4KS4EWP9
      note: when inspecting runner implementation
    - type: related_to
      target: mx_01M69V8E3R675TR01PVVGBK8EF
      note: when checking Gecko multi-process and proxy preferences
---

# Debugging Browser Interaction Failures

## Context
When automated browser operations fail, errors typically stem from: profile lock contention, stale or language-dependent DOM selectors, background timer throttling, or proxy connection dropouts.

## Steps
1. **Check for Profile Lock Contention**:
   - Symptoms: `TargetClosedError`, `Process already running`, or failure to launch `AsyncCamoufox`.
   - Resolution: Terminate leftover Camoufox processes using PowerShell (`Get-Process camoufox* | Stop-Process`). Verify that runners call `await runner.close()` in `finally` blocks.
2. **Inspect Screenshot & Artifacts**:
   - On error, `_capture_failure()` saves page screenshots to `test_sessions/_artifacts/`. Check the visual state of the page at the moment of failure.
3. **Verify DOM Selector Visibility**:
   - Check if YouTube UI changed or if an unexpected modal (e.g. cookie consent, channel creation, age gate) is obscuring the target button.
   - Run `_first_visible` with retry loops rather than immediate locator clicks.
4. **Inspect Proxy & DNS Resolution**:
   - Symptoms: `NS_ERROR_PROXY_CONNECTION_REFUSED` or timeouts on initial navigation.
   - Resolution: Verify `network.proxy.socks_remote_dns` is `True` and proxy credentials in `config.yaml` or SQLite are active.
5. **Verify Gecko Process Settings**:
   - Ensure `dom.ipc.processCount` is NOT set to 1 and `fission.autostart` is NOT set to false.

## Verify
- [ ] Orphaned browser processes are cleaned up.
- [ ] Failure screenshot in `test_sessions/_artifacts/` is reviewed.
- [ ] Target selector uses `:is(button, a)` without hardcoded strings.
