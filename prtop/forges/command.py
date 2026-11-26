"""The command-line tools prtop reads the forges through: `glab` and `gh`.

Each tool keeps its own sign-in, so prtop never handles a token.
"""

from __future__ import annotations

import json
import shlex
import subprocess

from prtop.model import ForgeError

# A connection the network dropped without closing it never answers: past this,
# the call fails and the forge reads as unreachable instead of freezing the fetch.
TIMEOUT_SECONDS = 60


def run_tool(cmd: list[str], install_url: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, encoding="utf-8", check=False, timeout=TIMEOUT_SECONDS)
    except FileNotFoundError:
        raise ForgeError(f"{cmd[0]} is not installed: {install_url}") from None
    except subprocess.TimeoutExpired:
        raise ForgeError(f"{shlex.join(cmd)} did not answer within {TIMEOUT_SECONDS} seconds") from None


def failure(what: str, result: subprocess.CompletedProcess[str]) -> ForgeError:
    return ForgeError(f"{what} failed\n{result.stderr.strip() or result.stdout.strip()}")


def get_json(cmd: list[str], install_url: str):
    """The JSON a command prints, or None when it prints nothing."""
    result = run_tool(cmd, install_url)
    if result.returncode != 0:
        raise failure(shlex.join(cmd), result)
    return json.loads(result.stdout) if result.stdout.strip() else None
