# monitor-prs

Data-gathering engine behind the `/monitor-prs` Claude Code skill.
Polls GitLab via the `glab` CLI, diffs state, alerts on changes, and tracks
production deployments — **without consuming any LLM tokens**.

The script is strictly **read-only against GitLab**: it never merges, rebases,
or triggers jobs. It reports what changed and what awaits action; acting on
the report is the caller's job (the skill asks for confirmation and runs the
`glab` mutation, or you do it by hand).

State lives in `~/.claude/pr-monitor-state.json`.

## Requirements

- Python 3.10+ (stdlib only — no `pip install` needed)
- [`glab`](https://gitlab.com/gitlab-org/cli) authenticated as `jdoe`
- macOS iTerm2 (optional; tab colour is set via OSC escape codes that other terminals ignore)

## Usage

```bash
# One check, then exit (what the /monitor-prs skill runs).
python3 monitor_prs.py

# Standalone terminal mode: repeat forever, aligned to wall-clock ticks
# (:00, :05, :10, … with the default 300s). Ctrl+C to stop.
python3 monitor_prs.py --loop        # every 5 min
python3 monitor_prs.py --loop 600    # custom interval in seconds

# Passive watcher: re-run the check on an interval and exit as soon as the
# report changes (activity alert, deployment progress, action flip) — designed
# to be launched as a background task by an LLM session so it gets re-invoked
# exactly when something needs attention. Prints a TIMEOUT line after the cap.
bash watch_prs.sh                 # defaults: 300s interval, 7200s cap
bash watch_prs.sh 60 3600         # custom interval / cap
```

Exit code is 1 (with the error on stderr) if `glab` fails; the watcher also
exits 1 when a check fails. In `--loop` mode `glab` errors are non-fatal —
the cycle logs them to stderr and retries at the next tick.

The watcher's first check only records a baseline (unless it already carries
an activity alert) — run a one-shot check and deal with pending items before
starting it.

`PR_MONITOR_STATE_FILE` overrides the state file path (both scripts honour
it) — useful for testing against a scratch copy.

## What one run does

1. **Lists open MRs** assigned to `jdoe` in the `acme` group (incl. subgroups).
2. **Hydrates each MR** with merge status, conflicts, approvals, comment count
   (calls run in parallel across MRs).
3. **Diffs against the saved state** and prints what changed: new comments, new
   approvers, fully-approved transitions, conflicts, rebase needed.
4. **Detects MRs merged since the last check** (by me, a teammate, or the
   calling LLM) and adds them under `deployments`, pointed at the `main`
   pipeline of their merge commit.
5. **Prints a "Merged, awaiting production deploy" table** — the merged MRs not
   yet in production, with a per-MR deploy status (awaiting prod trigger, prod
   deploying, prod deploy failed, pipeline building/failed). An MR drops off
   this table — and out of tracking — once production has been shipped at or
   after its merge commit: its own prod deploy succeeding, **or** any later
   `main` pipeline shipping to prod (a newer commit carries the earlier ones
   with it). See `latest_prod_shipped_pipeline_id`. This table is purely
   informational and never sets `Action needed`.
6. **Prints suggestion sections** for the caller to act on:
   - **Ready to merge** — fully approved + mergeable MRs;
   - **Needs attention** — conflicts, and rebases split into
     safe-to-do-automatically vs confirm-first (projects that reset approvals
     on push — everything outside `SAFE_TO_REBASE_AFTER_APPROVAL`).
7. **Prints `Action needed: yes/no`** and **sets the iTerm2 tab colour**. This
   flag tracks **only open-PR work** — ready to merge, needs rebase, or
   conflicts. Deployment progress never flips it.
   - **Orange** — something needs me on an open PR.
   - **Green** — all clear.

Long task names and PR titles are truncated (`TASK_MAX` / `TITLE_MAX`) so the
tables keep a fixed width and never wrap in the terminal.

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
      "project_path": "acme/common/frontend",
      "iid": 1659,
      "web_url": "https://gitlab.com/.../merge_requests/1659",
      "last_checked": "2026-07-06T10:30:00+00:00"
    }
  },
  "deployments": {
    "portal-frontend!47": {
      "title": "...",
      "task_name": "...",
      "project": "acme%2Fportal%2Fportal-frontend",
      "project_path": "acme/portal/portal-frontend",
      "pipeline_id": 12345,
      "pipeline_status": "success",
      "pipeline_url": "https://gitlab.com/.../pipelines/12345",
      "prod_job_id": 67890
    }
  },
  "last_checked": "2026-07-06T10:30:00+00:00"
}
```

Safe to delete — the next run will recreate it with a fresh baseline.

## Notes & caveats

- On the very first run nothing has been seen before, so no alerts fire — it
  just records a baseline and prints the table.
- `project_path`/`iid` in `open_mrs` are what allow a later run to recognise a
  vanished MR as merged and start tracking its deployment. Entries written by
  older versions lack them; those MRs are reported as plain "closed/merged".
- The script does **not** post Slack messages or open browsers; it just prints
  to stdout and colours the iTerm tab.
