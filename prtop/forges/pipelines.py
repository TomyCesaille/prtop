"""How a merged change travels to production, in terms every forge maps onto.

Pipeline and job statuses are GitLab's words (`running`, `manual`, `failed`…):
GitLab reports them as they are, and GitHub translates its workflow runs and
jobs into them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Hashable
from concurrent.futures import ThreadPoolExecutor

from prtop import model
from prtop.model import DeployState, ForgeError

PIPELINE_INFLIGHT = {
    "running", "pending", "preparing", "created",
    "scheduled", "waiting_for_resource",
}

# A prod deploy *job* is only actively shipping in these states. `created` is
# excluded: it means the job is blocked behind an untriggered gate, i.e. still
# awaiting, not deploying.
PROD_DEPLOYING = {"running", "pending", "preparing", "scheduled", "waiting_for_resource"}


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

    def to_act_on(self, jobs: list[dict]) -> dict | None:
        """The primary prod deploy job to act on: one waiting to be started by
        hand, else the first. Several match when no job is named as the primary,
        and one region can have shipped while another waits."""
        prod = self.primary(jobs)
        return next((j for j in prod if j.get("status") == "manual"), next(iter(prod), None))

    def shipped(self, jobs: list[dict]) -> bool:
        statuses = {j.get("status") for j in self.primary(jobs)}
        return "success" in statuses and statuses <= {"success", "skipped"}


def classify_pending(
    pipeline_status: str | None, jobs: list[dict], prod_jobs: ProdJobs, trigger_url: str
) -> DeployState:
    """Where a merged change whose pipeline has not shipped to production stands."""
    if pipeline_status in PIPELINE_INFLIGHT:
        return DeployState(model.PIPELINE_RUNNING, pipeline_status or "")
    if pipeline_status == "failed":
        return DeployState(model.PIPELINE_FAILED)

    prod = prod_jobs.primary(jobs)
    if not prod:
        return DeployState(model.NO_PROD_JOB)
    statuses = {j.get("status") for j in prod}
    if "failed" in statuses:
        return DeployState(model.PROD_FAILED)
    if statuses & PROD_DEPLOYING:
        return DeployState(model.PROD_DEPLOYING)
    if statuses & {"manual", "created", "skipped"}:
        return DeployState(model.AWAITING_TRIGGER, url=trigger_url)
    return DeployState(model.PROD_OTHER, ", ".join(sorted(s for s in statuses if s)))


def after_merge[T](
    find: Callable[[], T | None], sleep: Callable[[float], None], attempts: int = 3
) -> T | None:
    """What `find` finds for a merge commit. Right after the merge, its pipeline
    may not exist yet, so it is asked a few times."""
    for attempt in range(attempts):
        if (found := find()) is not None:
            return found
        if attempt < attempts - 1:
            sleep(2)
    return None


def judge_deployments[B: Hashable, F](
    deployments: dict[str, dict],
    branch: Callable[[dict], B],
    frontier: Callable[[B], F | None],
    evaluate: Callable[[dict, F | None], DeployState],
) -> dict[str, DeployState]:
    """Where each tracked deployment stands, judged per branch against one frontier.

    The frontier is the newest point of the branch whose primary production job
    succeeded. A branch's history is linear, so a change merged at or before it
    is in production even when its own job never ran: a later deploy carries the
    earlier commits. Without a frontier — none shipped lately, or the forge did
    not answer — each deployment is judged by its own pipeline, which reports a
    failure of its own as `check failed` on its row alone.
    """
    if not deployments:
        return {}
    branches = sorted({branch(d) for d in deployments.values()})

    def known_frontier(b: B) -> F | None:
        try:
            return frontier(b)
        except ForgeError:
            return None

    with ThreadPoolExecutor(max_workers=min(8, len(branches))) as pool:
        frontiers = dict(zip(branches, pool.map(known_frontier, branches)))

    def judge(item: tuple[str, dict]) -> tuple[str, DeployState]:
        key, deployment = item
        try:
            return key, evaluate(deployment, frontiers[branch(deployment)])
        except ForgeError as e:
            return key, DeployState(model.CHECK_FAILED, str(e))

    with ThreadPoolExecutor(max_workers=min(8, len(deployments))) as pool:
        return dict(pool.map(judge, deployments.items()))
