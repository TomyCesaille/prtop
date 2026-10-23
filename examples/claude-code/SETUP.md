# Set up prtop with Claude Code

Instructions for Claude Code, to set prtop up for the person who asked. Go one
step at a time, say what you are doing, and ask before anything that needs them.
Every GitLab call here reads; none writes.

## 1. Install prtop

- macOS or Linux: `brew install TomyCesaille/tap/prtop`
- Windows: `choco install prtop`
- Elsewhere, with Python 3.14 or later:
  `uv tool install git+https://github.com/TomyCesaille/prtop`, and glab on its own.

Check it with `prtop --version`.

## 2. Sign in to GitLab

prtop reads GitLab as the person, through glab. Run `glab auth status`. If their
instance is missing, ask them to run `! glab auth login --hostname <host>`
themselves: signing in opens their browser.

## 3. Point prtop at their instance

Unless they use gitlab.com, ask for their GitLab host and write it to prtop's env
file: `~/.config/prtop/.env`, or `%APPDATA%\prtop\.env` on Windows.

```bash
PRTOP_GITLAB_HOST=gitlab.example.com
```

## 4. First run

Run `prtop` and show them the table. Explain the marks: `!` on them, `?` to look
at, `~` waiting on someone else, `.` running. Tell them that `prtop` in their own
terminal is the live view, refreshing every 30 seconds.

## 5. Fit prtop to their GitLab (optional)

Offer each of these. For each one, show what you found and the setting you
propose, and write it to the env file only after their yes. Pass
`--hostname <host>` on every `glab api` call. The README's Configuration section
lists every setting.

- **The Task column.** List the branches of their open merge requests:
  `glab api "merge_requests?scope=assigned_to_me&state=opened&per_page=100"`, field
  `source_branch`. If the branches follow a convention, propose the matching
  setting:
  - a Jira key (`PROJ-123-title`): `PRTOP_TASK_SOURCE=jira`, with
    `PRTOP_TASK_URL=https://<their jira>/browse/{id}`;
  - a Notion page (`…-<32 hex characters>`): `PRTOP_TASK_SOURCE=notion`;
  - anything else: `PRTOP_TASK_SOURCE=branch` and a `PRTOP_TASK_BRANCH_PATTERN`.
    It is a regular expression searched in the branch name; a `task` group is
    the label, or else the `id` group followed by the `title` group, and every
    group is available to `PRTOP_TASK_URL`, as `{id}` for instance.

  Test a pattern against their branch names with a few lines of Python before
  proposing it. Closing issues (`Closes #123`) instead of a branch convention:
  `PRTOP_TASK_SOURCE=issues`.
- **Production deploy jobs.** prtop follows a merge to production by the name of
  the job that deploys it. Find the names in a project of theirs: the jobs that
  deployed to its production environment
  (`projects/<path>/environments`, then
  `projects/<path>/deployments?environment=<name>&per_page=20`, field
  `deployable.name`), or the jobs of a recent pipeline on the target branch
  (`projects/<path>/pipelines?ref=<branch>&per_page=5`, then
  `projects/<path>/pipelines/<id>/jobs?per_page=100`). Project paths are
  URL-encoded (`/` as `%2F`). The defaults are
  `PRTOP_DEPLOY_JOB_PATTERN=deploy.*prod|prod.*deploy` for every job that deploys
  to production, and `PRTOP_PRIMARY_DEPLOY_JOB_PATTERN=^deploy[_-]production$` for
  the one whose success means the app itself shipped. Both are searched in the
  job name, without regard to case. If their names don't match, propose
  patterns that do, and check them against the names you found. If nothing of
  theirs deploys to production, propose `PRTOP_DEPLOY_TRACKING=off`.
- **The tab colour.** If they use iTerm2 (`TERM_PROGRAM=iTerm.app`), offer
  `PRTOP_ITERM_TAB_COLOR=on`: the live view turns the tab orange while an open
  merge request is on them.

Run `prtop` again after a change and show the result.

## 6. The /prs command

Save
https://raw.githubusercontent.com/TomyCesaille/prtop/main/examples/claude-code/commands/prs.md
as `~/.claude/commands/prs.md`. `/prs` then checks their merge requests and offers
to act on what is on them.

## 7. Watching in the background

Show them
https://raw.githubusercontent.com/TomyCesaille/prtop/main/examples/claude-code/CLAUDE.md,
and with their yes, append it to `~/.claude/CLAUDE.md`. From then on, once one of
their merge requests is open, you start `prtop watch` in the background and hear
from it only when something moves.

## Done

Sum up what was installed and set, and where, and suggest they try `/prs`.
