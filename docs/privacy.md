# Privacy

> Filled in by **DT-44 / DT-45 / DT-47**. Goal and instructions are in the Notion ticket.

## What is collected

Only what the collectors send, as events ([event-schema.json](event-schema.json)), and what you choose.

- **Computers** (the desktop tracker): the app in front, its window title, and from when to when. Also away time,
  worked out from how long ago the keyboard or mouse was last used, never from which keys.
- **Android phones:** each app's name and package, from when to when, and when the screen turned on and off. No
  titles, and nothing from inside the apps.
- **Collectors not built yet:**
  - the browser extension sends the active tab's domain only, never the full address or the page (DT-18);
  - iPhone Shortcuts send app opens and closes, sleep, steps, calendar events and the meals you log (DT-26 to
    DT-28);
  - the Android app adds sleep, steps, meals and calendar from Health Connect (DT-23).
- **About each device:** its name and type, when it paired and was last seen, and a hash of its token (never the
  token itself).
- **Your choices:** the categories you set, your goals, your redaction words, and your nudge settings.
- **What the hub keeps of its own:** the text of stories and Wrapped, the badges you earned, and a log of the
  nudges it sent.

Nothing is read from inside an app: no screenshots, no keystrokes, no page contents or full web addresses. A window
title is the most detailed thing stored, and the redaction rules below keep the sensitive ones out.

## Where it is stored

- **On the hub computer**, in one SQLite file per profile, `daytrace-<profile>.db` (with its `-wal` and `-shm`
  files while the hub runs), in:
  - Windows: `%LOCALAPPDATA%\Daytrace`
  - macOS: `~/Library/Application Support/Daytrace`
  - Linux: `$XDG_DATA_HOME/daytrace` (by default `~/.local/share/daytrace`)
  - or the folder named in `DAYTRACE_DATA_DIR`.
- **What the Privacy page shows:** the file, its size, how many events it holds and from which devices. The folder
  is shown only on the hub computer itself.
- **Not encrypted by the hub.** Your computer account protects the file, and so does disk encryption (BitLocker,
  FileVault) if you use it.
- **On an Android phone:** events wait in the app's own private storage until the hub has them. The app turns
  Android's backups off, so neither the events nor the hub token go to a cloud backup or a new phone.
- **No other copies.** Daytrace uploads and backs up nothing. Your computer's own backups may include the file.

## What never leaves your network

Nothing does. The hub never talks to the internet, and it can show you (DT-45). The Privacy page reads
`GET /api/v1/privacy/network`.

- **Where it listens:** only on this computer and on the addresses your phone uses, which are your Wi-Fi or Ethernet
  adapter's.
  - It never listens on every interface, on a public address, on a VPN's or a virtual machine's adapter, or, for your
    own data, on Tailscale. Only the shared-dev profile listens on the tailnet.
  - When the Wi-Fi changes or the router hands out a new address, the hub starts listening on the new address
    within about 15 seconds and closes the old one. Phones are only ever told addresses it already answers on.
  - A carrier-grade NAT address (the range Tailscale also uses) on an ordinary adapter is never listened on.
- **Who it answers:** requests from this computer and your LAN, and from your tailnet on shared-dev only. It never
  answers the internet. It also refuses a Host name that could be DNS rebinding, and a request made by another web
  page, even one on this computer. It sends no CORS headers, so no other site can read what it answers.
- **What it reaches out to:** only the local model (LM Studio or Ollama), through one transport that lets a request
  through only to this computer, your LAN, or (shared-dev) your tailnet. It checks every address a name points to,
  and sends no proxy settings, no redirects and no stray credentials.
- **The socket guard:** under that, the hub process refuses any connection to an internet address made through
  Python's sockets, before a packet leaves, whatever code asks: a library, an SDK, anything added later. It is a
  Python audit hook on `socket.connect` and `socket.sendto`, which can't be switched off once on. The only
  exceptions are the mDNS multicast group and the documentation address the hub asks its route with, which never
  sends anything. Name lookups go to your computer's own resolver.
  - **What it can't see:** sockets that don't go through Python's socket module, such as a C extension's own, the
    uvloop event loop's (uvicorn uses it on macOS and Linux), or another process's. The hub's own way out, to the
    local model, uses Python sockets, so the guard covers it.
- **The count:** since it started, the hub counts every connection it made or blocked, and every request it served
  or refused, by where it went or came from. `internet_connections` is always 0, and a blocked destination is
  listed by name, so you can see what tried.
- **mDNS** (how phones find the hub) stays on your LAN's multicast group, and never crosses Tailscale.

## Redaction rules

Some titles say too much: a bank's account page, a patient portal, the logins in a password manager, a private
browser window, a therapy appointment. The hub never stores them (DT-44). A title that matches a rule is stored as
`[redacted]`. The app name and the times stay, so the time still counts on the timeline and in every total, and only
the words are gone. A redacted calendar event keeps whether it is all-day, and nothing else a collector sent with it,
such as a location or notes. A browser extension's site that matches a rule (a bank, a patient portal) is stored as
`[redacted]` too.

- **Built-in rules** (`hub/daytrace_hub/data/redaction_rules.json`):
  - **Banking and payments:** online banking, account pages, statements and transfers, the big Canadian and US banks,
    PayPal, Venmo, Revolut and others.
  - **Health portals and appointments:** MyChart and patient portals, records and results, prescriptions, and
    appointments with doctors, dentists and therapists.
  - **Password managers:** every title in 1Password, Bitwarden, LastPass, KeePass, Dashlane and others.
  - **Private and incognito windows:** InPrivate, Incognito and Private Browsing windows.
- **Your own rules:** on the Privacy page, add words or phrases (a client's name, a project) and switch built-in rules
  off. A word matches the same letters or digits, ignoring case: "Acme" hides `acme_notes.docx` and `Acme2026`, but
  not `Acmeville`. A phrase matches across spaces, underscores, hyphens and dots. Your words are hidden wherever they
  appear: in a title, an app's name, or a site. A change applies to the next event, from any device.
- **What is already stored:** saving a rule doesn't change history. The Privacy page shows how many stored events
  your rules would hide, and hides them when you confirm. That can't be undone.
- **Where it happens:**
  - On this computer, the desktop tracker redacts a title before it is ever held. It looks for new rules every few
    seconds.
  - Every event is redacted again as it arrives, whichever device sent it (phones, the browser extension, iPhone
    Shortcuts, the demo seed).
  - An event sent again after a rule changed is still one event, and the stored copy is redacted then too.
- **Nothing kept on the side:** the key that stops a resent event being stored twice is worked out from the redacted
  event, so not even a hash of the title is kept. The cost: two redacted events from a device that sends no event
  numbers (iPhone Shortcuts), matching in everything else (kind, times, app), count as one.
- **Code isn't health data:** a file named like a word (`pharmacy.ts`, `Clinic.cs`, `dr.py`) doesn't count, and
  "Dr." only counts before a capitalised name. The rules still err on the side of hiding: a TV show called "Hospital
  Playlist" is redacted.

## HTTPS on the LAN (mkcert)

TODO (DT-44 / DT-45 / DT-47)

## Export and delete

Everything the hub holds is yours to take or to erase (DT-46). Both only work from the hub computer itself, never
from a phone. The Privacy page (DT-36) will put them behind buttons; until then they are
`GET /api/v1/privacy/export` and `POST /api/v1/privacy/delete`.

- **Export:** one JSON file with every table: events, devices (without their tokens), categories, goals, badges,
  stories and settings, as of one moment.
- **Delete all:** send the exact phrase **delete all my daytrace data**, every time.
  - The hub empties every table and compacts the file. Every connection overwrites what it deletes, so the deleted
    rows (and older versions of rows it replaced) aren't left in the file.
  - Copies outside the hub are beyond it: a backup of your computer, or a disk's own spare blocks.
  - The hub keeps running: phones and browsers pair again, and the desktop tracker starts afresh with nothing from
    before.
  - Your own redaction words are kept by default, so what is recorded next stays protected. Ask for them to go too
    (`keep_redaction_rules: false`) if the words themselves are what you want gone.

