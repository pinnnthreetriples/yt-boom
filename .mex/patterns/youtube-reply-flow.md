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
last_updated: 2026-11-14
mex:
  id: mx_01M69VC039A3DJ99V5S1GNSRPJ
  type: pattern
  status: promoted
  revision: 4
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
3. **Dismiss Consent**: Call `_dismiss_consent(page)`; it clicks the `consent.youtube.com` form whose hidden `set_eom` flag is `false` (`CONSENT_ACCEPT_SELECTORS` in `page_helpers.py`). Verified live 2026-11-01 on `consent.youtube.com/m?...&uxe=eomtm&cm=2` (en, pl) and `/d` (en, de, fr, pl); the page is served only for some URL variants from a non-EU address. The youtube.com lightbox variant has no known structural handle.
4. **Locate Reply Trigger**: Use structural selector `#reply-button-end :is(button, a)` targeting the specific comment ID container (`ytd-comment-view-model:has(a[href*='lc={comment_id}'])`).
5. **Handle First-Time Channel Creation**: Call `_confirm_channel_creation(page)`. If `ytd-channel-creation-dialog-renderer` appears, click `#create-channel-button button` and wait for modal dismissal.
6. **Focus Contenteditable Input**: Click `#contenteditable-root` inside the active reply dialog.
7. **Type Natural Text**: Pass normalized payload text (`normalize_text()`) to `simulator.type_text()` with humanized jitter and typo injection.
8. **Submit & Verify**: Click `#submit-button button` and wait for network confirmation (`create_comment` or `create_comment_reply` API response).

## Gotchas
- **`<button>` vs `<a>`**: Accounts without a channel render an anchor tag with `href="/create_channel"`. Never query `button` alone; always use `:is(button, a)`.
- **Multi-lingual UI**: Never match text like `Reply` or `Ответить` because Kyrgyz (`Жооп`), Turkish, or other locales will fail. The same holds for `aria-label`, `title` and placeholders: `tests/test_selector_guard.py` fails on `has-text`, `text=`, `aria-label`, `get_by_text` and friends in `modules/*.py`. Text matching is permitted only for the Google auth speedbump screens in `google_authenticator.py` (see `google-auth-login.md`), never for YouTube UI.
- **Reply expander**: the visible control of the current layout is `#replies #more-replies-sub-thread button` (verified en/ru, 2026-11-01). `#more-replies` / `#less-replies` live in the collapsed `#expander` and are hidden, so the show control is never confused with the hide control; no label check is needed.
- **Verified structural handles (en-US, ru-RU; composer in ru, the account language wins over `?hl=`)**: sign-in link `ytd-masthead #buttons a[href*="ServiceLogin"]`, composer `#reply-dialog #contenteditable-root`, submit `#reply-dialog #submit-button button`, cancel `#cancel-button button` (never click it). Selector constants: `EXPAND_REPLIES_SELECTORS`, `REPLY_INPUT_SELECTORS`, `SUBMIT_BUTTON_*` in `youtube_runner.py`.
- **Reply button hidden by YouTube (not a locale problem)**: for some comments YouTube renders the full action row (`#toolbar`) but sets `hidden` on `ytd-button-renderer#reply-button-end`, so the button exists and is never visible. Verified live 2026-11-05 with a ru-RU and a ky-KG account on `dQw4w9WgXcQ`: the same comments (`@ochkalov`, `@Timeworks`, a verified channel's comment) lack the button for both accounts, while the other 18 of the first 20 have it. `YouTubeRunner._reply_disabled_on_target` detects it by structure only (comment visible + `#toolbar` visible + `#reply-button-end` present + no visible button inside), `_submit_reply` raises `ReplyUnavailableError`, `run()` returns `SubmissionResult(reply_unavailable=True)` without a screenshot. Callers skip the target at once (no pause), do not count it as an account error (`MultiAccountOrchestrator` keeps it out of the 3-errors circuit breaker) and persist it (`Database.mark_reply_unavailable` / a failed payload whose error starts with `REPLY_UNAVAILABLE_MARK`, honoured by `reserve_payload` and `has_replied_to` for 7 days). Single-video and multi-video runs fetch `EXTRA_CANDIDATES` spare comments so skipped targets do not empty the list. If "could not locate Reply button" appears, check the failure screenshot for a missing "Reply" on that comment before suspecting the UI language.
- **Comment pages, not just the first one**: InnerTube gives the comment section in pages of about 20 threads. `YouTubeCollector.fetch_popular_comments` reads them one after another (`_next_threads_token`: the `continuationItemRenderer` at the top level of the page's `continuationItems`; a thread's own reply continuation sits inside its `commentThreadRenderer` and is not a page) until `limit` targets are found, the comments end, a token repeats, or `COMMENT_PAGES_MAX` (25) pages were read; a failing later page keeps what was found. `skip` (external ids) leaves answered targets out without counting them: the orchestrator passes `Database.handled_refs(video_id)` (the same targets `has_replied_to` skips) plus the targets claimed in the run, so a video answered before still yields fresh targets. Before 2026-11-14 only the first page was read: in `first_reply` mode (one target per thread, threads without replies give none) a video with 339 comments gave 3–4 targets and stayed «ждут» at 4/5 (live check on `tfJKa7FsGKg`: 5 of the first 20 threads had replies; 10 pages gave 4 more fresh targets).
- **Hidden Duplicate Elements**: YouTube keeps hidden duplicate stubs of permalinked comments in the DOM. Always select the first visible element (`_first_visible`).

## Visibility check
A published reply can be accepted by YouTube (HTTP 200 + `commentId`) and still be hidden from everybody but its author (seen live 2026-11-05 on `dQw4w9WgXcQ` with the `vinijahayij` account). After a live reply the bot therefore looks for it as an anonymous viewer.
- **What**: `modules/comment_visibility.py` — `CommentVisibilityChecker.watch` waits `limits.visibility_check_delay_seconds` (45 s), opens `watch?v=<video>&lc=<comment id>` without cookies (the collector's curl_cffi session), posts the comments continuation to InnerTube `/next` and looks for the exact `commentId` in that answer. `[ВИДИМОСТЬ]` log line: green «виден всем зрителям» / yellow «не найден в анонимной выдаче» / yellow «не удалось проверить».
- **States** in `activity_log.visibility` (`Database.set_activity_visibility`, additive column, NULL = not checked): `seen`, `unseen` (the answer had comments but not ours), `unknown` (network error, no comment list in the answer). NULL when the check is off, in dry-run, without a `remote_id`, or for a failed submit.
- **Where it is wired**: `MultiAccountOrchestrator._watch_visibility` (pool / projects runs, skipped when a test injects `runner_factory`), `run_reply_bot` and `run_multi_video_pipeline` in `youtube_bot.py` (via `VisibilityWatcher`: `schedule` after the journal row of a success exists, `finish` in `finally` BEFORE `collector.close()` / `db.close()`, bounded by delay + 60 s; `cancel` on Ctrl+C / any exception). `watch_and_record` stores the result and never raises.
- **Switch**: `limits.visibility_check_enabled` (tab «Запуск», row «Проверка видимости») and `visibility_check_delay_seconds`; `modules.comment_visibility.visibility_enabled` / `make_visibility_checker` are the only place with the condition (delay 0 still turns it off). Tests set `visibility_check_enabled: false` in their config so no unit test waits 45 s or touches the network.
- **Shown**: journal rows of the tab «Запуск» (green / yellow marker, counters, filter «не видны»), the record card (line «видимость»), `--history` column «Видим.» (`виден` / `НЕ ВИДЕН` / `?` / `—`, ANSI colour only on a tty without `NO_COLOR`).
- **Why it does not touch anything else**: it is observation. It never changes `status` (a sent reply stays `success`), `stats()`, `record_account_error`, lifecycle stages or daily limits, never blocks an account and never resends. A miss may be propagation delay, so the wording is «не вижу», never «скрыт».

## Verify
- [ ] Comment permalink includes `&lc={id}`.
- [ ] Reply selector uses `#reply-button-end :is(button, a)`.
- [ ] Channel creation dialog is handled if present.
- [ ] Text payload has passed through `normalize_text()`.
- [ ] Network response verifies comment creation with HTTP 200.
