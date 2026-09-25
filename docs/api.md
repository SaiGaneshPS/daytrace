# API contract

This is the contract between the hub and everything that talks to it (collectors, the dashboard, the
Android widget). The **ticket** column says which ticket implements each endpoint; shapes can be refined in
that ticket's PR, and this file is updated in the same PR.

The event shape itself is defined in [event-schema.json](event-schema.json) and mirrored by
`hub/daytrace_hub/models.py`. `hub/tests/test_models.py` keeps the two in sync.

## Conventions

| Topic | Rule |
|---|---|
| Base URL | `http://<hub-host>:<port>/api/v1` (HTTPS once DT-47 lands). Ports: personal `8765` (this computer only), shared-dev `8766`, demo `8767`. |
| Body format | JSON, UTF-8. |
| Times | ISO 8601 with an offset, for example `2026-09-25T14:03:10-04:00`. Times without an offset are rejected. |
| Days | `YYYY-MM-DD`, interpreted in the time zone given by `tz` (an IANA name such as `America/Toronto`). Default: the hub computer's time zone. |
| Auth | `Authorization: Bearer <token>`. Devices get a token when they pair (DT-12); the dashboard on phones pairs as a `viewer`. |
| Local only | Endpoints marked *local only* accept requests only from the hub computer itself (`127.0.0.1` / `::1`). |
| Accuracy | Every response that feeds a chart or a number on screen includes `meta`: `{ "unit", "range": { "start", "end", "tz" }, "source": "real" \| "seed" \| "mixed", "estimated": true \| false }`. |

### Errors

```json
{ "error": { "code": "invalid_event", "message": "steps events need data.count as a whole number of 0 or more", "details": [] } }
```

| Status | `code` | When |
|---|---|---|
| 400 | `bad_request` | Malformed JSON or query parameters, wrong confirmation phrase |
| 401 | `unauthorized` | Missing, unknown or revoked token |
| 403 | `local_only` | A local-only endpoint was called from another machine |
| 404 | `not_found` | Unknown device, tab or resource |
| 413 | `batch_too_large` | More than 500 events in one request |
| 422 | `invalid_event` | An event breaks the schema or the model rules (details list each problem) |
| 429 | `too_many_attempts` | Too many wrong pairing codes |
| 503 | `ai_unavailable` | The local model server is not reachable |

## Endpoints

| Method and path | Auth | Ticket |
|---|---|---|
| `GET /health` | none | DT-11 |
| `POST /events` | device | DT-11 |
| `GET /devices/{device_id}/cursor` | device (own device) | DT-11 |
| `POST /pair/start`, `GET /pair/qr.png` | local only | DT-12 |
| `POST /pair/claim` | none (needs a valid code) | DT-12 |
| `GET /devices`, `DELETE /devices/{device_id}` | viewer / local only | DT-12 |
| `GET /timeline` | viewer | DT-13 |
| `GET /categories`, `PUT /categories/{app}` | viewer | DT-14 |
| `GET /ai/status`, `GET /story`, `POST /ask` | viewer | DT-37, DT-39, DT-40 |
| `GET /insights/{tab}`, `GET /wrapped` | viewer | DT-41 |
| `GET /streaks`, `GET /goals`, `PUT /goals/{goal_id}`, `GET /achievements` | viewer | DT-53 |
| `GET /privacy/network` | viewer | DT-45 |
| `GET /privacy/export`, `POST /privacy/delete` | local only | DT-46 |

### GET /health

```json
{ "status": "ok", "profile": "personal", "version": "0.1.0" }
```

### POST /events

Send one event object, or a batch `{ "events": [ ... ] }` of 1 to 500 events. Resending an event with the same
`(device_id, seq)` is safe: it is counted as a duplicate and not stored twice.

`seq` rules:
- Collectors with a local store (Android, desktop tracker, Mac bridge, browser extension) use an
  auto-increment counter per device.
- iPhone Shortcuts have no store, so they use the event's start time in Unix milliseconds.

Request (from `ios/shortcuts/payloads/app-event.json`):

```json
{ "device_id": "iphone-1", "seq": 1790359390000, "kind": "app_open", "source": "shortcuts",
  "start": "2026-09-25T14:03:10-04:00", "app": "Instagram" }
```

Response:

```json
{ "accepted": 1, "duplicates": 0, "last_seq": 1790359390000, "nudge": null }
```

When a rule fires (DT-43), `nudge` is `{ "rule": "focus_block", "title": "...", "body": "...", "created_at": "..." }`.
DT-42 adds the parsed meal items to the response for `meal` events.

### GET /devices/{device_id}/cursor

```json
{ "device_id": "android-1", "last_seq": 4812 }
```

A collector resends everything after `last_seq`. A device can only read its own cursor.

### Pairing

`POST /pair/start` (local only):

```json
{ "code": "493817", "expires_at": "2026-09-25T14:08:10-04:00",
  "url": "http://daytrace-hub.local:8765", "qr": "/api/v1/pair/qr.png?code=493817" }
```

`POST /pair/claim`:

```json
{ "code": "493817", "device_name": "Galaxy phone", "device_type": "android" }
```

`device_type` is one of `windows`, `macos`, `android`, `ios`, `browser`, `viewer`. The response contains the
token, which is shown only once:

```json
{ "device_id": "android-1", "token": "dt_..." }
```

Codes are single-use and expire after 5 minutes; 5 wrong tries lock the code (429).

### Devices

`GET /devices`:

```json
{ "devices": [ { "device_id": "android-1", "name": "Galaxy phone", "type": "android",
  "paired_at": "...", "last_seen": "...", "last_seq": 4812, "events_today": 1290 } ] }
```

`DELETE /devices/{device_id}` (local only) revokes the token and returns `204`.

### GET /timeline?date=2026-09-25&tz=America/Toronto

```json
{
  "date": "2026-09-25", "tz": "America/Toronto",
  "lanes": [ { "device_id": "windows-desk", "device_type": "windows",
    "sessions": [ { "start": "...", "end": "...", "minutes": 18.6, "app": "Code", "category": "work", "title": "stats.py" } ] } ],
  "calendar": [ { "start": "...", "end": "...", "title": "Study: algorithms" } ],
  "sleep": [ { "start": "...", "end": "...", "minutes": 445, "estimated": false } ],
  "meals": [ { "time": "...", "items": ["roti", "dal"], "meal_type": "dinner" } ],
  "totals": { "minutes": 512.4, "by_device": { "windows-desk": 301.2, "android-1": 211.2 } },
  "meta": { "unit": "minutes", "range": { "start": "...", "end": "...", "tz": "America/Toronto" }, "source": "real", "estimated": false }
}
```

`totals.minutes` always equals the sum of all session minutes (DT-59 checks this).

### Categories

`GET /categories`:

```json
{ "categories": ["social", "video", "work", "study", "comms", "games", "health", "other"],
  "apps": { "Instagram": "social", "Code": "work" },
  "overrides": { "Obsidian": { "category": "study", "source": "user" } } }
```

`PUT /categories/{app}` with `{ "category": "study" }` returns the saved override.

### AI

- `GET /ai/status` returns `{ "model": "...", "reachable": true, "tool_calling": true }`.
- `GET /story?date=` returns `{ "date": "...", "story": "...", "facts_used": [ { "label": "...", "value": 125, "unit": "minutes" } ], "model": "...", "cached": false }`.
- `POST /ask` with `{ "question": "...", "tz": "..." }` returns `{ "answer": "...", "facts_used": [...], "tools_called": ["get_totals"], "chart": null }`. `chart`, when present, is a small series the dashboard can draw.

Every number in `story` and `answer` appears in `facts_used` (the number check, DT-39). When the model is
down these return 503 `ai_unavailable`.

### Insights and Wrapped

`GET /insights/{tab}?range=7d&tz=...` where `tab` is `overview`, `apps`, `devices`, `focus`, `sleep`, `food`
or `calendar`, and `range` is `today`, `7d`, `30d` or `YYYY-MM-DD..YYYY-MM-DD`:

```json
{ "tab": "overview",
  "meta": { "unit": "minutes", "range": { "start": "...", "end": "...", "tz": "..." }, "source": "seed", "estimated": false },
  "metrics": [ { "id": "screen_time", "label": "Screen time", "value": 3120, "unit": "minutes",
    "explain": "All app and window time across devices, AFK removed.", "estimated": false } ],
  "series": { "trend": { "type": "stacked_area", "x": ["2026-09-19", "..."], "stacks": { "windows-desk": [301, "..."] } } } }
```

`GET /wrapped?week=2026-W39` returns `{ "week": "...", "stats": [...], "streaks": [...], "lines": ["...", "...", "..."], "meta": {...} }`.

### Streaks, goals and achievements

`GET /streaks`:

```json
{ "streaks": [ { "id": "focus_flame", "name": "Focus flame", "rule": "120 or more focused minutes in a day",
  "current": 5, "best": 9, "today": "at_risk", "remaining": { "value": 45, "unit": "minutes" },
  "days": [ { "date": "2026-09-24", "status": "met" } ] } ] }
```

`today` is `met`, `at_risk` or `no_data`; days with no data from the needed device neither extend nor break
a streak.

- `GET /goals` returns `{ "goals": [ { "id": "social_cap", "label": "Social apps", "target": 60, "unit": "minutes", "progress": 0.42 } ] }`; `PUT /goals/{goal_id}` with `{ "target": 45 }` saves a new target.
- `GET /achievements` returns `{ "achievements": [ { "id": "first_sync", "name": "First sync", "rule": "...", "unlocked_at": null } ] }`.

### Privacy

- `GET /privacy/network` returns `{ "since": "...", "allowed": { "localhost": 120, "lan": 44, "tailscale": 0 }, "blocked": { "count": 0, "destinations": [] } }`.
- `GET /privacy/export` (local only) streams all of the profile's data as JSON.
- `POST /privacy/delete` (local only) needs `{ "confirm": "delete all my daytrace data" }` on every call; any other phrase returns 400.
