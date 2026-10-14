"""The state file: what the previous run saw, so this run can tell what merged."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            print(f"prtop: {path} is malformed; starting fresh.", file=sys.stderr)
    return {"open_mrs": {}, "deployments": {}, "last_checked": None}


def save_state(path: Path, state: dict) -> None:
    state["last_checked"] = datetime.now(UTC).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)
