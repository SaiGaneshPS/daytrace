#!/usr/bin/env bash
# DT-15: post a few sample events to a running hub (macOS/Linux).
# Usage: DAYTRACE_DEVICE_ID=<device_id from pairing> scripts/send-sample-events.sh [hub-url]
# The script asks for the token without echoing it. To set it yourself, never type it into a command (shell
# history keeps it); use: read -rs DAYTRACE_TOKEN && export DAYTRACE_TOKEN
# The token reaches curl on stdin (-H @-), not as an argument, so it never shows in the process list.
# Needs only bash, date and curl (any version from the last decade); no python.
set -euo pipefail
hub_url="${1:-http://127.0.0.1:8765}"
device_id="${DAYTRACE_DEVICE_ID:?Set DAYTRACE_DEVICE_ID to the device_id pairing returned with the token}"
if [[ ! "$device_id" =~ ^[A-Za-z0-9._-]{1,64}$ ]]; then
  echo "DAYTRACE_DEVICE_ID does not look like a device_id" >&2
  exit 2
fi
if [[ -z "${DAYTRACE_TOKEN:-}" ]]; then
  read -rsp "Device token: " DAYTRACE_TOKEN
  echo
fi

# An ISO 8601 time with a +hh:mm offset, $1 minutes ago (GNU date first, then BSD/macOS date).
stamp() {
  local raw
  raw="$(date -d "-$1 minutes" +%Y-%m-%dT%H:%M:%S%z 2>/dev/null || date -v-"$1"M +%Y-%m-%dT%H:%M:%S%z)"
  echo "${raw:0:22}:${raw:22:2}"
}

body="$(printf '{"events":[%s,%s,%s]}' \
  "{\"device_id\":\"$device_id\",\"kind\":\"app_session\",\"source\":\"manual\",\"app\":\"Instagram\",\"start\":\"$(stamp 6)\",\"end\":\"$(stamp 2)\"}" \
  "{\"device_id\":\"$device_id\",\"kind\":\"app_session\",\"source\":\"manual\",\"app\":\"YouTube\",\"start\":\"$(stamp 2)\",\"end\":\"$(stamp 0)\"}" \
  "{\"device_id\":\"$device_id\",\"kind\":\"meal\",\"source\":\"manual\",\"start\":\"$(stamp 0)\",\"data\":{\"text\":\"two rotis and dal\",\"meal_type\":\"dinner\"}}")"

response="$(printf 'Authorization: Bearer %s\n' "$DAYTRACE_TOKEN" |
  curl --silent --show-error -X POST "$hub_url/api/v1/events" \
    -H @- -H 'Content-Type: application/json' --data "$body" --write-out '\n%{http_code}')"
status="${response##*$'\n'}"
echo "${response%$'\n'*}"
if [[ "$status" != "200" ]]; then
  echo "The hub answered HTTP $status" >&2
  exit 1
fi
if [[ "$response" != *'"rejected":[]'* ]]; then
  echo "The hub rejected some events (see \"rejected\" above; is DAYTRACE_DEVICE_ID the token's device?)" >&2
  exit 1
fi
