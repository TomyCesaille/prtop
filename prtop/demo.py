"""`prtop --demo`: the report for a made-up set of merge requests, one per kind
of row, without a forge, a state file or a terminal tab to touch."""

from __future__ import annotations

from prtop import model
from prtop.model import DeployState
from prtop.report import ACT, Report, assemble_rows, by_severity, deploy_row, open_row

HOST = "gitlab.example.com"
TRACKER = "https://jira.example.com/browse"


def _mr(repo: str, iid: int, title: str, task_id: str = "", task: str = "", **fields) -> tuple[str, dict]:
    return f"{repo}!{iid}", {
        "title": title,
        "task": f"{task_id} {task}" if task_id else "",
        "task_url": f"{TRACKER}/{task_id}" if task_id else None,
        "web_url": f"https://{HOST}/acme/{repo}/-/merge_requests/{iid}",
        "status": model.AWAITING_REVIEW,
        "fully_approved": False,
        "has_conflicts": False,
        "threads_await_user": True,
        **fields,
    }


OPEN_MRS = dict([
    _mr("web", 1931, "feat: sign in with SSO from the login page", "PROJ-150", "Single sign-on",
        status=model.MERGEABLE, fully_approved=True),
    _mr("api", 2549, "chore(deps): align grpcio-status with protobuf 5", has_conflicts=True),
    _mr("api", 2551, "feat: rate-limit the public search endpoint", "PROJ-147", "Search rate limiting",
        status=model.CHANGES_REQUESTED),
    _mr("web", 1922, "Draft: feat: dark mode toggle in the settings page", "PROJ-142", "Dark mode",
        status=model.DRAFT),
    _mr("api", 2547, "fix(deps): bump idna for a security advisory"),
    _mr("worker", 812, "fix: retry webhook deliveries on a 502", "PROJ-138", "Webhook retries",
        status=model.CI_RUNNING, fully_approved=True),
])

DEPLOYMENTS = [
    (*_mr("api", 2544, "fix: right-to-left text in transactional emails", "PROJ-131", "Right-to-left emails"),
     DeployState(model.AWAITING_TRIGGER, url=f"https://{HOST}/acme/api/-/commits/main?ref_type=HEADS")),
    (*_mr("web", 1918, "feat: export the audit log as CSV", "PROJ-129", "Audit log export"),
     DeployState(model.PROD_DEPLOYING)),
]


def demo_report() -> Report:
    open_rows = by_severity([open_row(key, entry) for key, entry in OPEN_MRS.items()])
    deploy_rows = by_severity([deploy_row(key, entry, state) for key, entry, state in DEPLOYMENTS])
    return Report(assemble_rows(open_rows, deploy_rows), True, any(row.mark == ACT for row in open_rows))
