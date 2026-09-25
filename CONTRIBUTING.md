# Contributing

## The workflow

1. Pick a ticket on the [Notion board](https://app.notion.com/p/2f1d4a21e5748062b721ee456b9738c1?v=2f1d4a21e574812cadd0000cd05757ea)
   and move it to **In progress**.
2. Create a branch from an up-to-date `main`, named `DT-<id>-short-slug`, for example `DT-11-ingest-api`.
3. Only touch the files the ticket lists. The repo layout is fixed (DT-1); changing it needs its own ticket.
4. Open a PR titled `DT-<id>: short summary`. One ticket per PR. Fill in the PR template.
5. **Every PR is reviewed for bugs before it is merged.** Fix the findings on the same branch, and reply to
   each one (fixed, or why no change is needed).
6. CI must pass. Once DT-5 is in place, the PR title moves the Notion ticket automatically
   (opened: In review, merged: Done).
7. Merge with **Squash and merge** so `main` keeps one commit per ticket.

`main` is protected: nobody can push to it directly, so everything goes through a PR.

## Before you push

| Part | Windows | macOS / Linux |
|---|---|---|
| Hub | `cd hub; .\.venv\Scripts\python.exe -m pytest` | `cd hub && .venv/bin/python -m pytest` |
| Dashboard | `cd dashboard; npm run build` | `cd dashboard && npm run build` |
| Android (after DT-19) | `cd android; .\gradlew.bat assembleDebug` | `cd android && ./gradlew assembleDebug` |

## Never commit

- Tokens, `.env` files, keystores (`*.jks`, `*.keystore`), certificates and private keys (`*.pem`, `*.key`,
  `*.p12`, `*.pfx`). The Android signing key lives outside the repo (for example `D:\Hackathon\keys`).
- Databases (`*.db`). Hub data lives in `DAYTRACE_DATA_DIR`.
- Your real activity data. Share only the `shared-dev` or `demo` hub profile (see docs/remote-collab.md).

This repo is **public**, so anything committed can be seen by anyone.

## Reviews

[.github/CODEOWNERS](.github/CODEOWNERS) requests reviews automatically: `ios/`, `mac/`, the macOS tracker and
the Mac setup guide go to @snoween, and everything else to @SaiGaneshPS.
