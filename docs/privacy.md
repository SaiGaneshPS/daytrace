# Privacy

> Filled in by **DT-44 / DT-45 / DT-47**. Goal and instructions are in the Notion ticket.

## What is collected

TODO (DT-44 / DT-45 / DT-47)

## Where it is stored

TODO (DT-44 / DT-45 / DT-47)

## What never leaves your network

TODO (DT-44 / DT-45 / DT-47)

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

TODO (DT-44 / DT-45 / DT-47)

