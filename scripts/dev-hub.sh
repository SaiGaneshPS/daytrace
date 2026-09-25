#!/usr/bin/env bash
# DT-8 / DT-10 / DT-48: start a hub profile on macOS or Linux.
# Usage: scripts/dev-hub.sh --profile personal|shared-dev|demo
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
exec "$here/../hub/.venv/bin/python" -m daytrace_hub run "$@"
