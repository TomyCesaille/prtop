"""GitHub, read through the `gh` CLI. github.com only, not GitHub Enterprise Server.

Strictly read-only: REST calls go through `GitHub.api`, which only issues GETs,
and GraphQL calls through `GitHub.query`, which only sends queries.
Authentication is gh's own (`gh auth login`), and `--hostname github.com` rides
on every call so neither `GH_HOST` nor the working directory's git remote
decides which host is read.

GitHub tells an open pull request's state through a few separate fields, read
in one GraphQL query; `review_state` folds them into prtop's review states. A
merged one is followed through the GitHub Actions runs of its merge commit, its
jobs translated into the statuses `prtop.forges.pipelines` reasons in.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from prtop import model
from prtop.config import Config
from prtop.forges.command import failure, get_json, run_tool
from prtop.forges.pipelines import ProdJobs, after_merge, classify_pending, judge_deployments
from prtop.model import DeployState, keyed_by_short_id
from prtop.tasks import Issue, TaskSource

HOST = "github.com"
GH_URL = "https://cli.github.com"

# Your open pull requests: the ones you opened and the ones assigned to you.
OPEN_PULL_REQUESTS = """
query($authored: String!, $assigned: String!) {
  viewer { login }
  authored: search(query: $authored, type: ISSUE, first: 100) { nodes { ... on PullRequest { id } } }
  assigned: search(query: $assigned, type: ISSUE, first: 100) { nodes { ... on PullRequest { id } } }
}
"""

# Their state, a batch at a time: GitHub works out each one's merge state as it
# answers, and a query that runs past ten seconds fails whole. Review threads
# come with their resolution only; who wrote in them is asked for one pull
# request at a time, and only when it decides the state.
PULL_REQUESTS = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on PullRequest {
      number title url isDraft headRefName
      repository { nameWithOwner viewerPermission }
      reviewDecision mergeable mergeStateStatus
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      reviewThreads(first: 100) { nodes { isResolved } }
      closingIssuesReferences(first: 10) { nodes { number title url } }
    }
  }
}
"""
BATCH = 20

REVIEW_THREADS = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        nodes { isResolved comments(first: 100) { nodes { author { login } } } }
      }
    }
  }
}
"""

# `mergeStateStatus` values that settle the review state on their own. BLOCKED
# needs a closer look (see `review_state`); DIRTY and DRAFT are read first.
MERGE_STATE = {
    "BEHIND": model.REBASE_NEEDED,
    "CLEAN": model.MERGEABLE,
    "HAS_HOOKS": model.MERGEABLE,
    # Mergeable with checks failing that the branch does not require.
    "UNSTABLE": model.MERGEABLE,
    "UNKNOWN": model.MERGE_CHECK_PENDING,
}

# The roles in a repository that can merge its pull requests. GitHub tells a pull
# request's merge state the same whoever asks.
MERGERS = ("ADMIN", "MAINTAIN", "WRITE")

# GitHub Actions run and job states, in the words of `prtop.forges.pipelines`. A
# job waiting on an environment's reviewers is a production job left to trigger.
ACTIONS_STATUS = {
    "queued": "pending",
    "pending": "pending",
    "requested": "pending",
    "in_progress": "running",
    "waiting": "manual",
}
ACTIONS_CONCLUSION = {
    "success": "success",
    "failure": "failed",
    "timed_out": "failed",
    "startup_failure": "failed",
    "cancelled": "canceled",
    "skipped": "skipped",
    "action_required": "manual",
}


def nodes(parent: dict | None, connection: str) -> list[dict]:
    """A GraphQL connection's nodes, without the nulls an error leaves behind."""
    return [n for n in ((parent or {}).get(connection) or {}).get("nodes") or [] if n]


def can_merge(pr: dict) -> bool:
    return (pr.get("repository") or {}).get("viewerPermission") in MERGERS


def review_state(pr: dict, threads_await_user: Callable[[], bool]) -> tuple[str, bool]:
    """GitHub's view of an open pull request as a review state of `prtop.model`,
    with whether its unresolved threads wait on you.

    Unresolved threads only matter when GitHub blocks the merge, the way GitLab
    only reports them when its project requires them resolved. GitHub does not
    say what blocks it, so the likeliest blocker is named: a reviewer's thread,
    then a missing review, then checks, then threads only you wrote in.
    `threads_await_user` asks the forge, so it is only called when it decides.
    """
    merge_state = pr.get("mergeStateStatus") or "UNKNOWN"
    decision = pr.get("reviewDecision")
    if pr.get("isDraft"):
        return model.DRAFT, True
    if pr.get("mergeable") == "CONFLICTING" or merge_state == "DIRTY":
        return model.CONFLICTS, True
    if decision == "CHANGES_REQUESTED":
        return model.CHANGES_REQUESTED, True
    if merge_state in MERGE_STATE:
        state = MERGE_STATE[merge_state]
        # In a repository you cannot merge in, the merge is its maintainers' move.
        if state == model.MERGEABLE and not can_merge(pr):
            return model.AWAITING_REVIEW, True
        return state, True
    if merge_state != "BLOCKED":
        return merge_state.lower(), True

    unresolved = any(not thread.get("isResolved") for thread in nodes(pr, "reviewThreads"))
    if unresolved and threads_await_user():
        return model.UNRESOLVED_THREADS, True
    if decision == "REVIEW_REQUIRED":
        return model.AWAITING_REVIEW, True
    commit = next(iter(nodes(pr, "commits")), {}).get("commit") or {}
    match (commit.get("statusCheckRollup") or {}).get("state"):
        case "PENDING" | "EXPECTED":
            return model.CI_RUNNING, True
        case "FAILURE" | "ERROR":
            return model.CI_MUST_PASS, True
    if unresolved:
        return model.UNRESOLVED_THREADS, False
    return model.BLOCKED, True


def unresolved_threads_await_user(threads: list[dict], login: str) -> bool:
    """Whether an unresolved thread carries a comment from anyone but you."""
    return any(
        not thread.get("isResolved")
        and any((comment.get("author") or {}).get("login") != login for comment in nodes(thread, "comments"))
        for thread in threads
    )


def actions_status(item: dict) -> str:
    """A workflow run's or a job's state, in the words of `prtop.forges.pipelines`."""
    if item.get("status") == "completed":
        conclusion = item.get("conclusion") or ""
        return ACTIONS_CONCLUSION.get(conclusion, conclusion)
    status = item.get("status") or ""
    return ACTIONS_STATUS.get(status, status)


# How long after a merge commit's last run finished its lack of a production job
# still leaves the tracking open: a deploy started by `workflow_run` is created
# only once the run it waits on is over.
DOWNSTREAM_GRACE = timedelta(minutes=5)


def last_update(runs: list[dict]) -> datetime | None:
    return max((datetime.fromisoformat(run["updated_at"]) for run in runs if run.get("updated_at")), default=None)


def pipeline_status(runs: list[dict]) -> str:
    """A merge commit's runs as one pipeline: in flight while any run is, failed
    once any run failed."""
    statuses = {actions_status(run) for run in runs}
    return next((s for s in ("running", "pending", "failed") if s in statuses), "success")


class GitHub:
    host = HOST

    def __init__(
        self,
        config: Config,
        *,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.owner = config.github_owner
        self.prod_jobs = ProdJobs(config.deploy_job_pattern, config.primary_deploy_job_pattern)
        self._sleep = sleep
        self._now = now
        # A completed run's jobs and a commit's ancestry never change, so a live
        # view asks for them once.
        self._finished_jobs: dict[tuple[int, int], list[dict]] = {}
        self._ancestry: dict[tuple[str, str, str], bool] = {}

    def api(self, path: str):
        return get_json(["gh", "api", "--hostname", HOST, path], GH_URL)

    def query(self, document: str, **variables: str | int | list[str]) -> dict:
        """A GraphQL query's data. Errors that come with data, such as an
        organization's single sign-on hiding some of its pull requests, are
        reported on stderr; the rest of the data stands."""
        cmd = ["gh", "api", "--hostname", HOST, "graphql", "-f", f"query={document}"]
        for name, value in variables.items():
            if isinstance(value, list):
                cmd += [arg for item in value for arg in ("-f", f"{name}[]={item}")]
            else:
                cmd += ["-F" if isinstance(value, int) else "-f", f"{name}={value}"]
        result = run_tool(cmd, GH_URL)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict) or not payload.get("data"):
            raise failure("gh api graphql", result)
        for error in payload.get("errors") or []:
            print(f"prtop: {HOST}: {error.get('message', error)}", file=sys.stderr)
        return payload["data"]

    # ---------- Open pull requests ----------

    def open_merge_requests(self, tasks: TaskSource) -> dict[str, dict]:
        scope = f"is:pr is:open archived:false{f' user:{self.owner}' if self.owner else ''}"
        data = self.query(
            OPEN_PULL_REQUESTS, authored=f"{scope} author:@me", assigned=f"{scope} assignee:@me"
        )
        login = (data.get("viewer") or {}).get("login", "")
        # One you opened and is assigned to you comes back from both searches.
        ids = list(dict.fromkeys(pr["id"] for pr in [*nodes(data, "authored"), *nodes(data, "assigned")]))
        if not ids:
            return {}
        batches = [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            prs = [pr for batch in pool.map(self._pull_requests, batches) for pr in batch]
            return keyed_by_short_id(pool.map(lambda pr: self._entry(pr, login, tasks), prs))

    def _pull_requests(self, ids: list[str]) -> list[dict]:
        return [pr for pr in self.query(PULL_REQUESTS, ids=ids).get("nodes") or [] if pr]

    def _entry(self, pr: dict, login: str, tasks: TaskSource) -> tuple[str, int, dict]:
        repo = pr["repository"]["nameWithOwner"]
        number = pr["number"]
        status, threads_await_user = review_state(
            pr, lambda: unresolved_threads_await_user(self._threads(repo, number), login)
        )
        issues = [
            Issue(f"#{i['number']}", i.get("title", ""), i.get("url"))
            for i in nodes(pr, "closingIssuesReferences")
        ]
        task = tasks.resolve(pr.get("headRefName"), lambda: issues)
        return repo, number, {
            "title": pr.get("title", ""),
            "task": task.label if task else "",
            "task_url": task.url if task else None,
            "status": status,
            # A repository that requires no review leaves `reviewDecision` empty:
            # for whoever can merge, nothing stands between the pull request and
            # its merge, as with a GitLab project without approval rules.
            "fully_approved": pr.get("reviewDecision") == "APPROVED"
            or (pr.get("reviewDecision") is None and can_merge(pr)),
            "has_conflicts": pr.get("mergeable") == "CONFLICTING",
            "threads_await_user": threads_await_user,
            "repo": repo,
            "number": number,
            "web_url": pr.get("url"),
            "last_checked": datetime.now(UTC).isoformat(),
        }

    def _threads(self, repo: str, number: int) -> list[dict]:
        owner, name = repo.split("/", 1)
        data = self.query(REVIEW_THREADS, owner=owner, name=name, number=number)
        return nodes((data.get("repository") or {}).get("pullRequest"), "reviewThreads")

    # ---------- Merges ----------

    def merged_deployment(self, entry: dict) -> dict | None:
        repo, number = entry.get("repo"), entry.get("number")
        if not repo or not number:
            return None
        pull = self.api(f"repos/{repo}/pulls/{number}") or {}
        if not pull.get("merged"):
            return None
        ref, sha = (pull.get("base") or {}).get("ref") or "main", pull.get("merge_commit_sha")
        # Without a workflow run for the merge commit, nothing could deploy it.
        if not sha or not after_merge(lambda: self._runs(repo, ref, sha) or None, self._sleep):
            return None
        return {
            "title": entry.get("title", ""),
            "task": entry.get("task", ""),
            "task_url": entry.get("task_url"),
            "repo": repo,
            "number": number,
            "web_url": entry.get("web_url"),
            "ref": ref,
            "sha": sha,
        }

    def _runs(self, repo: str, ref: str, sha: str | None = None, count: int = 100) -> list[dict]:
        query = f"branch={quote(ref, safe='')}&per_page={count}&exclude_pull_requests=true"
        if sha:
            query += f"&head_sha={sha}"
        return (self.api(f"repos/{repo}/actions/runs?{query}") or {}).get("workflow_runs") or []

    # ---------- Deployments ----------

    def deploy_states(self, deployments: dict[str, dict]) -> dict[str, DeployState]:
        return judge_deployments(
            deployments,
            lambda d: (d["repo"], d["ref"]),
            lambda branch: self._frontier(*branch),
            self._evaluate,
        )

    def _jobs(self, repo: str, run: dict) -> list[dict]:
        """The run's jobs, of its latest attempt and every page, with their states translated."""
        key = (run["id"], run.get("run_attempt", 1))
        if key in self._finished_jobs:
            return self._finished_jobs[key]
        jobs: list[dict] = []
        page = 1
        while True:
            listing = self.api(
                f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100&page={page}"
            ) or {}
            batch = listing.get("jobs") or []
            jobs += [
                {"id": job["id"], "name": job.get("name", ""), "status": actions_status(job), "run_id": run["id"]}
                for job in batch
            ]
            if not batch or len(jobs) >= listing.get("total_count", 0):
                break
            page += 1
        if run.get("status") == "completed":
            self._finished_jobs[key] = jobs
        return jobs

    def _commit(self, repo: str, ref: str, sha: str) -> tuple[list[dict], list[dict]]:
        """A commit's runs on the branch, and all their jobs together: a primary
        production job and its auxiliaries can run in different workflows."""
        runs = self._runs(repo, ref, sha)
        return runs, [job for run in runs for job in self._jobs(repo, run)]

    def _frontier(self, repo: str, ref: str) -> str | None:
        """The newest commit among the branch's recent runs whose primary
        production deploy succeeded. None when none of them shipped."""
        recent = sorted(self._runs(repo, ref, count=20), key=lambda r: r["id"], reverse=True)
        for sha in dict.fromkeys(run["head_sha"] for run in recent):
            if self.prod_jobs.shipped(self._commit(repo, ref, sha)[1]):
                return sha
        return None

    def _contains(self, repo: str, deployed: str, sha: str) -> bool:
        """Whether the deployed commit carries `sha`. Unlike pipeline ids on
        GitLab, run ids do not follow commit order: a deploy that waits on
        another workflow can start after a later commit's runs."""
        if deployed == sha:
            return True
        key = (repo, deployed, sha)
        if key not in self._ancestry:
            comparison = self.api(f"repos/{repo}/compare/{sha}...{deployed}?per_page=1") or {}
            self._ancestry[key] = comparison.get("status") in ("ahead", "identical")
        return self._ancestry[key]

    def _evaluate(self, dep: dict, frontier: str | None) -> DeployState:
        repo = dep["repo"]
        if frontier is not None and self._contains(repo, frontier, dep["sha"]):
            return DeployState(model.SHIPPED)
        runs, jobs = self._commit(repo, dep["ref"], dep["sha"])
        # Safety net for a prod deploy that fell outside the frontier's window.
        if self.prod_jobs.shipped(jobs):
            return DeployState(model.SHIPPED)
        trigger_url = ""
        if prod := self.prod_jobs.primary(jobs):
            dep["prod_run_id"] = prod[0]["run_id"]
            # The run's page is where a job waiting on an environment's reviewers is approved.
            trigger_url = f"https://{HOST}/{repo}/actions/runs/{prod[0]['run_id']}"
        state = classify_pending(pipeline_status(runs), jobs, self.prod_jobs, trigger_url)
        finished = last_update(runs)
        if state.kind == model.NO_PROD_JOB and finished and self._now() - finished < DOWNSTREAM_GRACE:
            return DeployState(model.PIPELINE_RUNNING, "pending")
        return state
