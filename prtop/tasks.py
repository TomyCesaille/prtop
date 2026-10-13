"""The task each merge request serves, read from its source branch."""

from __future__ import annotations

import re

# A branch named after a Notion page: `<prefix>/<ticket>-<title>-<32-hex page id>`.
# One task split across a stack of MRs carries a per-MR suffix after the page id
# (`…-mr1-theme-tokens`); the suffix stays out of the name so those MRs share one task.
NOTION_BRANCH_RE = re.compile(r"(?P<task>[^/]+?)-(?P<id>[0-9a-f]{32})(?:-[^/]*)?$")


def extract_task_name(branch: str | None) -> str:
    m = NOTION_BRANCH_RE.search(branch or "")
    return m.group("task").replace("-", " ") if m else ""
