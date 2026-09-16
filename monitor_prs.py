#!/usr/bin/env python3
"""
Report on the GitLab MRs assigned to the user and their deployment pipelines.

Covers both instances the company runs — gitlab.com and the self-hosted
gitlab.example.com the repos are migrating to (see `HOSTS`).

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
import unicodedata
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote


# ---------- Configuration ----------


class Host(NamedTuple):
    """A GitLab instance, and who the user is on it.

    `group` is the root group the MR list is taken from; the group endpoint
    covers its subgroups.
    """

    name: str
    username: str
    group: str


# Repos are moving from gitlab.com to the self-hosted instance one at a time, so
# both are live. The self-hosted scope is the subgroup holding the repos that
# have already moved, not the `acme` root: a repo mid-migration has an open
# MR on each side, and a root-level scope would report that MR twice. Widen it as
# more repos land.
HOSTS = (
    Host("gitlab.com", "jdoe", "acme"),
    Host("gitlab.example.com", "john.doe", "acme/portal"),
)
DEFAULT_HOST = HOSTS[0]

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
# The Notion hash ends the task slug, but a branch may carry a per-MR suffix
# after it (`…-mr1-theme-tokens`) when one task is split across a stack of
# MRs. The suffix stays out of the captured name so those MRs share one task.
TASK_BRANCH_RE = re.compile(rf"^{TRIGRAM}/(.+?)-[0-9a-f]{{32}}(?:-.+)?$")


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


def glab_api(host: str, path: str) -> Any:
    # `--hostname` is passed even for gitlab.com: without it `glab` resolves the
    # host from the git repo the working directory happens to sit in.
    out = _run(["glab", "api", "--hostname", host, path])
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
    """The `repo!iid` the table shows."""
    return f"{project_path.rsplit('/', 1)[-1]}!{iid}"


def mr_key(host: str, project_path: str, iid: int) -> str:
    """State-file key. The short id alone is ambiguous while a repo is
    mid-migration and carries the same MR number on both instances."""
    return f"{host}:{mr_short_id(project_path, iid)}"


def unresolved_threads_await_user(discussions: list[dict], username: str) -> bool:
    """Whether an unresolved thread carries a note from anyone but the user.

    A thread he opened on his own MR and nobody answered is a hint he left for
    the reviewer; GitLab counts it unresolved all the same. One note from someone
    else in the thread makes it feedback he has to answer. `username` is his
    handle on the instance the MR lives on — it differs between the two.
    """
    for discussion in discussions:
        notes = [n for n in discussion.get("notes") or [] if not n.get("system")]
        if not any(n.get("resolvable") and not n.get("resolved") for n in notes):
            continue
        if any((n.get("author") or {}).get("username") != username for n in notes):
            return True
    return False


# The severity mark is the primary status channel: plain ASCII, one column wide,
# and legible regardless of color, glyph rendering, or font size. Rows sort by
# it, so whatever is on the user sits at the top of the table. The four marks also
# read as a weight gradient — `!` is the heaviest shape, `.` the lightest — so
# the eye lands on the top of the table without resolving the characters.
ACT = "!"      # on you now
ANSWER = "?"   # on you to look at or reply to
PEOPLE = "~"   # on someone else, and it will sit there until a person moves
RUNNING = "."  # on a machine, and it resolves by itself

_MARK_ORDER = {ACT: 0, ANSWER: 1, PEOPLE: 2, RUNNING: 3}

# Glyphs are a second, redundant channel. A glyph never carries two different
# meanings across the open-MR and awaiting-deploy halves of the shared Status
# column — ⏳ spans both only because a pipeline in flight is one state — and
# silhouettes are picked to stay distinguishable when a terminal drops the color
# and renders them as monochrome outlines.
#
# The label is the state in plain words, not GitLab's `detailed_merge_status`
# enum: the report is read at a glance, and a label two words long leaves the
# Task column the width it needs. Keep a new one inside `STATUS_WIDTH`.
_MERGE_STATUS = {
    "draft_status":             (PEOPLE, "📝", "draft"),
    "not_approved":             (PEOPLE, "👀", "awaiting review"),
    "need_rebase":              (ACT, "🔁", "rebase needed"),
    "conflict":                 (ACT, "💥", "conflicts"),
    "ci_still_running":         (RUNNING, "⏳", "CI running"),
    "ci_must_pass":             (RUNNING, "⏳", "CI must pass"),
    "checking":                 (RUNNING, "🔍", "merge check running"),
    "unchecked":                (RUNNING, "🔍", "merge check pending"),
    "discussions_not_resolved": (ANSWER, "💬", "unresolved threads"),
    # Not one of GitLab's statuses: `fmt_status` substitutes it when every
    # unresolved thread is one the user opened and nobody answered.
    "own_threads_unresolved":   (ACT, "🧵", "resolve own threads"),
    "blocked_status":           (PEOPLE, "🚧", "blocked"),
    "requested_changes":        (ANSWER, "⛔", "changes requested"),
}

# These two already name the approval state; the suffix would repeat it.
_APPROVAL_IMPLIED = ("draft_status", "not_approved")


def status_signal(merge_status: str, fully_approved: bool) -> tuple[str, str, str]:
    # An unmapped status is a gap in this table, not a quiet state: surface it at
    # `?`, under its raw enum, so a merge status GitLab adds later can't sit
    # unnoticed at the bottom.
    if merge_status == "mergeable":
        return (ACT, "✅", "mergeable") if fully_approved else (PEOPLE, "👀", "mergeable")
    return _MERGE_STATUS.get(merge_status, (ANSWER, "❔", merge_status))


def fmt_status(
    merge_status: str,
    fully_approved: bool,
    has_conflicts: bool,
    threads_await_user: bool = True,
) -> tuple[str, str]:
    """(severity mark, Status cell) for an open MR."""
    # GitLab sets `has_conflicts` independently of `detailed_merge_status`, which
    # can still read `not_approved` on a conflicting MR. Conflicts win: they are
    # on the user whatever the review state says.
    if has_conflicts:
        merge_status = "conflict"
    elif merge_status == "discussions_not_resolved" and not threads_await_user:
        # A thread the user opened and nobody answered is a hint he left for the
        # reviewer. It blocks the merge once the MR is approved; before that,
        # the review is what the MR actually waits on.
        merge_status = "own_threads_unresolved" if fully_approved else "not_approved"
    mark, glyph, label = status_signal(merge_status, fully_approved)
    if merge_status not in _APPROVAL_IMPLIED:
        label += ", approved" if fully_approved else ", unapproved"
    return mark, f"{glyph} {label}"


# ---------- Fetch & gather ----------

def fetch_open_mr_summaries(host: Host) -> list[tuple[Host, dict]]:
    path = (
        f"groups/{quote(host.group, safe='')}/merge_requests"
        f"?assignee_username={host.username}&state=opened&per_page=100"
    )
    data = glab_api(host.name, path)
    return [(host, mr) for mr in data] if isinstance(data, list) else []


def fetch_discussions(host: str, base: str) -> list[dict]:
    """Every discussion on the MR, up to ten pages — a CodeRabbit-reviewed MR
    runs past one, and a single reviewer thread anywhere in them decides the
    status."""
    out: list[dict] = []
    for page in range(1, 11):
        batch = glab_api(host, f"{base}/discussions?per_page=100&page={page}") or []
        out.extend(batch)
        if len(batch) < 100:
            break
    return out


def fetch_mr_detail(item: tuple[Host, dict]) -> tuple[str, dict | None]:
    host, mr_summary = item
    project_path = project_path_from_summary(mr_summary)
    if not project_path:
        return "", None
    iid = mr_summary["iid"]
    encoded = quote(project_path, safe="")
    base = f"projects/{encoded}/merge_requests/{iid}"

    full = glab_api(host.name, base)
    approvals = glab_api(host.name, f"{base}/approvals")

    approvers = [
        (a.get("user") or {}).get("username")
        for a in (approvals.get("approved_by") or [])
    ]
    approvers = [u for u in approvers if u]

    merge_status = full.get("detailed_merge_status", "unknown")
    # The extra call earns its place only when threads are what blocks the merge.
    threads_await_user = True
    if merge_status == "discussions_not_resolved":
        threads_await_user = unresolved_threads_await_user(
            fetch_discussions(host.name, base), host.username
        )

    info = {
        "title": full.get("title", ""),
        "task_name": extract_task_name(full.get("source_branch")),
        "note_count": full.get("user_notes_count", 0),
        "state": full.get("state"),
        "approvals": approvers,
        "fully_approved": bool(approvals.get("approved")),
        "has_conflicts": bool(full.get("has_conflicts")),
        "merge_status": merge_status,
        "threads_await_user": threads_await_user,
        "host": host.name,
        "short_id": mr_short_id(project_path, iid),
        "project_path": project_path,
        "iid": iid,
        "web_url": full.get("web_url"),
        "last_checked": datetime.now(timezone.utc).isoformat(),
        # Transient (stripped before save):
        "_project_encoded": encoded,
    }
    return mr_key(host.name, project_path, iid), info



class HostFailure(NamedTuple):
    """An instance this run could not read, and what `glab` said."""

    host: str
    error: str


def gather_host_mrs(host: Host) -> tuple[dict[str, dict], str | None]:
    """Every open MR on one instance, or the error that stopped the attempt.

    Listing and hydration are both inside the guard: an instance can go down
    between the two, and either way the answer for this host is "unknown".
    """
    try:
        summaries = fetch_open_mr_summaries(host)
        if not summaries:
            return {}, None
        with ThreadPoolExecutor(max_workers=min(8, len(summaries))) as pool:
            results = list(pool.map(fetch_mr_detail, summaries))
    except GlabError as e:
        return {}, str(e)
    return {key: info for key, info in results if info is not None}, None


def gather_all_mrs() -> tuple[dict[str, dict], list[HostFailure]]:
    """Open MRs across every instance, and the instances that did not answer.

    One instance failing never blanks the other's MRs, but it is never silent
    either: `run_once` gives each failure a row of its own.
    """
    with ThreadPoolExecutor(max_workers=len(HOSTS)) as pool:
        results = list(pool.map(gather_host_mrs, HOSTS))

    out: dict[str, dict] = {}
    failures: list[HostFailure] = []
    for host, (mrs, error) in zip(HOSTS, results):
        out.update(mrs)
        if error is not None:
            failures.append(HostFailure(host.name, error))
    return out, failures


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

        host = prev.get("host", DEFAULT_HOST.name)
        encoded = quote(project_path, safe="")
        try:
            full = glab_api(host, f"projects/{encoded}/merge_requests/{iid}") or {}
        except GlabError:
            continue

        if full.get("state") != "merged":
            continue

        sha = full.get("merge_commit_sha") or full.get("squash_commit_sha") or full.get("sha")
        pipeline = find_main_pipeline_for_sha(host, encoded, sha)
        if pipeline is None:
            pipelines = glab_api(host, f"projects/{encoded}/pipelines?ref=main&per_page=5") or []
            pipeline = pipelines[0] if pipelines else None
        if pipeline is None:
            continue

        deployments[mr_id] = {
            "title": prev.get("title", ""),
            "task_name": prev.get("task_name", ""),
            "host": host,
            "short_id": prev.get("short_id") or mr_id,
            "project": encoded,
            "project_path": project_path,
            "iid": iid,
            "web_url": prev.get("web_url"),
            "pipeline_id": pipeline["id"],
            "pipeline_status": pipeline.get("status"),
            "pipeline_url": pipeline.get("web_url"),
        }


# ---------- Rendering ----------

TASK_MAX = 58
TITLE_MAX = 42

_OSC_RE = re.compile("\033\\][^\a]*\a")


def _hyperlink(text: str, url: str) -> str:
    """OSC 8 terminal hyperlink: renders as clickable `text` (cmd+click in
    iTerm2) with the URL invisible — unlike a markdown link, which the terminal
    shows raw."""
    return f"\033]8;;{url}\a{text}\033]8;;\a"


def _plain(text: str) -> str:
    """A cell stripped of its hyperlink escapes — the text the terminal shows."""
    return _OSC_RE.sub("", text)


def _display_width(text: str) -> int:
    """Terminal cell width: OSC escape sequences (hyperlinks) occupy no cells,
    and East-Asian-wide chars (incl. emoji) count as 2 so the emoji-bearing
    Status column stays aligned. `str.ljust` can't do either — it counts a
    single-codepoint emoji as 1 while the terminal renders it 2 cells wide."""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in _plain(text))


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _linked_mr_id(entry: dict, short_id: str) -> str:
    """The `repo!iid` cell, clickable through to the MR page. An entry written
    before `web_url` was recorded has its URL rebuilt from the host, the project
    path and the number after the `!` in the short id."""
    cell = f"`{short_id}`"
    project_path = entry.get("project_path")
    url = entry.get("web_url") or (
        f"https://{entry.get('host', DEFAULT_HOST.name)}/{project_path}"
        f"/-/merge_requests/{short_id.partition('!')[2]}"
        if project_path
        else ""
    )
    return _hyperlink(cell, url) if url else cell


class Row(NamedTuple):
    """One table line. Field order is column order — `HEADERS` labels it."""

    mark: str
    title: str
    task: str
    pr: str
    status: str


# The widest Status cell the two tables can produce
# (`🔍 merge check running, unapproved`). Flooring the column there
# holds the table at one width as MRs move between states, instead of reflowing
# the whole thing on every check. A cell carrying text no vocabulary bounds — an
# unmapped merge status, a `check failed:` message — still widens the column.
STATUS_WIDTH = 34

HEADERS = ("", "PR title", "Task", "PR", "Status")
MIN_WIDTHS = (0, 0, 0, 0, STATUS_WIDTH)


def _render_table(
    headers: Sequence[str], rows: Sequence[Row | None], min_widths: Sequence[int] = ()
) -> str:
    """`None` in `rows` renders as a horizontal rule, separating row groups.
    `min_widths` floors a column so its width survives a change of content."""
    body = [r for r in rows if r is not None]

    def width(i: int, header: str) -> int:
        floor = min_widths[i] if i < len(min_widths) else 0
        return max([floor, _display_width(header)] + [_display_width(r[i]) for r in body])

    widths = [width(i, h) for i, h in enumerate(headers)]

    def pad(cell: str, w: int) -> str:
        return cell + " " * (w - _display_width(cell))

    def line(cells: Sequence[str]) -> str:
        return "| " + " | ".join(pad(c, w) for c, w in zip(cells, widths)) + " |"

    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    return "\n".join([line(headers), sep, *(sep if r is None else line(r) for r in rows)])


def _sort_key(row: Row) -> tuple[int, str, str, int]:
    """Severity first, then task, repo and MR number — so the several MRs of one
    task stay adjacent inside a severity block, in repo then numeric order."""
    repo, _, iid = _plain(row.pr).strip("`").partition("!")
    # An unreachable-instance row carries no MR number.
    return _MARK_ORDER.get(row.mark, 9), row.task, repo, int(iid) if iid.isdigit() else 0


def _by_severity(rows: list[Row]) -> list[Row]:
    return sorted(rows, key=_sort_key)


def _split_by_mark(rows: list[Row]) -> list[Row | None]:
    """Rule off each severity block so the eye can land on one block at a time.
    Expects `_by_severity` order, where equal marks are already adjacent."""
    out: list[Row | None] = []
    for i, row in enumerate(rows):
        if i and row.mark != rows[i - 1].mark:
            out.append(None)
        out.append(row)
    return out


def open_mr_rows(open_mrs: dict) -> list[Row]:
    rows = []
    for key, info in open_mrs.items():
        mark, status = fmt_status(
            info["merge_status"], info["fully_approved"], info["has_conflicts"],
            info["threads_await_user"],
        )
        rows.append(Row(
            mark=mark,
            task=_truncate(info.get("task_name") or "—", TASK_MAX),
            title=_truncate(info["title"], TITLE_MAX),
            pr=_linked_mr_id(info, info.get("short_id") or key),
            status=status,
        ))
    return _by_severity(rows)
def unreachable_rows(failures: Sequence[HostFailure]) -> list[Row]:
    """One row per instance this run could not read.

    Marked `!` rather than `?`: the run has nothing to say about that instance,
    and it feeds `action_needed`, because a green tab claims "all clear" — a
    claim the run cannot make about MRs it never saw.
    """
    return [
        Row(mark=ACT, title=f.host, task="—", pr="—", status="⚠️ instance unreachable")
        for f in failures
    ]


# ---------- Deployments ----------

def find_main_pipeline_for_sha(
    host: str, encoded_project: str, sha: str | None, *, retries: int = 3
) -> dict | None:
    if not sha:
        return None
    for attempt in range(retries):
        pipelines = glab_api(host, f"projects/{encoded_project}/pipelines?ref=main&per_page=10") or []
        for p in pipelines:
            if p.get("sha") == sha:
                return p
        if attempt < retries - 1:
            time.sleep(2)
    return None


def pipeline_jobs(host: str, encoded_project: str, pipeline_id: int) -> list[dict]:
    return glab_api(host, f"projects/{encoded_project}/pipelines/{pipeline_id}/jobs?per_page=100") or []


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


def latest_prod_shipped_pipeline_id(host: str, encoded_project: str) -> int | None:
    """Highest `main` pipeline id whose primary production deploy succeeded.

    Because `main` is linear, any commit merged at or before that pipeline is
    live in production — a later commit shipping carries the earlier ones with
    it. Returns None when no recent pipeline has shipped prod (or the project
    has no prod deploy job at all)."""
    pipelines = glab_api(host, f"projects/{encoded_project}/pipelines?ref=main&per_page=20") or []
    for p in sorted(pipelines, key=lambda x: x["id"], reverse=True):
        if shipped_prod(pipeline_jobs(host, encoded_project, p["id"])):
            return p["id"]
    return None


def pending_deploy_status(pipeline_status: str, jobs: list[dict], dep: dict) -> tuple[str, str]:
    """(severity mark, Status cell) for an MR that is merged but not yet in
    production."""
    if pipeline_status in PIPELINE_INFLIGHT:
        return RUNNING, f"⏳ pipeline {pipeline_status}"
    if pipeline_status == "failed":
        return ACT, "❌ pipeline failed"

    prod = primary_prod_jobs(jobs)
    if not prod:
        return PEOPLE, f"➖ {pipeline_status}, no prod job"
    dep["prod_job_id"] = prod[0]["id"]
    statuses = {j.get("status") for j in prod}
    if "failed" in statuses:
        return ACT, "❌ prod deploy failed"
    if statuses & PROD_DEPLOYING:
        return RUNNING, "📦 prod deploying"
    if statuses & {"manual", "created", "skipped"}:
        commits_url = (
            f"https://{dep.get('host', DEFAULT_HOST.name)}/{dep['project_path']}"
            f"/-/commits/main?ref_type=HEADS"
        )
        return ACT, _hyperlink("🕹️ awaiting prod trigger", commits_url)
    return ANSWER, "➖ prod " + ", ".join(sorted(s for s in statuses if s))


def _evaluate_deployment(item: tuple[str, dict, int | None]) -> tuple[str, bool, Row | None]:
    """Returns (state key, is_in_production, table_row_or_None)."""
    key, dep, frontier = item
    dep.pop("stage", None)  # legacy field from the interactive era
    host = dep.get("host", DEFAULT_HOST.name)
    project = dep["project"]
    pipeline_id = dep["pipeline_id"]
    try:
        if frontier is not None and pipeline_id <= frontier:
            return key, True, None
        pipeline = glab_api(host, f"projects/{project}/pipelines/{pipeline_id}") or {}
        status = pipeline.get("status")
        dep["pipeline_status"] = status
        jobs = pipeline_jobs(host, project, pipeline_id)
        # Safety net for a prod deploy that fell outside the frontier's window.
        if shipped_prod(jobs):
            return key, True, None
        mark, deploy_status = pending_deploy_status(status, jobs, dep)
    except GlabError as e:
        mark, deploy_status = ANSWER, f"⚠️ check failed: {e}"

    row = Row(
        mark=mark,
        task=_truncate(dep.get("task_name") or "—", TASK_MAX),
        title=_truncate(dep.get("title", ""), TITLE_MAX),
        pr=_linked_mr_id(dep, dep.get("short_id") or key),
        status=deploy_status,
    )
    return key, False, row


def check_deployments(state: dict) -> list[Row]:
    """Refresh deployment tracking and return table rows for MRs that are
    merged but not yet in production, action-first.

    Deployed MRs are dropped from tracking. Production is judged per project
    against a single frontier (see `latest_prod_shipped_pipeline_id`), so an MR
    already carried to prod by a later commit is correctly treated as shipped.
    """
    deployments = state.get("deployments") or {}
    if not deployments:
        return []

    # A project is identified by (host, path): the same repo exists on both
    # instances mid-migration, with pipeline ids that share no sequence.
    projects = sorted({
        (dep.get("host", DEFAULT_HOST.name), dep["project"]) for dep in deployments.values()
    })
    def frontier(target: tuple[str, str]) -> int | None:
        # No frontier is the safe answer: evaluation falls through to the MR's
        # own pipeline, which reports its own failure as a `check failed` row.
        try:
            return latest_prod_shipped_pipeline_id(*target)
        except GlabError:
            return None

    with ThreadPoolExecutor(max_workers=min(8, len(projects))) as pool:
        frontiers = dict(zip(projects, pool.map(frontier, projects)))

    items = [
        (key, dep, frontiers.get((dep.get("host", DEFAULT_HOST.name), dep["project"])))
        for key, dep in deployments.items()
    ]
    pending: list[Row] = []
    to_remove: list[str] = []
    with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
        for key, in_prod, row in pool.map(_evaluate_deployment, items):
            if in_prod:
                to_remove.append(key)
            elif row is not None:
                pending.append(row)

    for key in to_remove:
        deployments.pop(key, None)

    return _by_severity(pending)


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

    confirmed, failures = gather_all_mrs()
    for failure in failures:
        print(f"glab error ({failure.host}): {failure.error}", file=sys.stderr)

    # An unreachable instance's MRs are unknown this run, not gone. Holding on to
    # the previous entries keeps the merge-detection baseline — an MR that merges
    # during the outage is still caught on the next run — and stops
    # `track_vanished_mrs` from reading the outage as a wave of merges. They stay
    # out of the table: a status this run could not confirm is not one to report.
    unreachable = {f.host for f in failures}
    known = dict(confirmed)
    for key, info in previous_mrs.items():
        if info.get("host", DEFAULT_HOST.name) in unreachable:
            known.setdefault(key, info)

    # Populates deployment tracking for anything merged since last check; its
    # result isn't printed — the tables are the whole report.
    track_vanished_mrs(known, previous_mrs, state)

    state["open_mrs"] = strip_volatile(known)
    # Two already-sorted lists merged into one severity order.
    open_rows = _by_severity([*unreachable_rows(failures), *open_mr_rows(confirmed)])
    deploy_rows = check_deployments(state)
    # Open MRs on top, merged-but-not-yet-deployed at the bottom (deploy status
    # in the same Status column), with a separator rule between the two groups
    # when both are present, and another between each severity block inside them.
    rows: list[Row | None] = _split_by_mark(open_rows)
    if deploy_rows:
        if open_rows:
            rows.append(None)
        rows.extend(_split_by_mark(deploy_rows))

    print()
    if rows:
        print(_render_table(HEADERS, rows, MIN_WIDTHS))
    else:
        print("_(no open MRs or pending deploys)_")

    # Action needed tracks what's on me for an open PR — merge, rebase, resolve
    # conflicts — plus an instance the run could not read, since green would
    # otherwise claim all-clear over MRs nobody saw. Deployment progress is
    # reported above but never flips this flag. Reflects persistent state, not
    # just transitions: an MR that has been fully-approved-and-mergeable for
    # several cycles is still on me.
    action_needed = any(row.mark == ACT for row in open_rows)

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
