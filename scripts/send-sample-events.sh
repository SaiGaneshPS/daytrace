#!/usr/bin/env bash
# DT-15: post a few sample events to a running hub (macOS/Linux).
# Usage: DAYTRACE_TOKEN=<device token> DAYTRACE_DEVICE_ID=<device_id from pairing> scripts/send-sample-events.sh [hub-url]
# The token comes from the DAYTRACE_TOKEN environment variable, never a command-line argument,
# so it stays out of your shell history and the process list. Tip: `read -rs DAYTRACE_TOKEN; export DAYTRACE_TOKEN`.
# It reaches curl on stdin (-H @-), not as an argument, for the same reason.
set -euo pipefail
hub_url="${1:-http://127.0.0.1:8765}"
: "${DAYTRACE_TOKEN:?Set DAYTRACE_TOKEN to a paired device token first}"
device_id="${DAYTRACE_DEVICE_ID:?Set DAYTRACE_DEVICE_ID to the device_id pairing returned with the token}"

body="$(python3 - "$device_id" <<'PY'
import json
import sys
from datetime import datetime, timedelta

device = sys.argv[1]
now = datetime.now().astimezone()


def stamp(moment):
    return moment.isoformat(timespec="seconds")


events = [
    {"device_id": device, "kind": "app_session", "source": "manual", "app": "Instagram",
     "start": stamp(now - timedelta(minutes=6)), "end": stamp(now - timedelta(minutes=2))},
    {"device_id": device, "kind": "app_session", "source": "manual", "app": "YouTube",
     "start": stamp(now - timedelta(minutes=2)), "end": stamp(now)},
    {"device_id": device, "kind": "meal", "source": "manual", "start": stamp(now),
     "data": {"text": "two rotis and dal", "meal_type": "dinner"}},
]
print(json.dumps({"events": events}))
PY
)"

printf 'Authorization: Bearer %s\n' "$DAYTRACE_TOKEN" |
  curl --silent --show-error --fail-with-body -X POST "$hub_url/api/v1/events" \
    -H @- -H 'Content-Type: application/json' --data "$body"
echo
