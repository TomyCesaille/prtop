"""The `prtop` command: one check by default, `--loop` to repeat it, `watch` to
wait for the report to change."""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import NamedTuple

from prtop import __version__, model
from prtop.config import Config, ConfigError, load_config
from prtop.forges import Forge, make_forge
from prtop.model import ForgeError
from prtop.report import (
    ACT,
    Row,
    by_severity,
    deploy_row,
    open_row,
    plain,
    render_report,
    split_by_mark,
    unreachable_row,
)
from prtop.state import load_state, save_state
from prtop.tasks import TaskSource, task_source

DEFAULT_LOOP_INTERVAL = 300
DEFAULT_WATCH_CAP = 7200


class Report(NamedTuple):
    text: str
    # Whether an open MR is on you now. Deployment progress never sets it.
    action_needed: bool


def track_merges(forge: Forge, current: dict, previous: dict, deployments: dict) -> None:
    """MRs that left the open list since the last check: each one that merged
    starts being tracked on its way to production."""
    for key in sorted(previous.keys() - current.keys()):
        try:
            deployment = forge.merged_deployment(previous[key])
        except ForgeError:
            continue
        if deployment is not None:
            deployments[key] = deployment


def deployment_rows(forge: Forge, deployments: dict) -> list[Row]:
    rows = []
    for key, state in forge.deploy_states(deployments).items():
        if state.kind == model.SHIPPED:
            deployments.pop(key)
        else:
            rows.append(deploy_row(key, deployments[key], state))
    return by_severity(rows)


def run_check(config: Config, forge: Forge, tasks: TaskSource) -> Report:
    state = load_state(config.state_file)
    previous = state.get("open_mrs") or {}
    deployments = state.setdefault("deployments", {})

    try:
        open_mrs, error = forge.open_merge_requests(tasks), None
    except ForgeError as e:
        open_mrs, error = {}, e
        print(f"prtop: {forge.host}: {e}", file=sys.stderr)

    # When the forge did not answer, the open MRs are unknown this run, not gone.
    # Holding on to the previous entries keeps the merge-detection baseline — an
    # MR that merges during the outage is still caught on the next run — and
    # stops the outage from reading as a wave of merges. They stay out of the
    # table: a status this run could not confirm is not one to report.
    known = previous if error is not None else open_mrs
    if config.deploy_tracking:
        track_merges(forge, known, previous, deployments)
    state["open_mrs"] = known

    open_rows = (
        [unreachable_row(forge.host)] if error is not None
        else by_severity([open_row(key, entry) for key, entry in open_mrs.items()])
    )
    deploy_rows = deployment_rows(forge, deployments) if config.deploy_tracking else []
    save_state(config.state_file, state)

    # Open MRs on top, merged-but-not-yet-deployed below, with a rule between the
    # two groups and another between each severity block inside them.
    rows: list[Row | None] = split_by_mark(open_rows)
    if deploy_rows:
        if open_rows:
            rows.append(None)
        rows.extend(split_by_mark(deploy_rows))
    text = render_report(rows, tasks.shows_column) if rows else "No open merge requests or pending deploys."

    # A forge the run could not read counts too: its row is marked `!`, since
    # green would otherwise claim all-clear over MRs nobody saw.
    return Report(text, any(row.mark == ACT for row in open_rows))


# ---------- Terminal tab color ----------

def _is_iterm(environ: Mapping[str, str]) -> bool:
    return environ.get("TERM_PROGRAM") == "iTerm.app" or environ.get("LC_TERMINAL") == "iTerm2"


def set_tab_color(action_needed: bool, mode: str, environ: Mapping[str, str] = os.environ) -> None:
    """Orange while an open MR is on you, green otherwise — a convenience
    duplicate of the `!` block at the top of the table, never the only place a
    state is reported."""
    if mode == "off" or (mode == "auto" and not _is_iterm(environ)):
        return
    r, g, b = (255, 140, 0) if action_needed else (0, 180, 0)
    sys.stdout.write(
        f"\033]6;1;bg;red;brightness;{r}\a"
        f"\033]6;1;bg;green;brightness;{g}\a"
        f"\033]6;1;bg;blue;brightness;{b}\a"
    )
    sys.stdout.flush()


# ---------- Wall-clock alignment ----------

def next_tick_time(interval: int) -> float:
    """Epoch time of the next multiple of `interval` seconds past the local hour.

    With interval=300, ticks land at :00, :05, :10, … local time (in any
    timezone offset by a whole number of minutes from UTC).
    """
    return (int(time.time()) // interval + 1) * interval


def sleep_until_next_tick(interval: int) -> None:
    next_tick = next_tick_time(interval)
    print(f"\nnext tick @ {datetime.fromtimestamp(next_tick):%Y-%m-%d %H:%M}", flush=True)
    time.sleep(next_tick - time.time())


# ---------- Entry point ----------

def check(config: Config, forge: Forge, tasks: TaskSource) -> None:
    report = run_check(config, forge, tasks)
    print()
    print(report.text, flush=True)
    set_tab_color(report.action_needed, config.tab_color)


def loop(config: Config, forge: Forge, tasks: TaskSource, interval: int) -> int:
    print(f"Looping every {interval}s, aligned to wall-clock ticks. Ctrl+C to stop.")
    try:
        # Kick off with an immediate check — unless the next aligned tick is under
        # 2s away, in which case wait for it rather than run twice back-to-back.
        if next_tick_time(interval) - time.time() < 2:
            sleep_until_next_tick(interval)
        while True:
            try:
                check(config, forge, tasks)
            except ForgeError as e:
                print(f"prtop (continuing): {e}", file=sys.stderr)
            sleep_until_next_tick(interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def watch(
    config: Config,
    forge: Forge,
    tasks: TaskSource,
    interval: int,
    cap: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Check every `interval` seconds and exit as soon as the report changes, so
    a backgrounded run re-invokes the agent that started it exactly when
    something needs attention. The first check only records a baseline.

    Exits 0 on a change or after `cap` quiet seconds (with a TIMEOUT line), and
    1 when a check fails.
    """
    started = clock()
    baseline = None
    while True:
        try:
            report = run_check(config, forge, tasks)
        except ForgeError as e:
            print(f"prtop: {e}", file=sys.stderr)
            print("[watch] check failed", flush=True)
            return 1
        if baseline is not None and plain(report.text) != baseline:
            print(report.text, flush=True)
            return 0
        baseline = plain(report.text)
        print(f"[watch] {datetime.now():%H:%M:%S} no change", flush=True)
        if clock() - started >= cap:
            print(f"TIMEOUT after {cap}s with no PR activity", flush=True)
            return 0
        sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="prtop",
        description="One table of the merge requests assigned to you, from review to production.",
    )
    parser.add_argument("--version", action="version", version=f"prtop {__version__}")
    parser.add_argument(
        "--loop", type=int, nargs="?", const=DEFAULT_LOOP_INTERVAL, default=None,
        metavar="SECONDS",
        help=f"repeat forever, aligned to wall-clock ticks (default {DEFAULT_LOOP_INTERVAL}s)",
    )
    commands = parser.add_subparsers(dest="command")
    watch_parser = commands.add_parser(
        "watch",
        help="check on an interval and exit as soon as the report changes",
        description=watch.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    watch_parser.add_argument(
        "--interval", type=int, default=DEFAULT_LOOP_INTERVAL, metavar="SECONDS",
        help=f"seconds between checks (default {DEFAULT_LOOP_INTERVAL})",
    )
    watch_parser.add_argument(
        "--cap", type=int, default=DEFAULT_WATCH_CAP, metavar="SECONDS",
        help=f"quiet seconds before giving up with a TIMEOUT line (default {DEFAULT_WATCH_CAP})",
    )
    args = parser.parse_args(argv)
    if args.command == "watch" and args.loop is not None:
        parser.error("--loop and watch are two ways to repeat; pick one")

    try:
        config = load_config()
        tasks = task_source(config)
    except ConfigError as e:
        print(f"prtop: {e}", file=sys.stderr)
        return 2
    forge = make_forge(config)

    if args.command == "watch":
        return watch(config, forge, tasks, args.interval, args.cap)
    if args.loop is not None:
        return loop(config, forge, tasks, args.loop)
    try:
        check(config, forge, tasks)
    except ForgeError as e:
        print(f"prtop: {e}", file=sys.stderr)
        return 1
    return 0
