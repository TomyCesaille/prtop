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

PROD_JOB_NAME_RE = re.compile(r"prod|production", re.I)
TASK_BRANCH_RE = re.compile(rf"^{TRIGRAM}/(.+?)-[0-9a-f]{{32}}$")

# Projects where approvals are known to survive a rebase.
# Other projects (notably portal/*) reset approvals on push.
SAFE_TO_REBASE_AFTER_APPROVAL = {
    "acme/common/frontend",
}


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


# ---------- Diff ----------

def diff_open_mrs(current: dict, previous: dict) -> list[str]:
    alerts: list[str] = []
    for mr_id, cur in sorted(current.items()):
        prev = previous.get(mr_id)
        if prev is None:
            continue
        msgs: list[str] = []

        delta = cur["note_count"] - prev.get("note_count", 0)
        if delta > 0:
            msgs.append(f"{delta} new comment{'s' if delta != 1 else ''}")

        new_approvers = sorted(set(cur["approvals"]) - set(prev.get("approvals", [])))
        if new_approvers:
            msgs.append("approved by " + ", ".join(f"@{u}" for u in new_approvers))

        if cur["fully_approved"] and not prev.get("fully_approved"):
            msgs.append("fully approved")

        if cur["merge_status"] == "need_rebase" and prev.get("merge_status") != "need_rebase":
            msgs.append("needs rebase")

        if cur["has_conflicts"] and not prev.get("has_conflicts"):
            msgs.append("has conflicts")

        if msgs:
            alerts.append(f"- `{mr_id}` — " + ", ".join(msgs))
    return alerts


def track_vanished_mrs(current: dict, previous: dict, state: dict) -> list[str]:
    """MRs that disappeared from the open list since last check: figure out
    whether they were merged, and if so start tracking their main pipeline."""
    alerts: list[str] = []
    deployments = state.setdefault("deployments", {})

    for mr_id in sorted(previous):
        if mr_id in current:
            continue
        prev = previous[mr_id]
        project_path = prev.get("project_path")
        iid = prev.get("iid")
        if not project_path or not iid:
            alerts.append(f"- `{mr_id}` — closed/merged.")
            continue

        encoded = quote(project_path, safe="")
        try:
            full = glab_api(f"projects/{encoded}/merge_requests/{iid}") or {}
        except GlabError:
            alerts.append(f"- `{mr_id}` — closed/merged (could not fetch final state).")
            continue

        if full.get("state") != "merged":
            alerts.append(f"- `{mr_id}` — {full.get('state', 'closed')} without merging.")
            continue

        sha = full.get("merge_commit_sha") or full.get("squash_commit_sha") or full.get("sha")
        pipeline = find_main_pipeline_for_sha(encoded, sha)
        if pipeline is None:
            pipelines = glab_api(f"projects/{encoded}/pipelines?ref=main&per_page=5") or []
            pipeline = pipelines[0] if pipelines else None
        if pipeline is None:
            alerts.append(f"- `{mr_id}` — merged, but no main pipeline found to track.")
            continue

        deployments[mr_id] = {
            "title": prev.get("title", ""),
            "project": encoded,
            "project_path": project_path,
            "pipeline_id": pipeline["id"],
            "pipeline_status": pipeline.get("status"),
            "pipeline_url": pipeline.get("web_url"),
        }
        alerts.append(f"- `{mr_id}` — merged; now tracking main pipeline #{pipeline['id']}.")
    return alerts


# ---------- Rendering ----------

TITLE_MAX = 60


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _render_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max([len(h)] + [len(r[i]) for r in rows]) for i, h in enumerate(headers)]

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"

    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    return "\n".join([line(headers), sep, *(line(r) for r in rows)])


def format_status_table(open_mrs: dict) -> str:
    if not open_mrs:
        return "_(no open MRs assigned)_"
    rows = [
        [
            info.get("task_name") or "—",
            _truncate(info["title"], TITLE_MAX),
            f"`{mr_id}`",
            fmt_status(info["merge_status"], info["fully_approved"]),
        ]
        for mr_id, info in sorted(open_mrs.items())
    ]
    return _render_table(["Task", "PR title", "PR", "Status"], rows)


# ---------- Action suggestions (info only — nothing is performed) ----------

def merge_notes(open_mrs: dict) -> list[str]:
    notes: list[str] = []
    for mr_id, info in sorted(open_mrs.items()):
        if info["fully_approved"] and info["merge_status"] == "mergeable":
            url = info.get("web_url") or ""
            notes.append(f"- `{mr_id}` — fully approved and mergeable; ready to merge. {url}".rstrip())
    return notes


def attention_notes(open_mrs: dict) -> list[str]:
    notes: list[str] = []
    for mr_id, info in sorted(open_mrs.items()):
        if info["has_conflicts"]:
            notes.append(f"- `{mr_id}` — has merge conflicts; manual resolution required.")
        elif info["merge_status"] != "need_rebase":
            continue
        elif (
            (info["fully_approved"] or info["approvals"])
            and info["project_path"] not in SAFE_TO_REBASE_AFTER_APPROVAL
        ):
            notes.append(
                f"- `{mr_id}` — needs rebase, but it has approvals and the project may "
                f"reset approvals on push; confirm before rebasing."
            )
        else:
            notes.append(f"- `{mr_id}` — needs rebase; safe to rebase automatically.")
    return notes


# ---------- Deployments ----------

def find_prod_job(jobs: list[dict]) -> dict | None:
    for j in jobs:
        if PROD_JOB_NAME_RE.search(j.get("name", "")):
            return j
    return None


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


def newer_main_pipeline_shipped_prod(encoded_project: str, after_pipeline_id: int) -> bool:
    pipelines = glab_api(f"projects/{encoded_project}/pipelines?ref=main&per_page=10") or []
    for p in pipelines:
        if p["id"] <= after_pipeline_id:
            continue
        jobs = glab_api(f"projects/{encoded_project}/pipelines/{p['id']}/jobs?per_page=100") or []
        prod = find_prod_job(jobs)
        if prod and prod.get("status") == "success":
            return True
    return False


def check_deployments(state: dict) -> tuple[list[str], bool]:
    """Returns (messages, action_needed)."""
    deployments = state.get("deployments") or {}
    msgs: list[str] = []
    action_needed = False
    to_remove: list[str] = []

    for mr_id, dep in deployments.items():
        try:
            project = dep["project"]
            pipeline_id = dep["pipeline_id"]
            dep.pop("stage", None)  # legacy field from the interactive era

            pipeline = glab_api(f"projects/{project}/pipelines/{pipeline_id}") or {}
            status = pipeline.get("status")
            dep["pipeline_status"] = status

            if status in PIPELINE_INFLIGHT:
                msgs.append(f"- `{mr_id}` — staging pipeline {status}.")
                continue
            if status == "failed":
                msgs.append(
                    f"- `{mr_id}` — staging pipeline **failed**! {pipeline.get('web_url', '')}"
                )
                action_needed = True
                continue
            if status != "success":
                msgs.append(f"- `{mr_id}` — staging pipeline status: {status} (unhandled).")
                continue

            jobs = glab_api(f"projects/{project}/pipelines/{pipeline_id}/jobs?per_page=100") or []
            prod = find_prod_job(jobs)
            if prod is None:
                msgs.append(f"- `{mr_id}` — deployed (staging only, no prod job).")
                to_remove.append(mr_id)
                continue

            dep["prod_job_id"] = prod["id"]
            pstatus = prod.get("status")

            if pstatus == "success":
                msgs.append(f"- `{mr_id}` — deployed to production successfully.")
                to_remove.append(mr_id)
            elif pstatus == "failed":
                msgs.append(
                    f"- `{mr_id}` — production deployment **failed**! {prod.get('web_url', '')}"
                )
                action_needed = True
            elif pstatus in PIPELINE_INFLIGHT:
                msgs.append(f"- `{mr_id}` — production deployment {pstatus}.")
            elif pstatus in ("manual", "created", "skipped"):
                if newer_main_pipeline_shipped_prod(project, pipeline_id):
                    msgs.append(
                        f"- `{mr_id}` — already shipped to production via a later main pipeline."
                    )
                    to_remove.append(mr_id)
                else:
                    msgs.append(
                        f"- `{mr_id}` — staging is healthy; production job #{prod['id']} "
                        f"awaits a manual trigger. {prod.get('web_url', '')}"
                    )
                    action_needed = True
            else:
                msgs.append(f"- `{mr_id}` — prod job status: {pstatus} (unhandled).")

        except GlabError as e:
            msgs.append(f"- `{mr_id}` — deployment check failed: {e}")
            action_needed = True

    for mr_id in to_remove:
        deployments.pop(mr_id, None)

    return msgs, action_needed


# ---------- Wall-clock alignment ----------

def sleep_until_next_tick(interval: int) -> None:
    """Sleep until the next multiple of `interval` seconds past the local hour.

    With interval=300, ticks land at :00, :05, :10, … local time (in any
    timezone offset by a whole number of minutes from UTC).
    """
    now = time.time()
    next_tick = (int(now) // interval + 1) * interval
    delay = next_tick - now
    next_dt = datetime.fromtimestamp(next_tick)
    print(
        f"\nSleeping {delay:.0f}s until next tick at {next_dt:%H:%M:%S}…",
        flush=True,
    )
    time.sleep(delay)


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
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n=== monitor-prs @ {ts} ===", flush=True)

    state = load_state()
    previous_mrs = state.get("open_mrs") or {}
    first_run = not previous_mrs

    open_mrs = gather_all_mrs()
    alerts = diff_open_mrs(open_mrs, previous_mrs)
    alerts += track_vanished_mrs(open_mrs, previous_mrs, state)

    state["open_mrs"] = strip_volatile(open_mrs)
    deploy_msgs, deploy_action_needed = check_deployments(state)

    if first_run:
        print("\nFirst run — baseline recorded. Watching:\n")
        print(format_status_table(open_mrs))
    elif alerts:
        print("\n**PR Activity Alert:**")
        print("\n".join(alerts))
        print("\nWatching:\n")
        print(format_status_table(open_mrs))
    else:
        print("\nNo new PR activity. Watching:\n")
        print(format_status_table(open_mrs))

    for header, msgs in [
        ("Ready to merge", merge_notes(open_mrs)),
        ("Needs attention", attention_notes(open_mrs)),
        ("Deployments", deploy_msgs),
    ]:
        if msgs:
            print(f"\n**{header}:**")
            print("\n".join(msgs))

    # Reflects persistent state, not just transitions: an MR that has been
    # fully-approved-and-mergeable for several cycles is still on me to merge.
    action_needed = (
        deploy_action_needed
        or any(info["has_conflicts"] for info in open_mrs.values())
        or any(info["merge_status"] == "need_rebase" for info in open_mrs.values())
        or any(
            info["fully_approved"] and info["merge_status"] == "mergeable"
            for info in open_mrs.values()
        )
    )

    save_state(state)
    print(f"\nAction needed: {'yes' if action_needed else 'no'}")
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
