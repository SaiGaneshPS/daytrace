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
| `GET /categories` | viewer | DT-14 |
| `PUT /categories/{key}`, `DELETE /categories/{key}` | dashboard (viewer token or the hub computer) | DT-14 |
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
to the hub computer's IANA time zone (sent back in `tz`). Unknown time zones and dates outside 1970-01-01 to
9998-12-31 get `400`, impossible dates such as `2026-02-30` get `422`.

Example with two devices (the `...` stands for more sessions of the same shape):

```json
{
  "date": "2026-09-25", "tz": "America/Toronto",
  "lanes": [
    { "device_id": "windows-1", "device_type": "windows", "name": "Desk PC", "counted": true, "seconds": 4500, "minutes": 75.0,
      "sessions": [ { "start": "2026-09-25T09:00:00-04:00", "end": "2026-09-25T09:40:00-04:00", "seconds": 2400,
        "minutes": 40.0, "app": "Code", "app_id": null, "title": "stats.py", "category": "work", "kind": "app",
        "estimated": false }, "..." ] },
    { "device_id": "android-1", "device_type": "android", "name": "Galaxy phone", "counted": true, "seconds": 2100, "minutes": 35.0,
      "sessions": [ "..." ] } ],
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
