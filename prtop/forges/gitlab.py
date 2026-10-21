"""GitLab, read through the `glab` CLI.

Strictly read-only: every call goes through `GitLab.api`, which only issues GETs.
Authentication is glab's own (`glab auth login --hostname <host>`), and
`--hostname` rides on every call so the working directory's git remote never
decides which instance is read.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.parse import quote

from prtop import model
from prtop.config import Config
from prtop.model import DeployState, ForgeError
from prtop.tasks import Issue, TaskSource

PIPELINE_INFLIGHT = {
    "running", "pending", "preparing", "created",
    "scheduled", "waiting_for_resource",
}

# A prod deploy *job* is only actively shipping in these states. `created` is
# excluded: it means the job is blocked behind an untriggered gate, i.e. still
# awaiting, not deploying.
PROD_DEPLOYING = {"running", "pending", "preparing", "scheduled", "waiting_for_resource"}

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


def _run(cmd: list[str]) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, encoding="utf-8", check=False)
    except FileNotFoundError:
        raise ForgeError("glab is not installed: https://gitlab.com/gitlab-org/cli") from None
    if res.returncode != 0:
        raise ForgeError(
            f"Command failed: {shlex.join(cmd)}\n{res.stderr.strip() or res.stdout.strip()}"
        )
    return res.stdout


def project_path_from_summary(mr_summary: dict) -> str | None:
    refs_full = (mr_summary.get("references") or {}).get("full")
    if refs_full and "!" in refs_full:
        return refs_full.split("!", 1)[0]
    web_url = mr_summary.get("web_url") or ""
    m = re.search(r"https?://[^/]+/(.+?)/-/merge_requests/", web_url)
    return m.group(1) if m else None


def mr_short_id(project_path: str, iid: int) -> str:
    """The `repo!iid` the table shows, and the state-file key."""
    return f"{project_path.rsplit('/', 1)[-1]}!{iid}"


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


class ProdJobs:
    """Which CI jobs deploy to production, and which of those is the app itself.

    Auxiliary deploy jobs (`deploy_production_heavy_duty`, …) ship side services
    and are often left manual indefinitely; judging "shipped" by every prod
    deploy job would report an app that shipped as never deployed.
    """

    def __init__(self, deploy_pattern: str, primary_pattern: str):
        self._deploy = re.compile(deploy_pattern, re.IGNORECASE)
        self._primary = re.compile(primary_pattern, re.IGNORECASE)

    def primary(self, jobs: list[dict]) -> list[dict]:
        """The primary prod deploy job(s), else every prod deploy job."""
        prod = [j for j in jobs if self._deploy.search(j.get("name", ""))]
        return [j for j in prod if self._primary.search(j.get("name", ""))] or prod

    def shipped(self, jobs: list[dict]) -> bool:
        statuses = {j.get("status") for j in self.primary(jobs)}
        return "success" in statuses and statuses <= {"success", "skipped"}


def classify_pending(
    pipeline_status: str | None, jobs: list[dict], prod_jobs: ProdJobs, trigger_url: str
) -> DeployState:
    """Where a merged MR whose pipeline has not shipped to production stands."""
    if pipeline_status in PIPELINE_INFLIGHT:
        return DeployState(model.PIPELINE_RUNNING, pipeline_status or "")
    if pipeline_status == "failed":
        return DeployState(model.PIPELINE_FAILED)

    prod = prod_jobs.primary(jobs)
    if not prod:
        return DeployState(model.NO_PROD_JOB, pipeline_status or "")
    statuses = {j.get("status") for j in prod}
    if "failed" in statuses:
        return DeployState(model.PROD_FAILED)
    if statuses & PROD_DEPLOYING:
        return DeployState(model.PROD_DEPLOYING)
    if statuses & {"manual", "created", "skipped"}:
        return DeployState(model.AWAITING_TRIGGER, url=trigger_url)
    return DeployState(model.PROD_OTHER, ", ".join(sorted(s for s in statuses if s)))


class GitLab:
    def __init__(self, config: Config):
        self.host = config.gitlab_host
        self.group = config.gitlab_group
        self._username = config.gitlab_username
        self.prod_jobs = ProdJobs(config.deploy_job_pattern, config.primary_deploy_job_pattern)

    def api(self, path: str):
        out = _run(["glab", "api", "--hostname", self.host, path])
        return json.loads(out) if out.strip() else None

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
        query = "scope=assigned_to_me&state=opened&non_archived=true&per_page=100"
        summaries = self.api(f"{scope}merge_requests?{query}")
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
        if detailed == "discussions_not_resolved":
            threads_await_user = unresolved_threads_await_user(
                self._discussions(base), self.username()
            )

        task = tasks.resolve(full.get("source_branch"), lambda: self._closing_issues(base))
        approvers = [(a.get("user") or {}).get("username") for a in approvals.get("approved_by") or []]
        return mr_short_id(project_path, iid), {
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

    def _discussions(self, base: str) -> list[dict]:
        """Every discussion on the MR, up to ten pages — a bot-reviewed MR runs
        past one, and a single reviewer thread anywhere in them decides the status."""
        out: list[dict] = []
        for page in range(1, 11):
            batch = self.api(f"{base}/discussions?per_page=100&page={page}") or []
            out.extend(batch)
            if len(batch) < 100:
                break
        return out

    def _closing_issues(self, base: str) -> list[Issue]:
        # An issue from an external tracker (Jira) carries a string `id` instead of an `iid`.
        return [
            Issue(f"#{i['iid']}" if i.get("iid") else str(i.get("id", "")), i.get("title", ""), i.get("web_url"))
            for i in self.api(f"{base}/closes_issues") or []
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
        # Right after a merge, the branch pipeline may not exist yet; the latest
        # one on the branch stands in for it.
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

    def _pipeline_for_sha(self, project: str, ref: str, sha: str | None, *, retries: int = 3) -> dict | None:
        if not sha:
            return None
        for attempt in range(retries):
            for p in self._pipelines(project, ref, 10):
                if p.get("sha") == sha:
                    return p
            if attempt < retries - 1:
                time.sleep(2)
        return None

    # ---------- Deployments ----------

    def deploy_states(self, deployments: dict[str, dict]) -> dict[str, DeployState]:
        """Production is judged per branch against a single frontier (see
        `_frontier`), so an MR carried to production by a later commit counts as
        shipped even though its own prod job never ran."""
        if not deployments:
            return {}
        branches = sorted({(d["project_path"], d["ref"]) for d in deployments.values()})

        def frontier(branch: tuple[str, str]) -> int | None:
            # No frontier is the safe answer: evaluation falls through to the MR's
            # own pipeline, which reports its own failure as a `check failed` row.
            try:
                return self._frontier(*branch)
            except ForgeError:
                return None

        with ThreadPoolExecutor(max_workers=min(8, len(branches))) as pool:
            frontiers = dict(zip(branches, pool.map(frontier, branches)))

        def evaluate(item: tuple[str, dict]) -> tuple[str, DeployState]:
            key, dep = item
            return key, self._evaluate(dep, frontiers.get((dep["project_path"], dep["ref"])))

        with ThreadPoolExecutor(max_workers=min(8, len(deployments))) as pool:
            return dict(pool.map(evaluate, deployments.items()))

    def _jobs(self, project: str, pipeline_id: int) -> list[dict]:
        return self.api(f"projects/{project}/pipelines/{pipeline_id}/jobs?per_page=100") or []

    def _frontier(self, project_path: str, ref: str) -> int | None:
        """Highest pipeline id on the branch whose primary production deploy succeeded.

        A branch's history is linear, so any commit merged at or before that
        pipeline is live in production — a later commit shipping carries the
        earlier ones with it. None when no recent pipeline has shipped, or the
        project has no prod deploy job at all."""
        project = quote(project_path, safe="")
        for p in sorted(self._pipelines(project, ref, 20), key=lambda x: x["id"], reverse=True):
            if self.prod_jobs.shipped(self._jobs(project, p["id"])):
                return p["id"]
        return None

    def _evaluate(self, dep: dict, frontier: int | None) -> DeployState:
        project = quote(dep["project_path"], safe="")
        pipeline_id = dep["pipeline_id"]
        try:
            if frontier is not None and pipeline_id <= frontier:
                return DeployState(model.SHIPPED)
            pipeline = self.api(f"projects/{project}/pipelines/{pipeline_id}") or {}
            dep["pipeline_status"] = pipeline.get("status")
            jobs = self._jobs(project, pipeline_id)
            # Safety net for a prod deploy that fell outside the frontier's window.
            if self.prod_jobs.shipped(jobs):
                return DeployState(model.SHIPPED)
            if prod := self.prod_jobs.primary(jobs):
                dep["prod_job_id"] = prod[0]["id"]
            # The branch's commits page is where a manual production job is triggered from.
            commits_url = f"https://{self.host}/{dep['project_path']}/-/commits/{dep['ref']}?ref_type=HEADS"
            return classify_pending(dep["pipeline_status"], jobs, self.prod_jobs, commits_url)
        except ForgeError as e:
            return DeployState(model.CHECK_FAILED, str(e))
