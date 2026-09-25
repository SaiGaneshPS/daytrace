# Daytrace

**Where did my day go?** Daytrace shows your day across your phone and your computer (apps, websites,
sleep, meals and calendar) on one timeline, and a local AI explains it. Your data stays on your own network:
there is no cloud server, and the AI runs on your own machine.

> Working name. Hackathon project.

## How it works

- A **hub** runs on any computer (Windows, macOS or Linux). It stores events in SQLite, tracks desktop
  activity, runs the stats and talks to a local LLM (LM Studio or Ollama).
- **Collectors** send events to the hub over your Wi-Fi: an Android app, iPhone Shortcuts, a Mac bridge for
  full iPhone Screen Time, and a browser extension.
- A **PWA dashboard**, served by the hub, works in any browser and can be installed on phones.

## Repo layout (fixed in DT-1)

| Folder | What lives there | Owner |
|---|---|---|
| `hub/` | Python hub: API, storage, trackers, stats, AI, nudges | You |
| `dashboard/` | Vite + React + TypeScript PWA | You |
| `android/` | Kotlin collector app (sideloaded APK) | You |
| `browser-extension/` | Active-website collector (domain only) | You |
| `ios/` | iPhone Shortcuts: setup guide, payloads, exported shortcuts | Teammate |
| `mac/` | Mac bridge for full iPhone Screen Time | Teammate |
| `docs/` | Architecture, API contract, setup guides, demo script | Both |
| `scripts/` | Dev helpers and the Notion sync script | You |

**Rule:** the folder and file layout is fixed. Changing it needs its own ticket.

## Quick start

See [docs/setup-windows.md](docs/setup-windows.md) or [docs/setup-mac.md](docs/setup-mac.md).

```bash
# hub
cd hub
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # macOS/Linux: .venv/bin/python
.venv/Scripts/python -m pytest

# dashboard
cd dashboard
npm ci
npm run build
```

## Working on it

Tickets live in Notion. Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a PR.
