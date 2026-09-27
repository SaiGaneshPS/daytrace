# Privacy

> Filled in by **DT-44 / DT-45 / DT-47**. Goal and instructions are in the Notion ticket.

## What is collected

TODO (DT-44 / DT-45 / DT-47)

## Where it is stored

TODO (DT-44 / DT-45 / DT-47)

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
- **The socket guard:** under that, the whole hub process refuses any connection to an internet address before a
  packet leaves, whatever code asks: a library, an SDK, anything added later. It is a Python audit hook, which
  can't be switched off once on. The only exceptions are the mDNS multicast group and the documentation address
  the hub asks its route with, which never sends anything. Name lookups go to your computer's own resolver.
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
from a phone.

- **Export:** the Privacy page's "Export all" downloads one JSON file with every table: events, devices (without
  their tokens), categories, goals, badges, stories and settings, as of one moment.
- **Delete all:** type the exact phrase **delete all my daytrace data**, every time. The hub then empties every
  table, overwrites what it deleted, and compacts the file, so nothing deleted stays on the disk. The hub keeps
  running; phones and browsers pair again, and the desktop tracker starts afresh. The built-in redaction rules
  still apply. Your own words and goals are deleted too, since they can say something about you.

