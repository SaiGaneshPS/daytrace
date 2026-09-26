# API contract

This is the contract between the hub and everything that talks to it (collectors, the dashboard, the
Android widget). The **ticket** column says which ticket implements each endpoint; shapes can be refined in
that ticket's PR, and this file is updated in the same PR.

The event shape itself is defined in [event-schema.json](event-schema.json) and mirrored by
`hub/daytrace_hub/models.py`. `hub/tests/test_models.py` keeps the two in sync.

## Conventions

| Topic | Rule |
|---|---|
| Base URL | `http://<hub-host>:<port>/api/v1` (HTTPS once DT-47 lands). |
| Profiles and ports | personal `8765`: your real data, reachable from your own home Wi-Fi (your phone syncs here) but never over Tailscale. shared-dev `8766`: seed and test data, the only port your teammate reaches over Tailscale. demo `8767`: seeded demo data. |
| Body format | JSON, UTF-8. |
| Times | ISO 8601 with an offset, for example `2026-09-25T14:03:10-04:00`. Seconds and up to 9 fractional digits are optional, `T` and `Z` may be lowercase. Times without an offset, Unix numbers and impossible dates are rejected. |
| Days | `YYYY-MM-DD`, interpreted in the time zone given by `tz` (an IANA name such as `America/Toronto`). Default: the hub computer's time zone. |
| Auth | `Authorization: Bearer <token>`. Devices get a token when they pair (DT-12); the dashboard on phones pairs as a `viewer`. |
| Networks | The hub listens on IPv4 (`0.0.0.0`). Every profile accepts requests only from loopback and private LAN addresses (`10/8`, `172.16/12`, `192.168/16`, `169.254/16`, and `fc00::/7`, `fe80::/10` for IPv6-mapped peers). On Wi-Fi you do not trust (venue, cafe), set `DAYTRACE_LAN_NETWORKS` to your own subnet, for example `192.168.1.0/24`, or stop the personal profile. Tailscale addresses (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) are accepted only by shared-dev. Anything else gets `403 forbidden_network`. HTTP and WebSocket are checked the same way. |
| Host names | To block DNS rebinding, the `Host` header must be an IP address, a single-label name (`localhost`, the PC name), a private name (`*.local`, `*.home.arpa`, `*.internal`, `*.lan`, `*.home`, `*.localdomain`) or, on shared-dev only, a Tailscale MagicDNS name (`*.ts.net`). Anything else gets `403 forbidden_host`. |
| Forwarding | Never forward the personal port with a VS Code tunnel, `tailscale serve` / `funnel` or `ssh -L`: forwarded traffic arrives from `127.0.0.1` and would look like the hub computer itself. Tunnel and `ts.net` host names are refused on personal, but `ssh -L` to `localhost` is not. |
| Local only | Endpoints marked *local only* accept requests only from the hub computer itself (`127.0.0.1` / `::1`). |
| Accuracy | Every response that feeds a chart or a number on screen includes `meta`: `{ "unit", "range": { "start", "end", "tz" }, "source": "real" \| "seed" \| "mixed", "estimated": true \| false }`. |

### Errors

```json
{ "error": { "code": "batch_too_large", "message": "at most 500 events per request", "details": [] } }
```

| Status | `code` | When |
|---|---|---|
| 400 | `bad_request` | Malformed JSON, a body that is not one event or `{"events": [...]}`, bad query parameters, wrong confirmation phrase |
| 401 | `unauthorized` | Missing, unknown or revoked token |
| 403 | `forbidden_network` | The request came from a network this profile does not serve: the public internet, a LAN outside `DAYTRACE_LAN_NETWORKS`, or Tailscale on a profile other than shared-dev. Applies to every path, before auth |
| 403 | `forbidden_host` | The `Host` header is a public DNS name (possible DNS rebinding). Applies to every path, before auth |
| 403 | `forbidden` | The token is valid but may not do this: a `viewer` token sending events, or a device asking for another device's cursor |
| 403 | `local_only` | A local-only endpoint was called from another machine |
| 405 | `method_not_allowed` | The path exists but not with this method |
| 404 | `not_found` | Unknown device, tab or resource |
| 413 | `batch_too_large` | More than 500 events in one request (checked before any event is validated), or a body over 2 MB (refused before it is read) |
| 422 | `invalid_request` | Invalid input for endpoints other than `POST /events` (bad events there are reported per event, see below) |
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

No token needed; the network and Host checks still apply.

```json
{ "status": "ok", "profile": "personal", "version": "0.1.0" }
```

### POST /events

Send one event object, or a batch `{ "events": [ ... ] }` of 1 to 500 events, at most 2 MB. The token is
checked before the body is read. `viewer` tokens cannot send events (`403 forbidden`).

**Every event's `device_id` must be the device the token belongs to**; events for any other device are rejected.
All good events in one request are stored in a single transaction.

#### Resending is always safe (deduplication)

The hub stores each event under one key per device, `UNIQUE(device_id, dedup_key)`, chosen in this order
(`Event.dedup_key()` in the models):

| The event has | Key | When the key already exists |
|---|---|---|
| `external_id` | `ext:<external_id>` | If anything changed, the new copy **replaces** the stored one (counted as `replaced`); an identical resend counts as `duplicates`. Use this for anything that can change after it was first sent: Health Connect records, HealthKit samples, calendar events, and daily totals. |
| `seq` (no `external_id`) | `seq:<seq>` | An identical resend is ignored (counted as `duplicates`). If the stored event with that `seq` is a **different** event (kind, source, start, end, app, app_id, title or data differ), the new one is listed in `rejected`: the collector restarted its numbering, for example after a reinstall, and must continue after `last_seq`. Collectors with a local store (Android, desktop tracker, Mac bridge, browser extension) number their events 0, 1, 2, ... per device. |
| neither | `content:<hash>` of kind, start, end, app, app_id, title and data | Ignored (`duplicates`). For stateless collectors such as iPhone Shortcuts: two different events in the same second get different keys, and resending the same event gives the same key. |

Daily totals use a derived `external_id` such as `steps:2026-09-25`, so the evening sync replaces the morning
number instead of being dropped or double counted.

#### Request and response

Request (from `ios/shortcuts/payloads/app-event.json`):

```json
{ "device_id": "iphone-1", "kind": "app_open", "source": "shortcuts",
  "start": "2026-09-25T14:03:10-04:00", "app": "Instagram" }
```

Response (`200` whenever the body has the right shape, even if some events were rejected):

```json
{ "accepted": 1, "replaced": 0, "duplicates": 0,
  "rejected": [ { "index": 3, "seq": 812, "external_id": null, "reason": "data.stage: sleep data.stage must be one of [...]" } ],
  "last_seq": 4812, "nudge": null }
```

- `rejected` lists events the hub will never accept as sent. Collectors mark them as failed and **do not resend
  them**, so one bad event can never block the events after it.
- When a rule fires (DT-43), `nudge` is `{ "rule": "focus_block", "title": "...", "body": "...", "created_at": "..." }`.
- DT-42 adds the parsed meal items to the response for `meal` events.

### GET /devices/{device_id}/cursor

```json
{ "device_id": "android-1", "last_seq": 4812 }
```

`last_seq` is the highest `seq` stored for that device (null for devices that never send `seq`). A device can
only read its own cursor (`403 forbidden` otherwise).

How collectors use it: an event counts as synced only after the request that carried it got a `200`, and
collectors resend **all** events that are not synced yet (never just "everything after `last_seq`", which would
skip gaps left by a failed batch). The cursor is for recovery, for example after reinstalling the app, when the
collector checks what the hub already has.

### Pairing

`POST /pair/start` (local only):

```json
{ "code": "493817", "expires_at": "2026-09-25T14:08:10-04:00",
  "url": "http://daytrace-hub.local:8765", "qr": "/api/v1/pair/qr.png?code=493817" }
```

`url` is the running profile's address as phones see it on the local network (the personal profile listens on
your home Wi-Fi so your own phone can reach it).

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
