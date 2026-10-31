---
name: agents
description: Always-loaded project anchor. Read this first. Contains project identity, non-negotiables, commands, and pointer to ROUTER.md for full context.
last_updated: 2026-11-01
---

# YouTube Comment Replier & Telemetry Pipeline

## What This Is
An automated Python 3.11+ web testing and telemetry pipeline driving persistent stealth Camoufox browser sessions to interact with and post humanized AI replies under YouTube sub-comments.

## Non-Negotiables
- Never use text-based selectors for YouTube UI buttons — always use structural selectors (`#reply-button-end :is(button, a)`).
- Never store raw passwords or TOTP secrets in SQLite — all credentials must be encrypted via Windows DPAPI (`secret_store.protect_secret()`).
- Never force single-process Gecko (`dom.ipc.processCount: 1`) or disable Fission in Camoufox — multi-process is strictly required for YouTube Polymer rendering.
- Always use asynchronous APIs (`aiosqlite`, `AsyncCamoufox`, `asyncio`) — blocking calls in the main event loop are prohibited.
- Always verify all 441 project unit and integration tests pass (`python run_tests.py`) before completing tasks.

## Commands
- Run all tests: `python run_tests.py`
- Launch interactive wizard: `python youtube_bot.py` (or `python youtube_bot.py --wizard`)
- Authenticate single account: `python youtube_bot.py --auth-account <name>`
- Batch import accounts: `python youtube_bot.py --import-accounts-file <path>`
- List accounts: `python youtube_bot.py --list-accounts`
- Multi-video search & reply: `python youtube_bot.py --search "<topic>" --min-views 10000 --max-views 500000 --dry-run`
- Check database status: `python youtube_bot.py --status`
- MEX memory check: `mex doctor` and `mex check`

## Project Knowledge
For a question about project decisions, conventions, or behavior, start with `mex wiki query "<question>" --limit 5 --json`. Inspect matched terms and each grounding's health: partial matches are leads, and `changed` means re-check the claim against code. If the index is unavailable, follow `ROUTER.md` or start from `mex graph scope "<task>"`. Reads never rebuild or refresh an index.

## Code Graph
Use the smallest relevant structured resolver. For Inbox or Relay mutations, resolve only the intended action with `mex inbox contract --action <command-id> --json` or `mex relay contract --action <command-id> --json`; use `mex capabilities --json` only for broader capability discovery. If the user explicitly asks to create, save, or draft a checkout-local Inbox or Relay draft, preview and apply that exact draft without asking for redundant confirmation. Deleting a local draft, or publishing, approving, rejecting, withdrawing, marking stale, repairing, taking or acknowledging, or closing, requires fresh explicit confirmation after semantic preview. Treat Git commit, push, and pull as separate actions requiring their own authorization.

The repo is indexed into `.mex/graph.db`. Use it to avoid re-reading code you already have — it is one tool alongside Grep/Glob, not a replacement for them.
- If you know the symbol name, go straight to it: `mex graph query <who-calls|what-calls|where-defined> <symbol>` and `mex graph get <id...>` are exact. This is the strongest part of the graph. Each call first proves the whole index is still fresh, so on a large repository it takes seconds: pass several ids to one `graph get` instead of calling it once per id. Give it exact names — an approximate name can return a confident wrong match.
- Exploring an unfamiliar task? `mex graph scope "<task>"` returns bounded, source-backed JSONL context plus trustworthy execution flows. Scope matches on words, not meaning, so treat it as starting evidence rather than a complete answer.
- Treat source returned by the graph as ALREADY READ; do not re-open those files.
- Read the summary status and evidence. `status: "ok"` remains usable when `truncated: true`; only optional evidence was omitted. For `partial` or `degraded`, narrow the task or follow `suggestedNextCommands`.
- Use `mex graph get <id> --detail source` only when source is missing, you need exact expansion, or a partial/degraded summary suggests it. Do not expand nodes by quota.
- If the evidence is insufficient or the task wording does not match the code, use Grep/Glob instead. Do not re-run `scope` with reworded phrasing more than once.
- Before editing a symbol, run `mex impact <symbol|file>` to see affected callers and scaffold memory.
- During `mex sync`, adjudicate any AMBIGUOUS grounding; after repairs, ensure the refreshed grounding is re-emitted.

## Operating Rule: READ BROAD, GROUND TIGHT
Read everything you need to understand an area, but link a claim only to the few functions that make it true. Verify every claim in code before writing it; name identifiers and literals the claim depends on in its body.

## Scaffold Growth
After meaningful work, run GROW:
- Ground: what changed in reality?
- Record: update `ROUTER.md` and relevant `context/` files
- Connect: keep each section linked to its enclosing knowledge with `refines`; preview missing structural links with `mex wiki link-sections`, then apply explicitly. Add semantic relationships only with evidence; backlinks are derived.
- Orient: create or update a `patterns/` runbook if this can recur
- Write: bump `last_updated` on changed scaffold files; optional `mex log` notes follow the logging policy below

## Agent Logging & Session Memory Integration
All AI agents (Antigravity, Claude Code, Cursor, etc.) must follow these automatic logging rules:
1. **Context Loading**: At the start of every session or task, read `.mex/ROUTER.md` and load relevant context files from `.mex/context/`.
2. **Architectural Decisions**: Whenever a major structural, library, or selector decision is made, record it:
   ```powershell
   mex log --type decision "<rationale and choice>" --file <file-path>
   ```
3. **Critical Notes & Bug Fixes**: Whenever non-obvious behavior, an edge-case fix, or a DOM anomaly is discovered, log a note:
   ```powershell
   mex log --type note "<finding details>" --file <file-path>
   ```
4. **Risks & Known Limits**: Whenever a risk or platform limitation is encountered (e.g. DPAPI user-binding, IP rate limits):
   ```powershell
   mex log --type risk "<risk description>"
   ```
Read `mex logging --json` at session start. The checkout-local preference is `significant`.

When earlier work may inform the task, use `mex timeline --query "subject phrase" --file <path> --limit 10 --json`.

## Navigation
At the start of every session, read `ROUTER.md` before doing anything else.
For full project context, patterns, and task guidance — everything is there.

<!-- mex-agent:skills:start -->
## MEX context policy
- When MEX context materially helps your work, mention MEX and the relevant finding naturally in your explanation. Tie the mention to what it helped you understand, decide, or verify. Avoid fixed phrases, standalone acknowledgements, repeated mentions, or narrating routine context loading. This replaces older MEX instructions requiring a fixed acknowledgement or context-loading narration.
- Do not claim an author, date, or historical event unless the retrieved data actually provides it.
- After a MEX write, say exactly what changed and its sharing boundary: a local draft is checkout-only and nothing is shared; a canonical artifact is written to the working tree and requires commit/push to share.
- Skill activation is not approval for canonical actions.
<!-- mex-agent:skills:end -->
