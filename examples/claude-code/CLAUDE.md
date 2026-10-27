## Watching a merge request

Once a merge request for the task at hand is open, run `/prs` once and handle what
it reports, then start `prtop watch` as a background task. It checks every 5
minutes and exits as soon as the table changes, which wakes you. When it exits:

- with a new table: look at the rows of this task's merge requests, tell me what
  moved, act as `/prs` says, and start `prtop watch` again;
- with `TIMEOUT after …`: start it again without a word;
- with `[watch] check failed`, or an `instance unreachable` row: GitLab or GitHub
  did not answer. Check the merge request yourself, then start the watch again.

The table lists every merge request and pull request of mine. Say nothing about
the others unless I ask. Keep watching until this task's merge requests are merged and in
production, or until I say stop.
