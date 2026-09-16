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
- [`glab`](https://gitlab.com/gitlab-org/cli) authenticated on **both** GitLab
  instances — `gitlab.com` as `jdoe`, `gitlab.example.com` as
  `john.doe` (`glab auth status` lists them)
- macOS iTerm2 (optional; tab colour and the table's hyperlinks use OSC escape codes that other terminals ignore)

## The two instances

The company is migrating repo by repo from gitlab.com to the self-hosted
`gitlab.example.com`, so both carry live MRs. `HOSTS` in `monitor_prs.py` names
each one with the user's handle there and the group to list:

| Instance | Handle | Group listed |
|---|---|---|
| `gitlab.com` | `jdoe` | `acme` |
| `gitlab.example.com` | `john.doe` | `acme/portal` |

The self-hosted scope is the `portal` subgroup — the repos that have moved
so far (`portal-backend`, `portal-frontend`) — rather than the
`acme` root. A repo mid-migration has the same MR open on both sides, and
a root-level scope would report it twice. Widen the group as more repos land.

An instance being unreachable fails the whole run rather than yielding a partial
table: a report silently missing one instance's MRs reads as "all clear".

## Usage

```bash
# One check, then exit (what the /monitor-prs skill runs).
python3 monitor_prs.py

# Standalone terminal mode: repeat forever, aligned to wall-clock ticks
# (:00, :05, :10, … with the default 300s). Ctrl+C to stop.
python3 monitor_prs.py --loop        # every 5 min
python3 monitor_prs.py --loop 600    # custom interval in seconds

# Passive watcher: re-run the check on an interval and exit as soon as the
# report changes (a status/approval change, a merge, deployment progress) —
# designed to be launched as a background task by an LLM session so it gets
# re-invoked when something changes. Prints a TIMEOUT line after the cap.
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

The report is deliberately just **one table** — no activity-delta alerts, no
prose suggestion sections, no status line. The leading mark column and the Status
column are the whole picture.

1. **Lists open MRs** assigned to the user in each instance's group (incl. subgroups).
2. **Hydrates each MR** with merge status, conflicts, approvals, comment count
   (calls run in parallel across MRs).
3. **Detects MRs merged since the last check** (by me, a teammate, or the
   calling LLM) and adds them under `deployments`, pointed at the `main`
   pipeline of their merge commit.
4. **Prints one table** — `⟨mark⟩ | PR title | Task | PR | Status`, sorted so
   rows needing action come first, then by task, repo and MR number, with a
   separator rule between each severity block and between the two row groups.

   The unlabelled first column is the severity mark, the primary status channel:
   plain ASCII, so it survives any terminal, font size, or colour vision.

   The PR cell (`repo!iid`) is a terminal hyperlink to the merge request
   (cmd+click in iTerm2), pointing at whichever instance the MR lives on. The
   cell never names the instance — the link carries it.

   | Mark | Meaning | Examples |
   |---|---|---|
   | `!` | on me now | mergeable and approved, rebase needed, conflicts, my own threads left to resolve, awaiting prod trigger, failed pipeline |
   | `?` | on me to look at or reply to | requested changes, a reviewer's unresolved threads, an unrecognised status |
   | `~` | on someone else, until a person moves | not approved, draft, blocked by another MR |
   | `.` | on a machine, resolves by itself | CI running, mergeability being computed, prod deploying |

   The marks read as a weight gradient — `!` is the heaviest shape, `.` the
   lightest — so the eye lands on the top of the table without resolving the
   characters.

   The Status column repeats the same state as a glyph plus a text label. Glyphs
   are redundant garnish — every distinction is readable from the mark and the
   label alone.

   The two row groups:
   - **open MRs first**, Status = review state in plain words, with the approval
     state appended (`draft`, `awaiting review`, `rebase needed, unapproved`,
     `mergeable, approved`, …);
   - **merged-but-not-yet-deployed MRs at the bottom**, Status = deploy state
     (awaiting prod trigger, prod deploying, prod deploy failed, pipeline
     building/failed). A merged MR drops off — and out of tracking — once
     production has been shipped at or after its merge commit: its own prod
     deploy succeeding, **or** any later `main` pipeline shipping to prod (a newer
     commit carries the earlier ones with it). See
     `latest_prod_shipped_pipeline_id`. These rows are purely informational and
     never affect the tab colour.
5. **Sets the iTerm2 tab colour**, a convenience duplicate of the `!` block at
   the top of the table. It tracks **only open-PR work** — a fully-approved
   mergeable MR, a needed rebase, or conflicts. Deployment progress never flips
   it, and nothing is ever reported by colour alone.
   - **Orange** — something needs me on an open PR.
   - **Green** — all clear.

Long task names and PR titles are truncated (`TASK_MAX` / `TITLE_MAX`) so the
table keeps a fixed width and never wraps in the terminal.

## State file

`~/.claude/pr-monitor-state.json`

Keys are `<host>:<repo>!<iid>`. The host prefix is what keeps the two instances
apart while a repo lives on both — `docs!1410` exists on each. The `repo!iid`
the table shows is stored separately as `short_id`.

```jsonc
{
  "open_mrs": {
    "gitlab.com:frontend!1659": {
      "title": "...",
      "task_name": "...",       // derived from the dev/<slug>-<hash> branch
      "note_count": 3,
      "state": "opened",
      "approvals": ["carol7"],
      "fully_approved": false,
      "has_conflicts": false,
      "merge_status": "not_approved",
      "threads_await_user": true,  // false when every unresolved thread is mine alone
      "host": "gitlab.com",         // which instance; `glab api --hostname` takes it
      "short_id": "frontend!1659",  // what the PR column shows
      "project_path": "acme/common/frontend",
      "iid": 1659,
      "web_url": "https://gitlab.com/.../merge_requests/1659",
      "last_checked": "2026-07-06T10:30:00+00:00"
    }
  },
  "deployments": {
    "gitlab.example.com:portal-frontend!47": {
      "title": "...",
      "task_name": "...",
      "host": "gitlab.example.com",
      "short_id": "portal-frontend!47",
      "project": "acme%2Fportal%2Fportal-frontend",
      "project_path": "acme/portal/portal-frontend",
      "iid": 47,
      "web_url": "https://gitlab.example.com/.../merge_requests/47",
      "pipeline_id": 12345,
      "pipeline_status": "success",
      "pipeline_url": "https://gitlab.example.com/.../pipelines/12345",
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
- `host`/`project_path`/`iid` in `open_mrs` are what allow a later run to
  recognise a vanished MR as merged and start tracking its deployment. Entries
  written by older versions lack `project_path`/`iid`; those MRs are silently
  skipped for deploy tracking. An entry without `host` is read as `gitlab.com`,
  so state written before the second instance existed still resolves.
- Because the report is table-only, the watcher wakes on anything visible in the
  table — a Status change (approval-to-full, rebase needed, conflict, mergeable),
  a merge, a new/removed MR, or deployment progress. It no longer wakes on a bare
  new comment or a partial approval that doesn't move the Status label, since
  those aren't shown. `note_count`/`approvals` are still recorded in the state
  file for anything that wants finer-grained detection.
- An unresolved thread I opened myself, with nobody else writing in it, is a
  hint left for the reviewer rather than feedback waiting on me. Such an MR
  reports as `awaiting review`, or as `resolve own threads` once it is approved
  and those threads are the only thing GitLab still blocks the merge on. One
  note from anyone else in the thread makes it `unresolved threads` again.
- The script does **not** post Slack messages or open browsers; it just prints
  to stdout and colours the iTerm tab.
