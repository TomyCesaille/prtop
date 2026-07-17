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
- **`watch_prs.sh`**: passive watcher for task-development sessions. Re-runs
  `monitor_prs.py` on an interval and exits when the report changes (activity
  alert, or any diff in the normalized output) or on a quiet-cap TIMEOUT, so a
  backgrounded run re-invokes the calling agent at the right moment. The
  "Passive PR monitoring" section of the global `~/.claude/CLAUDE.md` tells
  sessions when to start it.

## Source of truth

- Skill: `~/.claude/commands/monitor-prs.md`
  (looped variant: `monitor-prs-looped.md`)
- This Python engine: `monitor_prs.py`

The report is **one table** (`Task | PR title | PR | Status`) — nothing else, no
status line. Open MRs come first with their review status; merged-but-not-yet-
deployed MRs sit at the bottom with their deploy status in the same Status column.
No activity-delta alerts, no prose suggestion sections;
the skill reads the Status column directly. The merged rows are informational only;
the skill relays them but never acts on them. If you change the Status labels or
the columns, update the skill's interpretation table.

## Architecture

Single file, stdlib only. Sections in order:

1. **Configuration constants** (`GITLAB_USERNAME`, `GITLAB_GROUP`, `TRIGRAM`,
   `STATE_FILE`, regexes).
2. **`_run` / `glab_api`** — subprocess wrappers around `glab api`. All GitLab
   calls go through `glab_api`; never construct HTTP requests directly.
   `glab_api` is GET-only by design — keep it that way.
3. **State I/O** — `load_state` / `save_state`. Atomic write via `.json.tmp`.
4. **Fetch & gather** — `fetch_open_mr_summaries` (group-level list),
   `fetch_mr_detail` (per-MR hydration, two API calls), `gather_all_mrs`
   (ThreadPoolExecutor fan-out across MRs).
5. **Track merges** — `track_vanished_mrs` resolves MRs that left the open list;
   a merged one gets a `deployments` entry pointing at its merge-commit main
   pipeline (pure side effect, no output). `open_mr_rows` builds the open-MR
   rows for the table.
6. **Deployments** — `check_deployments` classifies each tracked MR against a
   per-project **production frontier** (`latest_prod_shipped_pipeline_id`: the
   highest `main` pipeline whose prod deploy jobs succeeded). Any MR whose merge
   pipeline id is ≤ that frontier is in prod → dropped from tracking; the rest
   are returned as rows (with a status from `pending_deploy_status`) that
   `run_once` appends to the bottom of the same table. Frontiers (per project)
   and per-MR evaluation both fan out over a `ThreadPoolExecutor`. Purely
   informational — never contributes to `action_needed`.
7. **iTerm tab colour** — OSC escape codes, emitted once at the very end; the
   only signal `action_needed` drives (it is not printed).
8. **`run_once` / `main`** — top-level flow and CLI; single check by default,
   `--loop [SECONDS]` for wall-clock-aligned repetition. In `--loop` mode the
   only chrome is one `next tick @ <date HH:MM>` line before each sleep.

## State schema

`~/.claude/pr-monitor-state.json`

```jsonc
{
  "open_mrs":    { "<project>!<iid>": { title, task_name, note_count, state,
                                        approvals[], fully_approved, has_conflicts,
                                        merge_status, project_path, iid, web_url,
                                        last_checked } },
  "deployments": { "<project>!<iid>": { title, task_name, project (url-encoded),
                                        project_path, pipeline_id, pipeline_status,
                                        pipeline_url, prod_job_id? } },
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
- `user_notes_count` on the MR object excludes system notes — it's recorded in
  the state as `note_count` (not currently surfaced in the report). Don't
  paginate `/notes` to count.
- The group endpoint
  `groups/acme/merge_requests?assignee_username=…&state=opened&per_page=100`
  returns subgroup MRs too. `references.full` gives `"<full-path>!<iid>"`.
- The production *deploy* job is recognised by name match
  `deploy.*prod|prod.*deploy` (`DEPLOY_PROD_JOB_RE`, case-insensitive). This
  deliberately excludes prod *test*/*migrate*/*route-traffic* jobs (e.g.
  `test_scenarios_production`, `migrate_production`, `route_traffic_production`)
  that a looser `prod|production` match would wrongly pick up.
- **"Shipped prod" is judged by the _primary_ deploy job** — the one whose name
  is exactly `deploy_production`/`deploy-production` (`PRIMARY_PROD_DEPLOY_RE`),
  via `primary_prod_jobs` → `shipped_prod`. The auxiliary suffixed variants
  (`deploy_production_heavy_duty`, `deploy_production_certificates_exporter`, …)
  are separate side services, often left `manual`/`created` indefinitely;
  gating on *all* prod-deploy jobs succeeding wrongly reports the app as
  never-deployed. Only when no canonical-named job exists does it fall back to
  "any prod-deploy job succeeded".
- Right after a merge, the pipeline on `main` may not exist yet.
  `find_main_pipeline_for_sha` polls a few times before giving up, then
  `track_vanished_mrs` falls back to the most recent main pipeline.
- GitLab recomputes `has_conflicts`/`detailed_merge_status` lazily — a
  conflict can show up with `merge_status` still `not_approved`, which is why
  `action_needed` checks `has_conflicts` independently of `merge_status`.

## Behavioural rules to preserve

- **The script never mutates GitLab.** Merging, rebasing, and playing jobs
  are the skill's job, gated on user confirmation there. Don't add `--method`
  support back to `glab_api`.
- **One run per invocation by default.** `--loop [SECONDS]` is the only loop
  (wall-clock-aligned, for standalone terminal use; `glab` errors are
  non-fatal there). Never add prompts back in either mode.
- **First run** records a baseline; `track_vanished_mrs` sees an empty previous
  set, so nothing is detected as merged on that run.
- **Report is one table, nothing else** (no status line). Open MRs on top,
  merged-awaiting-deploy rows at the bottom, split by a separator rule (a `None`
  entry in the rows list, which `_render_table` renders as a horizontal rule).
  Rebase-safety nuance (which projects reset approvals on push) now lives in the
  skill, not here — don't reintroduce prose alert/suggestion sections or a second
  table.
- **Production-frontier model**: an MR is judged deployed against a single
  per-project frontier, not just its own pipeline — so an MR carried to prod by
  a *later* commit is correctly treated as shipped and dropped, even if its own
  prod job was never triggered. Compare merge-pipeline id ≤ frontier id (`main`
  is linear, so pipeline id order == commit order).
- **`action_needed` covers open-PR work only** — a fully-approved mergeable MR,
  a needed rebase, or conflicts. Deployment status must never flip it. It is
  never printed; it exists solely to pick the tab colour.
- **`🕹️ awaiting prod trigger` is a markdown link** to the project's
  `/-/commits/main?ref_type=HEADS` page (built from the deployment entry's
  `project_path`) — that page is where the manual prod job gets triggered from.
  Keep the label text inside the link unchanged; the skill matches on it.
- **iTerm colour at the very end**, exactly once, after the table.
- **Table padding goes through `_display_width`, never `len`/`str.ljust`.** A
  single-codepoint emoji in the Status column (👀, 🚀, …) is 1 code point but 2
  terminal cells; padding by `len` under-counts it and skews the right border.

## Testing

There is no test suite. Don't run the script against the live state file for
testing — a run advances the merge-detection baseline (an MR that merged between
the real last run and your test run would be recorded as tracked/handled and
missed by the next real run). Point `PR_MONITOR_STATE_FILE` at a scratch copy
instead:

```bash
cp ~/.claude/pr-monitor-state.json /tmp/scratch-state.json
PR_MONITOR_STATE_FILE=/tmp/scratch-state.json python3 monitor_prs.py
```

To exercise `track_vanished_mrs`, seed the scratch state's `open_mrs` with a
real, already-merged MR (with `project_path` + `iid`) and run.

To test `watch_prs.sh` deterministically, copy it next to a stub
`monitor_prs.py` whose output changes on the Nth run — the watcher only knows
"the script in my directory", so no GitLab is involved.

## What NOT to do

- Don't add a dependency on `requests`, `aiohttp`, etc. Stdlib + `glab` only.
- Don't write to `~/.claude/` other than `pr-monitor-state.json`.
- Don't change the state-file schema without also updating the skill.
- Don't add LLM calls, interactive prompts, or GitLab mutations.
