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
last_updated: 2026-11-01
mex:
  id: mx_01M69VC00B4PVK7X08CM18XG49
  type: pattern
  status: promoted
  revision: 4
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
2. **Open Sign-in**: Navigate to `https://www.youtube.com/?hl=en&gl=US`, read the href of the masthead sign-in link (`SIGN_IN_LINK`). The link carries the browser/account language (`hl=ru`, `hl=pl`...) and Google then renders every screen in it, so a href that is not `hl=en` is opened with `with_hl_en(href)` (other params kept, `referer` = youtube.com) instead of clicked; an English or non-`https://accounts.google.com` href is clicked as before.
3. **Execute Form State Machine**:
   - Enter email into `input#identifierId` and click Next.
   - Enter password into `input[type="password"]` and click Next.
   - If challenged for 2FA, generate a fresh TOTP code using `generate_fresh_totp(secret)`. Enter into `input#totpPin` and submit.
   - If challenged for recovery email (KPE), enter `recovery_email` into `input#knowledge-preregistered-email-response`.
4. **Speedbumps**: interstitials (passkeys, phone additions, "Was it you?", "Try another way", the "Use another account" tile) have no structural handle and the login must stay fully automatic, so `DECLINE_BUTTONS`, `WAS_IT_YOU_BUTTONS`, `ACKNOWLEDGE_BUTTONS`, `TRY_ANOTHER_WAY` and `USE_ANOTHER_ACCOUNT` match by en/ru label. **Text matching is permitted only for these Google auth speedbump screens, never for YouTube UI.** Because step 2 forces `hl=en`, English labels are what the screens show for any account language (verified on the first screen with ru-RU / pl-PL / en-US browsers; screens after the password step can only be verified by a real login). If a screen still has no handler the flow raises `неизвестный экран`, and `GoogleAuthenticator(artifacts_dir=...)` first writes `google_unknown_screen_<ts>.html` there: the URL without query plus `dom_snapshot.sanitize_html` output (tags, ids, names, types, jsname, data-* names, button/link/heading texts; no values, emails, tokens, scripts) so a handler can be added. `tests/test_selector_guard.py` allowlists exactly these five constants and the `_buttons` helper; any other text selector fails it.
5. **Ensure Channel**: Navigate to `channel_creation` to verify or create the YouTube channel identity.
6. **Save Session Owner**: Record the authenticated email in `account_owner.txt` within the session directory.

## Gotchas
- **Material 3 Alert Elements**: Google places normal headers inside `[role="alert"]` and `[aria-live="assertive"]`. Do not flag them as errors; filter using `_NON_ERROR_STRINGS`.
- **Passkey Traps**: Clicking "Continue" on passkey interstitials launches native Windows Hello UI. Never match it (only refusals are in `DECLINE_BUTTONS`).
- **Structural handles (verified en/ru/pl on the first screen, 2026-11-01)**: `input#identifierId`, `input[name="identifier"]`, `#identifierNext button` (the id sits on a wrapper `div`, not on the button). `#passwordNext`, `#totpNext`, `input#totpPin`, `div[data-challengetype="6"]` follow the same pattern but need a signed-in flow to verify. Without a Next button the flow presses Enter.
- **Consent**: `CONSENT_REJECT_SELECTORS` (hidden `set_eom=true` form) verified live 2026-11-01 on `consent.youtube.com/m` (`uxe=eomtm&cm=2`; en, pl) and `/d` (en, de, fr, pl): 2 matches (the page lays buttons out twice, one copy `display:none`), the visible one is "Reject all" / "Accept all" (on `/d`: "Confirm your settings").
- **Still text-dependent (not a selector)**: `_NON_ERROR_STRINGS` filters headings out of `[role=alert]` blocks in en/ru only; other UI languages can misread a heading as an error.
- **ytcfg reads**: Camoufox runs `page.evaluate` in an isolated world, so `window.ytcfg` is invisible and `LOGGED_IN` always read `false` for a signed-in profile (and `mw:` is off on purpose). `YTCFG_JS` therefore reads the inline `ytcfg.set({...})` script text. `CHANNEL_ID` is absent on the home page even for accounts with a channel: the channel check falls through to the avatar-menu `create_channel` probe.
- **TOTP Window Rollover**: Submitting in the last 2 seconds of a 30s window risks server-side timeout. `generate_fresh_totp` must wait for the new window.

## Verify
- [ ] `LOGIN_INFO` cookie is present on `.youtube.com`.
- [ ] Session owner is written to `account_owner.txt`.
- [ ] Account channel exists or was created successfully.
