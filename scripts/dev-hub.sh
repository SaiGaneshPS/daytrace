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

# Something already answering here is not this run's hub: say so instead of taking it for ours.
if already="$(ask /api/v1/health 2 2>/dev/null)"; then
  echo "A hub already answers at $hub (the $(printf '%s' "$already" | field profile) profile): stop it, then run this again." >&2
  exit 1
fi

step "Starting the demo hub at $hub"
"$python" -m daytrace_hub run --profile demo &
server=$!
trap 'kill "$server" 2>/dev/null || true' EXIT
ready=0
for _ in $(seq 1 120); do
  if ! kill -0 "$server" 2>/dev/null; then wait "$server" || code=$?; echo "The hub stopped (exit code ${code:-0}): see its messages above." >&2; exit 1; fi
  if health="$(ask /api/v1/health 3 2>/dev/null)"; then
    if [ "$(printf '%s' "$health" | field profile)" != demo ]; then echo "Something else answers at $hub." >&2; exit 1; fi
    ready=1; break
  fi
  sleep 0.5
done
[ "$ready" = 1 ] || { echo "The hub didn't answer at $hub within a minute." >&2; exit 1; }

if [ "$warm" = 1 ]; then
  step "Waking up the local model"
  # Nothing here may stop the demo: at worst the AI steps show their plain fallbacks.
  if status="$(ask /api/v1/ai/status 180)" && [ "$(printf '%s' "$status" | field reachable)" = True ]; then
    echo "The model $(printf '%s' "$status" | field model) answers. Writing ahead what the demo shows (the first time can take a minute)..."
    yesterday="$("$python" -c "import datetime; print(datetime.date.today() - datetime.timedelta(days=1))")"
    start=$SECONDS
    if ask "/api/v1/story?date=$yesterday" 600 >/dev/null; then echo "  Yesterday's story: $((SECONDS - start)) s"; else echo "  Yesterday's story didn't finish: it is written when first opened." >&2; fi
    start=$SECONDS
    if ask /api/v1/wrapped 600 >/dev/null; then echo "  Last week's Wrapped: $((SECONDS - start)) s"; else echo "  Last week's Wrapped didn't finish: it is written when first opened." >&2; fi
  else
    echo "The local model isn't answering. Start LM Studio and load a model, then run this again with --no-seed: until then the AI steps show their plain fallbacks." >&2
  fi
fi

if [ "$open" = 1 ]; then
  case "$(uname -s)" in
    Darwin) open "$hub" || true ;;
    *) if command -v xdg-open >/dev/null; then xdg-open "$hub" >/dev/null 2>&1 || true; fi ;;
  esac
fi
step "Ready: $hub (Ctrl+C stops the hub)"
echo "Live events or a nudge without the phone: hub/.venv/bin/python -m daytrace_hub demo live (or nudge)"
wait "$server"
