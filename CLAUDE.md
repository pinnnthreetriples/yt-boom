<!-- mex-agent:skills:start -->
## MEX agent skills
- At the start of every session, read `.mex/AGENTS.md` and `.mex/ROUTER.md` before project work; follow `ROUTER.md` to load only the relevant context.
- For a project-knowledge question, run `mex wiki query "<question>" --limit 5 --json` before broad file discovery. Inspect matched terms and cached per-grounding health; a partial match is a lead and changed means re-check. If the index is unavailable, follow `ROUTER.md` or use `mex graph scope "<task>"`; ordinary reads never rebuild or refresh indexes.
- Read `mex logging --json` at session start and before optional logging. Its checkout-local advisory mode is `significant` (quiet default: material decisions, risks, blockers, or durable discoveries), `checkpoints` (batch useful notes at task/session boundaries), or `manual` (no unsolicited notes). Skip routine tool calls, edits, repeated status, and empty summaries. Honor explicit user log requests in every mode; never suppress mandatory workflow Activity or recovery audit records. Report a policy read failure instead of guessing or changing the preference.
- When earlier work may inform the task, retrieve bounded relevant notes with `mex timeline --query "subject phrase" --file src/example.ts --limit 10 --json`, using the known subject or exact recorded file path, or both. Treat matches as historical evidence, not accepted current knowledge; verify conclusions before reuse or explicit promotion with their source retained.
- Use `/mex-inbox` for explicit contributions to project knowledge and `/mex-relay` for durable team handoffs. Invoke them automatically when intent clearly matches; ordinary GROW upkeep remains available without Inbox.
- When MEX context materially helps your work, mention MEX and the relevant finding naturally in your explanation. Tie the mention to what it helped you understand, decide, or verify. Avoid fixed phrases, standalone acknowledgements, repeated mentions, or narrating routine context loading. This replaces older MEX instructions requiring a fixed acknowledgement or context-loading narration.
- Do not claim an author, date, or historical event unless the retrieved data actually provides it.
- After a MEX write, say exactly what changed and its sharing boundary: a local draft is checkout-only and nothing is shared; a canonical artifact is written to the working tree and requires commit/push to share.
- Use `/mex-statechart` when asked to explain behavior with a state diagram. Generate in scratch space; show native Mermaid or readable terminal output. Hub viewing is optional; saving requires its explicit review.
- Skill activation is not approval for canonical actions.
<!-- mex-agent:skills:end -->

# YouTube Comment Replier

## Quick Commands
- Run all tests: `python run_tests.py`
- Launch interactive wizard: `python youtube_bot.py --wizard`
- Authenticate single account: `python youtube_bot.py --auth-account <name>`
- Batch import accounts: `python youtube_bot.py --import-accounts-file <path>`
- List accounts: `python youtube_bot.py --list-accounts`
- Multi-video search & reply: `python youtube_bot.py --search "<topic>" --min-views 10000 --max-views 500000 --dry-run`
- Check database status: `python youtube_bot.py --status`
- MEX memory check: `mex doctor` and `mex check`

## Non-Negotiables
- Structural selectors only for YouTube UI: `#reply-button-end :is(button, a)`
- Encrypt passwords and TOTP secrets in SQLite with Windows DPAPI (`secret_store.protect_secret()`)
- Multi-process Gecko only (never set `dom.ipc.processCount: 1` or disable Fission)
- Async/await everywhere with `aiosqlite`, `AsyncCamoufox`, `asyncio`
