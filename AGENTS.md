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

The report is **one table** (`⟨mark⟩ | PR title | Task | PR | Status`) — nothing
else, no tally and no status line. Open MRs come first with their
review status; merged-but-not-yet-deployed MRs sit at the bottom with their
deploy status in the same Status column. No activity-delta alerts, no prose
suggestion sections; the skill reads the mark and Status columns directly. The
merged rows are informational only; the skill relays them but never acts on them.
If you change the marks, the Status labels, or the columns, update the skill's
interpretation table.

**Accessibility contract.** The report is read by scanning, and must not rely on
color, emoji fidelity, or fine glyph detail. So state is encoded four times over,
weakest channel last:

1. **Severity mark** — plain ASCII, one cell wide. This is the channel that must
   never regress.

   | Mark | Meaning |
   |---|---|
   | `!` | on the user now |
   | `?` | on the user to look at or reply to |
   | `~` | on someone else; it sits there until a person moves |
   | `.` | on a machine; it resolves by itself |

   The marks also read as a weight gradient (`!` heaviest → `.` lightest), so
   the eye finds the top of the table without resolving the characters. Keep any
   new mark inside that gradient.
2. **Position** — rows sort by mark, then by task, repo and MR number, and
   `_split_by_mark` rules off each severity block, so a block can be read without
   scanning the marks at all and one task's MRs stay adjacent inside it.
3. **Text label** (`conflicts, unapproved`) — unambiguous on its own. Plain words
   rather than GitLab's `detailed_merge_status` enum, and short enough to fit
   `STATUS_WIDTH`; the approval state rides along as `, approved` / `, unapproved`
   except where the label already implies it (`draft`, `awaiting review`).
4. **Glyph** — redundant garnish. Never the sole carrier of a distinction, never
   two different meanings across the open/deploy halves of the Status column, and
   never distinguished from another glyph by hue alone.

Adding a state means picking its mark first and its glyph last. `test_status.py`
holds this contract as executable tests — an `AccessibilityContract` case fails
if a state goes glyph-only, a mark stops being single-char ASCII, or a glyph
picks up a second meaning.

## Architecture

Single file, stdlib only. Sections in order:

1. **Configuration constants** (`GITLAB_USERNAME`, `GITLAB_GROUP`, `TRIGRAM`,
   `STATE_FILE`, regexes).
2. **`_run` / `glab_api`** — subprocess wrappers around `glab api`. All GitLab
   calls go through `glab_api`; never construct HTTP requests directly.
   `glab_api` is GET-only by design — keep it that way.
3. **State I/O** — `load_state` / `save_state`. Atomic write via `.json.tmp`.
4. **Fetch & gather** — `fetch_open_mr_summaries` (group-level list),
   `fetch_mr_detail` (per-MR hydration, two API calls, plus `fetch_discussions`
   on an MR whose threads block the merge), `gather_all_mrs`
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
7. **iTerm tab colour** — OSC escape codes, emitted once at the very end. It is
   a convenience duplicate of the `!` block at the top of the table, never the
   only place a state is reported.
8. **`run_once` / `main`** — top-level flow and CLI; single check by default,
   `--loop [SECONDS]` for wall-clock-aligned repetition. In `--loop` mode the
   only chrome is one `next tick @ <date HH:MM>` line before each sleep.

## State schema

`~/.claude/pr-monitor-state.json`

```jsonc
{
  "open_mrs":    { "<project>!<iid>": { title, task_name, note_count, state,
                                        approvals[], fully_approved, has_conflicts,
                                        merge_status, threads_await_user,
                                        project_path, iid, web_url,
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
- **`detailed_merge_status` names one blocker**, so `discussions_not_resolved`
  hides the approval state — which is why the report keeps reading it from
  `/approvals` and appending it as `, approved` / `, unapproved`.
- `/merge_requests/<iid>/discussions` is where thread state lives: resolvability
  and resolution are per *note* (`resolvable`, `resolved`), a thread is
  unresolved when any of its notes is, and plain comments come back as
  `individual_note` discussions whose notes aren't resolvable. It paginates —
  `fetch_discussions` walks up to ten pages, because a CodeRabbit-reviewed MR
  runs past one and a single reviewer thread anywhere in them decides the
  status.

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
- **`action_needed` covers open-PR work only** — derived as "any open row marked
  `!`", which is a fully-approved mergeable MR, a needed rebase, or conflicts.
  Deploy rows carry `!` marks of their own (failed pipeline, awaiting prod
  trigger) but are excluded from the flag, so deployment status never flips the
  tab colour.
- **The user's own unresolved threads are hints, not feedback.** A thread he
  opened on his own MR that nobody answered is something he left for the
  reviewer, so `? 💬 unresolved threads` would put his own note back on his
  plate. `unresolved_threads_await_user` decides it — one note from anyone else
  in an unresolved thread makes the whole MR feedback to answer again — and
  `fmt_status` swaps the status for `not_approved` while the review is still
  outstanding, or for the synthetic `own_threads_unresolved`
  (`! 🧵 resolve own threads`) once the MR is approved and those threads are all
  that blocks the merge.
- **`has_conflicts` outranks `detailed_merge_status`.** GitLab sets the two
  independently and reports `not_approved` on MRs that do have conflicts;
  `fmt_status` rewrites the status to `conflict` so the mark, glyph, and label
  agree instead of the conflict showing up only as an orange tab.
- **The PR title cell is an OSC 8 terminal hyperlink** (`_linked_title`) to the
  merge request: `web_url` for an open MR, rebuilt from `project_path` and the
  number in the MR id for a pending deploy. The title is truncated to
  `TITLE_MAX` before the link wraps it, so the padding stays right.
- **`🕹️ awaiting prod trigger` is an OSC 8 terminal hyperlink** (`_hyperlink`,
  BEL-terminated) to the project's `/-/commits/main?ref_type=HEADS` page (built
  from the deployment entry's `project_path`) — that page is where the manual
  prod job gets triggered from. Not a markdown link: the terminal would show
  the URL raw. BEL termination keeps it stripped by `watch_prs.sh`'s
  `normalize` and by `_display_width`, so it never skews diffs or padding.
  Keep the label text inside the link unchanged; the skill matches on it.
- **iTerm colour at the very end**, exactly once, after the table.
- **Table padding goes through `_display_width`, never `len`/`str.ljust`.** A
  single-codepoint emoji in the Status column (👀, ✅, …) is 1 code point but 2
  terminal cells; padding by `len` under-counts it and skews the right border.
- **The Status column is floored at `STATUS_WIDTH`** (via `MIN_WIDTHS`), the
  widest cell the status vocabulary can produce, so one MR changing state can't
  reflow the whole table. `watch_prs.sh` exits on any text change and hands the
  new report to an agent to read: with a floating column every row's padding
  shifts at once, hiding which row actually moved. A new or longer status label
  means updating the constant; `test_status.py` enumerates the vocabulary and
  fails if it drifts either way.

## Testing

```bash
python3 test_status.py
```

`test_status.py` covers the pure layer — severity marks, deploy signals, the
accessibility contract, ordering, and table rendering. Stdlib
`unittest`, no network, no state file. Run it after any change to marks, glyphs,
status labels, or the table; add a case alongside each new state.

Everything above that layer (GitLab fetching, merge detection, the production
frontier) has no automated coverage and needs a real run. Don't run the script
against the live state file for testing — a run advances the merge-detection
baseline (an MR that merged between the real last run and your test run would be
recorded as tracked/handled and missed by the next real run). Point
`PR_MONITOR_STATE_FILE` at a scratch copy instead:

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
