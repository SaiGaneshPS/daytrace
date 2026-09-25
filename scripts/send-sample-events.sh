#!/usr/bin/env bash
# DT-15: post a few sample events to a running hub (macOS/Linux).
# Usage: DAYTRACE_TOKEN=<device token> scripts/send-sample-events.sh [hub-url]
# The token comes from the DAYTRACE_TOKEN environment variable, never a command-line argument,
# so it stays out of your shell history and the process list. Tip: `read -rs DAYTRACE_TOKEN; export DAYTRACE_TOKEN`.
set -euo pipefail
hub_url="${1:-http://127.0.0.1:8765}"
: "${DAYTRACE_TOKEN:?Set DAYTRACE_TOKEN to a paired device token first}"
echo "Implemented in DT-15 (hub: $hub_url)."
