"""The state directory: per forge, what the previous run saw, so this run can
tell what merged; and the latest fetch's report, which a prtop fetching
seconds later reads instead."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from prtop.config import Config
from prtop.report import Report, Row

# A state directory removed while prtop writes into it is made again, up to this
# many times in a row before the write fails.
WRITE_ATTEMPTS = 3


def _write(path: Path, data: dict) -> None:
    text = json.dumps(data, indent=2)
    for _ in range(WRITE_ATTEMPTS - 1):
        try:
            _replace(path, text)
        except FileNotFoundError:
            continue
        return
    _replace(path, text)


def _replace(path: Path, text: str) -> None:
    """`path` holds `text` whole or not at all. Each write has a temporary file
    of its own, so prtops saving the same file at once never move each other's."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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


def save_report(config: Config, report: Report, fetched_at: datetime) -> None:
    _write(config.report_file, {
        "config": repr(config),
        "fetched_at": fetched_at.isoformat(),
        "show_task": report.show_task,
        "action_needed": report.action_needed,
        "rows": report.rows,
    })


def load_report(config: Config, now: datetime, max_age: timedelta) -> Report | None:
    """The saved report, when a prtop with the same settings fetched it less
    than `max_age` before `now`."""
    try:
        saved = json.loads(config.report_file.read_text(encoding="utf-8"))
        age = now - datetime.fromisoformat(saved["fetched_at"])
        rows = [None if row is None else Row(*row) for row in saved["rows"]]
        report = Report(rows, saved["show_task"], saved["action_needed"])
        same_config = saved["config"] == repr(config)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    # A save from the future is a clock that moved back: its age says nothing.
    return report if same_config and timedelta(0) <= age < max_age else None
