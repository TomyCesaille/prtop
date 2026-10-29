# Changelog

prtop's user-facing changes, newest first. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Several prtops running at once share their fetches: the live view, one-shot
  tables and `prtop watch`. One about to fetch less than 10 seconds after another
  fetched, with the same settings, shows that fetch instead of calling GitLab or
  GitHub again. Each fetch saves its table to `report.json` in the state
  directory.

### Changed

- On macOS and Linux, the live view keeps the mouse the way htop does: the wheel
  does not scroll the terminal's history behind the table. Selecting text takes
  Option in iTerm2, Shift in most other terminals.

### Fixed

- A merge request whose pipeline failed, was canceled or never ran reads as on
  you (`!`): `CI failed`, `CI canceled` or `no pipeline`, where it read as
  `CI must pass`, on a machine. On GitHub, failing required checks read as
  `CI failed`.

## [0.2.0] - 2026-10-26

### Added

- GitHub: the open pull requests you opened or are assigned to on github.com,
  read through `gh`, alone (`PRTOP_FORGES=github`) or in one table with a GitLab
  instance's merge requests (`PRTOP_FORGES=gitlab,github`). They read like merge
  requests, `repo!number` linked to the pull request. `PRTOP_GITHUB_OWNER` limits
  them to one organization or user.
- Merged pull requests followed to production through the GitHub Actions runs of
  their merge commit, with the same deploy job patterns. A job waiting on a
  protected environment's reviewers reads as awaiting prod trigger, linked to its
  run.
- The live view's help names prtop's version.

### Changed

- `PRTOP_STATE_DIR` replaces `PRTOP_STATE_FILE`: each forge keeps its
  `<host>.json` there. The default location does not move.
- The state file keys a merge request by its full path, `acme/web!7`, where the
  PR column shows `web!7`.
- The Chocolatey package installs `gh` alongside `glab`.

## [0.1.0] - 2026-10-23

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

[Unreleased]: https://github.com/TomyCesaille/prtop/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/TomyCesaille/prtop/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/TomyCesaille/prtop/releases/tag/v0.1.0
