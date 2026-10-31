---
name: account-management
description: Workflow for importing, encrypting, and managing Google account credentials and persistent hardware profiles.
triggers:
  - "account management"
  - "import accounts"
  - "dpapi"
  - "credentials"
edges:
  - target: context/modules.md
    condition: when reviewing account parser and secret store implementations
  - target: context/decisions.md
    condition: when reviewing DPAPI and fingerprint persistence decisions
grounds_to: []
last_updated: 2026-10-31
mex:
  id: mx_01M69VBZR243QKWN4X78PQXWDC
  type: pattern
  status: promoted
  revision: 3
  title: account-management
  relations:
    - type: related_to
      target: mx_01M69V83DQFH77W8JJ4KS4EWP9
      note: when reviewing account parser and secret store implementations
    - type: related_to
      target: mx_01M69V8E3R675TR01PVVGBK8EF
      note: when reviewing DPAPI and fingerprint persistence decisions
---

# Account Management and Credential Storage

## Context
Accounts are stored in the `accounts` table of `test_metrics.db`. To safeguard against data leaks, passwords and TOTP secrets must be encrypted using Windows DPAPI. Each account is assigned a persistent session directory and hardware fingerprint.

## Steps
1. **Parse Input Format**: Use `modules.account_parser.parse_account_line()` to parse credentials from `email:password:totp_secret[:recovery_email][:proxy]` format.
2. **Encrypt Credentials**: Wrap passwords and TOTP secrets with `secret_store.protect_secret()` before database insertion.
3. **Database Upsert**: Call `database.Database.upsert_account()` to persist the account record.
4. **Pin Hardware Fingerprint**: When the account session directory is first created, invoke `get_or_create_fingerprint(session_dir)` to generate and atomically store the account session fingerprint file.
5. **Session Verification**: Use `youtube_bot.py --auth-account <name>` to perform automated headful authentication and store browser cookies.

## Gotchas
- **Windows User Binding**: DPAPI ciphertext is bound to the current Windows user login. Moving `test_metrics.db` to another user account or machine causes `SecretStoreError`.
- **Proxy Invariants**: Ensure proxy strings include valid schemes (`socks5://` or `http://`) and credentials where needed.

## Verify
- [ ] Stored password starts with `dpapi:` in SQLite.
- [ ] Stored TOTP secret starts with `dpapi:` in SQLite.
- [ ] Fingerprint file exists in the account session folder.
