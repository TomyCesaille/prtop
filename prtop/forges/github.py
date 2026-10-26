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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import cache
from urllib.parse import quote

from prtop import model
from prtop.config import Config
from prtop.forges.command import failure, get_json, run_tool
from prtop.forges.pipelines import ProdJobs, classify_pending, judge_deployments
from prtop.model import DeployState, mr_key
from prtop.tasks import Issue, TaskSource

HOST = "github.com"
GH_URL = "https://cli.github.com"

# Your open pull requests, one search for the ones you opened and one for the
# ones assigned to you, a page of a hundred at a time.
OPEN_PULL_REQUESTS = """
query($search: String!, $after: String) {
  viewer { login }
  search(query: $search, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { id } }
  }
}
"""

# Their state, a batch at a time: GitHub works out each one's merge state as it
# answers, and a query that runs past ten seconds fails whole. Review threads
# come with the resolution of the first hundred only; the rest, and who wrote in
# them, are asked for one pull request at a time, and only when they decide the
# state.
PULL_REQUESTS = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on PullRequest {
      number title url isDraft headRefName
      repository { nameWithOwner viewerPermission }
      reviewDecision mergeable mergeStateStatus
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      reviewThreads(first: 100) { pageInfo { hasNextPage } nodes { isResolved } }
      closingIssuesReferences(first: 10) { pageInfo { hasNextPage } nodes { number title url } }
    }
  }
}
"""
BATCH = 20

REVIEW_THREADS = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { isResolved comments(first: 100) { pageInfo { hasNextPage } nodes { author { login } } } }
      }
    }
  }
}
"""

CLOSING_ISSUES = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { number title url }
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


def review_state(pr: dict, threads: Callable[[], list[dict]], login: str) -> tuple[str, bool]:
    """GitHub's view of an open pull request as a review state of `prtop.model`,
    with whether its unresolved threads wait on you.

    Unresolved threads only matter when GitHub blocks the merge, the way GitLab
    only reports them when its project requires them resolved. GitHub does not
    say what blocks it, so the likeliest blocker is named: a reviewer's thread,
    then a missing review, then checks, then threads only you wrote in.
    `threads` asks the forge for every thread and who wrote in it, so it is only
    called when they decide.
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
    if not unresolved and ((pr.get("reviewThreads") or {}).get("pageInfo") or {}).get("hasNextPage"):
        unresolved = any(not thread.get("isResolved") for thread in threads())
    if unresolved and unresolved_threads_await_user(threads(), login):
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
    """Whether an unresolved thread carries a comment from anyone but you. One
    whose comments run past a page counts: no hint left for a reviewer runs to
    a hundred comments."""
    return any(
        not thread.get("isResolved")
        and (
            bool(((thread.get("comments") or {}).get("pageInfo") or {}).get("hasNextPage"))
            or any((comment.get("author") or {}).get("login") != login for comment in nodes(thread, "comments"))
        )
        for thread in threads
    )


def actions_status(item: dict) -> str:
    """A workflow run's or a job's state, in the words of `prtop.forges.pipelines`."""
    if item.get("status") == "completed":
        conclusion = item.get("conclusion") or ""
        return ACTIONS_CONCLUSION.get(conclusion, conclusion)
    status = item.get("status") or ""
    return ACTIONS_STATUS.get(status, status)


# How long after a merge commit last moved (its latest run, or the merge itself
# before any run shows up) its lack of a production job still leaves the tracking
# open: its first runs take a while to show up, and a deploy started by
# `workflow_run` is created only once the run it waits on is over.
DOWNSTREAM_GRACE = timedelta(minutes=5)


def runs_on(repo: str, ref: str) -> str:
    """The listing of a branch's workflow runs, newest first."""
    return f"repos/{repo}/actions/runs?branch={quote(ref, safe='')}&exclude_pull_requests=true"


def last_update(runs: list[dict], merged_at: str | None = None) -> datetime | None:
    """When the merge commit last moved: its latest run's update, or the merge
    itself while no run has shown up."""
    stamps = [merged_at, *(run.get("updated_at") for run in runs)]
    return max((datetime.fromisoformat(stamp) for stamp in stamps if stamp), default=None)


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
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.owner = config.github_owner
        self.prod_jobs = ProdJobs(config.deploy_job_pattern, config.primary_deploy_job_pattern)
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

    def _every_page(
        self, document: str, connection: Callable[[dict], dict | None], **variables: str | int
    ) -> tuple[dict, list[dict]]:
        """The nodes of a connection across all its pages, and the last page's data."""
        found: list[dict] = []
        after: dict[str, str] = {}
        while True:
            data = self.query(document, **variables, **after)
            page = connection(data) or {}
            found += [node for node in page.get("nodes") or [] if node]
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return data, found
            after = {"after": info["endCursor"]}

    # ---------- Open pull requests ----------

    def open_merge_requests(self, tasks: TaskSource) -> dict[str, dict]:
        scope = f"is:pr is:open archived:false{f' user:{self.owner}' if self.owner else ''}"
        with ThreadPoolExecutor(max_workers=8) as pool:
            (data, authored), (_, assigned) = pool.map(
                lambda search: self._every_page(OPEN_PULL_REQUESTS, lambda d: d.get("search"), search=search),
                (f"{scope} author:@me", f"{scope} assignee:@me"),
            )
            login = (data.get("viewer") or {}).get("login", "")
            # One you opened and is assigned to you comes back from both searches.
            ids = list(dict.fromkeys(pr["id"] for pr in [*authored, *assigned]))
            if not ids:
                return {}
            batches = [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]
            prs = [pr for batch in pool.map(self._pull_requests, batches) for pr in batch]
            return dict(pool.map(lambda pr: self._entry(pr, login, tasks), prs))

    def _pull_requests(self, ids: list[str]) -> list[dict]:
        return [pr for pr in self.query(PULL_REQUESTS, ids=ids).get("nodes") or [] if pr]

    def _entry(self, pr: dict, login: str, tasks: TaskSource) -> tuple[str, dict]:
        repo = pr["repository"]["nameWithOwner"]
        number = pr["number"]
        status, threads_await_user = review_state(
            pr, cache(lambda: self._pull_request_pages(REVIEW_THREADS, "reviewThreads", repo, number)), login
        )
        task = tasks.resolve(pr.get("headRefName"), lambda: self._closing_issues(pr, repo, number))
        return mr_key(repo, number), {
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

    def _pull_request_pages(self, document: str, connection: str, repo: str, number: int) -> list[dict]:
        """Every node of one of a pull request's connections, across its pages."""
        owner, name = repo.split("/", 1)
        return self._every_page(
            document,
            lambda data: ((data.get("repository") or {}).get("pullRequest") or {}).get(connection),
            owner=owner, name=name, number=number,
        )[1]

    def _closing_issues(self, pr: dict, repo: str, number: int) -> list[Issue]:
        found = nodes(pr, "closingIssuesReferences")
        if ((pr.get("closingIssuesReferences") or {}).get("pageInfo") or {}).get("hasNextPage"):
            found = self._pull_request_pages(CLOSING_ISSUES, "closingIssuesReferences", repo, number)
        return [Issue(f"#{i['number']}", i.get("title", ""), i.get("url")) for i in found]

    # ---------- Merges ----------

    def merged_deployment(self, entry: dict) -> dict | None:
        repo, number = entry.get("repo"), entry.get("number")
        if not repo or not number:
            return None
        pull = self.api(f"repos/{repo}/pulls/{number}") or {}
        sha = pull.get("merge_commit_sha")
        if not pull.get("merged") or not sha:
            return None
        return {
            "title": entry.get("title", ""),
            "task": entry.get("task", ""),
            "task_url": entry.get("task_url"),
            "repo": repo,
            "number": number,
            "web_url": entry.get("web_url"),
            "ref": (pull.get("base") or {}).get("ref") or "main",
            "sha": sha,
            "merged_at": pull.get("merged_at"),
        }

    def _listing(self, path: str, field: str) -> list[dict]:
        """Every item of a REST listing, up to ten pages of a hundred."""
        items: list[dict] = []
        for page in range(1, 11):
            batch = (self.api(f"{path}&per_page=100&page={page}") or {}).get(field) or []
            items += batch
            if len(batch) < 100:
                break
        return items

    def _commit_runs(self, repo: str, ref: str, sha: str) -> list[dict]:
        """A commit's runs on the branch, past the first page: a scheduled
        workflow runs on the default branch's latest commit again and again, and
        buries the runs its push started."""
        return self._listing(f"{runs_on(repo, ref)}&head_sha={sha}", "workflow_runs")

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
        jobs = [
            {"id": job["id"], "name": job.get("name", ""), "status": actions_status(job), "run_id": run["id"]}
            for job in self._listing(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest", "jobs")
        ]
        if run.get("status") == "completed":
            self._finished_jobs[key] = jobs
        return jobs

    def _commit(self, repo: str, ref: str, sha: str) -> tuple[list[dict], list[dict]]:
        """A commit's runs on the branch, and all their jobs together: a primary
        production job and its auxiliaries can run in different workflows."""
        runs = self._commit_runs(repo, ref, sha)
        return runs, [job for run in runs for job in self._jobs(repo, run)]

    def _frontier(self, repo: str, ref: str) -> str | None:
        """The newest commit among the branch's twenty latest runs whose primary
        production deploy succeeded. None when none of them shipped.

        Newest by ancestry: run ids only roughly follow commit order (see
        `_contains`), so the commits are checked newest run first, and each one
        the frontier found so far does not carry is checked too."""
        recent = (self.api(f"{runs_on(repo, ref)}&per_page=20") or {}).get("workflow_runs") or []
        frontier = None
        for sha in dict.fromkeys(run["head_sha"] for run in sorted(recent, key=lambda r: r["id"], reverse=True)):
            if frontier is not None and self._contains(repo, frontier, sha):
                continue
            if self._shipped(*self._commit(repo, ref, sha)):
                frontier = sha
        return frontier

    def _shipped(self, runs: list[dict], jobs: list[dict]) -> bool:
        """Whether the commit's primary production deploy succeeded. Without a
        job named as the primary, any production deploy job decides, once every
        run is over and the downstream grace has passed: until then, the primary
        can still show up in a workflow started by `workflow_run`."""
        if not self.prod_jobs.shipped(jobs):
            return False
        if self.prod_jobs.names_primary(jobs):
            return True
        finished = last_update(runs)
        return all(run.get("status") == "completed" for run in runs) and (
            finished is None or self._now() - finished >= DOWNSTREAM_GRACE
        )

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
        if self._shipped(runs, jobs):
            return DeployState(model.SHIPPED)
        if self.prod_jobs.shipped(jobs):
            # Only jobs not named as the primary deployed so far.
            return DeployState(model.PIPELINE_RUNNING, "pending")
        trigger_url = ""
        if prod := self.prod_jobs.to_act_on(jobs):
            dep["prod_run_id"] = prod["run_id"]
            # The run's page is where a job waiting on an environment's reviewers is approved.
            trigger_url = f"https://{HOST}/{repo}/actions/runs/{prod['run_id']}"
        state = classify_pending(pipeline_status(runs), jobs, self.prod_jobs, trigger_url)
        if state.kind != model.NO_PROD_JOB:
            return state
        # A run held on a non-production environment's reviewers can still lead
        # to production once approved.
        if any(run.get("status") != "completed" for run in runs):
            return DeployState(model.PIPELINE_RUNNING, "waiting")
        finished = last_update(runs, dep.get("merged_at"))
        if finished and self._now() - finished < DOWNSTREAM_GRACE:
            return DeployState(model.PIPELINE_RUNNING, "pending")
        return state
