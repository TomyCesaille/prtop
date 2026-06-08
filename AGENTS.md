# AGENTS.md — notes for future LLM edits

This repo is a deterministic, **no-LLM** port of the
`/monitor-prs` Claude Code skill. When changing behaviour here, keep
parity with that skill unless the user explicitly asks for a divergence.

## Source of truth

- Original skill: `~/.claude/commands/monitor-prs.md`
  (looped variant: `monitor-prs-looped.md`)
- This Python port: `monitor_prs.py`

When the skill is updated, mirror semantically-equivalent changes here.

## Architecture

Single file, stdlib only. Sections in order:

1. **Configuration constants** (`GITLAB_USERNAME`, `GITLAB_GROUP`, `TRIGRAM`,
   `STATE_FILE`, regexes, `SAFE_TO_REBASE_AFTER_APPROVAL`).
2. **`_run` / `glab_api`** — subprocess wrappers around `glab api`. All GitLab
   calls go through `glab_api`; never construct HTTP requests directly.
3. **State I/O** — `load_state` / `save_state`. Atomic write via `.json.tmp`.
4. **Fetch & gather** — `fetch_open_mr_summaries` (group-level list),
   `fetch_mr_detail` (per-MR hydration, two API calls), `gather_all_mrs`
   (ThreadPoolExecutor fan-out across MRs).
5. **Diff** — `diff_open_mrs` produces a list of `- \`mr_id\` — change, change`
   strings; `format_status_table` renders the markdown summary.
6. **Special actions** — `maybe_rebase_mrs`, `maybe_merge_fully_approved`,
   `merge_and_track`.
7. **Deployments** — `check_deployments` implements the
   `staging → staging_done → prod_approved` state machine.
8. **iTerm tab colour** — OSC escape codes.
9. **`run_once` / `main`** — top-level flow and CLI.

## State schema

Shared with the original skill at `~/.claude/pr-monitor-state.json`. Do not
add fields the skill won't recognise without also updating the skill.

```jsonc
{
  "open_mrs":    { "<project>!<iid>": { title, task_name, note_count, state,
                                        approvals[], fully_approved, has_conflicts,
                                        merge_status, last_checked } },
  "deployments": { "<project>!<iid>": { title, project (url-encoded), project_path,
                                        pipeline_id, pipeline_status, pipeline_url,
                                        stage, prod_job_id? } },
  "last_checked": "<iso>"
}
```

`stage` ∈ `{"staging", "staging_done", "prod_approved"}`.

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
- After merging via API, the pipeline on `main` may not exist yet.
  `find_main_pipeline_for_sha` polls a few times before giving up.

## Behavioural rules to preserve

- **First run** records a baseline and reports no diffs — never alert on MRs
  seen for the first time.
- **Never auto-trigger production deploys.** They are always behind an
  interactive confirmation.
- **Rebase safety**: only auto-rebase if the MR has no approvals OR the project
  is in `SAFE_TO_REBASE_AFTER_APPROVAL`. Otherwise ask first — some
  projects (`portal/*`) reset approvals on push.
- **Newer-pipeline-shipped-prod check**: before transitioning to
  `staging_done`, scan later main pipelines for an already-successful prod
  job. If found, mark deployed and drop from `deployments`.
- **iTerm colour at the very end**, exactly once per cycle. Orange iff
  something needs the user.
- **Non-TTY / `--no-interactive`** → all `ask_yes_no` calls return `False`.
  Report state but take no destructive action.

## Adding a project to the rebase-safe list

Append the full path (e.g. `"acme/common/foo"`) to
`SAFE_TO_REBASE_AFTER_APPROVAL`. The check is exact string match against
`info["_project_path"]`.

## Testing

There is no test suite. Verify changes by running against the live state file
in `--no-interactive` mode:

```bash
python3 monitor_prs.py --no-interactive
```

If you need to test diff logic without GitLab, point `STATE_FILE` at a temp
path and craft a fixture state. Don't commit fixture state.

## What NOT to do

- Don't add a dependency on `requests`, `aiohttp`, etc. Stdlib + `glab` only.
- Don't write to `~/.claude/` other than `pr-monitor-state.json`.
- Don't change the state-file schema without also updating the original skill.
- Don't add LLM calls. The whole point of this port is to skip them.
