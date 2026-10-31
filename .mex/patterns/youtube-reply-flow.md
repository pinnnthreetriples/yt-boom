---
name: youtube-reply-flow
description: Workflow for navigating to a YouTube comment and posting a humanized reply under it.
triggers:
  - "reply flow"
  - "post reply"
  - "youtube runner"
  - "sub-comment"
edges:
  - target: context/modules.md
    condition: when reviewing YouTubeRunner implementation details
  - target: context/conventions.md
    condition: when reviewing selector rules and typography conventions
grounds_to: []
last_updated: 2026-10-31
mex:
  id: mx_01M69VC039A3DJ99V5S1GNSRPJ
  type: pattern
  status: promoted
  revision: 3
  title: youtube-reply-flow
  relations:
    - type: related_to
      target: mx_01M69V83DQFH77W8JJ4KS4EWP9
      note: when reviewing YouTubeRunner implementation details
    - type: related_to
      target: mx_01M69TVZY58165JDM14J2ZVZZM
      note: when reviewing selector rules and typography conventions
---

# YouTube Reply Flow

## Context
Posting replies under YouTube comments requires driving `modules.youtube_runner.YouTubeRunner`. YouTube lazily loads comment threads, renders comments inside Shadow DOM and custom elements (`ytd-comment-view-model`), and distinguishes between channel and non-channel accounts.

## Steps
1. **Initialize Runner**: Instantiate `YouTubeRunner` with account settings, pinned fingerprint, and proxy config.
2. **Navigate to Permalinks**: Open the video URL with `&lc={comment_id}` query parameter to ensure the target parent comment or sub-comment is highlighted.
3. **Dismiss Consent**: Call `_dismiss_consent(page)` to clear any GDPR or cookie modals.
4. **Locate Reply Trigger**: Use structural selector `#reply-button-end :is(button, a)` targeting the specific comment ID container (`ytd-comment-view-model:has(a[href*='lc={comment_id}'])`).
5. **Handle First-Time Channel Creation**: Call `_confirm_channel_creation(page)`. If `ytd-channel-creation-dialog-renderer` appears, click `#create-channel-button button` and wait for modal dismissal.
6. **Focus Contenteditable Input**: Click `#contenteditable-root` inside the active reply dialog.
7. **Type Natural Text**: Pass normalized payload text (`normalize_text()`) to `simulator.type_text()` with humanized jitter and typo injection.
8. **Submit & Verify**: Click `#submit-button button` and wait for network confirmation (`create_comment` or `create_comment_reply` API response).

## Gotchas
- **`<button>` vs `<a>`**: Accounts without a channel render an anchor tag with `href="/create_channel"`. Never query `button` alone; always use `:is(button, a)`.
- **Multi-lingual UI**: Never match text like `Reply` or `Ответить` because Kyrgyz (`Жооп`), Turkish, or other locales will fail.
- **Hidden Duplicate Elements**: YouTube keeps hidden duplicate stubs of permalinked comments in the DOM. Always select the first visible element (`_first_visible`).

## Verify
- [ ] Comment permalink includes `&lc={id}`.
- [ ] Reply selector uses `#reply-button-end :is(button, a)`.
- [ ] Channel creation dialog is handled if present.
- [ ] Text payload has passed through `normalize_text()`.
- [ ] Network response verifies comment creation with HTTP 200.
