# Contributing

## The workflow

1. Pick a ticket on the Notion board (link added in DT-4). Move it to **In progress**.
2. Create a branch named `DT-<id>-short-slug`, for example `DT-11-ingest-api`.
3. Only touch the files the ticket lists. The repo layout is fixed (DT-1); changing it needs its own ticket.
4. Open a PR titled `DT-<id>: short summary`. One ticket per PR.
5. CI must pass. Once DT-5 is in place, the PR title moves the Notion ticket automatically
   (opened: In review, merged: Done).

## Before you push

- Hub: `cd hub` then `.venv/Scripts/python -m pytest` (macOS/Linux: `.venv/bin/python -m pytest`).
- Dashboard: `cd dashboard` then `npm run build`.
- Android: `cd android` then `./gradlew assembleDebug` (after DT-19).

## Never commit

- Tokens, `.env` files, keystores (`*.jks`, `*.keystore`). The Android signing key lives in `D:\Hackathon\keys`.
- Databases (`*.db`). Hub data lives in `DAYTRACE_DATA_DIR`.
- Your real activity data. Share only the `shared-dev` or `demo` hub profile (see docs/remote-collab.md).

## Reviews

`.github/CODEOWNERS` routes `ios/` and `mac/` to the teammate and everything else to you.
