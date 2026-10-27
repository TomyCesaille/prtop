"""The `prtop` command.

In a terminal it holds the screen and fetches again every few seconds, the way
`htop` shows processes. When its output goes anywhere else — a pipe, a file, an
agent — it prints the table and exits. `prtop watch` checks until the report
changes, for an agent to be woken by.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import time
from collections.abc import Callable
from contextlib import redirect_stderr
from datetime import UTC, datetime, timedelta

from prtop import __version__, live, model
from prtop.config import Config, ConfigError, load_config
from prtop.demo import TITLE as DEMO_TITLE
from prtop.demo import demo_story
from prtop.forges import Forge, make_forges
from prtop.model import ForgeError
from prtop.report import (
    ACT,
    Report,
    Row,
    assemble_rows,
    by_severity,
    deploy_row,
    open_row,
    unreachable_row,
)
from prtop.state import load_report, load_state, save_report, save_state
from prtop.tasks import TaskSource, task_source
from prtop.terminal import fullscreen

DEFAULT_WATCH_DELAY = 300
DEFAULT_WATCH_CAP = 7200


def track_merges(forge: Forge, current: dict, previous: dict, deployments: dict) -> dict:
    """MRs that left the open list since the last check: each one that merged
    starts being tracked on its way to production.

    Returns the ones the forge could not answer for. They belong in the next
    baseline, so the next run asks about them again."""
    unconfirmed = {}
    for key in sorted(previous.keys() - current.keys()):
        try:
            deployment = forge.merged_deployment(previous[key])
        except ForgeError as e:
            print(f"prtop: {key}: {e}", file=sys.stderr)
            unconfirmed[key] = previous[key]
            continue
        if deployment is not None:
            deployments[key] = deployment
    return unconfirmed


def unfinished_deployments(forge: Forge, deployments: dict) -> dict[str, model.DeployState]:
    """Where each tracked deployment stands; the ones that finished stop being tracked."""
    states = forge.deploy_states(deployments)
    for key, state in states.items():
        if state.kind in model.FINISHED:
            deployments.pop(key)
    return {key: state for key, state in states.items() if key in deployments}


def check_forge(config: Config, forge: Forge, tasks: TaskSource) -> tuple[list[Row], list[Row]]:
    """One forge's open rows and deploy rows, against its own state file."""
    state_file = config.state_file(forge.host)
    state = load_state(state_file)
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
        known = {**known, **track_merges(forge, known, previous, deployments)}
    state["open_mrs"] = known

    deploy_states = unfinished_deployments(forge, deployments) if config.deploy_tracking else {}
    ids = model.short_ids([*open_mrs, *deploy_states])
    open_rows = (
        [unreachable_row(forge.host)] if error is not None
        else [open_row(ids[key], entry) for key, entry in open_mrs.items()]
    )
    deploy_rows = [deploy_row(ids[key], deployments[key], state) for key, state in deploy_states.items()]
    save_state(state_file, state)
    return open_rows, deploy_rows


def run_check(config: Config, forges: list[Forge], tasks: TaskSource) -> Report:
    """One table for every forge: their rows sort together, by mark first."""
    open_rows: list[Row] = []
    deploy_rows: list[Row] = []
    for forge in forges:
        forge_open, forge_deploy = check_forge(config, forge, tasks)
        open_rows += forge_open
        deploy_rows += forge_deploy
    open_rows, deploy_rows = by_severity(open_rows), by_severity(deploy_rows)

    # A forge the run could not read counts too: its row is marked `!`, since
    # green would otherwise claim all-clear over MRs nobody saw.
    action_needed = any(row.mark == ACT for row in open_rows)
    return Report(assemble_rows(open_rows, deploy_rows), tasks.shows_column, action_needed)


# ---------- Beside the live view's table ----------

def set_tab_color(action_needed: bool) -> None:
    """iTerm2 tab color: orange while an open MR is on you, green otherwise — a
    convenience duplicate of the `!` block at the top of the table, never the
    only place a state is reported."""
    r, g, b = (255, 140, 0) if action_needed else (0, 180, 0)
    sys.stdout.write(
        f"\033]6;1;bg;red;brightness;{r}\a"
        f"\033]6;1;bg;green;brightness;{g}\a"
        f"\033]6;1;bg;blue;brightness;{b}\a"
    )
    sys.stdout.flush()


def report_hook(config: Config, forges: list[Forge]) -> Callable[[Report], None]:
    """What the live view does with each report beside showing it: saves it
    for `prtop watch`, and colors the iTerm2 tab when asked."""
    hosts = [forge.host for forge in forges]

    def on_report(report: Report) -> None:
        save_report(config.report_file, hosts, report, datetime.now(UTC))
        if config.iterm_tab_color:
            set_tab_color(report.action_needed)

    return on_report


# ---------- Entry point ----------

def batch(
    fetch: Callable[[], Report], delay: int, iterations: int, *, sleep: Callable[[float], None] = time.sleep
) -> int:
    """The table, `iterations` times over, for output that is not a terminal."""
    for i in range(iterations):
        if i:
            sleep(delay)
        try:
            report = fetch()
        except ForgeError as e:
            print(f"prtop: {e}", file=sys.stderr)
            return 1
        print()
        print(report.text, flush=True)
    return 0


def show_live(
    fetch: Callable[[], Report],
    title: str,
    delay: int,
    max_iterations: int | None,
    on_report: Callable[[Report], None] | None,
) -> int:
    messages = live.Messages()
    fetcher = live.Fetcher(fetch, messages.take)
    view = None
    try:
        with fullscreen() as terminal, redirect_stderr(messages):
            view = live.run(fetcher, terminal, title, delay, max_iterations, on_report=on_report)
    except KeyboardInterrupt:
        pass
    if view is not None and view.finished:
        if view.report is not None:
            print(view.report.text)
        for message in view.messages:
            print(message, file=sys.stderr)
    if fetcher.busy:
        # A fetch still in flight would hold the exit until the forge answers.
        # The state file is replaced in one step, so cutting it short leaves
        # the previous state whole.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    return 0


def watch(
    config: Config,
    forges: list[Forge],
    tasks: TaskSource,
    delay: int,
    cap: int,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Check every `delay` seconds and exit as soon as the report changes, so
    a backgrounded run re-invokes the agent that started it exactly when
    something needs attention. The first check only records a baseline.

    A check reads the live view's latest report instead of fetching when the
    live view fetched less than `delay` seconds ago, so a watch next to an
    open live view makes no forge calls of its own.

    Exits 0 on a change or after `cap` quiet seconds (with a TIMEOUT line), and
    1 when a check fails.
    """
    started = clock()
    hosts = [forge.host for forge in forges]
    baseline = None
    while True:
        report = load_report(config.report_file, hosts, datetime.now(UTC), timedelta(seconds=delay))
        if report is None:
            try:
                report = run_check(config, forges, tasks)
            except ForgeError as e:
                print(f"prtop: {e}", file=sys.stderr)
                print("[watch] check failed", flush=True)
                return 1
        # Links included: they tell two forges' identical PR ids apart.
        if baseline is not None and report.text != baseline:
            print(report.text, flush=True)
            return 0
        baseline = report.text
        print(f"[watch] {datetime.now():%H:%M:%S} no change", flush=True)
        if clock() - started >= cap:
            print(f"TIMEOUT after {cap}s with no PR activity", flush=True)
            return 0
        sleep(delay)


def _utf8_output() -> None:
    # The table carries emoji, which a Windows code page cannot encode: output
    # piped to another program would otherwise fail there.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"{value} is not 1 or more")
    return value


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="prtop",
        description=(
            "Your GitLab merge requests and GitHub pull requests, from review to production. "
            "In a terminal prtop fills the screen and fetches again every few seconds, like "
            "htop; anywhere else it prints one table."
        ),
        epilog="keys: q quit, h help, Ctrl+L fetch now, Z pause or resume",
    )
    parser.add_argument("--version", action="version", version=f"prtop {__version__}")
    parser.add_argument(
        "-d", "--delay", type=positive_int, metavar="SECONDS",
        help=f"seconds between fetches (default {live.DEFAULT_DELAY}, or {DEFAULT_WATCH_DELAY} for watch)",
    )
    parser.add_argument(
        "-n", "--max-iterations", type=positive_int, metavar="N",
        help="stop after N fetches, leaving the last table on screen "
        "(default: until q in a terminal, one fetch anywhere else)",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="show made-up merge requests, without reading any forge",
    )
    commands = parser.add_subparsers(dest="command")
    watch_parser = commands.add_parser(
        "watch",
        help="check on an interval and exit as soon as the report changes",
        description=watch.__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    watch_parser.add_argument(
        "-d", "--delay", type=positive_int, default=argparse.SUPPRESS, metavar="SECONDS",
        help=f"seconds between checks (default {DEFAULT_WATCH_DELAY})",
    )
    watch_parser.add_argument(
        "--cap", type=positive_int, default=DEFAULT_WATCH_CAP, metavar="SECONDS",
        help=f"quiet seconds before giving up with a TIMEOUT line (default {DEFAULT_WATCH_CAP})",
    )
    args = parser.parse_args(argv)
    if args.command == "watch" and args.max_iterations is not None:
        parser.error("watch stops at the first change; -n does not apply to it")
    if args.command == "watch" and args.demo:
        parser.error("watch needs a forge to read; --demo has none")
    if args.delay is None:
        args.delay = DEFAULT_WATCH_DELAY if args.command == "watch" else live.DEFAULT_DELAY
    return args


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    args = parse_args(argv)
    on_report = None
    if args.demo:
        fetch, title = demo_story(), DEMO_TITLE
    else:
        try:
            config = load_config()
            tasks = task_source(config)
        except ConfigError as e:
            print(f"prtop: {e}", file=sys.stderr)
            return 2
        forges = make_forges(config)
        if args.command == "watch":
            return watch(config, forges, tasks, args.delay, args.cap)

        def fetch() -> Report:
            return run_check(config, forges, tasks)

        title, on_report = " + ".join(forge.host for forge in forges), report_hook(config, forges)

    if sys.stdin.isatty() and sys.stdout.isatty():
        return show_live(fetch, title, args.delay, args.max_iterations, on_report)
    return batch(fetch, args.delay, args.max_iterations or 1)
