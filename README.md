# monitor-prs

Local Python port of the `/monitor-prs` Claude Code skill.
Polls GitLab via the `glab` CLI, diffs state, alerts on changes, and tracks
production deployments — **without consuming any LLM tokens**.

State lives in `~/.claude/pr-monitor-state.json` (shared with the original skill,
so you can switch back and forth).

## Requirements

- Python 3.10+ (stdlib only — no `pip install` needed)
- [`glab`](https://gitlab.com/gitlab-org/cli) authenticated as `jdoe`
- macOS iTerm2 (optional; tab colour is set via OSC escape codes that other terminals ignore)

## Usage

```bash
# Loop forever, aligned to every 5-minute wall-clock tick (:00, :05, :10, …).
# Ctrl+C to stop.
python3 monitor_prs.py

# Custom interval in seconds (still wall-clock aligned).
python3 monitor_prs.py --interval 600

# Never prompt for confirmation — pure read-only report.
python3 monitor_prs.py --no-interactive

# Single iteration (debug / testing escape hatch).
python3 monitor_prs.py --once
```

Iterations fire at the next multiple of `--interval` past the hour, so a run
started at 10:13:47 with the default 300s sleeps to 10:15:00, then 10:20:00,
10:25:00, etc. — independent of how long each iteration takes.

## What it does each cycle

1. **Lists open MRs** assigned to `jdoe` in the `acme` group (incl. subgroups).
2. **Hydrates each MR** with merge status, conflicts, approvals, comment count
   (calls run in parallel across MRs).
3. **Diffs against the saved state** and prints what changed: new comments, new
   approvers, fully-approved transitions, conflicts, rebase needed, closed/merged.
4. **Offers interactive actions** (skipped silently with `--no-interactive` or non-TTY):
   - **Fully approved + mergeable** → asks to merge. On yes: merges, then adds an
     entry under `deployments` to track the resulting `main` pipeline.
   - **Needs rebase** → rebases automatically if the MR has no approvals OR the
     project is known to preserve approvals on push (currently only
     `acme/common/frontend`). Otherwise asks first.
5. **Tracks deployments** through stages `staging → staging_done → prod_approved`:
   - Watches the merged-MR pipeline on `main`.
   - When staging succeeds and the manual prod job is still pending, asks to
     trigger it.
   - Removes the deployment when the prod job succeeds, or alerts if it fails.
   - Also detects the case where a *later* main pipeline has already shipped
     prod (your changes rode along on a subsequent commit).
6. **Sets the iTerm2 tab colour**:
   - **Orange** — something needs you (fully approved, rebase/conflicts,
     `staging_done`, failed pipeline).
   - **Green** — all clear.

## State file

`~/.claude/pr-monitor-state.json`

```jsonc
{
  "open_mrs": {
    "frontend!1659": {
      "title": "...",
      "task_name": "...",       // derived from the dev/<slug>-<hash> branch
      "note_count": 3,
      "state": "opened",
      "approvals": ["carol7"],
      "fully_approved": false,
      "has_conflicts": false,
      "merge_status": "not_approved",
      "last_checked": "2026-06-08T10:30:00+00:00"
    }
  },
  "deployments": {
    "portal-frontend!47": {
      "title": "...",
      "project": "acme%2Fportal%2Fportal-frontend",
      "project_path": "acme/portal/portal-frontend",
      "pipeline_id": 12345,
      "pipeline_status": "success",
      "pipeline_url": "https://gitlab.com/.../pipelines/12345",
      "stage": "staging_done",
      "prod_job_id": 67890
    }
  },
  "last_checked": "2026-06-08T10:30:00+00:00"
}
```

Safe to delete — the next run will recreate it with a fresh baseline.

## Notes & caveats

- On the very first run nothing has been seen before, so no alerts fire — it
  just records a baseline and prints the table.
- Interactive prompts (merge, rebase-when-approved, trigger prod) only appear
  on a real TTY. Run with `--no-interactive` to make this explicit for cron /
  background use.
- Errors talking to `glab` are non-fatal in `--loop` mode — the cycle prints
  the error to stderr and continues at the next interval.
- The script does **not** post Slack messages or open browsers; it just prints
  to stdout and colours the iTerm tab.
