"""Where the Task column comes from.

A task source turns a merge request into the task it serves: a label for the
column and, when the source knows one, a link to the task. With no source
configured the column is left out of the table altogether.

- `none`: no Task column.
- `issues`: the issues the MR closes (`Closes #123` in its description), as the
  forge tracks them.
- `notion`: a branch named after a Notion page,
  `<prefix>/<ticket>-<title>-<32-hex page id>`, linked to the page.
- `jira`: a branch carrying a Jira key, `<prefix>/PROJ-123-<title>`, linked when
  `PRTOP_TASK_URL` gives the browse URL.
- `branch`: any branch convention, described by `PRTOP_TASK_BRANCH_PATTERN`.
"""

from __future__ import annotations

import re
import string
from collections.abc import Callable
from typing import NamedTuple, Protocol

from prtop.config import Config, ConfigError


class Task(NamedTuple):
    label: str
    url: str | None = None


class Issue(NamedTuple):
    """An issue a merge request closes, as the forge reports it."""

    ref: str  # how the forge names it, e.g. `#123`
    title: str
    url: str | None = None


ClosingIssues = Callable[[], list[Issue]]


class TaskSource(Protocol):
    shows_column: bool

    def resolve(self, branch: str | None, closing_issues: ClosingIssues) -> Task | None:
        """The task behind an MR. `closing_issues` asks the forge, so a source
        that does not need it costs no API call."""
        ...


class NoTasks:
    shows_column = False

    def resolve(self, branch: str | None, closing_issues: ClosingIssues) -> Task | None:
        return None


class IssueTasks:
    shows_column = True

    def resolve(self, branch: str | None, closing_issues: ClosingIssues) -> Task | None:
        issues = closing_issues()
        if not issues:
            return None
        first = issues[0]
        more = f" (+{len(issues) - 1})" if len(issues) > 1 else ""
        return Task(f"{first.ref} {first.title}{more}", first.url)


class BranchTasks:
    """Reads the task out of the source branch name.

    The pattern is searched rather than matched, so it may skip a `user/` or
    `feature/` prefix. Its named groups build the cell: the `task` group is the
    label, dashes read as spaces; without one, the label is the `id` group
    followed by the `title` group, dashes and underscores read as spaces. Every
    group is available to the URL template, as `{id}` for instance.
    """

    shows_column = True

    def __init__(self, pattern: str, url_template: str | None = None):
        self.pattern = re.compile(pattern)
        self.url_template = url_template
        if url_template:
            fields = {name for _, name, _, _ in string.Formatter().parse(url_template) if name}
            if missing := fields - set(self.pattern.groupindex):
                raise ConfigError(
                    f"PRTOP_TASK_URL uses {', '.join(sorted(missing))}, "
                    "which the branch pattern does not capture"
                )

    def resolve(self, branch: str | None, closing_issues: ClosingIssues) -> Task | None:
        m = self.pattern.search(branch or "")
        if not m:
            return None
        groups = {name: value or "" for name, value in m.groupdict().items()}
        if groups.get("task"):
            label = groups["task"].replace("-", " ")
        else:
            title = re.sub(r"[-_]+", " ", groups.get("title", "")).strip()
            label = " ".join(part for part in (groups.get("id") or m.group(0), title) if part)
        url = self.url_template.format_map(groups) if self.url_template else None
        return Task(label, url)


# One task split across a stack of MRs carries a per-MR suffix after the page id
# (`…-mr1-theme-tokens`); the suffix stays out of the label so those MRs share one task.
NOTION_PATTERN = r"(?P<task>[^/]+?)-(?P<id>[0-9a-f]{32})(?:-[^/]*)?$"
NOTION_URL = "https://www.notion.so/{id}"
JIRA_PATTERN = r"(?P<id>[A-Z][A-Z0-9]+-\d+)(?:[-_](?P<title>[^/]+))?$"

_PRESETS = {
    "notion": (NOTION_PATTERN, NOTION_URL),
    "jira": (JIRA_PATTERN, None),
}


def task_source(config: Config) -> TaskSource:
    if config.task_source == "none":
        return NoTasks()
    if config.task_source == "issues":
        return IssueTasks()
    pattern, url = _PRESETS.get(config.task_source, (None, None))
    pattern = config.task_branch_pattern or pattern
    if pattern is None:
        raise ConfigError("PRTOP_TASK_SOURCE=branch needs PRTOP_TASK_BRANCH_PATTERN")
    return BranchTasks(pattern, config.task_url or url)
