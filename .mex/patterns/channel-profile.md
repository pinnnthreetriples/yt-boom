---
name: channel-profile
description: Changing an account's YouTube channel nickname, description and avatar through the wizard card and the Studio editor.
triggers:
  - "avatar"
  - "channel name"
  - "nickname"
  - "channel description"
  - "profile"
edges:
  - target: context/modules.md
    condition: when reviewing account_menu_logic or profile_manager implementation details
  - target: patterns/google-auth-login.md
    condition: when the account is not signed in yet
grounds_to: []
last_updated: 2026-11-01
---

# Pattern: Channel Profile (nickname, description, avatar)

## Context
Only the **YouTube channel** is changed (Studio → Customization → Profile). Per YouTube's help page the channel name and picture are independent of the Google Account, which is never touched. The @handle is not changed.

## Flow
1. Wizard stage 1 «Аккаунты» → `9` «Профиль канала» → pick an account (Space/Enter) → the card: current values plus rows «Изменить никнейм» / «Добавить|Изменить описание» / «Добавить|Изменить аватарку» / «Применить на YouTube».
2. Each edit is saved at once as a **draft** in `accounts.profile_name|description|avatar` (no browser). Description: lines until a blank line, a lone `-` clears it. Avatar: a path (drag the file into the window); it is validated (`validate_avatar`: png/jpg/gif/bmp, ≤ 4 MB, real file signature) and copied to `profiles/avatars/<account>-<hash>.<ext>` (git-ignored), so a different picture is a different path.
3. «Применить» (or `--apply-profile <name>`) runs `ProfileEditor.apply`: one browser session through `YouTubeRunner` (the account's proxy, fingerprint and persistent profile) → youtube.com (`ytcfg` proves the sign-in and gives `CHANNEL_ID`) → `studio.youtube.com/channel/<id>/editing/profile` → types only the fields that differ from the live channel → PUBLISH → reload and compare. Success stores the snapshot in `accounts.profile_applied`.

"Unapplied edits" = draft ≠ `profile_applied` (`profile_pending_fields` in `modules/account_menu_logic.py`).

## Gotchas
- **Name limit**: YouTube allows 2 name changes per 14 days. Changes are logged in `accounts.profile_name_changes` (JSON, pruned to the window by `recent_name_changes`); with none left the name is skipped with a note and the rest still applies.
- **Studio selectors are undocumented**: the constants at the top of `modules/profile_manager.py` are structural candidates (first visible wins, never button texts). The post-publish reload check fails the apply when a value did not stick. Re-verify them against a live account when Studio changes.
- **One profile, one browser**: do not apply while the orchestrator or a warm-up holds the account's Camoufox profile; `runner.start()` fails and the error is reported.
- Typing uses `InteractionSimulator.type_text(..., allow_typos=False)`: a typo injected into a name or description would be published.

## Verify
- [ ] `python run_tests.py` passes (profile tests: `tests/test_profile_manager.py`).
- [ ] After applying, the channel page shows the new name/description/picture; a second apply reports «уже актуален».
