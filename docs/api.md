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
| Dashboard | Every path outside `/api` is the dashboard (DT-30): its files, and `index.html` for any page, so reloading `/insights` works. An unknown `/api` path is still a JSON `404`. Dashboard responses carry a strict Content-Security-Policy (only this hub is contacted). |
| Profiles and ports | personal `8765`: your real data, reachable from your own home Wi-Fi (your phone syncs here) but never over Tailscale. shared-dev `8766`: seed and test data, the only port your teammate reaches over Tailscale. demo `8767`: seeded demo data. |
| Body format | JSON, UTF-8. |
| Times | ISO 8601 with an offset, for example `2026-09-25T14:03:10-04:00`. Seconds and up to 9 fractional digits are optional, `T` and `Z` may be lowercase. Times without an offset, Unix numbers and impossible dates are rejected. |
| Days | `YYYY-MM-DD`, interpreted in the time zone given by `tz` (an IANA name such as `America/Toronto`). Default: the hub computer's time zone. |
| Auth | `Authorization: Bearer <token>`. Devices get a token when they pair (DT-12); the dashboard on phones pairs as a `viewer`. |
| Networks | The hub listens only on this computer (`127.0.0.1`, `::1`) and the addresses phones use (the LAN adapter's, not a VPN's or a virtual machine's; the tailnet's only for shared-dev), never on a public address or every interface, and follows them as Wi-Fi and DHCP change (DT-45). Every profile accepts requests only from loopback and private LAN addresses (`10/8`, `172.16/12`, `192.168/16`, `169.254/16`, and `fc00::/7`, `fe80::/10` for IPv6-mapped peers). On Wi-Fi you do not trust (venue, cafe), set `DAYTRACE_LAN_NETWORKS` to your own subnet, for example `192.168.1.0/24`, or stop the personal profile. Tailscale addresses (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) are accepted only by shared-dev. Anything else gets `403 forbidden_network`. A request from another web page (an `Origin` that isn't the hub's own or a browser extension's, including another page on this computer, `null`, or one that doesn't parse) gets `403 forbidden_origin`. The dashboard's dev server sends the hub's own origin through its proxy. No CORS headers are ever sent. HTTP and WebSocket are checked the same way. |
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
| `GET /categories` | viewer | DT-14 |
| `PUT /categories/{key}`, `DELETE /categories/{key}` | dashboard (viewer token or the hub computer) | DT-14 |
| `GET /ai/status`, `GET /story`, `POST /ask` | viewer | DT-37, DT-39, DT-40 |
| `GET /insights/{tab}`, `GET /wrapped` | viewer | DT-41 |
| `GET /streaks`, `GET /goals`, `GET /achievements` | viewer | DT-53 |
| `PUT /goals/{goal_id}` | dashboard (viewer token or the hub computer) | DT-53 |
| `GET /privacy/redaction`, `POST /privacy/redaction/check`, `GET /privacy/redaction/stored` | viewer | DT-44 |
| `PUT /privacy/redaction`, `POST /privacy/redaction/apply` | dashboard (viewer token or the hub computer) | DT-44 |
| `GET /privacy/network` | viewer | DT-45 |
| `GET /privacy/export`, `POST /privacy/delete` | local only | DT-46 |

### GET /health

No token needed; the network and Host checks still apply.

```json
{ "status": "ok", "profile": "personal", "version": "0.1.0", "local": true }
```

`local` (DT-32) is true when the request comes from the hub computer's own dashboard, by the same check that
`POST /pair/start`, `GET /pair/qr.png` and `DELETE /devices/{id}` make. The Devices page uses it to show the
pairing code there and a "Pair this device" form anywhere else.

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
| `external_id` | `ext:<external_id>` | If anything changed, the new copy **replaces** the stored one (counted as `replaced`); an identical resend counts as `duplicates`. When both copies have a `seq`, an older copy (lower `seq`, for example a slow retry) never overwrites a newer one and counts as `duplicates`. Without `seq` the last copy to arrive wins, so stateless collectors send one request at a time. Use this for anything that can change after it was first sent: Health Connect records, HealthKit samples, calendar events, and daily totals. The Android app sends every event with both: `external_id` is `kind|start_ms|package` (a SHA-256 hash when that is not printable ASCII), and an event it later extends (a collection repeated after a crash) goes again with a new, higher `seq` so it replaces the shorter copy. |
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

`GET /pair/qr.png?for=app|browser` (local only) is a PNG for the active code, or `404` when no code can be claimed:

- `for=app` (the default), for the Daytrace app's scanner: `{"daytrace":1,"url":"http://192.168.1.23:8765","code":"493817"}`.
- `for=browser` (DT-32), for a phone's camera: `http://192.168.1.23:8765/devices#pair=493817`, which opens the
  Devices page on the phone and pairs that browser as a viewer. A browser never sends the part after `#` to any
  server, so the code stays out of requests and logs.

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
to the hub computer's IANA time zone (sent back in `tz`). Unknown time zones and dates outside 1970-01-01 to
9998-12-31 get `400`, impossible dates such as `2026-02-30` get `422`.

Example with two devices (the `...` stands for more sessions of the same shape):

```json
{
  "date": "2026-09-25", "tz": "America/Toronto",
  "lanes": [
    { "device_id": "windows-1", "device_type": "windows", "name": "Desk PC", "counted": true,
      "last_seen": "2026-09-25T22:41:07.000000Z", "seconds": 4500, "minutes": 75.0,
      "sessions": [ { "start": "2026-09-25T09:00:00-04:00", "end": "2026-09-25T09:40:00-04:00", "seconds": 2400,
        "minutes": 40.0, "app": "Code", "app_id": null, "title": "stats.py", "category": "work", "kind": "app",
        "estimated": false }, "..." ] },
    { "device_id": "android-1", "device_type": "android", "name": "Galaxy phone", "counted": true,
      "last_seen": null, "seconds": 2100, "minutes": 35.0, "sessions": [ "..." ] } ],
  "calendar": [ { "start": "2026-09-25T15:00:00-04:00", "end": "2026-09-25T17:00:00-04:00", "title": "Study: algorithms",
    "all_day": false, "device_id": "iphone-1" } ],
  "sleep": [ { "start": "2026-09-24T23:40:00-04:00", "end": "2026-09-25T07:05:00-04:00", "minutes": 445.0,
    "stage": "asleep", "estimated": false, "device_id": "iphone-1" } ],
  "meals": [ { "time": "2026-09-25T19:30:00-04:00", "items": ["roti", "dal"], "text": null, "meal_type": "dinner",
    "device_id": "iphone-1" } ],
  "totals": { "seconds": 6600, "minutes": 110.0,
    "by_device_seconds": { "windows-1": 4500, "android-1": 2100 }, "by_device": { "windows-1": 75.0, "android-1": 35.0 },
    "any_screen_seconds": 6000, "any_screen_minutes": 100.0, "sleep_seconds": 26700, "sleep_minutes": 445.0 },
  "meta": { "unit": "minutes", "range": { "start": "2026-09-25T00:00:00-04:00", "end": "2026-09-26T00:00:00-04:00",
    "tz": "America/Toronto" }, "source": "real", "estimated": false }
}
```

A lane's `last_seen` is when its device last reached the hub (UTC, updated at most once a minute; null for a device
that never has, like a seeded one). The Today tab marks a device that synced in the last minute as live.

How the numbers are made (`hub/daytrace_hub/sessions.py`):

- The day runs from local midnight to local midnight in `tz`, so DST days are 23 or 25 hours and sessions are
  split at local midnight. Nothing after the current time is counted.
- iPhone `app_open` / `app_close` pairs become sessions, ending early if a `screen_off` came first. An open with
  no close ends at the device's next screen event (another open or close, screen on or off; not a calendar
  entry or a health sync) or after 30 minutes, and is marked `estimated`. An open and close more than 6 hours
  apart count as a missed close.
- Desktop readings of the same app and title at most 5 s apart are merged; phone usage stats are used exactly.
- On one device, overlapping sessions never double count: the most recently started app owns the screen, and
  an app it covered continues afterwards. AFK periods are cut out.
- Session boundaries are snapped to whole seconds, so every total is whole seconds and adds up exactly:
  `lane.seconds` is the sum of its sessions, `totals.seconds` the sum of the counted lanes (and of
  `by_device_seconds`), and `any_screen_seconds` can never exceed `totals.seconds` (DT-59 checks this).
  **Do arithmetic with the `seconds` fields.** Every `minutes` field is rounded to 2 decimals on its own for
  display, so rounded minutes can differ from a sum of rounded minutes by 0.01 per item.
- `totals` adds up screen time per device; `any_screen_seconds` is the time at least one screen was in use.
  Browser-extension lanes (`counted: false`) show which sites were open, but that time is already in the
  desktop lane, so they are not added.
- `calendar` lists events overlapping the day (once, even when two phones sync the same calendar). `sleep`
  lists sleep that **ended** on this day, at full length, so last night's sleep shows on this morning; the
  same entry from two phones is listed once. `totals.sleep_seconds` counts time asleep once, however many
  stages or copies overlap, and leaves out `in_bed` and `awake`.
- `meta.source` is `seed`, `real` or `mixed` over every event that shaped the day (sessions, AFK cuts, lanes).
  `meta.estimated` is true when any session end or sleep entry was inferred.

### Categories

Every session has one category (`hub/daytrace_hub/categories.py`). For apps, in this order:

1. The user's override on the app id or the app name.
2. The category the collector sent (the schema's `category` hint).
3. The built-in list (`hub/daytrace_hub/data/categories.json`, about 200 apps): by app id (Android package,
   iOS/macOS bundle id, Windows exe name), then by app name.
4. A guess the local AI saved (DT-42), which only fills in apps the list does not know.
5. `other`. Browsers, music and utilities are `other` on purpose; the web lane shows what the browser was for.

For websites, a user override on the exact domain and then the collector's hint come first; after that the
most specific domain wins: each suffix is tried from longest to shortest (`m.youtube.com`, then `youtube.com`),
and at each level a user override beats the built-in list, which beats an AI guess. So setting `google.com` to
study leaves `mail.google.com` as comms.

Keys are compared Unicode-normalized and without case, `.exe`, `www.`, a port or a trailing dot. Every
consumer of sessions (timeline, stats, goals, insights) gets them already categorized from
`sessions.sessions_for()`, so they always agree.

`GET /categories` (any paired device's token, or no token from the hub computer) lists the categories and every
app or site seen in the last 30 days, most used first (at most 500):

```json
{ "categories": ["social", "video", "work", "study", "comms", "games", "health", "other"],
  "apps": [ { "key": "com.instagram.android", "app": "Instagram", "app_id": "com.instagram.android", "kind": "app",
              "category": "social", "source": "builtin", "override": null, "events": 212 },
            { "key": "md.obsidian", "app": "Obsidian", "app_id": "md.obsidian", "kind": "app",
              "category": "study", "source": "user", "override": "md.obsidian", "events": 57 },
            { "key": "m.youtube.com", "app": "m.youtube.com", "app_id": null, "kind": "web",
              "category": "video", "source": "builtin", "override": null, "events": 40 } ],
  "overrides": [ { "key": "md.obsidian", "category": "study", "source": "user", "updated_at": "2026-09-25T18:00:00.000000Z" } ] }
```

- One row per app, however it was spelled (`Code` and `Code.exe`, `www.youtube.com` and `youtube.com`). `app` is
  the name to show (the app id or domain when there is no name).
- `source` is `user`, `ai`, `builtin`, `collector` or `default`. `override` is the override that decided (it
  may be saved under the name or a parent domain rather than `key`); DELETE it to undo.
- Only the dashboard can change categories: a `viewer` token, or no token from the hub computer. Collector
  tokens get `403 forbidden`.
- `PUT /categories/{key}` with `{ "category": "study" }` saves the user's choice for that key (an app id, app
  name or domain; slashes are fine) and returns `{ "key", "category", "source", "updated_at" }`. A domain also
  covers its subdomains, except where a more specific entry exists. Keys with control or formatting characters,
  or longer than 200 characters, get `400`.
- `DELETE /categories/{key}` removes an override (`204`, or `404` if there was none).

### AI

- `GET /ai/status` (DT-37) always answers `200`:

  ```json
  { "base_url": "http://127.0.0.1:1234/v1", "model": "qwen3-14b", "reachable": true, "tool_calling": true,
    "models": ["qwen3-14b"], "error": null }
  ```

  - `reachable` is whether a model server answers `GET /models` (asked once, so a stopped server shows up quickly).
  - `model` is `DAYTRACE_LLM_MODEL` when it is loaded, otherwise the first model listed.
  - `tool_calling` is whether that model calls a tool when asked to. It is checked with one short reply and
    remembered for 10 minutes; it is `null` when there is no usable model.
  - `error` says, in plain words, what is wrong: no server, the model not loaded, or an address that isn't local.
  - The model server must be on this computer or the profile's LAN ranges (`DAYTRACE_LAN_NETWORKS` narrows them),
    or on the tailnet for profiles without real data (never personal): `DAYTRACE_LLM_BASE_URL` (default LM Studio
    `http://127.0.0.1:1234/v1`; Ollama is `http://127.0.0.1:11434/v1`). Host names are resolved by the hub, every
    address is checked, and the connection goes to a checked address. Cloud metadata addresses are always refused.
    Only plain request headers are sent, never credentials.
  - A malformed `DAYTRACE_LLM_BASE_URL` does not stop the hub: `error` explains it.
- `GET /story?date=&tz=` (DT-39, viewer) returns a 4 to 6 sentence story of the day:

  ```json
  { "date": "2026-09-25", "tz": "America/Toronto", "story": "...",
    "facts_used": [ { "label": "screen time", "value": 155, "unit": "minutes" } ],
    "model": "qwen3-14b", "cached": false, "fallback": false, "reason": null, "in_progress": false }
  ```

  - The facts come from the stats engine. `unit` is one of `minutes`, `times`, `score`, `percent`, `per hour`
    or `time` (HH:MM, local). A session still going at midnight is not the day's first or last screen use.
  - Every amount in `story` is read whole and must match one fact of the same kind:
    - Durations are read however they are written ("2 hours 35 minutes", "2h35m", "two and a half hours", "a
      three-hour block"). So are clock times ("11:40 pm", "9 am", "half past eight"), percents, scores ("81 out
      of 100"), counts ("12 times", "twice"), rates ("4 switches an hour"), dates and number words.
    - A duration matches to the minute (plus or minus 1), or at the precision it was said in ("about 2 hours",
      "2.6 hours"). A clock time matches to the minute, "9 am" to the hour; without am or pm, either half of the
      day. Scores and percents match within 1, counts exactly.
    - A number with a unit no fact has ("155 seconds", "81 apps") only matches an amount written in a fact's
      label ("10+ minutes"). A date must be the story's day.
  - A story with any other number, the wrong length, or cut off is retried once, told what was wrong. After that,
    or when the model is away, the answer is a plain template story from the same facts, with `fallback: true`,
    `model: null` and `reason` saying why. It is still `200`.
  - Stories the model wrote are cached per day and time zone (`cached: true`, with the facts they were written
    from). A new one is written when the facts, the prompt, the number check or the configured model change. A
    second request while one is being written waits for it instead of asking the model again.
  - `in_progress: true` means the day is not over: the story is of the day so far, has no "last screen use",
    and is written again at most every 15 minutes. A day without data gets a short note and no model call.
- `POST /ask` (DT-40, viewer) with `{ "question": "How much YouTube after 11 pm last week?", "tz": "America/Toronto" }`
  (up to 500 characters) answers from your data:

  ```json
  { "answer": "Last week you watched 1 hour 50 minutes of YouTube after 11 pm, ...",
    "facts_used": [ { "label": "time in apps matching YouTube between 23:00 and 03:00, from Monday 2026-09-21 to Sunday 2026-09-27",
                      "value": 110, "unit": "minutes" } ],
    "tools_called": ["get_totals"],
    "chart": { "kind": "bar", "title": "...", "unit": "minutes", "points": [ { "label": "2026-09-21", "value": 30 } ] },
    "model": "qwen3-14b", "fallback": false, "declined": false, "reason": null }
  ```

  - The model calls tools that read the stats engine, at most 4 per question, each over at most 31 days and
    returning at most 40 facts (summaries first):
    - `get_totals`: screen time grouped by app, category, device, hour or day. It can be narrowed to an app or
      site (a word of its name, or part of it for 4 letters or more), a category, phones or computers, and a
      time of day. 23:00 to 03:00 runs into the next morning and counts for the evening it started on. A range
      with no data gets no total: missing is not zero.
    - `get_sessions`: the sessions themselves, with their times.
    - `get_focus`: focused time, focus score, pickups and switches per hour, per day.
    - `get_sleep`: sleep per night, and screen time after 11 pm the night before.
    - `get_calendar`: calendar events with their times, upcoming ones included (not all-day events).
    - `compare_plan`: how calendar time was spent, up to now.

    Streaks get a tool with DT-53.
  - `facts_used` is every fact the tools returned, and the answer goes through the story's number check against
    them. Dates must be days the tools looked at.
  - An answer that fails is retried once. After that, the facts themselves are the answer (`fallback: true`,
    `model: null`, `reason` says why).
  - A question that is not about your day is declined politely (`declined: true`).
  - `chart`, when present, is a small series from the last tool that had one.

Every number in `story` and `answer` appears in `facts_used` (the number check, DT-39). `/story` falls back to a
template when the model is down; `/ask` returns 503 `ai_unavailable` then (400 for an empty question).

### Insights and Wrapped

`GET /insights/day?date=2026-09-25&tz=America/Toronto` (DT-31, viewer) gives the Today tab's numbers from the stats
engine, so the dashboard never works one out itself:

```json
{ "date": "2026-09-25", "tz": "America/Toronto", "in_progress": false,
  "screen_minutes": 157.5, "phone_minutes": 37.5, "computer_minutes": 120.0,
  "focused_minutes": 45.0, "focus_score": 36, "work_or_study_minutes": 60.0, "distracted_minutes": 65.0,
  "pickups": 3, "switches_per_hour": 2.3,
  "sleep_minutes": 447.0, "sleep_estimated": true, "steps": 8412,
  "top_apps": [ { "app": "Minecraft", "category": "games", "minutes": 60.0 } ],
  "screen_estimated": false, "estimated": true }
```

- `screen_minutes` is the timeline's total (per device, added up); `phone_minutes` and `computer_minutes` add up to it.
  All three are null on a day without screen data (missing, not zero).
- `work_or_study_minutes` and `distracted_minutes` are the focus score's parts (DT-33 charts them next to the story):
  time in work or study apps and in social, video or game apps, on any device, overlaps counted once.
  `focus_score = round(100 x focused / (work_or_study + distracted))`; focused time is part of work or study.
- `top_apps`: up to 10 apps and sites, most time first, each with the category holding most of its time.
- `sleep_minutes`: last night (the night ending this morning). `steps`: the day's steps; when two phones send
  steps, the larger total.
- `screen_estimated` is true when some of the screen time was inferred (an iPhone app with no close event);
  `estimated` is true when anything was (screen time or sleep), so a badge on screen time follows `screen_estimated`.
- `in_progress` is true only for a day that has begun and not yet ended (today): every number is the day so far.
  A future day is false, with no data.

`GET /insights/{tab}?range=7d&tz=America/Toronto` (DT-41, viewer) gives an Insights tab its metrics and its
chart-ready series. `tab` is `overview`, `apps`, `devices`, `focus`, `sleep`, `food` or `calendar`. `range` is
`today`, a number of days ending today such as `7d` or `30d` (1 to 92), or `YYYY-MM-DD..YYYY-MM-DD` (up to 92
days); anything else is `400`.

```json
{ "tab": "overview", "tz": "America/Toronto", "in_progress": true, "cached": false,
  "range": { "first": "2026-09-19", "last": "2026-09-25", "days": 7, "label": "Last 7 days" },
  "metrics": [ { "id": "screen_time", "label": "Screen time", "value": 3120.5, "unit": "minutes",
    "explain": "All app and site time across devices, each device counted, away time removed.", "estimated": false } ],
  "series": {
    "screen_by_device": { "kind": "stacked", "title": "Screen time by device", "unit": "minutes", "explain": "...",
      "estimated": false, "x": ["2026-09-19", "..."], "lines": [ { "name": "Desk PC", "key": "windows-1", "values": [301.5, null, "..."] } ] },
    "categories": { "kind": "donut", "items": [ { "name": "Work", "key": "work", "category": "work", "value": 1450.0 } ], "...": "..." },
    "hours": { "kind": "heatmap", "x": ["00", "...", "23"], "y": ["Mon", "...", "Sun"], "cells": [ { "x": 9, "y": 0, "value": 42.0 } ] } },
  "meta": { "unit": "minutes", "range": { "start": "...", "end": "...", "tz": "..." }, "source": "seed", "estimated": false } }
```

- Every metric has a unit and a line saying what it means (`explain`), and `value` is null when there is no data
  for it in the range (missing, not zero). Counts that need no screen (meals, calendar events, nights) are 0 only
  on days the hub could have heard about: a device sent something for the day, or one was paired then. Before
  recording began, and on days still to come, they are null.
- A series has a `kind` and what that kind needs:
  - `trend`, `stacked`: `x` labels and `lines`, one value per label (null where a day had no data; in a line per
    device, also where that device sent nothing that day).
  - `bars`: `x` and one line, or `items` by name.
  - `donut`: `items`. `treemap`: `items` with `children`.
  - `heatmap`: `x`, `y` and `cells` (indexes into them).
  - `sankey`: `nodes` and `links`. Node names are unique: a device name two devices share, or one that is also a
    category's, gets the device id, as in `Galaxy phone (android-3)`. Lines and heatmap rows use the same names.
  - `scatter`: `points`, with `stats` (rho, p, n) and a `note` for a correlation.
  - `gauge`: `value` and `max`.
- The tabs and their series:
  - **overview:** screen time by device and day, categories, the focus score by day, weekday by hour, and phone
    against computer by day (null where no phone, or no computer, had data). Its metrics add the best day (most
    focused minutes, `best_day` and `best_day_focused`) and the toughest (most screen time after 11 pm,
    `toughest_day` and `toughest_day_late`), each with its reason in `explain`.
  - **apps:** top apps and sites, categories with their apps (treemap), categories by day, app switches an hour.
  - **devices:** each device's share, by day, by hour, and device to category (Sankey).
  - **focus:** the average score (gauge); focused, other work or study, and distracted time by day; switches by hour;
    late nights against the next day's focus (scatter, with Spearman's rho and "correlation, not cause").
  - **sleep:** each night, measured or estimated; bedtime and wake time (minutes after 18:00 the evening before);
    after 11 pm.
  - **food:** meals by day and type, when you ate, the most logged foods.
  - **calendar:** planned time by day and where it went (on plan, off plan, other screen time, no screen); the
    longest events with their on-plan share; weekday by hour.
- The overview also has `changes`: each headline number against the same number of days just before, as
  `{ id, label, unit, now, before, delta, change_pct, direction: up|down|same, better: up|down, days }`. Only whole
  days count (today, still going, is left out), and it needs at least 4 on each side (or half the range, for a
  short one); otherwise the list is empty. `direction` is `same` when the change rounds to nothing, and `better`
  says which way is good (less screen time, more focus and sleep). Other tabs have an empty list.
- Every series is cut from the same pieces as the stats engine's totals (`Stats.crosstab`), so a tab's charts add up
  to its totals and agree with Today and each other.
- Answers are cached per range and time zone until the data changes (`cached: true`). The database keeps a change
  counter (migration `0005`): its triggers count events added, replaced or deleted, category choices, and devices
  paired, renamed or revoked, whichever process makes the change, so a seed run from the command line counts too.
  A range that is not over (today in it, or days still to come) is also worked out again after a minute.
- On 14 seeded days every tab answers in about 0.3 s.

`GET /wrapped?week=2026-W38&tz=America/Toronto` (DT-41, viewer) is the week in review, Monday to Sunday. Without
`week`, it is last week (the last whole one).

```json
{ "week": "2026-W38", "first": "2026-09-14", "last": "2026-09-20", "tz": "America/Toronto", "in_progress": false,
  "metrics": [ "the overview's metrics for the week" ], "top_apps": [ { "app": "Code", "category": "work", "minutes": 1494.0 } ],
  "lines": [ "You spent 3942 minutes on screens this week.", "...", "..." ],
  "facts_used": [ { "label": "screen time this week", "value": 3942, "unit": "minutes" } ],
  "model": "google/gemma-4-e4b", "cached": false, "fallback": false, "reason": null, "streaks": [ { "id": "focus_flame", "met": 5, "days_with_data": 7, "longest": 4, "...": "..." } ], "meta": { "...": "..." } }
```

- `lines` are always three: highlight lines by the local model from the week's facts. Every number in them is
  checked like the story's (DT-39); lines that fail are retried once, then replaced by three plain lines from the
  same facts (`fallback: true`, `model: null`, `reason` says why).
- `in_progress` is true only while the week is going: a week still to come says false, as `/insights` does.
- The facts compare the week with the one before only a day for a day (average screen time a day), over whole days,
  and only when both weeks have at least 4 whole days with data. A week that only began being recorded, or this week
  on a Monday morning, would make any change look huge.
- The lines are cached per week and time zone like the story: until the facts or what wrote them change, and for a
  week not over yet at most every 15 minutes.
- `streaks` has each streak's week (DT-53): the days that met its rule (`met`, `dates`), the days with data, and the
  longest run inside the week.
- The first answer takes as long as the model writes (about 15 s with Gemma 4 E4B on this PC); cached answers are
  instant.

### Streaks, goals and achievements

DT-53. The rules are in `hub/daytrace_hub/data/streak_rules.json`, and every number comes from the stats engine,
so a streak agrees with Today and Insights. Every streak, goal and badge says its rule in words and the days that
counted.

`GET /streaks?tz=America/Toronto&days=30` (viewer):

```json
{ "tz": "America/Toronto", "date": "2026-09-25", "since": "2026-09-12",
  "streaks": [ { "id": "focus_flame", "name": "Focus flame", "rule": "240 or more focused minutes in a day",
    "needs": "a computer's data for the day", "unit": "minutes", "target": 240.0,
    "current": 3, "best": 5, "today": "met", "value": 384.6, "remaining": null,
    "counted": ["2026-09-23", "2026-09-24", "2026-09-25"],
    "best_dates": ["2026-09-17", "2026-09-18", "2026-09-19", "2026-09-20", "2026-09-21"],
    "days": [ { "date": "2026-09-22", "status": "missed", "value": 140.0 }, "..." ] } ] }
```

- The streaks: **Focus flame** (the focus target, in focused minutes), **Screens down** (15 minutes or less on the
  phone after 11 pm the night before), **Logged it** (a meal logged), **Balanced** (the social cap) and **Synced**
  (every paired phone and computer sent data that day).
- Each day is `met`, `missed` or `no_data`. No data means what the rule needs (`needs`) sent nothing for that day,
  such as no computer on a Sunday for Focus flame, a phone that sent nothing all day for Logged it, or for Screens down
  a night the phone didn't show up both that evening (from 18:00) and on the day. Such a day neither extends nor
  breaks a streak.
- `today` is `met` as soon as today qualifies. A limit (Screens down, Balanced, the bedtime) qualifies only once what
  it measures is over: the day, the night (03:00), or the sleep window (12:00, or as soon as the health app sends the
  night). Until then today is `at_risk`, and `remaining` says what is left: the minutes still to go, or the room
  left under the limit. `current` counts the days up to yesterday while today is at risk. Once today can no longer
  qualify (the limit passed), it is `missed` and `current` is 0.
- `best` is the longest run in the hub's history, up to a year back (`since` is the first day judged: the first day
  with screen data). `days` lists the last `days` days (1 to 366), oldest first.
- Goals apply to the whole history: a new target judges the past days again, so a streak always means what its rule
  says now.

`GET /goals?tz=` (viewer) is the daily goals with today's progress. `PUT /goals/{goal_id}` (the dashboard) with
`{ "target": 45 }`, or `{ "target": "23:00" }` for the bedtime, saves a new target; out of range is `400`, an unknown
goal is `404`.

```json
{ "tz": "America/Toronto", "date": "2026-09-25", "goals": [
  { "id": "social_cap", "label": "Social apps", "rule": "60 minutes or less in social apps in a day",
    "explain": "...", "kind": "at_most", "unit": "minutes", "target": 60.0, "default": 60, "min": 5.0, "max": 600.0,
    "today": { "value": 17.98, "status": "at_risk", "progress": 30 } } ] }
```

- `focus_target` (focused minutes, at least; 10 to 720, default 120), `social_cap` (social minutes, at most; 5 to
  600, default 60) and `bedtime` (asleep by, the night before; 20:00 to 03:00, default 23:30).
- `progress` is 0 to 100: toward a target, or how much of a limit is used. A bedtime is 100 when met, 0 when missed,
  and null while the night can still change. The bedtime is read by the wall clock, DST nights included.
- The demo profile's seed sets the focus target to 240 minutes, the goal its 5-day focus streak is built around.

`GET /achievements?tz=` (viewer):

```json
{ "tz": "America/Toronto", "unlocked": 4, "achievements": [
  { "id": "streak_7", "name": "One week strong", "rule": "Any streak reached 7 days.", "unlocked": true,
    "earned_on": "2026-09-18", "unlocked_at": "2026-09-25T21:00:00Z", "dates": ["2026-09-12", "...", "2026-09-18"],
    "progress": { "value": 7, "target": 7, "unit": "days" } } ] }
```

- The badges: first sync, a full set (a Windows PC, a Mac, an Android phone and an iPhone all sent data), 7-day and
  30-day streaks, 1,000 focused minutes, and a perfect week (Monday to Sunday with no goal missed and every goal met
  on at least 5 days; a day without data for a goal doesn't count against it).
- A badge, once earned, is kept with the day it was earned and when the hub first saw it (the `achievements`
  table, migration `0006`): later data never takes it back, and its `progress` shows complete. Locked badges show
  how far along they are. Re-running the seed clears them, since it replaces the history they came from.
- Streaks, goals and badges are worked out together, once for requests that arrive together, and reused for the
  rest of the minute unless the data or a goal changes. A day's readings that can't change any more are kept until
  new data arrives, so a new minute only reads today (and last night until 03:00). 90 seeded days take about
  0.15 s to work out from nothing on this PC.

### Privacy

Redaction (DT-44): a window, app or calendar title that matches a rule is stored as `[redacted]`, with the app name and
the times kept, and so is a browser extension's site. Your own words are hidden wherever they appear: a title, an app's
name or id, or a site (docs/privacy.md has the rules).

- `GET /privacy/redaction` (viewer) lists the rules in force:
  `{ "redacted": "[redacted]", "rules": [ { "id": "banking", "name": "Banking and payments", "description": "...", "builtin": true, "enabled": true, "words": [] }, ... ] }`.
  Your own rules come last, as `custom-1`, `custom-2` and so on, with their words.
- `PUT /privacy/redaction` (the dashboard) with `{ "disabled": ["health"], "custom": [ { "name": "Work client", "words": ["Acme", "Project Falcon"] } ] }`
  replaces your choices. Built-in rules can be switched off by id, and you can have up to 20 rules of your own, each
  with up to 50 words or phrases of 2 to 100 characters. A word matches the same letters or digits, ignoring case,
  and never as a regular expression. "Acme" hides `acme_notes.docx` and `Acme2026` but not `Acmeville`, and a phrase
  matches across spaces, underscores, hyphens and dots. A list longer than the limits is `422`. Any other problem is
  `400`, with the reason. The change applies to the next event stored.
- `POST /privacy/redaction/check` (viewer) with `{ "title": "Online Banking - TD Bank", "app": "Edge" }` (or
  `{ "domain": "mychart.example.org" }` for a site) says what would be stored:
  `{ "redacted": true, "stored_as": "[redacted]", "rule": "banking", "rule_name": "Banking and payments" }`.
- Saving rules leaves stored events alone. `GET /privacy/redaction/stored` (viewer) counts the stored events the rules
  in force would hide, as `{ "matches": 12 }`. `POST /privacy/redaction/apply` (the dashboard) with
  `{ "confirm": true }` hides them, returning `{ "redacted": 12 }`. It can't be undone, and without `confirm: true` it
  is `400`. It works a batch at a time, so collectors keep writing meanwhile. An event from a stateless collector is
  keyed again from its redacted form, and two that then match in everything are kept once.

- `GET /privacy/network` (viewer, DT-45) is the proof behind "no internet": every connection since the hub started,
  by the kind of network at the other end.

```json
{ "since": "2026-09-27T07:09:24Z", "internet_connections": 0,
  "outgoing": { "localhost": 2, "lan": 0, "tailscale": 0, "internet": 0 },
  "blocked": { "count": 1, "destinations": [ { "host": "8.8.8.8", "port": 1234, "count": 1, "last": "..." } ] },
  "incoming": { "localhost": 120, "lan": 44, "tailscale": 0, "internet": 0 },
  "refused": { "localhost": 0, "lan": 0, "tailscale": 0, "internet": 3 },
  "listening": ["127.0.0.1:8767", "[::1]:8767", "192.168.2.179:8767"], "guarded": true }
```

  - `outgoing` counts connections to the local model, the only thing the hub reaches out to, by the address
    each one really went to.
  - `blocked` counts every attempt refused, listing the first 20 destinations. That includes the model transport's
    refusals and the socket guard's: with `guarded`, which is on in every real hub, nothing in the hub process can
    open a connection to the internet, whatever code asks.
  - `incoming` counts requests served. `refused` counts requests turned away: from a network the profile doesn't
    serve, with a Host name that could be DNS rebinding, or from another site's page.
  - `internet_connections` is outgoing plus incoming internet, and is always 0. `listening` is where the hub
    listens right now.
- `GET /privacy/export` (the hub computer only, DT-46) is everything the profile holds, as one JSON file
  (`Content-Disposition: attachment`, `daytrace-<profile>-<date>.json`), streamed in pieces:
  `{ "daytrace_export": 1, "profile": "demo", "exported_at": "...", "schema_version": 6, "note": "...", "tables": { "events": [ ... ], "devices": [ ... ], ... } }`.
  - It holds every table with your data, including a table a later version adds, as of one moment, so a device
    syncing during the export doesn't split it. The hub reads a copy it takes first and removes afterwards, so a
    slow download never holds the database.
  - JSON columns (an event's `data`, settings, goals, badges) come as JSON. Device tokens are never exported, not
    even their hashes. The database's own bookkeeping (migrations, the change counter) is left out.
- `POST /privacy/delete` (the hub computer only, DT-46) needs `{ "confirm": "delete all my daytrace data" }`,
  exactly, on every call. Any other phrase is `400`, and nothing is deleted.
  - It empties every data table and keeps the schema, so the hub keeps working. Paired devices must pair again, a
    pairing code shown before it no longer works, and the desktop tracker starts afresh with nothing it held from
    before.
  - Your own redaction rules are kept, so what is recorded next stays protected, unless you send
    `"keep_redaction_rules": false`.
  - It returns `{ "deleted": { "events": 1520, ... }, "wiped": true }`. `wiped` means the deleted rows are gone from
    the file too: compacted, with the write-ahead log folded back. Every connection overwrites what it deletes
    (`secure_delete`), so a row replaced earlier leaves nothing behind either. `wiped` is `false` only when another
    connection kept the compacting from finishing just then.
