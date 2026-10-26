"""GitLab, read through the `glab` CLI.

Strictly read-only: every call goes through `GitLab.api`, which only issues GETs.
Authentication is glab's own (`glab auth login --hostname <host>`), and
`--hostname` rides on every call so the working directory's git remote never
decides which instance is read.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.parse import quote

from prtop import model
from prtop.config import Config
from prtop.forges.command import get_json
from prtop.forges.pipelines import ProdJobs, after_merge, classify_pending, judge_deployments
from prtop.model import DeployState, mr_key
from prtop.tasks import Issue, TaskSource

GLAB_URL = "https://gitlab.com/gitlab-org/cli"

# GitLab's `detailed_merge_status`, onto prtop's review states. A status missing
# here passes through raw and shows up as unrecognised.
REVIEW_STATUS = {
    "draft_status": model.DRAFT,
    "not_approved": model.AWAITING_REVIEW,
    "requested_changes": model.CHANGES_REQUESTED,
    "discussions_not_resolved": model.UNRESOLVED_THREADS,
    "need_rebase": model.REBASE_NEEDED,
    "conflict": model.CONFLICTS,
    "ci_still_running": model.CI_RUNNING,
    "ci_must_pass": model.CI_MUST_PASS,
    "checking": model.MERGE_CHECK_RUNNING,
    "unchecked": model.MERGE_CHECK_PENDING,
    "blocked_status": model.BLOCKED,
    "mergeable": model.MERGEABLE,
}


def project_path_from_summary(mr_summary: dict) -> str | None:
    refs_full = (mr_summary.get("references") or {}).get("full")
    if refs_full and "!" in refs_full:
        return refs_full.split("!", 1)[0]
    web_url = mr_summary.get("web_url") or ""
    m = re.search(r"https?://[^/]+/(.+?)/-/merge_requests/", web_url)
    return m.group(1) if m else None


def unresolved_threads_await_user(discussions: list[dict], username: str) -> bool:
    """Whether an unresolved thread carries a note from anyone but you.

    A thread you opened on your own MR and nobody answered is a hint you left for
    the reviewer; GitLab counts it unresolved all the same. One note from
    someone else in the thread makes it feedback you have to answer.
    """
    for discussion in discussions:
        notes = [n for n in discussion.get("notes") or [] if not n.get("system")]
        if not any(n.get("resolvable") and not n.get("resolved") for n in notes):
            continue
        if any((n.get("author") or {}).get("username") != username for n in notes):
            return True
    return False


class GitLab:
    def __init__(self, config: Config, *, sleep: Callable[[float], None] = time.sleep):
        self.host = config.gitlab_host
        self.group = config.gitlab_group
        self._username = config.gitlab_username
        self.prod_jobs = ProdJobs(config.deploy_job_pattern, config.primary_deploy_job_pattern)
        self._sleep = sleep

    def api(self, path: str):
        return get_json(["glab", "api", "--hostname", self.host, path], GLAB_URL)

    def username(self) -> str:
        # Only needed to tell your own threads apart, so it is asked for at most
        # once a run, and only when an MR's threads block its merge.
        if self._username is None:
            self._username = (self.api("user") or {}).get("username", "")
        return self._username

    # ---------- Open merge requests ----------

    def open_merge_requests(self, tasks: TaskSource) -> dict[str, dict]:
        scope = f"groups/{quote(self.group, safe='')}/" if self.group else ""
        # An MR in an archived project can no longer be acted on.
        summaries = self._pages(f"{scope}merge_requests?scope=assigned_to_me&state=opened&non_archived=true")
        if not summaries:
            return {}
        with ThreadPoolExecutor(max_workers=min(8, len(summaries))) as pool:
            hydrated = list(pool.map(lambda summary: self._hydrate(summary, tasks), summaries))
        return dict(item for item in hydrated if item is not None)

    def _hydrate(self, summary: dict, tasks: TaskSource) -> tuple[str, dict] | None:
        project_path = project_path_from_summary(summary)
        if not project_path:
            return None
        iid = summary["iid"]
        base = f"projects/{quote(project_path, safe='')}/merge_requests/{iid}"
        full = self.api(f"{base}?include_diverged_commits_count=true")
        approvals = self.api(f"{base}/approvals")

        detailed = full.get("detailed_merge_status", "unknown")
        # GitLab reports `not_approved` ahead of `need_rebase`, so an MR in review
        # that fell behind its target would only say so once approved.
        if detailed == "not_approved" and not full.get("draft") and full.get("diverged_commits_count"):
            detailed = "need_rebase"
        threads_await_user = True
        # The extra calls earn their place only when threads are what blocks the merge.
        # A bot-reviewed MR runs past a page of discussions, and a single reviewer
        # thread anywhere in them decides the status.
        if detailed == "discussions_not_resolved":
            threads_await_user = unresolved_threads_await_user(
                self._pages(f"{base}/discussions"), self.username()
            )

        task = tasks.resolve(full.get("source_branch"), lambda: self._closing_issues(base))
        approvers = [(a.get("user") or {}).get("username") for a in approvals.get("approved_by") or []]
        return mr_key(project_path, iid), {
            "title": full.get("title", ""),
            "task": task.label if task else "",
            "task_url": task.url if task else None,
            "status": REVIEW_STATUS.get(detailed, detailed),
            "fully_approved": bool(approvals.get("approved")),
            "has_conflicts": bool(full.get("has_conflicts")),
            "threads_await_user": threads_await_user,
            "approvals": [u for u in approvers if u],
            "note_count": full.get("user_notes_count", 0),
            "project_path": project_path,
            "iid": iid,
            "web_url": full.get("web_url"),
            "last_checked": datetime.now(UTC).isoformat(),
        }

    def _pages(self, path: str) -> list[dict]:
        """Every item of a listing, up to ten pages of a hundred."""
        separator = "&" if "?" in path else "?"
        out: list[dict] = []
        for page in range(1, 11):
            batch = self.api(f"{path}{separator}per_page=100&page={page}") or []
            out.extend(batch)
            if len(batch) < 100:
                break
        return out

    def _closing_issues(self, base: str) -> list[Issue]:
        # An issue from an external tracker (Jira) carries a string `id` instead of an `iid`.
        return [
            Issue(f"#{i['iid']}" if i.get("iid") else str(i.get("id", "")), i.get("title", ""), i.get("web_url"))
            for i in self._pages(f"{base}/closes_issues")
        ]

    # ---------- Merges ----------

    def merged_deployment(self, entry: dict) -> dict | None:
        project_path, iid = entry.get("project_path"), entry.get("iid")
        if not project_path or not iid:
            return None
        project = quote(project_path, safe="")
        full = self.api(f"projects/{project}/merge_requests/{iid}") or {}
        if full.get("state") != "merged":
            return None

        ref = full.get("target_branch") or "main"
        sha = full.get("merge_commit_sha") or full.get("squash_commit_sha") or full.get("sha")
        # When the merge commit's pipeline does not show up, the latest one on the
        # branch stands in for it.
        pipeline = self._pipeline_for_sha(project, ref, sha) or next(
            iter(self._pipelines(project, ref, 5)), None
        )
        if pipeline is None:
            return None
        return {
            "title": entry.get("title", ""),
            "task": entry.get("task", ""),
            "task_url": entry.get("task_url"),
            "project_path": project_path,
            "iid": iid,
            "web_url": entry.get("web_url"),
            "ref": ref,
            "pipeline_id": pipeline["id"],
            "pipeline_status": pipeline.get("status"),
            "pipeline_url": pipeline.get("web_url"),
        }

    def _pipelines(self, project: str, ref: str, count: int) -> list[dict]:
        return self.api(f"projects/{project}/pipelines?ref={quote(ref, safe='')}&per_page={count}") or []

    def _pipeline_for_sha(self, project: str, ref: str, sha: str | None) -> dict | None:
        if not sha:
            return None
        return after_merge(
            lambda: next((p for p in self._pipelines(project, ref, 10) if p.get("sha") == sha), None),
            self._sleep,
        )

    # ---------- Deployments ----------

    def deploy_states(self, deployments: dict[str, dict]) -> dict[str, DeployState]:
        return judge_deployments(
            deployments,
            lambda d: (d["project_path"], d["ref"]),
            lambda branch: self._frontier(*branch),
            self._evaluate,
        )

    def _jobs(self, project: str, pipeline_id: int) -> list[dict]:
        return self.api(f"projects/{project}/pipelines/{pipeline_id}/jobs?per_page=100") or []

    def _frontier(self, project_path: str, ref: str) -> int | None:
        """The highest pipeline id on the branch whose primary production deploy
        succeeded: pipeline ids follow the branch's commit order. None when no
        recent pipeline has shipped, or the project has no prod deploy job at all."""
        project = quote(project_path, safe="")
        for p in sorted(self._pipelines(project, ref, 20), key=lambda x: x["id"], reverse=True):
            if self.prod_jobs.shipped(self._jobs(project, p["id"])):
                return p["id"]
        return None

    def _evaluate(self, dep: dict, frontier: int | None) -> DeployState:
        project = quote(dep["project_path"], safe="")
        pipeline_id = dep["pipeline_id"]
        if frontier is not None and pipeline_id <= frontier:
            return DeployState(model.SHIPPED)
        pipeline = self.api(f"projects/{project}/pipelines/{pipeline_id}") or {}
        dep["pipeline_status"] = pipeline.get("status")
        jobs = self._jobs(project, pipeline_id)
        # Safety net for a prod deploy that fell outside the frontier's window.
        if self.prod_jobs.shipped(jobs):
            return DeployState(model.SHIPPED)
        if prod := self.prod_jobs.to_act_on(jobs):
            dep["prod_job_id"] = prod["id"]
        # The branch's commits page is where a manual production job is triggered from.
        commits_url = f"https://{self.host}/{dep['project_path']}/-/commits/{dep['ref']}?ref_type=HEADS"
        return classify_pending(dep["pipeline_status"], jobs, self.prod_jobs, commits_url)
