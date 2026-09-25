# Hub

Python 3.12 + FastAPI + SQLite. Runs on Windows, macOS and Linux.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # macOS/Linux: .venv/bin/python
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m pytest
.venv/Scripts/python -m daytrace_hub --help
```

CI (`.github/workflows/hub.yml`) runs the same lint and tests on Windows, macOS and Linux, and also installs
the built wheel to catch missing package data (`migrations/*.sql`, `data/*.json`).

## Module map (which ticket fills which file)

| File | Ticket |
|---|---|
| `config.py`, `db.py`, `migrations/`, `__main__.py`, `app.py` | DT-10 |
| `models.py` | DT-9 |
| `auth.py`, `api/events.py` | DT-11 |
| `discovery.py`, `api/devices.py` | DT-12 |
| `sessions.py`, `api/timeline.py` | DT-13 |
| `categories.py`, `api/categories.py`, `data/categories.json` | DT-14 (AI part: DT-42) |
| `seed.py` | DT-15 |
| `tracker/base.py`, `tracker/windows.py` | DT-16 |
| `tracker/macos.py` | DT-17 (teammate) |
| `llm.py`, `api/ai.py` | DT-37 |
| `stats.py` | DT-38 |
| `story.py` | DT-39 |
| `ask.py` | DT-40 |
| `api/insights.py` | DT-41 |
| `nudges.py`, `notify.py` | DT-43 |
| `redaction.py`, `data/redaction_rules.json` | DT-44 |
| `api/privacy.py` | DT-45, DT-46 |
