# Changelog

prtop's user-facing changes, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

The first release.

### Added

- One table of the GitLab merge requests assigned to you, each marked with whose
  move it is (`!` you now, `?` you to look at, `~` someone else, `.` a machine),
  its state in words, and links to the merge request and its task.
- A live view in a terminal: the table fills the screen and is fetched again every
  30 seconds (`-d`), with keys to quit, show the help, fetch now and pause. Rows
  that changed show in reverse video.
- One table when the output is not a terminal, for scripts and AI agents, and
  `prtop watch`, which exits as soon as the table changes.
- Merged merge requests followed to production through their pipeline and the
  production deploy job, found by name (`PRTOP_DEPLOY_JOB_PATTERN`,
  `PRTOP_PRIMARY_DEPLOY_JOB_PATTERN`).
- A Task column read from the issues a merge request closes, from Notion or Jira
  branch names, or from any branch convention.
- Settings from `PRTOP_*` variables and an env file, kept where macOS, Linux and
  Windows each expect them.
- `--demo`, the table for made-up merge requests.
- An iTerm2 tab colour, off unless `PRTOP_ITERM_TAB_COLOR` turns it on.
