# Contributing

## The workflow

1. Pick a ticket on the [Notion board](https://app.notion.com/p/2f1d4a21e5748062b721ee456b9738c1?v=2f1d4a21e574812cadd0000cd05757ea)
   (team members only) and move it to **In progress**.
2. Create a branch from an up-to-date `main`, named `DT-<id>-short-slug`, for example `DT-11-ingest-api`.
3. Only touch the files the ticket lists. The repo layout is fixed (DT-1); changing it needs its own ticket.
4. Open a PR titled `DT-<id>: short summary`. One ticket per PR. Fill in the PR template.
5. **Every PR is reviewed for bugs before it is merged.** Post the findings on the PR, fix them on the same
   branch, and post a reply saying what happened to each one (fixed, or why no change is needed). Only
   then tick the review box in the PR description and merge.
6. CI must pass. Once DT-5 is in place, the PR title moves the Notion ticket automatically
   (opened: In review, merged: Done).
7. Merge with **Squash and merge** (the only merge method the repo allows). The squash commit takes the PR
   title, so `main` gets one `DT-<id>: ...` commit per ticket.

`main` is protected: nobody can push to it directly (admins included), force-pushes and deletion are
blocked, and a PR is required. No approval count is required, because GitHub never lets authors approve their
own PRs and most tickets have a single owner. The bug review in step 5 is the gate instead.

## Before you push

Run from the **repo root**:

| Part | Windows (PowerShell) | macOS / Linux |
|---|---|---|
| Hub lint | `hub\.venv\Scripts\python.exe -m ruff check hub` | `hub/.venv/bin/python -m ruff check hub` |
| Hub tests | `hub\.venv\Scripts\python.exe -m pytest hub` | `hub/.venv/bin/python -m pytest hub` |
| Dashboard | `npm --prefix dashboard run build` | `npm --prefix dashboard run build` |
| Android (after DT-19) | `android\gradlew.bat -p android assembleDebug` | `./android/gradlew -p android assembleDebug` |

One-time setup for each part is in its own README ([hub](hub/README.md), [dashboard](dashboard/README.md),
[android](android/README.md)).

## Never commit

This repo is **public**: anyone can read everything in it, including the history.

- Tokens and `.env` files.
- Keystores and their passwords: `*.jks`, `*.keystore`, `keystore.properties`. Signing files live outside the
  repo (for example `D:\Hackathon\keys`).
- Certificates and private keys: `*.pem`, `*.key`, `*.p12`, `*.pfx` (mkcert, DT-47).
- **Exported iPhone Shortcuts that contain your hub address or device token.** Before exporting to
  `ios/shortcuts/exported/`, replace them with Shortcuts **Import Questions**, so each person enters their
  own values when they import it.
- Databases (`*.db`). Hub data lives in `DAYTRACE_DATA_DIR`.
- Your real activity data. Share only the `shared-dev` or `demo` hub profile (see docs/remote-collab.md).

## Reviews

[.github/CODEOWNERS](.github/CODEOWNERS) requests a review from the owner of the files a PR touches:
`ios/`, `mac/`, the macOS tracker and the Mac setup guide go to @snoween, and everything else to @SaiGaneshPS.
GitHub never asks the PR's own author to review, so PRs in your own area get no automatic reviewer. The bug
review in step 5 covers them.
