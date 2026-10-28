---
description: Check my merge requests and pull requests with prtop, and act on what is on me
---

Run `prtop` once with Bash and read its output: one table of my GitLab merge
requests and GitHub pull requests, the open ones first and the merged ones on their
way to production below. Run it once; don't loop or poll.

The first column says whose move it is. Act on it:

| Mark | Status | What to do |
|---|---|---|
| `!` | `mergeable, approved` | Ask me whether to merge it. Merge only on a yes. |
| `!` | `rebase needed, …` | Offer to rebase it through the API. |
| `!` | `conflicts, …` | Tell me, and offer to resolve them in a local checkout. |
| `!` | `resolve own threads, approved` | Only threads I opened block the merge. Ask whether to resolve them and merge. |
| `!` | `CI failed, …` | Read the failed jobs' logs and tell me what broke. Offer to fix what the change broke, or to retry a job that failed for a reason outside it. |
| `!` | `CI canceled, …` | Offer to retry the pipeline. |
| `!` | `no pipeline, …` | No pipeline ran on the last commit, and none starts on its own. Offer to start one. |
| `!` | `instance unreachable` | The forge the row names did not answer, so its open merge requests are unaccounted for. Relay the error from stderr; if it happens again, suggest `glab auth status` or `gh auth status`. |
| `?` | `changes requested, …` or `unresolved threads, …` | Sum up what the reviewers are waiting on, and offer to address it. |
| `?` | a label prtop does not recognise | Report the raw label. |
| `~` or `.` | anything | Context only: another person, or a machine, holds the next move. |

The merged rows at the bottom report deployment progress. Relay them when I ask,
and trigger a production job only when I ask.

When you relay a state, name it in words, never by its emoji alone. If every row is
`~` or `.`, say so in one line instead of pasting the table.

## Acting

prtop only reads. To act, find the row in prtop's state files, one per forge in
`~/.local/state/prtop/` (`%LOCALAPPDATA%\prtop\` on Windows): `<gitlab host>.json`
and `github.com.json`, under `open_mrs` or `deployments`, keyed by the full path
and number, `acme/web!7` where the PR column shows `web!7`. Pass `--hostname` on every call, so the working directory's git
remote cannot pick another host.

On GitLab, an entry carries the `project_path` (encode its slashes as `%2F`) and
`iid`, and a merged one the `prod_job_id` to play:

```bash
glab api --hostname <host> --method PUT "projects/<encoded path>/merge_requests/<iid>/merge"
glab api --hostname <host> --method PUT "projects/<encoded path>/merge_requests/<iid>/rebase"
glab api --hostname <host> --method POST "projects/<encoded path>/jobs/<job id>/play"
```

The merge request's `head_pipeline.id` leads to the pipeline that must pass:

```bash
glab api --hostname <host> "projects/<encoded path>/merge_requests/<iid>"
glab api --hostname <host> "projects/<encoded path>/pipelines/<pipeline id>/jobs?scope[]=failed"
glab api --hostname <host> "projects/<encoded path>/jobs/<job id>/trace"
glab api --hostname <host> --method POST "projects/<encoded path>/jobs/<job id>/retry"
glab api --hostname <host> --method POST "projects/<encoded path>/pipelines/<pipeline id>/retry"
glab api --hostname <host> --method POST "projects/<encoded path>/merge_requests/<iid>/pipelines"
```

On GitHub, an entry carries the `repo` (`owner/name`) and `number`, and a merged
one the `prod_run_id` whose deployment waits for approval:

```bash
gh api --hostname github.com --method PUT "repos/<repo>/pulls/<number>/merge"
gh api --hostname github.com --method PUT "repos/<repo>/pulls/<number>/update-branch"
gh api --hostname github.com "repos/<repo>/actions/runs/<run id>/pending_deployments"
gh api --hostname github.com --method POST "repos/<repo>/actions/runs/<run id>/pending_deployments" \
  -F "environment_ids[]=<id>" -f state=approved -f comment="<why>"
```

The checks link to their runs, whose failed jobs carry the logs. `--repo
github.com/<repo>` pins the host as `--hostname` does:

```bash
gh pr checks <number> --repo github.com/<repo>
gh run view <run id> --repo github.com/<repo> --log-failed
gh run rerun <run id> --repo github.com/<repo> --failed
```
