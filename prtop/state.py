"""The state directory: per forge, what the previous run saw, so this run can
tell what merged; and the live view's latest report, which `prtop watch` reads
instead of fetching again."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from prtop.report import Report, Row


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"prtop: {path} is malformed; starting fresh.", file=sys.stderr)
    return {"open_mrs": {}, "deployments": {}, "last_checked": None}


def save_state(path: Path, state: dict) -> None:
    state["last_checked"] = datetime.now(UTC).isoformat()
    _write(path, state)


def save_report(path: Path, hosts: Sequence[str], report: Report, fetched_at: datetime) -> None:
    _write(path, {
        "hosts": list(hosts),
        "fetched_at": fetched_at.isoformat(),
        "show_task": report.show_task,
        "action_needed": report.action_needed,
        "rows": report.rows,
    })


def load_report(path: Path, hosts: Sequence[str], now: datetime, max_age: timedelta) -> Report | None:
    """The saved report, when it reads the same forges and was fetched less
    than `max_age` before `now`."""
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        age = now - datetime.fromisoformat(saved["fetched_at"])
        rows = [None if row is None else Row(*row) for row in saved["rows"]]
        report = Report(rows, saved["show_task"], saved["action_needed"])
        same_forges = saved["hosts"] == list(hosts)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    # A save from the future is a clock that moved back: its age says nothing.
    return report if same_forges and timedelta(0) <= age < max_age else None
