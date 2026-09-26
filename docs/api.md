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
| Local only | Endpoints marked *local only* (and dashboard reads without a token) accept requests only from the hub computer talking to itself: the client is `127.0.0.1` / `::1`, the `Host` is `localhost`, `127.0.0.1` or `[::1]`, an `Origin` (when sent) is a loopback origin, and `Sec-Fetch-Site` is not `cross-site`. So open the dashboard at `http://localhost:<port>` on the hub computer. Web pages from other sites, and DNS rebinding through LAN names, get `403 local_only`. |
| Accuracy | Every response that feeds a chart or a number on screen includes `meta`: `{ "unit", "range": { "start", "end", "tz" }, "source": "real" \| "seed" \| "mixed", "estimated": true \| false }`. |

### Errors

```json
{ "error": { "code": "batch_too_large", "message": "at most 500 events per request", "details": [] } }
```

| Status | `code` | When |
|---|---|---|
| 400 | `bad_request` | Malformed JSON, a body that is not one event or `{"events": [...]}`, bad query parameters, wrong confirmation phrase |
| 400 | `invalid_code` | The pairing code is wrong, already used or expired |
| 401 | `unauthorized` | Missing, unknown or revoked token |
| 403 | `forbidden_network` | The request came from a network this profile does not serve: the public internet, a LAN outside `DAYTRACE_LAN_NETWORKS`, or Tailscale on a profile other than shared-dev. Applies to every path, before auth |
| 403 | `forbidden_host` | The `Host` header is a public DNS name (possible DNS rebinding). Applies to every path, before auth |
| 403 | `forbidden` | The token is valid but may not do this: a `viewer` token sending events, or a device asking for another device's cursor |
| 403 | `local_only` | A local-only endpoint was called from another machine |
| 405 | `method_not_allowed` | The path exists but not with this method |
| 404 | `not_found` | Unknown device, tab or resource |
| 413 | `batch_too_large` | More than 500 events in one request (checked before any event is validated) |
| 413 | `body_too_large` | The body is over 8 MB (refused before it is read). Send fewer events per request; any single valid event always fits |
| 422 | `invalid_request` | Invalid input for endpoints other than `POST /events` (bad events there are reported per event, see below) |
| 429 | `too_many_attempts` | Too many wrong pairing codes |
| 500 | `internal_error` | A bug in the hub; the details are in the hub's log |
| 503 | `busy` | Another writer held the database too long. Safe to retry the same request (`Retry-After: 1`) |
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

Send one event object, or a batch `{ "events": [ ... ] }` of 1 to 500 events, as UTF-8 JSON of at most 8 MB.
The token is checked before the body is read. `viewer` tokens cannot send events (`403 forbidden`).

**Every event's `device_id` must be the device the token belongs to**; events for any other device are rejected.
All good events in one request are stored in a single transaction.

#### Resending is always safe (deduplication)

The hub stores each event under one key per device, `UNIQUE(device_id, dedup_key)`, chosen in this order
(`Event.dedup_key()` in the models):

| The event has | Key | When the key already exists |
|---|---|---|
| `external_id` | `ext:<external_id>` | If anything changed, the new copy **replaces** the stored one (counted as `replaced`); an identical resend counts as `duplicates`. When both copies have a `seq`, an older copy (lower `seq`, for example a slow retry) never overwrites a newer one and counts as `duplicates`. Without `seq` the last copy to arrive wins, so stateless collectors send one request at a time. Use this for anything that can change after it was first sent: Health Connect records, HealthKit samples, calendar events, and daily totals. |
| `seq` (no `external_id`) | `seq:<seq>` | An identical resend is ignored (counted as `duplicates`). If the stored event with that `seq` is a **different** event (kind, source, start, end, app, app_id, title or data differ), the new one is rejected with code `seq_conflict`: the collector restarted its numbering, for example after a reinstall. Collectors with a local store (Android, desktop tracker, Mac bridge, browser extension) number their events 0, 1, 2, ... per device. |
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
  "rejected": [ { "index": 3, "code": "invalid", "seq": 812, "external_id": null, "reason": "data.stage: sleep data.stage must be one of [...]" } ],
  "last_seq": 4812, "nudge": null }
```

- `rejected` lists events the hub did not store, each with a `code`:
  - `invalid` or `wrong_device`: the hub will never accept the event as sent. Collectors mark it as failed and
    **do not resend it**, so one bad event can never block the events after it.
  - `seq_conflict`: the `seq` belongs to a different stored event. Collectors renumber their unsynced events
    after `last_seq` and send them again; nothing is lost.
- Besides the schema, the hub rejects (`invalid`) text with broken characters (half of an emoji), numbers that
  are not finite (`1e400`), and `data` over 16 KB as compact UTF-8 JSON.
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

`POST /pair/start` (local only, no body):

```json
{ "code": "493817", "expires_at": "2026-09-25T14:08:10-04:00",
  "url": "http://192.168.1.23:8765",
  "urls": ["http://192.168.1.23:8765"],
  "mdns_url": "http://daytrace-desktop-ab12cd.local:8765",
  "qr": "/api/v1/pair/qr.png" }
```

- `url` is the hub's LAN IP as phones see it (the interface with the default route, skipping VPN and virtual
  adapters). The QR code uses it because Android browsers do not reliably resolve `.local` names. It is `null`
  (and so is `qr`) when this computer has no LAN address right now; the code still works for devices that reach
  the hub another way.
- `urls` lists every address phones can use: LAN addresses first, then the Tailscale address on shared-dev.
- `mdns_url` is set only when mDNS is on.
- Only one code is active at a time; starting again replaces it. The response is sent with `Cache-Control: no-store`.

`GET /pair/qr.png` (local only) is a PNG of `{"daytrace":1,"url":"http://192.168.1.23:8765","code":"493817"}`
for the active code, or `404` when no code can be claimed.

`POST /pair/claim` (no token; the network rules still apply):

```json
{ "code": "493817", "device_name": "Galaxy phone", "device_type": "android" }
```

- `code` may be typed with a space or dash (`493 817`, `493-817`).
- `device_name` is 1 to 64 characters after trimming, without control or formatting characters (emoji are fine).
- `device_type` is one of `windows`, `macos`, `android`, `ios`, `browser`, `viewer`. Phone browsers opening
  the dashboard pair as `viewer`; the iPhone's Shortcuts use `ios`.

Response `201` with `Cache-Control: no-store`. The token is shown only once:

```json
{ "device_id": "android-1", "device_type": "android", "name": "Galaxy phone", "token": "dt_...", "profile": "personal" }
```

- Device IDs count up per type and are never reused: `windows-1`, `mac-1`, `android-1`, `iphone-1`,
  `browser-1`, `viewer-1`. Collectors send the `device_id` they got here (the Shortcuts ask for it on import).
- Codes are single use and expire after 5 minutes: `400 invalid_code` for a wrong, used or expired code (the
  message says how many tries are left).
- 5 wrong tries from one address lock that address out of the code, and 20 wrong tries in total lock the code
  for everyone (`429 too_many_attempts`, even for the right code), until a new one is started. One noisy
  device on the Wi-Fi cannot lock out your phone.

### Devices

`GET /devices` (any paired device's token, or no token from the hub computer itself):

```json
{ "devices": [ { "device_id": "android-1", "name": "Galaxy phone", "device_type": "android",
  "paired_at": "2026-09-25T18:00:00.000000Z", "last_seen": "2026-09-25T22:41:07.000000Z", "revoked_at": null,
  "last_seq": 4812, "event_count": 15230, "events_24h": 1290 } ] }
```

Revoked devices are listed too (`revoked_at` set). `events_24h` counts events that started in the last 24 hours.
`last_seen` is updated at most once a minute.

`DELETE /devices/{device_id}` (local only) revokes the token and returns `204` (again `204` if it was already
revoked, `404` for an unknown device). The device's data stays.

### Local network discovery

While a profile runs, it advertises `_daytrace._tcp` on the LAN (`Daytrace hub (<profile>)`, TXT `profile`,
`version`, `api=/api/v1`) with the host name `daytrace-<pc name>.local`, for example
`daytrace-desktop-ab12cd.local`, so two hubs on one Wi-Fi never claim the same name. Only LAN addresses are
announced (mDNS does not cross Tailscale).

- The announcement starts in the background (the hub serves at once) and the addresses are checked every
  30 seconds, so a new Wi-Fi, a new DHCP address or Wi-Fi connecting after the hub started is picked up.
- `DAYTRACE_MDNS=off` turns it off (`on` / `off`; anything else is an error). `DAYTRACE_MDNS_NAME` sets the host
  name (one DNS label).
- Check it with `dns-sd -B _daytrace._tcp` on a Mac. Windows Firewall asks once to allow Python on private
  networks; allow it, or phones cannot reach the hub at all.

### GET /timeline?date=2026-09-25&tz=America/Toronto

Any paired device's token, or no token from the hub computer. `date` defaults to today in `tz`; `tz` defaults
to the hub computer's current offset. Unknown time zones get `400`, impossible dates `422`.

```json
{
  "date": "2026-09-25", "tz": "America/Toronto",
  "lanes": [ { "device_id": "windows-1", "device_type": "windows", "name": "Desk PC", "counted": true,
    "seconds": 4500, "minutes": 75.0,
    "sessions": [ { "start": "2026-09-25T09:00:00-04:00", "end": "2026-09-25T09:40:00-04:00", "seconds": 2400,
      "minutes": 40.0, "app": "Code", "app_id": null, "title": "stats.py", "category": "work", "kind": "app",
      "estimated": false } ] } ],
  "calendar": [ { "start": "...", "end": "...", "title": "Study: algorithms", "all_day": false, "device_id": "iphone-1" } ],
  "sleep": [ { "start": "...", "end": "...", "minutes": 445.0, "stage": "asleep", "estimated": false, "device_id": "iphone-1" } ],
  "meals": [ { "time": "...", "items": ["roti", "dal"], "text": null, "meal_type": "dinner", "device_id": "iphone-1" } ],
  "totals": { "seconds": 10500, "minutes": 175.0, "by_device": { "windows-1": 75.0, "android-1": 35.0 },
    "any_screen_seconds": 9900, "any_screen_minutes": 165.0 },
  "meta": { "unit": "minutes", "range": { "start": "2026-09-25T00:00:00-04:00", "end": "2026-09-26T00:00:00-04:00",
    "tz": "America/Toronto" }, "source": "real", "estimated": false }
}
```

How the numbers are made (`hub/daytrace_hub/sessions.py`):

- The day runs from local midnight to local midnight in `tz`, so DST days are 23 or 25 hours and sessions are
  split at local midnight.
- iPhone `app_open` / `app_close` pairs become sessions. An open with no close ends at the device's next
  event or after 30 minutes and is marked `estimated`. An open and close more than 6 hours apart count as a
  missed close.
- Desktop readings of the same app and title at most 5 s apart are merged; phone usage stats are used exactly.
- On one device, overlapping sessions never double count: the most recently started app owns the screen, and
  an app it covered continues afterwards. AFK periods are cut out.
- Every duration is whole seconds; `minutes` is rounded from seconds for display. `lane.seconds` is exactly
  the sum of its sessions, and `totals.seconds` exactly the sum of the counted lanes (DT-59 checks this).
- `totals` adds up screen time per device. `any_screen_seconds` is the time at least one screen was in use.
  Browser-extension lanes (`counted: false`) show which sites were open, but that time is already in the
  desktop lane, so they are not added.
- `calendar` lists events overlapping the day (once, even when two phones sync the same calendar). `sleep`
  lists sleep that **ended** on this day, at full length, so last night's sleep shows on this morning.
- `meta.source` is `seed`, `real` or `mixed` over the events behind the day. `meta.estimated` is true when
  any session end or sleep entry was inferred.

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
  "series": { "trend": { "type": "stacked_area", "x": ["2026-09-19", "..."], "stacks": { "windows-1": [301, "..."] } } } }
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
