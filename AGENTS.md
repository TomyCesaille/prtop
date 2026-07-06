# AGENTS.md — notes for future LLM edits

This repo is the deterministic, **no-LLM** data-gathering engine behind the
`/monitor-prs` Claude Code skill. The skill runs `monitor_prs.py` once
per check, reads its stdout, and handles all user interaction and GitLab
mutations itself. When changing behaviour here, keep the skill's
interpretation table in sync.

## Division of responsibilities

- **This script**: read-only against GitLab. Fetch, diff, track, report,
  suggest. One run per invocation — no loop, no prompts, no merges, no
  rebases, no job triggers.
- **The skill** (`~/.claude/commands/monitor-prs.md`): relays the
  report, asks the user for confirmations, and performs approved actions via
  `glab api`. Looping lives in `monitor-prs-looped.md`.

## Source of truth

- Skill: `~/.claude/commands/monitor-prs.md`
  (looped variant: `monitor-prs-looped.md`)
- This Python engine: `monitor_prs.py`

The skill maps the script's suggestion strings ("ready to merge", "safe to
rebase automatically", "awaits a manual trigger", …) to actions — if you
reword them here, update the skill's table.

## Architecture

Single file, stdlib only. Sections in order:

1. **Configuration constants** (`GITLAB_USERNAME`, `GITLAB_GROUP`, `TRIGRAM`,
   `STATE_FILE`, regexes, `SAFE_TO_REBASE_AFTER_APPROVAL`).
2. **`_run` / `glab_api`** — subprocess wrappers around `glab api`. All GitLab
   calls go through `glab_api`; never construct HTTP requests directly.
   `glab_api` is GET-only by design — keep it that way.
3. **State I/O** — `load_state` / `save_state`. Atomic write via `.json.tmp`.
4. **Fetch & gather** — `fetch_open_mr_summaries` (group-level list),
   `fetch_mr_detail` (per-MR hydration, two API calls), `gather_all_mrs`
   (ThreadPoolExecutor fan-out across MRs).
5. **Diff** — `diff_open_mrs` produces `- \`mr_id\` — change, change` lines;
   `track_vanished_mrs` resolves MRs that left the open list (merged → add a
   `deployments` entry pointing at the merge commit's main pipeline; closed →
   plain alert); `format_status_table` renders the markdown summary.
6. **Suggestions** — `merge_notes` (fully approved + mergeable) and
   `attention_notes` (conflicts, safe vs confirm-first rebases). Info only.
7. **Deployments** — `check_deployments` re-derives each tracked deployment's
   status per run (no stage machine): staging pipeline → prod job →
   success/failure/awaiting-manual-trigger.
8. **iTerm tab colour** — OSC escape codes, emitted once at the very end.
9. **`run_once` / `main`** — top-level flow; one check, then exit.

## State schema

`~/.claude/pr-monitor-state.json`

```jsonc
{
  "open_mrs":    { "<project>!<iid>": { title, task_name, note_count, state,
                                        approvals[], fully_approved, has_conflicts,
                                        merge_status, project_path, iid, web_url,
                                        last_checked } },
  "deployments": { "<project>!<iid>": { title, project (url-encoded), project_path,
                                        pipeline_id, pipeline_status, pipeline_url,
                                        prod_job_id? } },
  "last_checked": "<iso>"
}
```

`project_path`/`iid` in `open_mrs` are what let `track_vanished_mrs` recognise
a vanished MR as merged. Legacy deployment entries may still carry a `stage`
field; `check_deployments` pops it.

## glab quirks

- **`glab api` does not accept `--jq`** — pipe through `json.loads` and filter
  in Python.
- **Project paths must be URL-encoded** in `glab api` paths
  (`acme/common/frontend` → `acme%2Fcommon%2Ffrontend`). Use
  `urllib.parse.quote(path, safe="")`.
- `user_notes_count` on the MR object excludes system notes — that's the right
  field to diff against. Don't paginate `/notes` to count.
- The group endpoint
  `groups/acme/merge_requests?assignee_username=…&state=opened&per_page=100`
  returns subgroup MRs too. `references.full` gives `"<full-path>!<iid>"`.
- Manual production jobs are recognised by name match `prod|production`
  (case-insensitive). Don't rely on `stage == "deploy"` alone — staging jobs
  also live there.
- Right after a merge, the pipeline on `main` may not exist yet.
  `find_main_pipeline_for_sha` polls a few times before giving up, then
  `track_vanished_mrs` falls back to the most recent main pipeline.
- GitLab recomputes `has_conflicts`/`detailed_merge_status` lazily — a
  conflict can show up with `merge_status` still `not_approved`, which is why
  `attention_notes` checks `has_conflicts` independently.

## Behavioural rules to preserve

- **The script never mutates GitLab.** Merging, rebasing, and playing jobs
  are the skill's job, gated on user confirmation there. Don't add `--method`
  support back to `glab_api`.
- **One run per invocation.** No loop, no sleep-until-tick, no prompts.
- **First run** records a baseline and reports no diffs — never alert on MRs
  seen for the first time.
- **Rebase safety info**: an MR with approvals in a project outside
  `SAFE_TO_REBASE_AFTER_APPROVAL` is reported as confirm-first — some
  projects (`portal/*`) reset approvals on push.
- **Newer-pipeline-shipped-prod check**: before reporting "awaits a manual
  trigger", scan later main pipelines for an already-successful prod job. If
  found, report deployed and drop from `deployments`.
- **Every orange trigger must appear in the report.** `action_needed` and the
  printed sections are computed from the same conditions; if you add a
  condition to one, add it to the other.
- **iTerm colour at the very end**, exactly once, after the plain
  `Action needed: yes/no` line.

## Adding a project to the rebase-safe list

Append the full path (e.g. `"acme/common/foo"`) to
`SAFE_TO_REBASE_AFTER_APPROVAL`. The check is exact string match against
`info["project_path"]`.

## Testing

There is no test suite. Don't run the script against the live state file for
testing — a run advances the diff baseline and swallows the user's pending
alerts. Instead, import the module, point `STATE_FILE` at a scratch copy, and
call `run_once()`:

```python
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("mp", "monitor_prs.py")
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)
mp.STATE_FILE = Path("/tmp/scratch-state.json")  # copy of the real one
mp.run_once()
```

To exercise `track_vanished_mrs`, seed the scratch state's `open_mrs` with a
real, already-merged MR (with `project_path` + `iid`) and run.

## What NOT to do

- Don't add a dependency on `requests`, `aiohttp`, etc. Stdlib + `glab` only.
- Don't write to `~/.claude/` other than `pr-monitor-state.json`.
- Don't change the state-file schema without also updating the skill.
- Don't add LLM calls, interactive prompts, or GitLab mutations.
