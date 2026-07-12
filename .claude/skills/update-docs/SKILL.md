---
name: update-docs
description: Update all project documentation to reflect changes made in this session — README, SESSION_STATE, NEXT_SESSION_PROMPT, and timer README if firmware changed. Run this before committing so doc updates land in the same commit as the code.
allowed-tools: [Read, Edit, Write, Glob, Grep, Bash, PowerShell]
---

# Update Docs

Update all project documentation to reflect what changed in this session, then stage the doc files so they're included in the next commit.

## Arguments

`$ARGUMENTS`

Optional: a short summary of what was built (e.g. "added backup/restore to settings"). If omitted, derive from git diff.

## Step 1 — Understand what changed

Run `git diff HEAD` (or `git diff HEAD~1..HEAD` if changes are already committed) in the base station repo to see which files changed. Also check `C:\Kris\Projects\F3K_Timer_1` if firmware files may have changed.

```powershell
git -C "C:\Kris\Projects\F3K_Timer_Project" diff HEAD
git -C "C:\Kris\Projects\F3K_Timer_1" diff HEAD
```

Identify: which subsystem changed (base station, firmware, or both), what features were added/fixed, what's still pending.

## Step 2 — Update SESSION_STATE.md

File: `C:\Kris\Projects\F3K_Timer_Project\SESSION_STATE.md`

- Update `*Last updated:*` line with today's date and a brief session label
- In **Immediate Next Steps**: mark completed items with ~~strikethrough~~ + **DONE (session N)**; remove or reorder based on current priority
- In **Recent Decisions / Context**: prepend a new `- **Session N (date) — title:**` block describing what was built (key decisions, approach taken, files changed)
- Do NOT rewrite the whole file — make targeted edits

## Step 3 — Update README.md (base station)

File: `C:\Kris\Projects\F3K_Timer_Project\README.md`

Update only the sections that are now stale:

- **Web UI table** (`| Route | Purpose |`): update the row for any route that gained new functionality; keep rows concise (≤1 line each)
- **GliderScore Integration** section: update if import/export changed
- **Architecture** file tree: add new files if any were created
- Do NOT rewrite prose that is still accurate

## Step 4 — Update NEXT_SESSION_PROMPT.md

File: `C:\Kris\Projects\F3K_Timer_Project\NEXT_SESSION_PROMPT.md`

Replace only the `### Where we are (end of session N)` section:

- Bump the session number
- Summarise what was completed this session (3–6 bullet points, concrete and specific)
- Update **Next priorities** to the current top 3 items from SESSION_STATE "Immediate Next Steps" (first non-done items)

Do not change "## PROMPT TO PASTE", "### How to work", or any other section.

## Step 5 — Update timer README (if firmware changed)

File: `C:\Kris\Projects\F3K_Timer_1\README.md`

Only if `C:\Kris\Projects\F3K_Timer_1\src\` files changed.

- **Features** section: add new firmware features (one bullet each, concise)
- **State Machine** diagram: update if new states were added
- Keep the existing structure; only add/change what's new

## Step 6 — Stage the doc files

`SESSION_STATE.md` and `NEXT_SESSION_PROMPT.md` are **gitignored** (local workflow files only — do not force-add them). Only stage the committed docs:

```powershell
# Stage base station README
git -C "C:\Kris\Projects\F3K_Timer_Project" add README.md

# Stage timer README only if it changed
git -C "C:\Kris\Projects\F3K_Timer_1" add README.md
```

## Step 7 — Report

List the files updated and a one-line summary of each change. The user can now run "commit this" (or commit both repos if firmware also changed).

## Rules

- **Today's date** is available in `$CURRENT_DATE` — use it for the SESSION_STATE timestamp.
- **Session number**: read from the existing `*Last updated:*` line in SESSION_STATE.md and increment by 1.
- Keep doc updates **factual and brief** — describe what was built, not how it works internally.
- Do NOT update `GLIDERSCORE.md`, `GUARDRAILS.md`, or `PROJECT_PHASES.md` unless explicitly told to.
- Do NOT add placeholder text like "TBD" or "coming soon" — only document what exists.
- If SESSION_STATE was already updated mid-session (partial updates), read its current state carefully before editing to avoid duplicating entries.
