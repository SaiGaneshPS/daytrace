# Demo script

Three minutes, eight steps, a fallback for every one. Everything runs on the demo profile (seeded data, port 8767):
your real data (the personal profile) is never on screen.

## Start it (5 minutes before)

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev-hub.ps1 -Profile demo
```

On a Mac or Linux: `scripts/dev-hub.sh --profile demo`. It:
1. seeds 14 fresh days (only demo data is replaced);
2. starts the hub;
3. wakes the local model up by writing yesterday's story and last week's Wrapped ahead, so both show at once on stage;
4. opens the dashboard at `http://localhost:8767`.

It says what it did at each step. When the model isn't answering, it says so: start LM Studio, load the model, and
run it again. `-NoSeed` keeps the data as it is; `-NoOpen` doesn't open the browser. Ctrl+C stops the hub.

Without the phone, one command plays its part (`live` or `nudge`), on the hub computer:

```powershell
.\hub\.venv\Scripts\python.exe -m daytrace_hub demo live
.\hub\.venv\Scripts\python.exe -m daytrace_hub demo nudge
```

It sends events as the demo's Android phone, through the same path a phone uses. `nudge` also shows the nudge as a
desktop notification. A second nudge within 5 minutes stays quiet (the hub sends one at a time); `--again` lets it
speak.

## Before the demo (checklist)

- [ ] **Network.** The PC and the phone are on the same network. Home routers that keep Wi-Fi devices apart (client
      isolation, as the Bell Home Hub does) need the PC on Ethernet or the phone on the PC's Mobile hotspot.
      Check: Devices shows the phone's last contact as "just now".
- [ ] **Pairing.** The phone is paired to the demo hub (Devices, pairing code or QR), and its lane shows on Today.
- [ ] **Phone battery.** Above 50%, charger nearby. Daytrace is in Samsung's "Never sleeping apps", battery
      Unrestricted, and live mode is on (DT-24).
- [ ] **Model loaded.** LM Studio is running with the model loaded; the start script said "The model ... answers".
      A question on Ask answers in under 20 seconds.
- [ ] **Screen scaling.** The browser zoom is 100% (Ctrl+0), and the display scaling is what the room's screen needs.
      The dashboard adapts to any width. Check that Today's timeline fits without scrolling sideways.
- [ ] **Browser.** An InPrivate window on `http://localhost:8767` (Ctrl+Shift+N in Edge). It starts with no
      celebrated badges, so step 5's confetti plays, and no extensions or saved logins show.
- [ ] **Notifications.** Windows' Do not disturb is off, so the nudge's notification shows.
- [ ] **Quiet screen.** Close chats, mail and anything else with private titles. Only the demo browser is open.
- [ ] **Internet cable or Wi-Fi switch within reach** for step 4 (unplug it, or switch Airplane mode on for the PC
      only, not the network the phone uses).
- [ ] **Rehearsed today** at least twice, all eight steps, with the timings below.

## 1. Hook (20 s)

- **Say:** "Where did my day go? Your phone knows part of it, your laptop another part. Nothing puts it together,
  and the apps that try send it all to someone's cloud. Daytrace keeps it on your own network."
- **Do:** Today. Point at the timeline: one lane per device (Windows, Mac, Android, iPhone), the calendar, sleep.
- **See:** the day so far, colored by category, with totals above it.
- **Fallback:** if Today is slow, it is the hub starting: wait for the stat cards. If it can't reach the hub, run the
  start script again (`-NoSeed`).

## 2. Live: phone to PC (30 s)

- **Say:** "Watch: I open Instagram on my phone..."
- **Do:** open Instagram on the paired phone for a few seconds, then YouTube, then lock it.
- **See:** within a few seconds the Android lane on Today grows up to now, with its live dot.
- **Fallback:** no phone, or it doesn't sync: run `daytrace_hub demo live` on the PC. The same thing lands on the
  Android lane at once (Today refreshes by itself).

## 3. A nudge (25 s)

- **Say:** "Tracking alone rarely changes anything, so Daytrace nudges at the right moment. I'm meant to be
  studying..."
- **Do:** during a calendar block called "Study ...", open TikTok on the phone.
- **See:** the phone shows "Time to focus: TikTok during "Study ...", which runs until ...".
- **Fallback:** `daytrace_hub demo nudge` on the PC. It puts a study block on the phone's calendar and opens TikTok in
  it, and the same nudge shows as a desktop notification. If it prints "No nudge", run it with `--again`.

## 4. The AI, with the internet off (35 s)

- **Say:** "Everything you're about to see runs on this PC. I'll unplug the internet."
- **Do:** unplug the network cable (or switch the PC's Wi-Fi off). Ask: tap "How much YouTube did I watch last
  week?", or type "When did I sleep last night?".
- **See:** an answer with its numbers, the facts it used, and the tools it called. Then Privacy: "0 internet
  connections since the hub started".
- **Fallback:** if the model is slow, open Story for yesterday, already written by the start script. If the model
  is down, Ask says so and answers from the numbers; Story and Wrapped show their plain versions. Either way, the
  Privacy page still says 0.

## 5. Streaks and a badge (20 s)

- **Say:** "It's meant to be fun: streaks, goals and badges, all from real numbers."
- **Do:** Streaks. Scroll to the badges.
- **See:** each streak's flame and days; the goal rings; badges pop with confetti the first time they are seen (the
  InPrivate window).
- **Fallback:** no confetti means this browser has celebrated them before: open a new InPrivate window.

## 6. Insights (25 s)

- **Do:** Insights, 7 days. Overview (screen time, best and toughest day), then Apps and devices (the leaderboard,
  the switches between devices), Focus and sleep (late nights against the next day's focus, "correlation, not
  cause").
- **Say:** "Every chart is computed by plain code; the AI only puts it in words, and its numbers are checked."
- **Fallback:** if a chart is slow, stay on Overview: the other tabs are the same data cut differently.

## 7. Wrapped (15 s)

- **Do:** Wrapped. The card for last week reveals itself; tap Share on the phone, or Save as image on the PC.
- **See:** the week's screen time, focus, sleep, top apps, streaks, and three lines by the local model.
- **Fallback:** the start script wrote it ahead, so it is instant. Without a model it shows plain lines, still
  correct.

## 8. Close (10 s)

- **Say:** "Your devices, your network, your AI. Daytrace: where your day went, and nobody else's business."
- **Do:** back to Privacy: the 0, the folder the data lives in, Export and Delete.

## Fallbacks at a glance

| Step | If it fails | Do |
|---|---|---|
| Start | the model isn't answering | Start LM Studio, load the model, run the start script with `-NoSeed` |
| 1 Hook | the page is empty | Run the start script again (it seeds and starts the hub) |
| 2 Live | the phone doesn't sync | `daytrace_hub demo live` |
| 3 Nudge | no nudge on the phone | `daytrace_hub demo nudge` (or `--again`) |
| 4 AI | slow or no model | Story for yesterday (written ahead), then Privacy |
| 5 Streaks | no confetti | A new InPrivate window |
| 6 Insights | a slow chart | Stay on Overview |
| 7 Wrapped | no model | The plain lines are correct too |
