---
name: google-auth-login
description: Workflow for authenticating Google accounts into persistent browser sessions with 2FA TOTP and speedbump handling.
triggers:
  - "google auth"
  - "login"
  - "totp"
  - "speedbump"
edges:
  - target: context/modules.md
    condition: when reviewing GoogleAuthenticator implementation details
  - target: context/decisions.md
    condition: when reviewing 2FA and Material 3 design decisions
grounds_to: []
last_updated: 2026-10-31
mex:
  id: mx_01M69VC00B4PVK7X08CM18XG49
  type: pattern
  status: promoted
  revision: 3
  title: google-auth-login
  relations:
    - type: related_to
      target: mx_01M69V83DQFH77W8JJ4KS4EWP9
      note: when reviewing GoogleAuthenticator implementation details
    - type: related_to
      target: mx_01M69V8E3R675TR01PVVGBK8EF
      note: when reviewing 2FA and Material 3 design decisions
---

# Google Authenticator Login Flow

## Context
Automated sign-in to Google accounts requires navigating `accounts.google.com` while handling Material 3 input states, 2FA TOTP verification, and security confirmation interstitials without getting blocked by bot challenges or Windows Hello passkey prompts.

## Steps
1. **Check Existing Session**: Call `is_session_authenticated(context)` to check for a valid `LOGIN_INFO` cookie on `.youtube.com`. If present, skip sign-in.
2. **Open Sign-in**: Navigate to `https://www.youtube.com/?hl=en&gl=US` and click the masthead sign-in link.
3. **Execute Form State Machine**:
   - Enter email into `input#identifierId` and click Next.
   - Enter password into `input[type="password"]` and click Next.
   - If challenged for 2FA, generate a fresh TOTP code using `generate_fresh_totp(secret)`. Enter into `input#totpPin` and submit.
   - If challenged for recovery email (KPE), enter `recovery_email` into `input#knowledge-preregistered-email-response`.
4. **Dismiss Speedbumps**: If interstitial screens appear (passkeys, phone additions, "Was it you?"), click `DECLINE_BUTTONS` ("Not now", "Skip", "Cancel") to bypass without triggering native OS prompts.
5. **Ensure Channel**: Navigate to `channel_creation` to verify or create the YouTube channel identity.
6. **Save Session Owner**: Record the authenticated email in `account_owner.txt` within the session directory.

## Gotchas
- **Material 3 Alert Elements**: Google places normal headers inside `[role="alert"]` and `[aria-live="assertive"]`. Do not flag them as errors; filter using `_NON_ERROR_STRINGS`.
- **Passkey Traps**: Clicking "Continue" on passkey interstitials launches native Windows Hello UI. Always decline passkey setups.
- **TOTP Window Rollover**: Submitting in the last 2 seconds of a 30s window risks server-side timeout. `generate_fresh_totp` must wait for the new window.

## Verify
- [ ] `LOGIN_INFO` cookie is present on `.youtube.com`.
- [ ] Session owner is written to `account_owner.txt`.
- [ ] Account channel exists or was created successfully.
