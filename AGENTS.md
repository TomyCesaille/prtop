# AGENTS.md

Notes for an AI agent changing prtop. [README.md](README.md) says what prtop does;
this file says how the code is laid out and the rules it holds to.

## Layout

Standard library only, Python 3.14 or later. `glab` and `gh` are the outside
tools, one per forge.

| Module | Holds |
|---|---|
| `prtop/cli.py` | The command: one fetch (`run_check`, one `check_forge` per forge), the choice between the live view and one table, `watch`, the arguments. |
| `prtop/live.py` | The live view: the frame, the fetch loop, the keys. Pure enough to test with a fake terminal and clock. |
| `prtop/terminal.py` | The real terminal: alternate screen, key reading on POSIX (`termios`) and Windows (`msvcrt`), key decoding. |
| `prtop/report.py` | Marks, Status cells, row order and the table. The accessibility contract lives here. |
| `prtop/model.py` | The vocabulary a forge hands to the report: review states, deploy states, the state-file key and the `repo!number` the PR column shows, `ForgeError`. |
| `prtop/forges/` | One module per forge behind the `Forge` protocol: `gitlab.py` and `github.py` each own every call and field name of their service. `command.py` runs `glab` and `gh`; `pipelines.py` holds what both map production tracking onto: job statuses, the production jobs, the frontier. |
| `prtop/tasks.py` | Task sources: what fills the Task column. |
| `prtop/config.py` | `PRTOP_*` settings, the env file, where files live on each platform. |
| `prtop/state.py` | The state file, written atomically. |
| `prtop/demo.py` | The made-up report behind `--demo`, and the story its live view plays, one event per fetch. |
| `docs/` | The website, an Astro site. `docs/public/prtop.gif`, shown on the site and in the README, is `prtop --demo -d 4` recorded in a 141×19 terminal. `docs/infographic/how_to_read_prtop.py` draws `docs/public/how-to-read-prtop.png` from the demo's live view. |

## Rules to hold

- **Read-only against the forges.** `GitLab.api` and `GitHub.api` issue GETs,
  `GitHub.query` sends GraphQL queries, and nothing else reaches a forge.
  Merging, rebasing and triggering jobs belong to whoever reads the report.
- **Every forge reads the same in the table.** A pull request is `repo!number`
  like a merge request, with the same states and marks; the link on the cell says
  where it lives. Each forge keeps its own state file, `<host>.json`, and the live
  view and `prtop watch` tell two forges' identical ids apart by that link. Two repositories of
  the same name on one forge show their full paths, `acme/web!7` beside
  `labs/web!7`.
- **Output that is not a terminal is one table and nothing else.** Agents parse it.
  The live view's status line, key bar and highlighting never reach it, and errors
  go to stderr.
- **A forge that does not answer degrades the report, never fails it.** One
  `! 🔌 instance unreachable` row stands in for that forge's open merge requests,
  beside the other forge's rows, marked `!` because a green tab would claim
  all-clear over merge requests nobody saw. The
  previous open entries stay in the state file, not in the table: they are the
  baseline that tells the next fetch what merged, so an outage never reads as a
  wave of merges. A merge check that fails keeps its entry in that baseline and is
  asked about again on the next fetch.
- **Production is judged against a frontier.** Per project and branch, the
  frontier is the newest pipeline whose primary production job succeeded. A
  merged merge request at or below it is in production, even when its own job
  never ran: a branch's history is linear, so a later deploy carries the earlier
  commits. GitLab compares pipeline ids; GitHub asks whether the deployed commit
  contains the merge commit, since a workflow run can start after a later
  commit's runs, and picks the frontier among the commits of the branch's 20
  latest runs the same way. A pipeline that finished with no production job ends the
  tracking too (`model.FINISHED`).
- **`action_needed` counts open merge requests only.** Deploy rows carry `!` marks
  of their own but never turn the tab orange.
- **Your own unresolved threads are hints, not feedback.** A thread only you wrote
  in reads as `awaiting review`, or as `resolve own threads` once the merge request
  is approved. One note from anyone else makes it `unresolved threads`.
- **Conflicts outrank the review state.** GitLab reports `has_conflicts` apart from
  `detailed_merge_status`, which can still say `not_approved`; GitHub reports
  `mergeable: CONFLICTING` apart from a draft.

### The accessibility contract

The report is read by scanning, by people who cannot all rely on colour, emoji
fidelity or fine glyph detail. Each state is told four ways, weakest last:

1. **Mark**: one ASCII character, from heaviest to lightest shape: `!` on you now,
   `?` on you to look at, `~` on someone else, `.` on a machine.
2. **Position**: rows sort by mark, and a rule separates each mark's block.
3. **Label**: plain words that stand on their own (`conflicts, unapproved`).
4. **Glyph**: garnish. One code point that every terminal draws two cells wide;
   never the only difference between two states; never two meanings across the
   open and deploy halves of the Status column.

Adding a state means choosing its mark first and its glyph last. The
`AccessibilityContract` tests in `tests/test_report.py` hold these rules.

### The table

- Pad through `display_width`, never `len` or `str.ljust`: an emoji is one code
  point and two cells, and a hyperlink's escapes take no cell at all.
- The Status column is floored at `STATUS_WIDTH`, the widest cell the vocabulary
  produces, so one merge request changing state does not reflow every row and
  `prtop watch` reports the row that moved rather than a table that shifted. A
  longer label means raising the constant; a test enumerates the vocabulary.
- Links are OSC 8 hyperlinks ended by BEL, which `plain` strips for comparisons
  and widths.

## Packaging

- **Homebrew:** the formula lives in
  [TomyCesaille/homebrew-tap](https://github.com/TomyCesaille/homebrew-tap) and
  installs the tagged source archive into a virtualenv, with `glab` and Python
  3.14 as dependencies. `.github/workflows/homebrew.yml` opens the formula update
  there for a new tag, and once the tap serves the version, installs it from the
  tap and checks it, through the `homebrew` environment.
- **Chocolatey:** `packaging/chocolatey/build.py` assembles the package around the
  Windows embeddable Python, with `prtop/` beside `python.exe`; one shim, `prtop`,
  runs `python -m prtop`, and `.ignore` files keep the bundled Python off PATH.
  `.github/workflows/chocolatey.yml` builds, installs, runs and uninstalls it on
  every change, and publishes it from a tag through the `chocolatey` environment.

## GitLab

- `--hostname` goes on every `glab api` call; without it `glab` picks the host
  from the working directory's git remote.
- Project paths are URL-encoded in API paths (`urllib.parse.quote(path, safe="")`).
- `detailed_merge_status` names one blocker at a time, so the approval state is
  read from `/approvals`. It reports `not_approved` ahead of `need_rebase`, so a
  merge request in review that fell behind its target is found through
  `diverged_commits_count`.
- `/discussions` paginates; a bot-reviewed merge request runs past one page.
- Right after a merge, the branch pipeline may not exist yet: the merge commit's
  pipeline is looked for a few times before the latest one stands in.

## GitHub

- github.com only. `--hostname github.com` goes on every `gh` call, so neither
  `GH_HOST` nor the working directory's remote picks another host.
- The listing is two steps. One search query returns the ids of the open pull
  requests you opened and of those assigned to you; their state is then read in
  batches of 20 through `nodes(ids:)`, in parallel. GitHub works out each one's
  `mergeStateStatus` as it answers, and a query that runs past ten seconds fails
  whole with an HTTP 502, which a single query over 40-odd pull requests does.
- Review threads come with `isResolved` only. Who wrote in them is asked for one
  pull request at a time, and only when GitHub reports `BLOCKED` and a thread is
  unresolved: the listing stays at a few rate-limit points a fetch.
- `mergeStateStatus` is the same whoever asks. `viewerPermission` decides whether
  a mergeable pull request is yours to merge or its maintainers'; an empty
  `reviewDecision` (no review required) counts as approved only for who can merge.
- `gh api graphql` exits 1 whenever the response carries errors, data included.
  `GitHub.query` keeps the data and reports the errors on stderr: an organization
  whose single sign-on the token lacks hides its pull requests without hiding the
  others.
- A merged pull request is followed through the GitHub Actions runs of its merge
  commit on the base branch, read as one pipeline: their jobs are judged
  together, for the frontier's candidate commits too, since a primary production
  job and its auxiliaries can run in different workflows. Every page of the runs
  and of their jobs is read: a scheduled workflow runs on the default branch's
  latest commit again and again, burying the runs its push started.
  Run and job states are translated into `pipelines.py`'s words; `waiting`, a
  job held by a protected environment's reviewers, is a manual job, and the
  page of the run holding it is where it is approved. A finished run's jobs and a commit
  comparison never change, so the forge keeps them for the life of the process.
- A deploy started by `workflow_run` is created only once the run it waits on is
  over, so runs without a production job end the tracking only five minutes
  (`DOWNSTREAM_GRACE`) after the last of them finished.

## State file

One per forge, `<host>.json`, keyed by the merge request's full path and number,
`acme/web!1931`: the same key from one run to the next, whatever else is open,
where the PR column shows `web!1931`.
GitLab's entries are below; GitHub's carry `repo` (`owner/name`) and `number`
where GitLab's carry `project_path` and `iid`, and a deployment carries `ref`,
`sha` and `prod_run_id`.

```jsonc
{
  "open_mrs": {
    "acme/web!1931": { "title", "task", "task_url", "status", "fully_approved",
                       "has_conflicts", "threads_await_user", "approvals", "note_count",
                       "project_path", "iid", "web_url", "last_checked" }
  },
  "deployments": {
    "acme/api!2544": { "title", "task", "task_url", "project_path", "iid", "web_url",
                       "ref", "pipeline_id", "pipeline_status", "pipeline_url",
                       "prod_job_id" }
  },
  "last_checked": "<ISO 8601>"
}
```

## Testing

```bash
python -m unittest
python -m coverage run -m unittest && python -m coverage report
ruff check
```

The tests need neither network, `glab` nor `gh`: forges answer from memory, the
live view runs against a fake terminal and clock, and each platform's terminal
against stand-ins for `termios`, `msvcrt` and the Windows console, so one run
covers both. Two tests run `prtop --demo` in a pseudo-terminal on POSIX. CI
(`.github/workflows/ci.yml`) runs them on Linux, macOS and Windows with Python 3.14
and 3.15, fails under 100% line and branch coverage on any of them, installs the
package and runs the command it installs, and lints with a pinned ruff.

Never point a development run at your real state file: a run moves the
merge-detection baseline, so a merge that happened since the last real run would
be consumed by the test. Use a scratch copy:

```bash
PRTOP_STATE_DIR=/tmp/prtop-scratch prtop
```

## Conventions

- Test names state behaviour in the user's words:
  `test_a_deployment_that_shipped_leaves_the_report`.
- Comments describe the code as it is, with no reference to what it used to be.
- Commit titles follow Conventional Commits (`feat:`, `fix:`, `refactor:`…).
