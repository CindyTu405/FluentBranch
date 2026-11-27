# CT-Chat-Router: working rules

Repo root = the directory containing this file (git, branch main). All paths below are relative to it.
Before starting work read `PLAN_v2.md`, `BUGS.md`, `BACKLOG.md`, and for branch / highlight-offset work also `DESIGN_P3.md`. `PLAN_v2.md` is authoritative. Stale docs (`HANDOVER.md`, `PLAN_20261012.md`) are archived in `docs/archive/`; do not treat them as current.
The `ct-chat-router` skill holds the project map, key code facts and known pitfalls.

## Tone
Finished work gets a short report: what changed, what's verified, what's left. Agree because it's right, not because the user said it.
Reply in the language the user writes in.

## Who writes code
- ALL code is written by the `implementer` subagent. The main agent does NOT write code itself, even for hard technical points. The main agent diagnoses, spikes, and writes precise specs; the subagent implements. (The user corrected this once when F7 was hand-written.)
- Exception: the main agent may edit directly only when ALL hold: one file, <= 5 lines, no logic change (typo, copy text, a class value, a config value). Anything with a new concept or needing a teaching guide goes to `implementer`. Commit format and verification rules still apply.
- Subagent reports must be terse: one-line summary + commit hash.
- Commit-message format: write the exact message in the task text and say "EXACTLY", then `git log --oneline -3` to confirm before pushing.
- Parallel work: give each parallel subagent `isolation: "worktree"` so it commits on its own branch. A worktree starts from HEAD (commit first) and lacks the gitignored `frontend/node_modules`, `backend/.venv` and `.env` files: tell the subagent to symlink them from the main checkout before building. Merge the branches into main one at a time, then re-run the acceptance commands on main. A single subagent works in the main checkout.

## Code comments
- Comment only what the code cannot say: a non-obvious why, an invariant, an ordering requirement, a security constraint. One line where possible.
- No task ids in code or comments (`(G2-9)`, `(B12)`…); traceability lives in commit messages and git blame.
- No tutorial narration, no restating the next line, no change history ("不再…", "改成…"), no commented-out code.
- No run instructions in file headers; harness usage lives only in `backend/dev/run_harnesses.sh`.

## Requirements vs approval
- A user posting a LIST of items (e.g. G1-G9) is only raising requirements, NOT approving implementation. Discuss each item's details and get explicit confirmation BEFORE delegating any code. Implementing an unconfirmed list produced wrong work that had to be fully reverted (e051fec).
- 先寫教學再執行: for any change that introduces a new front-end or back-end concept (bug fixes included), write the teaching guide first (`docs/*.html`, self-contained, same style as `docs/20261020_create-all-guide.html`; filename `YYYYMMDD_(<task id>)<name>-guide.html` with the creation date, e.g. `20261102_(G8)message-actions-guide.html`, omit `(<task id>)` when there is none; structure: concept, why, code, short practice list), commit it as its own `docs:` commit during planning, so the user reads it before saying 開始. When a design decision is made or a step lands, update the same guide (decision table + implementation record) in a separate `docs:` commit.

## Batch workflow
1. Plan: discuss every item of the batch until each detail is confirmed. Write the tasks into the plan doc (spec + runnable acceptance commands) and any teaching guide. The user approves the whole package once with 開始.
2. Execute without stopping: one branch per batch, one commit per task. After each task run its acceptance commands; for UI also check desktop and mobile in the built-in browser pane. Stop and ask only when a spec detail proves wrong or ambiguous, or a decision would change user-visible behavior. Decide invisible details (naming, internal structure) yourself and list them in the final report.
3. Accept: push the branch, open the PR, and give the user one numbered checklist inline, with tuning knobs (which class/value, what each direction does) for every visual change. Acceptance fixes are follow-up commits on the same branch. Merge to main and deploy only after the user confirms.

## Planning and verification
- Break each phase into numbered tasks with acceptance criteria in the plan doc (like `PLAN_v2.md`), track progress with the task list, commit per task with the task id (e.g. `feat: (P0-1) ...`).
- Write each task's acceptance criteria as RUNNABLE commands ("run X → prints Y"). On completion re-run them yourself, plus `git log` / `git status`, and read the changed code, before marking done and pushing. A subagent's "completed" is an unverified claim.
- Before editing for a user request, run `git status` and `git log --oneline -5`: the user sometimes commits the same change between turns.
- Verify UI changes by viewport width (desktop >= 768, mobile < 768). When a change splits mobile and desktop behavior, re-test the desktop features of the same element too (drag handle position and result, column widths, footer position).
- Browser checks of the local app use the built-in browser pane (`preview_start` with name `frontend`); use Claude in Chrome only for the production site or when the user's Chrome sign-in is needed. Procedure: `.claude/skills/ct-chat-router/references/frontend-ui-patterns.md`, section Verification.
- Frontend-only edits: `cd frontend && npm run build` is the quick gate (no UI test suite). Say explicitly in the report that visual behavior was not browser-verified when it wasn't.
- Backend streaming: verify with a fake-LLM harness on a real uvicorn server, never real Gemini/OpenRouter calls (they cost money). Ask the user to run the real-model check.
- All files are LF. Before a scripted edit still check `grep -c $'\r' <file>` and write back with the file's own ending.
- `database.py` echoes SQL only when `SQL_ECHO` is true (default off, B9). When on, any script importing it prints every SQL statement and its parameters. Filter the output.
- DB gotcha: this machine has BOTH a docker pgvector (chat_db, port 5432) and a native PostgreSQL 16. Always `docker compose ps` first. If chat_db is down, STOP and report; do not start a throwaway native cluster.

## Commits and git
- One step per commit: `fix:` / `feat:` / `docs:` with step id, e.g. `feat: (P5b) ...`.
- Plan-doc changes always get their own separate commit, never bundled with code.
- Bugs go in `BUGS.md` and get a separate step and commit, not folded into the current step.
- Update the plan file status when a step is finished or verified.
- Undoing a batch of premature commits: `git revert --no-commit <hashes newest-first>` then one commit (no force-push). Keep the requirements-doc (BACKLOG) commit OUT of the revert range.

## Visual overlap rule
When a floating control (⋯, buttons) covers text, fade the text with a `mask-image` gradient rather than hard-clipping or adding padding. Confirm the symptom first ("covered by the button" and "clipped too early" are different bugs).

## Production deploy (EC2 VM)
- The VM runs `docker-compose.prod.yml`, selected by `COMPOSE_FILE=docker-compose.prod.yml` in the VM's `.env`; plain `sudo docker compose ...` there uses the prod file. Local dev uses `docker-compose.yml`.
- Prod `api` has no source volume mount: after `git pull`, rebuild (`sudo docker compose up -d --build`) for code changes to take effect.
- Migrations are NOT run automatically. After any pull that adds `backend/alembic/versions/*`, run `sudo docker compose exec api alembic upgrade head`. Skipping it gives `UndefinedColumn` 500s on `POST /chat` (seen 2026-11-02 with `branch_id`).
- Host shell does not have `$POSTGRES_USER`/`$POSTGRES_DB`; inspect the DB with `sudo docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'`.
- One-time VM setup (e.g. `CREDENTIAL_ENCRYPTION_KEY`) is in `docs/20261102_prod-deploy-guide.html`.

## Context hygiene
Durable state lives in the skill, the PLAN docs and git. Long conversations are summarized automatically; suggest a new session when the topic changes, not because of length.
