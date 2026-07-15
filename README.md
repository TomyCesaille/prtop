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
# report changes (a status/approval change, a merge, deployment progress, an
# action flip) — designed to be launched as a background task by an LLM session
# so it gets re-invoked when something changes. Prints a TIMEOUT line after the cap.
bash watch_prs.sh                 # defaults: 300s interval, 7200s cap
bash watch_prs.sh 60 3600         # custom interval / cap
```

Exit code is 1 (with the error on stderr) if `glab` fails; the watcher also
exits 1 when a check fails. In `--loop` mode `glab` errors are non-fatal —
the cycle logs them to stderr and retries at the next tick.

The watcher's first check only records a baseline — run a one-shot check and
deal with pending items before starting it.

`PR_MONITOR_STATE_FILE` overrides the state file path (both scripts honour
it) — useful for testing against a scratch copy.

## What one run does

The report is deliberately just **one table plus an `Action needed` line** — no
activity-delta alerts, no prose suggestion sections. The table's Status column is
the whole picture.

1. **Lists open MRs** assigned to `jdoe` in the `acme` group (incl. subgroups).
2. **Hydrates each MR** with merge status, conflicts, approvals, comment count
   (calls run in parallel across MRs).
3. **Detects MRs merged since the last check** (by me, a teammate, or the
   calling LLM) and adds them under `deployments`, pointed at the `main`
   pipeline of their merge commit.
4. **Prints one table** — `Task | PR title | PR | Status`, the two groups split
   by a separator rule when both are present:
   - **open MRs first**, Status = review state (draft, not-approved, need-rebase,
     conflict, mergeable, fully-approved, …);
   - **merged-but-not-yet-deployed MRs at the bottom**, Status = deploy state
     (awaiting prod trigger, prod deploying, prod deploy failed, pipeline
     building/failed). A merged MR drops off — and out of tracking — once
     production has been shipped at or after its merge commit: its own prod
     deploy succeeding, **or** any later `main` pipeline shipping to prod (a newer
     commit carries the earlier ones with it). See
     `latest_prod_shipped_pipeline_id`. These rows are purely informational and
     never set `Action needed`.
5. **Prints `Action needed: yes/no`** and **sets the iTerm2 tab colour**. This
   flag tracks **only open-PR work** — a fully-approved mergeable MR, a needed
   rebase, or conflicts. Deployment progress never flips it.
   - **Orange** — something needs me on an open PR.
   - **Green** — all clear.

Long task names and PR titles are truncated (`TASK_MAX` / `TITLE_MAX`) so the
table keeps a fixed width and never wraps in the terminal.

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

- On the very first run the previous state is empty, so nothing merged is
  detected — it just records a baseline and prints the table.
- `project_path`/`iid` in `open_mrs` are what allow a later run to recognise a
  vanished MR as merged and start tracking its deployment. Entries written by
  older versions lack them; those MRs are silently skipped for deploy tracking.
- Because the report is table-only, the watcher wakes on anything visible in the
  table — a Status change (approval-to-full, rebase needed, conflict, mergeable),
  a merge, a new/removed MR, deployment progress, or an `Action needed` flip. It
  no longer wakes on a bare new comment or a partial approval that doesn't move
  the Status label, since those aren't shown. `note_count`/`approvals` are still
  recorded in the state file for anything that wants finer-grained detection.
- The script does **not** post Slack messages or open browsers; it just prints
  to stdout and colours the iTerm tab.
