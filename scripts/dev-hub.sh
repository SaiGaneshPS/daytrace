#!/usr/bin/env bash
# DT-8 / DT-10 / DT-48: start a hub profile on macOS or Linux.
# Usage: scripts/dev-hub.sh --profile personal|shared-dev|demo
#        scripts/dev-hub.sh --profile demo [--days 14] [--no-seed] [--no-warm-up] [--no-open]
# The demo profile is made ready for a demo (docs/demo-script.md): its days are seeded again (always safe: only demo
# data is replaced), the hub starts, the local model is woken up by writing yesterday's story and last week's Wrapped
# ahead (so they show at once on stage), and the dashboard opens. Ctrl+C stops the hub. Other profiles just start.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
python="$here/../hub/.venv/bin/python"

profile=personal days=14 seed=1 warm=1 open=1 rest=()
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) profile="$2"; shift 2 ;;
    --profile=*) profile="${1#*=}"; shift ;;
    --days) days="$2"; shift 2 ;;
    --no-seed) seed=0; shift ;;
    --no-warm-up) warm=0; shift ;;
    --no-open) open=0; shift ;;
    *) rest+=("$1"); shift ;;
  esac
done
if [ "$profile" != demo ]; then
  exec "$python" -m daytrace_hub run --profile "$profile" ${rest[@]+"${rest[@]}"}
fi

port="$("$python" -c "from daytrace_hub.config import get_profile; print(get_profile('demo').port)")"
hub="http://localhost:$port"
step() { printf '\n== %s\n' "$1"; }
ask() { curl -sS --max-time "${2:-15}" "$hub$1"; }  # the hub trusts this computer at localhost: no token needed
field() { "$python" -c "import json, sys; print(json.load(sys.stdin).get('$1'))"; }

if [ "$seed" = 1 ]; then
  step "Seeding $days days of demo data"
  "$python" -m daytrace_hub seed --profile demo --days "$days"
fi

step "Starting the demo hub at $hub"
"$python" -m daytrace_hub run --profile demo &
server=$!
trap 'kill "$server" 2>/dev/null || true' EXIT
for _ in $(seq 1 120); do
  if ask /api/v1/health 3 >/dev/null 2>&1; then break; fi
  if ! kill -0 "$server" 2>/dev/null; then echo "The hub stopped: see its messages above." >&2; exit 1; fi
  sleep 0.5
done
ask /api/v1/health 3 >/dev/null || { echo "The hub didn't answer at $hub within a minute." >&2; exit 1; }

if [ "$warm" = 1 ]; then
  step "Waking up the local model"
  status="$(ask /api/v1/ai/status 30)"
  if [ "$(printf '%s' "$status" | field reachable)" = True ]; then
    echo "The model $(printf '%s' "$status" | field model) answers. Writing ahead what the demo shows (the first time can take a minute)..."
    yesterday="$("$python" -c "import datetime; print(datetime.date.today() - datetime.timedelta(days=1))")"
    start=$SECONDS; ask "/api/v1/story?date=$yesterday" 600 >/dev/null; echo "  Yesterday's story: $((SECONDS - start)) s"
    start=$SECONDS; ask /api/v1/wrapped 600 >/dev/null; echo "  Last week's Wrapped: $((SECONDS - start)) s"
  else
    echo "The local model isn't answering ($(printf '%s' "$status" | field error)). Start LM Studio and load a model, then run this again: until then the AI steps show their plain fallbacks." >&2
  fi
fi

if [ "$open" = 1 ]; then
  if command -v open >/dev/null; then open "$hub"; elif command -v xdg-open >/dev/null; then xdg-open "$hub" >/dev/null 2>&1 || true; fi
fi
step "Ready: $hub (Ctrl+C stops the hub)"
echo "Live events or a nudge without the phone: hub/.venv/bin/python -m daytrace_hub demo live (or nudge)"
wait "$server"
