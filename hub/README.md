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

## The local model (DT-37)

The day story and "Ask your day" use a model running on your own machines, never a cloud service:

| Variable | Default | Meaning |
|---|---|---|
| `DAYTRACE_LLM_BASE_URL` | `http://127.0.0.1:1234/v1` (LM Studio) | Any OpenAI-compatible server; Ollama is `http://127.0.0.1:11434/v1` |
| `DAYTRACE_LLM_MODEL` | the first model the server lists | The model to use |

In LM Studio, load a chat model and start the server (Developer tab). `GET /api/v1/ai/status` shows whether the
hub reaches it and whether the model can call tools, and explains any problem (a typo in the address included:
the hub still starts).

The address must be this computer or your LAN; the tailnet is allowed only for profiles without real data, so
your own data never crosses Tailscale. `llm.py` resolves names itself, checks every address, and connects only
to a checked one. It never uses a proxy or follows a redirect, and never sends credentials, even ones the
OpenAI SDK finds in `OPENAI_*` variables.

Tests use a fake server (`FakeModelServer` in `tests/conftest.py`) behind the real guard, so no model is needed.
A test that tries to reach a real model server fails loudly; tests marked `real_network` may open sockets on this
computer.

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
