#!/usr/bin/env bash
# Poll prtop until the report changes, then exit — so a backgrounded
# run re-invokes the calling agent exactly when something needs attention.
#
# Usage: bash watch_prs.sh [interval_seconds] [cap_seconds]
#   interval: seconds between checks (default 300)
#   cap:      exit with a TIMEOUT line after this many quiet seconds (default 7200)
#
# The first check records a baseline; run a one-shot check and handle pending
# items before starting this.
#
# Exit codes: 0 = activity/change detected or TIMEOUT; 1 = monitor script error.

set -u

INTERVAL=${1:-300}
CAP=${2:-7200}
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

normalize() {
  # Drop the per-run timestamp header and the iTerm OSC color codes so only
  # meaningful report changes count as a diff.
  grep -v '^=== monitor-prs @' | perl -pe 's/\e\][^\a]*\a//g'
}

prev=""
while :; do
  out=$(cd "$SCRIPT_DIR" && python3 -m prtop 2>&1)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    printf '%s\n' "$out"
    echo "[watch] prtop failed (exit $rc)"
    exit 1
  fi

  norm=$(printf '%s\n' "$out" | normalize)
  if [ -n "$prev" ] && [ "$norm" != "$prev" ]; then
    printf '%s\n' "$out"
    exit 0
  fi
  prev=$norm

  echo "[watch] $(date +%H:%M:%S) no change"
  if [ "$SECONDS" -ge "$CAP" ]; then
    echo "TIMEOUT after ${CAP}s with no PR activity"
    exit 0
  fi
  sleep "$INTERVAL"
done
