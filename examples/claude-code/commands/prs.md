---
description: Check the merge requests assigned to me with prtop, and act on what is on me
---

Run `prtop` once with Bash and read its output: one table of the merge requests
assigned to me, the open ones first and the merged ones on their way to production
below. Run it once; don't loop or poll.

The first column says whose move it is. Act on it:

| Mark | Status | What to do |
|---|---|---|
| `!` | `mergeable, approved` | Ask me whether to merge it. Merge only on a yes. |
| `!` | `rebase needed, …` | Offer to rebase it through the API. |
| `!` | `conflicts, …` | Tell me, and offer to resolve them in a local checkout. |
| `!` | `resolve own threads, approved` | Only threads I opened block the merge. Ask whether to resolve them and merge. |
| `!` | `instance unreachable` | GitLab did not answer, so the open merge requests are unaccounted for. Relay the error from stderr; if it happens again, suggest `glab auth status`. |
| `?` | `changes requested, …` or `unresolved threads, …` | Sum up what the reviewers are waiting on, and offer to address it. |
| `?` | a label prtop does not recognise | Report the raw label. |
| `~` or `.` | anything | Context only: another person, or a machine, holds the next move. |

The merged rows at the bottom report deployment progress. Relay them when I ask,
and trigger a production job only when I ask.

When you relay a state, name it in words, never by its emoji alone. If every row is
`~` or `.`, say so in one line instead of pasting the table.

## Acting

prtop only reads. To act, call `glab api` on the instance prtop reads
(`PRTOP_GITLAB_HOST`), with `--hostname` on every call so the working directory's
git remote cannot pick another instance:

```bash
glab api --hostname <host> --method PUT "projects/<encoded path>/merge_requests/<iid>/merge"
glab api --hostname <host> --method PUT "projects/<encoded path>/merge_requests/<iid>/rebase"
glab api --hostname <host> --method POST "projects/<encoded path>/jobs/<job id>/play"
```

A row's project path and number are in prtop's state file,
`~/.local/state/prtop/<host>.json` (`%LOCALAPPDATA%\prtop\<host>.json` on Windows),
under `open_mrs` or `deployments`, keyed by the PR column's `repo!iid`. A merged
row also carries the `prod_job_id` to play. Encode the path's slashes as `%2F`.
