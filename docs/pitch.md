# Pitch

The story behind the talk and the slides. The talk is about 3 minutes around the live demo
([demo-script.md](demo-script.md)), and each section below is one or two slides. The sources are at the end.

## Problem

**Where did my day go?** Your phone knows part of your day, your laptop another part, and nothing puts the two
together.

- **47%** of US adults under 30 tried to cut their screen time in the past year, and **51%** say their phone mostly
  hurts their sleep, against 27% of older adults [1].
- **91%** of 18 to 34 year olds are doing something to reduce their screen time [2].

People already want to cut back. What they lack is a true picture of where the time went, across every device, and
a nudge at the moment it matters.

## Why now

- **Tracking alone doesn't change much.** Screen-time apps make people more aware of their use, but rarely lead them
  to use their phones less [3].
- **A pause at the right moment does.** In a 6-week field study with 280 people, a short pause before a chosen app
  opened cut how often that app was actually opened by **57%** [4].
- **Local AI is good enough now.** A small open model (Gemma 4 E4B, about 5 GB) runs on an ordinary gaming PC and
  answers "How much YouTube did I watch last week?" in seconds, from your own numbers.
- **"Local" still has to be built safely.** In April 2026 a researcher showed that malware running as the user can
  quietly pull everything Windows Recall captured [5]. What you do on your screens all day is some of the most
  personal data there is.

## What exists and the gap

- **Built-in Screen Time and Digital Wellbeing** each see one ecosystem. An Android phone and a Windows PC never
  meet, and you get numbers, not explanations.
- **Blockers** prove the demand. Opal passed 1 million daily users and $10 million a year in recurring revenue, and
  two thirds of its daily users are high school and college students [6]. But blockers block; they don't show you
  your day.
- **Desktop time trackers** mostly see one computer, and many send your activity to their own cloud.

**The gap:** nothing puts your phone and your computer on one timeline, explains the day in plain words, nudges you
at the right moment, and keeps all of it on your own network.

## Demo

Eight steps in three minutes, each with a fallback ([demo-script.md](demo-script.md)):

1. **Hook:** Today, one lane per device, with the calendar and sleep.
2. **Live:** open Instagram on the phone, and its lane on the PC grows.
3. **A nudge:** TikTok during a study block gets a gentle nudge.
4. **The AI, with the internet unplugged:** Ask answers from the numbers, and Privacy still says 0 internet
   connections.
5. **Streaks and a badge:** flames, goal rings and confetti.
6. **Insights:** the week, the apps and devices, and late nights against the next day's focus.
7. **Wrapped:** the week's card, saved or shared.
8. **Close:** your devices, your network, your AI.

If the live demo fails, play the backup recording, [docs/img/demo.webm](img/demo.webm): about 2 minutes, captioned,
no sound, on the demo profile's made-up data. It shows every step, with `daytrace_hub demo live` and `demo nudge`
playing the phone's part.

The slides follow the sections of this page, and are kept outside the repo.

## How it works

- **Collectors** on each device (a Windows tracker today, an Android app, and iPhone Shortcuts, a Mac bridge and a
  browser extension to come) send events to the **hub** over your Wi-Fi.
- **The hub** (Python and SQLite on any computer) turns them into sessions, and plain code works out every number:
  totals, focused time, pickups, sleep, streaks.
- **A local model** puts those numbers into words: the day's story, answers to your questions, Wrapped's lines.
  Every number it writes is checked against the facts, and if one doesn't match, you get a plain summary instead.
- **The dashboard** is a web app the hub serves itself, so it works in any browser, on any device.

The details: [architecture.md](architecture.md).

## Privacy

- **No cloud:** the hub keeps everything in one file on your computer.
- **No internet:** it listens only on your home network, reaches out only to the local model, and a socket guard
  refuses the rest. The Privacy page shows the count: 0 internet connections.
- **Sensitive titles are never stored:** banking, health portals, password managers, private windows, and any words
  you add.
- **Every device is paired** with a one-time code and can be revoked.
- **Yours to take or delete:** export everything, or delete everything, from the hub's own computer.

The details: [privacy.md](privacy.md).

## What's next

- **Every device:** pairing the Android app on real home networks (PR #16), then health and calendar from the phone
  (DT-23), live mode and nudges on the phone (DT-24), iPhone Shortcuts and the Mac bridge (DT-26 to DT-29), the
  macOS tracker (DT-17) and the browser extension (DT-18).
- **Phones as first-class screens:** HTTPS on the home network (DT-47), so the dashboard installs and works offline
  on phones (DT-35), and a signed Android app on GitHub Releases (DT-25).
- **Smarter without more data:** the local model sorts unknown apps into categories and reads meals from plain text
  (DT-42).
- **Tighter tokens:** collectors that can send events but not read them back.

## Sources

1. YouGov, "For many Americans, their smartphone is the last thing they see at night and the first thing they see
   in the morning", 2025 (surveys of about 1,100 US adults in May and July 2025).
   <https://yougov.com/en-us/articles/53735-for-many-americans-their-smartphone-is-the-last-thing-they-see-at-night-and-the-first-thing-they-see-in-the-morning>
2. Ipsos Consumer Tracker, "Almost all younger people are trying to limit their screen time", September 16, 2026.
   <https://www.ipsos.com/en-us/almost-all-younger-people-are-trying-limit-their-screen-time>
3. Laura Zimmermann, "Your Screen-Time App Is Keeping Track: Consumers Are Happy to Monitor but Unlikely to Reduce
   Smartphone Usage", Journal of the Association for Consumer Research 6(3), 2021.
   <https://www.journals.uchicago.edu/doi/abs/10.1086/714365>
4. David J. Grüning et al., "Directing smartphone use through the self-nudge app one sec", PNAS, 2023.
   <https://www.pnas.org/doi/10.1073/pnas.2213114120>
5. Computerworld, "Microsoft's Windows Recall still allows silent data extraction", April 2026.
   <https://www.computerworld.com/article/4159649/microsofts-windows-recall-still-allows-silent-data-extraction-2.html>
6. RevenueCat, Sub Club podcast with Opal's Kenneth Schlenker, April 29, 2026.
   <https://www.revenuecat.com/blog/growth/kenneth-schlenker-sub-club-podcast-2026>
