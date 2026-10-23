# Set up prtop with Claude Code

Instructions for Claude Code, to set prtop up for the person who asked. Go one
step at a time, say what you are doing, and ask before anything that needs them.
Every GitLab and GitHub call here reads; none writes.

## 1. Install prtop

- macOS or Linux: `brew install TomyCesaille/tap/prtop`
- Windows: `choco install prtop`
- Elsewhere, with Python 3.14 or later:
  `uv tool install git+https://github.com/TomyCesaille/prtop`, and glab or gh on
  its own.

Check it with `prtop --version`.

## 2. Sign in

Ask whether their merge requests live on GitLab, their pull requests on
github.com, or both. prtop reads GitLab through glab and GitHub through gh, as
the person.

- GitLab: run `glab auth status`. If their instance is missing, ask them to run
  `! glab auth login --hostname <host>` themselves: signing in opens their
  browser.
- GitHub: run `gh auth status`. If they are not signed in to github.com, ask them
  to run `! gh auth login` themselves. prtop reads github.com only, not GitHub
  Enterprise Server.

## 3. Point prtop at their forges

Write what they use to prtop's env file: `~/.config/prtop/.env`, or
`%APPDATA%\prtop\.env` on Windows. `PRTOP_FORGES` is `gitlab` unless set; for
GitHub alone it is `github`, for both `gitlab,github`. Unless they use gitlab.com,
ask for their GitLab host too.

```bash
PRTOP_FORGES=gitlab,github
PRTOP_GITLAB_HOST=gitlab.example.com
```

## 4. First run

Run `prtop` and show them the table. Explain the marks: `!` on them, `?` to look
at, `~` waiting on someone else, `.` running. Tell them that `prtop` in their own
terminal is the live view, refreshing every 30 seconds.

## 5. Fit prtop to their GitLab (optional)

Offer each of these. For each one, show what you found and the setting you
propose, and write it to the env file only after their yes. Pass
`--hostname <host>` on every `glab api` call, and `--hostname github.com` on every
`gh api` call. The README's Configuration section lists every setting.

- **The Task column.** List the branches of their open merge requests:
  `glab api "merge_requests?scope=assigned_to_me&state=opened&per_page=100"`, field
  `source_branch`; on GitHub, `gh search prs --author @me --state open --json url`,
  then `gh pr view <url> --json headRefName` for each. If the branches follow a
  convention, propose the matching setting:
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
- **Only some of GitHub.** If their pull requests span organizations and they
  want one, propose `PRTOP_GITHUB_OWNER=<organization or user>`.
- **Production deploy jobs.** prtop follows a merge to production by the name of
  the job that deploys it. Find the names in a project of theirs. On GitLab: the
  jobs that deployed to its production environment
  (`projects/<path>/environments`, then
  `projects/<path>/deployments?environment=<name>&per_page=20`, field
  `deployable.name`), or the jobs of a recent pipeline on the target branch
  (`projects/<path>/pipelines?ref=<branch>&per_page=5`, then
  `projects/<path>/pipelines/<id>/jobs?per_page=100`). Project paths are
  URL-encoded (`/` as `%2F`). On GitHub: the jobs of a recent GitHub Actions run
  on the default branch (`repos/<owner>/<repo>/actions/runs?branch=<branch>&per_page=5`,
  then `repos/<owner>/<repo>/actions/runs/<id>/jobs`, field `name`). The defaults
  are
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
as `~/.claude/commands/prs.md`. `/prs` then checks their merge requests and pull
requests, and offers to act on what is on them.

## 7. Watching in the background

Show them
https://raw.githubusercontent.com/TomyCesaille/prtop/main/examples/claude-code/CLAUDE.md,
and with their yes, append it to `~/.claude/CLAUDE.md`. From then on, once one of
their merge requests or pull requests is open, you start `prtop watch` in the
background and hear from it only when something moves.

## Done

Sum up what was installed and set, and where, and suggest they try `/prs`.
