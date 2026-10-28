<img src="docs/public/favicon.svg" alt="" width="64" height="64">

# prtop

htop for your merge requests. Your GitLab MRs and GitHub PRs, from review to
production, live in your terminal. Built to wake Claude Code.

[![Python](https://img.shields.io/badge/Python-3.14%2B-3776AB?logo=python&logoColor=fff)](#install-manually)
[![license](https://img.shields.io/github/license/TomyCesaille/prtop)](https://github.com/TomyCesaille/prtop/blob/main/LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/TomyCesaille/prtop/ci.yml?branch=main)](https://github.com/TomyCesaille/prtop/actions/workflows/ci.yml)

![prtop in a terminal: a countdown to the next fetch, then the merge requests that changed, highlighted](docs/public/prtop.gif)

[Website](https://tomycesaille.github.io/prtop/) | [Claude Code setup](examples/claude-code)

## Install with Claude Code, for Claude Code

Paste this into Claude Code:

```text
Set up prtop for me: follow https://raw.githubusercontent.com/TomyCesaille/prtop/main/examples/claude-code/SETUP.md
```

Claude installs prtop for you and for itself. It checks your GitLab or GitHub
sign-in and fits the settings to your projects, then sets itself up to use prtop:
a `/prs` command, and watching your merge requests in the background while it
works. It asks you at each step.

## Install manually

```bash
brew install TomyCesaille/tap/prtop    # macOS, Linux
choco install prtop                    # Windows
```

Both install `glab` and `gh` alongside prtop. Elsewhere, with Python 3.14 or
later: `uv tool install git+https://github.com/TomyCesaille/prtop`, plus `glab`,
`gh` or both.

prtop reads GitLab through `glab` and GitHub through `gh`, signed in as you. Sign
in to the ones you use:

```bash
glab auth login --hostname gitlab.example.com
gh auth login
```

Then tell prtop what to read, in `~/.config/prtop/.env` (`%APPDATA%\prtop\.env`
on Windows):

```bash
# gitlab (the default), github, or gitlab,github for both
PRTOP_FORGES=gitlab,github
PRTOP_GITLAB_HOST=gitlab.example.com
```

On Windows, run prtop in Windows Terminal, the default terminal on Windows 11:
the older console shows the emoji as boxes and cannot open links.

## Use

```bash
prtop
```

In a terminal, prtop takes the screen and fetches again every 30 seconds. The top
line names the forges it reads, the time of the last fetch and the countdown to
the next one; the bottom line lists the keys. On macOS and Linux it keeps the
mouse the way htop does: the wheel does not scroll the terminal's history, and
selecting text takes Option in iTerm2, Shift in most other terminals.

| Key | Does |
|---|---|
| `q`, F10, Ctrl+C | quit |
| `h`, `?`, F1 | show or hide the help: prtop's version, the keys and what the marks mean |
| Ctrl+L | fetch now |
| `Z` | pause or resume fetching |

| Option | Does |
|---|---|
| `-d SECONDS` | seconds between fetches (default 30) |
| `-n N` | stop after N fetches and leave the last table in the terminal; `prtop -n 1` prints one table |
| `--demo` | show made-up merge requests, without reading any forge |

When its output is not a terminal (a pipe, a file, a script, an AI agent), prtop
prints the table once and exits. Errors go to stderr, so the output stays one
table.

Several prtops running at once share their fetches: one about to fetch less than
10 seconds after another fetched, with the same settings, shows that fetch
instead of calling GitLab or GitHub again.

## How to read prtop

![prtop's live view, annotated. 1, the mark says whose move it is: ! on you (mergeable, rebase, conflicts, own threads, failed pipeline, prod trigger), ? to look at (changes requested, open threads, unknown status), ~ someone else (awaiting review, draft, blocked), . running (CI, merge check, deploying). 2, links open the merge request, its task and the prod trigger. 3, highlighted rows changed at the last fetch. 4, open merge requests, sorted by mark. 5, merged ones, until production runs them.](docs/public/how-to-read-prtop.png)

## Configuration

Settings are `PRTOP_*` variables, read from the environment first and from the env
file second. The env file holds `KEY=VALUE` lines; `#` starts a comment, and an
empty value counts as unset. [`.env.example`](.env.example) lists every setting.

| | macOS and Linux | Windows |
|---|---|---|
| Env file | `~/.config/prtop/.env` | `%APPDATA%\prtop\.env` |
| State, one file per forge | `~/.local/state/prtop/<host>.json` | `%LOCALAPPDATA%\prtop\<host>.json` |
| The latest fetch's table | `~/.local/state/prtop/report.json` | `%LOCALAPPDATA%\prtop\report.json` |

`XDG_CONFIG_HOME` and `XDG_STATE_HOME` replace the base directory on every
platform; `PRTOP_CONFIG_FILE` names the env file outright, and `PRTOP_STATE_DIR`
the state directory.

| Setting | Default | |
|---|---|---|
| `PRTOP_FORGES` | `gitlab` | What to read: `gitlab`, `github`, or `gitlab,github` for both in one table. |
| `PRTOP_GITLAB_HOST` | `gitlab.com` | The GitLab instance to read. |
| `PRTOP_GITLAB_GROUP` | the whole instance | Only list merge requests in this group and its subgroups. |
| `PRTOP_GITLAB_USERNAME` | read from the instance | Your username, to tell your own threads from a reviewer's. |
| `PRTOP_GITHUB_OWNER` | all of github.com | Only list pull requests in this organization's or user's repositories. |
| `PRTOP_TASK_SOURCE` | `none` | Where the Task column comes from: `none`, `issues`, `notion`, `jira` or `branch`. |
| `PRTOP_TASK_BRANCH_PATTERN` | | The branch convention for `PRTOP_TASK_SOURCE=branch`. |
| `PRTOP_TASK_URL` | | The task link, built from the pattern's groups. |
| `PRTOP_DEPLOY_TRACKING` | `on` | Follow merged merge requests and pull requests to production. |
| `PRTOP_DEPLOY_JOB_PATTERN` | `deploy.*prod\|prod.*deploy` | Jobs that deploy to production, in GitLab CI or GitHub Actions. |
| `PRTOP_PRIMARY_DEPLOY_JOB_PATTERN` | `^deploy[_-]production$` | The one among them that ships the app. |
| `PRTOP_ITERM_TAB_COLOR` | `off` | In iTerm2, the live view colours the tab orange while an open merge request is on you, green otherwise. |
| `PRTOP_CONFIG_FILE` | see above | The env file. |
| `PRTOP_STATE_DIR` | see above | The state directory. |

A setting prtop does not know is reported on stderr and ignored, so a typo shows
up rather than silently keeping a default.

### The Task column

With `PRTOP_TASK_SOURCE=none` the column is left out and the titles take its width.

- `issues`: the issues the merge request or pull request closes (`Closes #123` in
  its description).
- `notion`: a branch named after a Notion page,
  `<prefix>/<ticket>-<title>-<32-hex page id>`, linked to the page.
- `jira`: a branch carrying a Jira key, `<prefix>/PROJ-123-<title>`. Set
  `PRTOP_TASK_URL=https://jira.example.com/browse/{id}` to link it.
- `branch`: any branch convention, as a regular expression searched in the branch
  name. A `task` group is the label; without one, the label is the `id` group
  followed by the `title` group. Every group is available to `PRTOP_TASK_URL`:

  ```bash
  PRTOP_TASK_SOURCE=branch
  PRTOP_TASK_BRANCH_PATTERN=(?P<id>\d+)-(?P<title>[^/]+)$
  PRTOP_TASK_URL=https://tracker.example.com/tasks/{id}
  ```

## GitHub

prtop reads github.com; GitHub Enterprise Server is not supported. It lists the
open pull requests you opened and the ones assigned to you, in the table a GitLab
merge request gets: the PR column reads `repo!number` and links to the pull
request.

- The state comes from GitHub's merge state, review decision and checks.
  Unresolved threads count only while GitHub blocks the merge, as on a GitLab
  project that requires them resolved. A pull request in a repository you cannot
  merge in waits on its maintainers, so it never reads as mergeable by you.
- A merged pull request is followed through the GitHub Actions runs of its merge
  commit, and the production job is found by the same patterns. A job waiting on
  a protected environment's reviewers reads as `awaiting prod trigger`, linked to
  the run where it is approved; a run held on another environment's reviewers
  reads as `pipeline waiting`.

## With an AI agent

An agent reads the one-table output: `prtop` run by an agent prints the table and
exits, and the mark column is the state to act on. prtop never writes to GitLab or
GitHub, so merging, rebasing or triggering a deploy stays with the agent, behind
your confirmation.

`prtop watch` waits for something to happen, for an agent to run in the
background and be woken by:

- it checks every 5 minutes (`-d SECONDS`) and prints a `[watch] <time> no change`
  line after each quiet check;
- it exits 0 with the new table as soon as the table changes;
- it exits 0 with a `TIMEOUT after <cap>s with no PR activity` line after
  `--cap SECONDS` of quiet (2 hours by default);
- it exits 1 with a `[watch] check failed` line when a check fails.

[`examples/claude-code`](examples/claude-code) sets this up for Claude Code: a
`/prs` command and the instructions for watching a merge request in the
background.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), and [AGENTS.md](AGENTS.md) for the rules
the code holds to.

## License

[MIT](LICENSE)
