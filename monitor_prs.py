#!/usr/bin/env python3
"""
Report on GitLab MRs assigned to jdoe and their deployment pipelines.

Data-gathering engine behind the /monitor-prs Claude Code skill.

Talks to GitLab only via the `glab` CLI; no LLM tokens consumed.
Strictly read-only against GitLab: it never merges, rebases, or triggers
jobs — it prints what changed and what awaits action, for the calling LLM
(or human) to act on. Runs one check and exits by default; `--loop` repeats
on a wall-clock-aligned interval for standalone terminal use.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


# ---------- Configuration ----------

GITLAB_USERNAME = "jdoe"
GITLAB_GROUP = "acme"
TRIGRAM = "dev"
STATE_FILE = Path(
    os.environ.get("PR_MONITOR_STATE_FILE")
    or Path.home() / ".claude" / "pr-monitor-state.json"
)
DEFAULT_LOOP_INTERVAL = 300

PIPELINE_INFLIGHT = {
    "running", "pending", "preparing", "created",
    "scheduled", "waiting_for_resource",
}

# A prod deploy *job* is only actively shipping in these states. `created` is
# excluded: it means the job is blocked behind an untriggered gate, i.e. still
# awaiting, not deploying.
PROD_DEPLOYING = {"running", "pending", "preparing", "scheduled", "waiting_for_resource"}

# Matches any production *deploy* job (deploy_production, deploy-production,
# deploy_production_heavy_duty, …) while excluding prod test/migrate/route jobs.
DEPLOY_PROD_JOB_RE = re.compile(r"deploy.*prod|prod.*deploy", re.I)
# The canonical prod deploy job, without the auxiliary-service suffixes
# (_heavy_duty, _certificates_exporter, …) that are deployed separately and
# often left un-triggered. Its success is what marks "shipped to production".
PRIMARY_PROD_DEPLOY_RE = re.compile(r"^deploy[_-]production$", re.I)
TASK_BRANCH_RE = re.compile(rf"^{TRIGRAM}/(.+?)-[0-9a-f]{{32}}$")


# ---------- Subprocess helpers ----------

class GlabError(RuntimeError):
    pass


def _run(cmd: list[str]) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise GlabError(
            f"Command failed: {shlex.join(cmd)}\n{res.stderr.strip() or res.stdout.strip()}"
        )
    return res.stdout


def glab_api(path: str) -> Any:
    out = _run(["glab", "api", path])
    return json.loads(out) if out.strip() else None


# ---------- State ----------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            print(f"warning: {STATE_FILE} is malformed; starting fresh.", file=sys.stderr)
    return {"open_mrs": {}, "deployments": {}, "last_checked": None}


def save_state(state: dict) -> None:
    state["last_checked"] = datetime.now(timezone.utc).isoformat()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


# ---------- Domain helpers ----------

def extract_task_name(branch: str | None) -> str:
    if not branch:
        return ""
    m = TASK_BRANCH_RE.match(branch)
    return m.group(1).replace("-", " ") if m else ""


def project_path_from_summary(mr_summary: dict) -> str | None:
    refs_full = (mr_summary.get("references") or {}).get("full")
    if refs_full and "!" in refs_full:
        return refs_full.split("!", 1)[0]
    web_url = mr_summary.get("web_url") or ""
    m = re.search(r"https?://[^/]+/(.+?)/-/merge_requests/", web_url)
    return m.group(1) if m else None


def mr_short_id(project_path: str, iid: int) -> str:
    return f"{project_path.rsplit('/', 1)[-1]}!{iid}"


_STATUS_EMOJI = {
    "draft_status": "📝",
    "not_approved": "👀",
    "need_rebase": "🔁",
    "conflict": "⚠️",
    "ci_still_running": "⏳",
    "ci_must_pass": "⏳",
    "checking": "🔍",
    "unchecked": "🔍",
    "discussions_not_resolved": "💬",
    "blocked_status": "🚧",
    "requested_changes": "🔴",
}


def status_emoji(merge_status: str, fully_approved: bool) -> str:
    if merge_status == "mergeable":
        return "🚀" if fully_approved else "👀"
    return _STATUS_EMOJI.get(merge_status, "🔹")


def fmt_status(merge_status: str, fully_approved: bool) -> str:
    # `draft_status` and `not_approved` already encode the approval state — no
    # need to tack on a redundant qualifier.
    if merge_status in ("draft_status", "not_approved"):
        label = merge_status
    else:
        label = f"{merge_status} + {'fully_approved' if fully_approved else 'not_approved'}"
    return f"{status_emoji(merge_status, fully_approved)} {label}"


# ---------- Fetch & gather ----------

def fetch_open_mr_summaries() -> list[dict]:
    path = (
        f"groups/{GITLAB_GROUP}/merge_requests"
        f"?assignee_username={GITLAB_USERNAME}&state=opened&per_page=100"
    )
    data = glab_api(path)
    return data if isinstance(data, list) else []


def fetch_mr_detail(mr_summary: dict) -> tuple[str, dict] | None:
    project_path = project_path_from_summary(mr_summary)
    if not project_path:
        return None
    iid = mr_summary["iid"]
    encoded = quote(project_path, safe="")
    base = f"projects/{encoded}/merge_requests/{iid}"

    full = glab_api(base)
    approvals = glab_api(f"{base}/approvals")

    approvers = [
        (a.get("user") or {}).get("username")
        for a in (approvals.get("approved_by") or [])
    ]
    approvers = [u for u in approvers if u]

    info = {
        "title": full.get("title", ""),
        "task_name": extract_task_name(full.get("source_branch")),
        "note_count": full.get("user_notes_count", 0),
        "state": full.get("state"),
        "approvals": approvers,
        "fully_approved": bool(approvals.get("approved")),
        "has_conflicts": bool(full.get("has_conflicts")),
        "merge_status": full.get("detailed_merge_status", "unknown"),
        "project_path": project_path,
        "iid": iid,
        "web_url": full.get("web_url"),
        "last_checked": datetime.now(timezone.utc).isoformat(),
        # Transient (stripped before save):
        "_project_encoded": encoded,
    }
    return mr_short_id(project_path, iid), info


def gather_all_mrs() -> dict[str, dict]:
    summaries = fetch_open_mr_summaries()
    if not summaries:
        return {}
    out: dict[str, dict] = {}
    workers = min(8, len(summaries))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(fetch_mr_detail, summaries):
            if result is None:
                continue
            short_id, info = result
            out[short_id] = info
    return out


def strip_volatile(open_mrs: dict) -> dict:
    return {
        k: {kk: vv for kk, vv in info.items() if not kk.startswith("_")}
        for k, info in open_mrs.items()
    }


# ---------- Track merges into deployment tracking ----------

def track_vanished_mrs(current: dict, previous: dict, state: dict) -> None:
    """MRs that dropped off the open list since last check: if one merged, start
    tracking its main pipeline so it shows up in the awaiting-deploy table."""
    deployments = state.setdefault("deployments", {})

    for mr_id in sorted(previous):
        if mr_id in current:
            continue
        prev = previous[mr_id]
        project_path = prev.get("project_path")
        iid = prev.get("iid")
        if not project_path or not iid:
            continue

        encoded = quote(project_path, safe="")
        try:
            full = glab_api(f"projects/{encoded}/merge_requests/{iid}") or {}
        except GlabError:
            continue

        if full.get("state") != "merged":
            continue

        sha = full.get("merge_commit_sha") or full.get("squash_commit_sha") or full.get("sha")
        pipeline = find_main_pipeline_for_sha(encoded, sha)
        if pipeline is None:
            pipelines = glab_api(f"projects/{encoded}/pipelines?ref=main&per_page=5") or []
            pipeline = pipelines[0] if pipelines else None
        if pipeline is None:
            continue

        deployments[mr_id] = {
            "title": prev.get("title", ""),
            "task_name": prev.get("task_name", ""),
            "project": encoded,
            "project_path": project_path,
            "pipeline_id": pipeline["id"],
            "pipeline_status": pipeline.get("status"),
            "pipeline_url": pipeline.get("web_url"),
        }


# ---------- Rendering ----------

TASK_MAX = 40
TITLE_MAX = 50


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _render_table(headers: list[str], rows: list[list[str] | None]) -> str:
    """`None` in `rows` renders as a horizontal rule, separating row groups."""
    body = [r for r in rows if r is not None]
    widths = [max([len(h)] + [len(r[i]) for r in body]) for i, h in enumerate(headers)]

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"

    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    return "\n".join([line(headers), sep, *(sep if r is None else line(r) for r in rows)])


def open_mr_rows(open_mrs: dict) -> list[list[str]]:
    return [
        [
            _truncate(info.get("task_name") or "—", TASK_MAX),
            _truncate(info["title"], TITLE_MAX),
            f"`{mr_id}`",
            fmt_status(info["merge_status"], info["fully_approved"]),
        ]
        for mr_id, info in sorted(open_mrs.items())
    ]


# ---------- Deployments ----------

def find_main_pipeline_for_sha(encoded_project: str, sha: str | None, *, retries: int = 3) -> dict | None:
    if not sha:
        return None
    for attempt in range(retries):
        pipelines = glab_api(f"projects/{encoded_project}/pipelines?ref=main&per_page=10") or []
        for p in pipelines:
            if p.get("sha") == sha:
                return p
        if attempt < retries - 1:
            time.sleep(2)
    return None


def pipeline_jobs(encoded_project: str, pipeline_id: int) -> list[dict]:
    return glab_api(f"projects/{encoded_project}/pipelines/{pipeline_id}/jobs?per_page=100") or []


def primary_prod_jobs(jobs: list[dict]) -> list[dict]:
    """The canonical prod deploy job(s) — `deploy_production`/`deploy-production`
    if present, else every prod deploy job. Auxiliary suffixed variants
    (`_heavy_duty`, `_certificates_exporter`, …) are excluded so a left-manual
    side service doesn't mask that the app itself shipped."""
    prod = [j for j in jobs if DEPLOY_PROD_JOB_RE.search(j.get("name", ""))]
    primary = [j for j in prod if PRIMARY_PROD_DEPLOY_RE.match(j.get("name", ""))]
    return primary or prod


def shipped_prod(jobs: list[dict]) -> bool:
    statuses = {j.get("status") for j in primary_prod_jobs(jobs)}
    return "success" in statuses and statuses <= {"success", "skipped"}


def latest_prod_shipped_pipeline_id(encoded_project: str) -> int | None:
    """Highest `main` pipeline id whose primary production deploy succeeded.

    Because `main` is linear, any commit merged at or before that pipeline is
    live in production — a later commit shipping carries the earlier ones with
    it. Returns None when no recent pipeline has shipped prod (or the project
    has no prod deploy job at all)."""
    pipelines = glab_api(f"projects/{encoded_project}/pipelines?ref=main&per_page=20") or []
    for p in sorted(pipelines, key=lambda x: x["id"], reverse=True):
        if shipped_prod(pipeline_jobs(encoded_project, p["id"])):
            return p["id"]
    return None


def pending_deploy_status(pipeline_status: str, jobs: list[dict], dep: dict) -> str:
    """Human status for an MR that is merged but not yet in production."""
    if pipeline_status in PIPELINE_INFLIGHT:
        return f"⏳ pipeline {pipeline_status}"
    if pipeline_status == "failed":
        return "❌ pipeline failed"

    prod = primary_prod_jobs(jobs)
    if not prod:
        return f"🔹 {pipeline_status}, no prod job"
    dep["prod_job_id"] = prod[0]["id"]
    statuses = {j.get("status") for j in prod}
    if "failed" in statuses:
        return "❌ prod deploy failed"
    if statuses & PROD_DEPLOYING:
        return "🚀 prod deploying"
    if statuses & {"manual", "created", "skipped"}:
        return "🕹️ awaiting prod trigger"
    return "🔹 prod " + ", ".join(sorted(s for s in statuses if s))


def _evaluate_deployment(item: tuple[str, dict, int | None]) -> tuple[str, bool, list[str] | None]:
    """Returns (mr_id, is_in_production, table_row_or_None)."""
    mr_id, dep, frontier = item
    dep.pop("stage", None)  # legacy field from the interactive era
    project = dep["project"]
    pipeline_id = dep["pipeline_id"]
    try:
        if frontier is not None and pipeline_id <= frontier:
            return mr_id, True, None
        pipeline = glab_api(f"projects/{project}/pipelines/{pipeline_id}") or {}
        status = pipeline.get("status")
        dep["pipeline_status"] = status
        jobs = pipeline_jobs(project, pipeline_id)
        # Safety net for a prod deploy that fell outside the frontier's window.
        if shipped_prod(jobs):
            return mr_id, True, None
        deploy_status = pending_deploy_status(status, jobs, dep)
    except GlabError as e:
        deploy_status = f"⚠️ check failed: {e}"

    row = [
        _truncate(dep.get("task_name") or "—", TASK_MAX),
        _truncate(dep.get("title", ""), TITLE_MAX),
        f"`{mr_id}`",
        deploy_status,
    ]
    return mr_id, False, row


def check_deployments(state: dict) -> list[list[str]]:
    """Refresh deployment tracking and return table rows for MRs that are
    merged but not yet in production, sorted by MR id.

    Deployed MRs are dropped from tracking. Production is judged per project
    against a single frontier (see `latest_prod_shipped_pipeline_id`), so an MR
    already carried to prod by a later commit is correctly treated as shipped.
    """
    deployments = state.get("deployments") or {}
    if not deployments:
        return []

    projects = sorted({dep["project"] for dep in deployments.values()})
    with ThreadPoolExecutor(max_workers=min(8, len(projects))) as pool:
        frontiers = dict(zip(projects, pool.map(latest_prod_shipped_pipeline_id, projects)))

    items = [(mr_id, dep, frontiers.get(dep["project"])) for mr_id, dep in deployments.items()]
    pending: list[tuple[str, list[str]]] = []
    to_remove: list[str] = []
    with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
        for mr_id, in_prod, row in pool.map(_evaluate_deployment, items):
            if in_prod:
                to_remove.append(mr_id)
            elif row is not None:
                pending.append((mr_id, row))

    for mr_id in to_remove:
        deployments.pop(mr_id, None)

    return [row for _, row in sorted(pending)]


# ---------- Wall-clock alignment ----------

def next_tick_time(interval: int) -> float:
    """Epoch time of the next multiple of `interval` seconds past the local hour.

    With interval=300, ticks land at :00, :05, :10, … local time (in any
    timezone offset by a whole number of minutes from UTC).
    """
    return (int(time.time()) // interval + 1) * interval


def sleep_until_next_tick(interval: int) -> None:
    next_tick = next_tick_time(interval)
    next_dt = datetime.fromtimestamp(next_tick)
    print(f"\nnext tick @ {next_dt:%Y-%m-%d %H:%M}", flush=True)
    time.sleep(next_tick - time.time())


# ---------- iTerm tab color ----------

def set_iterm_tab_color(action_needed: bool) -> None:
    r, g, b = (255, 140, 0) if action_needed else (0, 180, 0)
    sys.stdout.write(
        f"\033]6;1;bg;red;brightness;{r}\a"
        f"\033]6;1;bg;green;brightness;{g}\a"
        f"\033]6;1;bg;blue;brightness;{b}\a"
    )
    sys.stdout.flush()


# ---------- Main run ----------

def run_once() -> None:
    state = load_state()
    previous_mrs = state.get("open_mrs") or {}

    open_mrs = gather_all_mrs()
    # Populates deployment tracking for anything merged since last check; its
    # result isn't printed — the tables are the whole report.
    track_vanished_mrs(open_mrs, previous_mrs, state)

    state["open_mrs"] = strip_volatile(open_mrs)
    open_rows = open_mr_rows(open_mrs)
    deploy_rows = check_deployments(state)
    # Open MRs on top, merged-but-not-yet-deployed at the bottom (deploy status
    # in the same Status column), with a separator rule between the two groups
    # when both are present.
    rows: list[list[str] | None] = list(open_rows)
    if deploy_rows:
        if open_rows:
            rows.append(None)
        rows.extend(deploy_rows)

    print()
    if rows:
        print(_render_table(["Task", "PR title", "PR", "Status"], rows))
    else:
        print("_(no open MRs or pending deploys)_")

    # Action needed tracks only what's on me for an open PR — merge, rebase, or
    # resolve conflicts. Deployment progress is reported above but never flips
    # this flag. Reflects persistent state, not just transitions: an MR that has
    # been fully-approved-and-mergeable for several cycles is still on me.
    action_needed = (
        any(info["has_conflicts"] for info in open_mrs.values())
        or any(info["merge_status"] == "need_rebase" for info in open_mrs.values())
        or any(
            info["fully_approved"] and info["merge_status"] == "mergeable"
            for info in open_mrs.values()
        )
    )

    save_state(state)
    set_iterm_tab_color(action_needed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--loop", type=int, nargs="?", const=DEFAULT_LOOP_INTERVAL, default=None,
        metavar="SECONDS",
        help=(
            "Repeat forever, aligned to wall-clock ticks "
            f"(default {DEFAULT_LOOP_INTERVAL}s). Ctrl+C to stop."
        ),
    )
    args = parser.parse_args()

    if args.loop is None:
        try:
            run_once()
        except GlabError as e:
            print(f"glab error: {e}", file=sys.stderr)
            return 1
        return 0

    print(f"Looping every {args.loop}s, aligned to wall-clock ticks. Ctrl+C to stop.")
    try:
        # Kick off with an immediate check — unless the next aligned tick is
        # under 2s away, in which case just wait for it rather than running
        # twice back-to-back.
        if next_tick_time(args.loop) - time.time() < 2:
            sleep_until_next_tick(args.loop)
        while True:
            try:
                run_once()
            except GlabError as e:
                print(f"glab error (continuing): {e}", file=sys.stderr)
            sleep_until_next_tick(args.loop)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
