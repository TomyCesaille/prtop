"""The vocabulary a forge hands to the report.

A forge maps its own merge-request and pipeline states onto these, so the report
never learns which service it is reading.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import NamedTuple


class ForgeError(RuntimeError):
    """The forge did not answer, or answered something unreadable."""


def mr_key(project_path: str, number: int) -> str:
    """A merge request's key in the state file, `acme/web!7`: the same merge
    request from one run to the next, whatever else is open beside it."""
    return f"{project_path}!{number}"


def short_ids(keys: Iterable[str]) -> dict[str, str]:
    """The `repo!number` the PR column shows for each `mr_key`, the same on every
    forge: the cell links to the merge request, which says where it lives. Where
    two repositories of the same name would collide, both show their full path,
    `acme/web!7` beside `labs/web!7`."""

    def short(key: str) -> str:
        path, _, number = key.rpartition("!")
        return f"{path.rsplit('/', 1)[-1]}!{number}"

    keys = set(keys)
    taken = Counter(map(short, keys))
    return {key: short(key) if taken[short(key)] == 1 else key for key in keys}


# Review states of an open merge request. A state the forge cannot map passes
# through under its own raw name, and the report surfaces it as unrecognised.
DRAFT = "draft"
AWAITING_REVIEW = "awaiting_review"
CHANGES_REQUESTED = "changes_requested"
UNRESOLVED_THREADS = "unresolved_threads"
REBASE_NEEDED = "rebase_needed"
CONFLICTS = "conflicts"
CI_RUNNING = "ci_running"
CI_MUST_PASS = "ci_must_pass"
MERGE_CHECK_RUNNING = "merge_check_running"
MERGE_CHECK_PENDING = "merge_check_pending"
BLOCKED = "blocked"
MERGEABLE = "mergeable"


# Where a merged merge request stands on its way to production.
SHIPPED = "shipped"
PIPELINE_RUNNING = "pipeline_running"
PIPELINE_FAILED = "pipeline_failed"
NO_PROD_JOB = "no_prod_job"
PROD_FAILED = "prod_failed"
PROD_DEPLOYING = "prod_deploying"
AWAITING_TRIGGER = "awaiting_trigger"
PROD_OTHER = "prod_other"
CHECK_FAILED = "check_failed"

# The kinds that end the tracking: the MR is in production, or its pipeline
# finished without a production job to put it there.
FINISHED = (SHIPPED, NO_PROD_JOB)


class DeployState(NamedTuple):
    kind: str
    # The pipeline or job status behind the kind, or the error of a failed check.
    detail: str = ""
    # Where to act on it: the page a manual production job is triggered from.
    url: str | None = None
