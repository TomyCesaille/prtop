#!/usr/bin/env python3
"""
Monitor GitLab merge requests assigned to jdoe and track production
deployments. Local Python port of the /monitor-prs Claude Code skill.

Talks to GitLab only via the `glab` CLI; no LLM tokens consumed.
Shares the same state file as the original skill so the two can be used
interchangeably.
"""

from __future__ import annotations

import argparse
import json
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
STATE_FILE = Path.home() / ".claude" / "pr-monitor-state.json"
DEFAULT_LOOP_INTERVAL = 300

PIPELINE_INFLIGHT = {
    "running", "pending", "preparing", "created",
    "scheduled", "waiting_for_resource",
}

PROD_JOB_NAME_RE = re.compile(r"prod|production", re.I)
TASK_BRANCH_RE = re.compile(rf"^{TRIGRAM}/(.+?)-[0-9a-f]{{32}}$")

# Projects where approvals are known to survive a rebase.
# Other projects (notably portal/*) reset approvals on push, so we ask first.
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


def glab_api(path: str, *, method: str = "GET") -> Any:
    cmd = ["glab", "api"]
    if method != "GET":
        cmd += ["--method", method]
    cmd.append(path)
    out = _run(cmd)
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
        "last_checked": datetime.now(timezone.utc).isoformat(),
        # Transient (stripped before save):
        "_project_path": project_path,
        "_project_encoded": encoded,
        "_iid": iid,
        "_web_url": full.get("web_url"),
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

    for mr_id in sorted(previous):
        if mr_id not in current:
            alerts.append(f"- `{mr_id}` — closed/merged")
    return alerts


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


# ---------- Interactive prompts ----------

def ask_yes_no(question: str, *, interactive: bool) -> bool:
    if not interactive or not sys.stdin.isatty():
        return False
    while True:
        ans = input(f"{question} [y/N] ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("", "n", "no"):
            return False


# ---------- Special actions: rebase / merge ----------

def rebase_mr(encoded_project: str, iid: int) -> None:
    glab_api(f"projects/{encoded_project}/merge_requests/{iid}/rebase", method="PUT")


def maybe_rebase_mrs(open_mrs: dict, *, interactive: bool) -> list[str]:
    msgs: list[str] = []
    for mr_id, info in open_mrs.items():
        if info["merge_status"] != "need_rebase":
            continue
        if info["has_conflicts"]:
            msgs.append(
                f"- `{mr_id}` — needs rebase but has conflicts (manual resolution required)."
            )
            continue

        approved = info["fully_approved"] or bool(info["approvals"])
        risky = approved and info["_project_path"] not in SAFE_TO_REBASE_AFTER_APPROVAL

        if risky:
            if ask_yes_no(
                f"`{mr_id}` needs rebase but has approvals; project may wipe approvals on push. "
                "Rebase anyway? 🔁",
                interactive=interactive,
            ):
                rebase_mr(info["_project_encoded"], info["_iid"])
                msgs.append(f"- `{mr_id}` — rebase requested.")
            else:
                msgs.append(
                    f"- `{mr_id}` — needs rebase; **skipping** to avoid losing approvals."
                )
            continue

        rebase_mr(info["_project_encoded"], info["_iid"])
        msgs.append(f"- `{mr_id}` — rebase requested.")
    return msgs


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


def merge_and_track(mr_id: str, info: dict, state: dict) -> str:
    encoded = info["_project_encoded"]
    iid = info["_iid"]
    project_path = info["_project_path"]

    merge_resp = glab_api(f"projects/{encoded}/merge_requests/{iid}/merge", method="PUT") or {}
    target_sha = merge_resp.get("merge_commit_sha") or merge_resp.get("sha")

    pipeline = find_main_pipeline_for_sha(encoded, target_sha)
    if pipeline is None:
        pipelines = glab_api(f"projects/{encoded}/pipelines?ref=main&per_page=5") or []
        pipeline = pipelines[0] if pipelines else None

    if pipeline is None:
        return f"- `{mr_id}` — merged, but could not find a main pipeline to track."

    state.setdefault("deployments", {})[mr_id] = {
        "title": info["title"],
        "project": encoded,
        "project_path": project_path,
        "pipeline_id": pipeline["id"],
        "pipeline_status": pipeline.get("status"),
        "pipeline_url": pipeline.get("web_url"),
        "stage": "staging",
    }
    return f"- `{mr_id}` — merged, tracking pipeline #{pipeline['id']}."


def maybe_merge_fully_approved(
    open_mrs: dict, previous: dict, state: dict, *, interactive: bool
) -> list[str]:
    msgs: list[str] = []
    merged: list[str] = []

    for mr_id, info in list(open_mrs.items()):
        if not info["fully_approved"]:
            continue
        prev = previous.get(mr_id) or {}
        if prev.get("fully_approved"):
            continue

        if info["merge_status"] != "mergeable":
            msgs.append(
                f"- `{mr_id}` is fully approved but merge status is "
                f"`{info['merge_status']}` — can't merge yet."
            )
            continue

        if ask_yes_no(
            f"`{mr_id}` is now fully approved. Merge it? 🚀",
            interactive=interactive,
        ):
            msgs.append(merge_and_track(mr_id, info, state))
            merged.append(mr_id)
        else:
            msgs.append(f"- `{mr_id}` is now fully approved. Want me to merge it? 🚀")

    for mr_id in merged:
        open_mrs.pop(mr_id, None)
    return msgs


# ---------- Deployments ----------

def find_prod_job(jobs: list[dict]) -> dict | None:
    for j in jobs:
        if PROD_JOB_NAME_RE.search(j.get("name", "")):
            return j
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


def trigger_prod_job(encoded_project: str, job_id: int) -> None:
    glab_api(f"projects/{encoded_project}/jobs/{job_id}/play", method="POST")


def check_deployments(state: dict, *, interactive: bool) -> tuple[list[str], bool]:
    """Returns (messages, action_needed)."""
    deployments = state.get("deployments") or {}
    if not deployments:
        return [], False

    msgs: list[str] = []
    action_needed = False
    to_remove: list[str] = []

    for mr_id, dep in list(deployments.items()):
        try:
            project = dep["project"]
            pipeline_id = dep["pipeline_id"]
            stage = dep.get("stage", "staging")

            pipeline = glab_api(f"projects/{project}/pipelines/{pipeline_id}") or {}
            status = pipeline.get("status")
            dep["pipeline_status"] = status

            if stage == "staging":
                if status in PIPELINE_INFLIGHT:
                    continue
                if status == "failed":
                    msgs.append(
                        f"- `{mr_id}` staging pipeline **failed**! {pipeline.get('web_url', '')}"
                    )
                    action_needed = True
                    continue
                if status != "success":
                    msgs.append(f"- `{mr_id}` staging pipeline status: {status} (unhandled)")
                    continue

                jobs = glab_api(
                    f"projects/{project}/pipelines/{pipeline_id}/jobs?per_page=100"
                ) or []
                prod = find_prod_job(jobs)
                if prod is None:
                    msgs.append(f"- `{mr_id}` deployed (staging only, no prod job).")
                    to_remove.append(mr_id)
                    continue

                dep["prod_job_id"] = prod["id"]
                pstatus = prod.get("status")

                if pstatus == "success":
                    msgs.append(f"- `{mr_id}` deployed to production successfully.")
                    to_remove.append(mr_id)
                    continue
                if pstatus == "failed":
                    msgs.append(
                        f"- `{mr_id}` production deployment **failed**! "
                        f"{prod.get('web_url', '')}"
                    )
                    action_needed = True
                    continue
                if pstatus in ("manual", "created", "skipped"):
                    if newer_main_pipeline_shipped_prod(project, pipeline_id):
                        msgs.append(
                            f"- `{mr_id}` already shipped to production via a later main pipeline."
                        )
                        to_remove.append(mr_id)
                        continue
                    dep["stage"] = "staging_done"
                    msgs.append(
                        f"- `{mr_id}` — staging is healthy. "
                        f"Want me to trigger production deployment? 🚀"
                    )
                    action_needed = True
                    if ask_yes_no(f"Trigger production deployment for `{mr_id}`? 🚀",
                                  interactive=interactive):
                        trigger_prod_job(project, prod["id"])
                        dep["stage"] = "prod_approved"
                        msgs.append(f"- `{mr_id}` — production job triggered.")
                    continue

                msgs.append(f"- `{mr_id}` prod job status: {pstatus} (unhandled)")
                continue

            if stage == "staging_done":
                if ask_yes_no(
                    f"`{mr_id}` staging done. Trigger production deployment now? 🚀",
                    interactive=interactive,
                ):
                    job_id = dep.get("prod_job_id")
                    if not job_id:
                        jobs = glab_api(
                            f"projects/{project}/pipelines/{pipeline_id}/jobs?per_page=100"
                        ) or []
                        prod = find_prod_job(jobs)
                        if prod:
                            job_id = prod["id"]
                            dep["prod_job_id"] = job_id
                    if job_id:
                        trigger_prod_job(project, job_id)
                        dep["stage"] = "prod_approved"
                        msgs.append(f"- `{mr_id}` — production job triggered.")
                    else:
                        msgs.append(f"- `{mr_id}` — could not find manual production job.")
                        action_needed = True
                else:
                    msgs.append(
                        f"- `{mr_id}` — still waiting for your approval to deploy to production."
                    )
                    action_needed = True
                continue

            if stage == "prod_approved":
                job_id = dep.get("prod_job_id")
                if not job_id:
                    msgs.append(f"- `{mr_id}` — no prod_job_id in state; can't track.")
                    action_needed = True
                    continue
                job = glab_api(f"projects/{project}/jobs/{job_id}") or {}
                jstatus = job.get("status")
                if jstatus == "success":
                    msgs.append(f"- `{mr_id}` deployed to production successfully.")
                    to_remove.append(mr_id)
                elif jstatus == "failed":
                    msgs.append(
                        f"- `{mr_id}` production deployment **failed**! "
                        f"{job.get('web_url', '')}"
                    )
                    action_needed = True
                # else: still running, stay quiet
                continue

            msgs.append(f"- `{mr_id}` — unknown stage `{stage}`.")

        except GlabError as e:
            msgs.append(f"- `{mr_id}` deployment check failed: {e}")
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

def run_once(*, interactive: bool) -> None:
    state = load_state()
    previous_mrs = state.get("open_mrs") or {}
    first_run = not previous_mrs

    open_mrs = gather_all_mrs()
    alerts = diff_open_mrs(open_mrs, previous_mrs)

    merge_msgs = maybe_merge_fully_approved(
        open_mrs, previous_mrs, state, interactive=interactive
    )
    rebase_msgs = maybe_rebase_mrs(open_mrs, interactive=interactive)

    state["open_mrs"] = strip_volatile(open_mrs)
    deploy_msgs, deploy_action_needed = check_deployments(state, interactive=interactive)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n=== monitor-prs @ {ts} ===")

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
        ("Approvals / merge", merge_msgs),
        ("Rebase", rebase_msgs),
        ("Deployments", deploy_msgs),
    ]:
        if msgs:
            print(f"\n**{header}:**")
            print("\n".join(msgs))

    # Tab colour reflects persistent state, not just transitions: an MR that
    # has been fully-approved-and-mergeable for several cycles is still on me
    # to merge.
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
    set_iterm_tab_color(action_needed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--interval", type=int, default=DEFAULT_LOOP_INTERVAL,
        help=f"Seconds between iterations (default {DEFAULT_LOOP_INTERVAL}).",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single iteration and exit (debug/testing escape hatch).",
    )
    parser.add_argument(
        "--no-interactive", action="store_true",
        help="Never prompt — read-only / report-only.",
    )
    args = parser.parse_args()

    interactive = sys.stdin.isatty() and not args.no_interactive

    if args.once:
        try:
            run_once(interactive=interactive)
        except GlabError as e:
            print(f"glab error: {e}", file=sys.stderr)
            return 1
        return 0

    print(f"Looping every {args.interval}s, aligned to wall-clock ticks. Ctrl+C to stop.")
    try:
        while True:
            try:
                run_once(interactive=interactive)
            except GlabError as e:
                print(f"glab error (continuing): {e}", file=sys.stderr)
            sleep_until_next_tick(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
