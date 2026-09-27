# Pitch

The story behind the talk and the slides, one section per slide or two. The live demo takes 3 minutes
([demo-script.md](demo-script.md)), and the slides before and after it about 2 more, so plan on 5. If the slot is
shorter, cut slides, not demo steps. The sources are at the end.

## Problem

**Where did my day go?** Your phone keeps one record of your day and your computer another. An Android phone and a
Windows PC never meet, and even Apple's Screen Time, which combines an iPhone and a Mac, gives you totals rather
than an explanation.

- **47%** of US adults under 30 say they tried to cut their screen time in the past year, and **51%** say their
  phone mostly hurts their sleep, against 27% of older adults [1].
- **91%** of 18 to 34 year olds say they do at least one thing to limit their screen time [2].

The two surveys ask different things: trying to cut back over a year, and doing anything at all to limit it. Either
way, most young adults want less. What they lack is a true picture of where the time went, across every device, and
a nudge at the moment it matters.

## Why now

- **Tracking alone doesn't change much.** Screen-time apps make people more aware of their use, but rarely lead them
  to use their phones less [3].
- **The moment matters.** In a 6-week field study with 280 people, a short pause before a chosen app opened, with
  the choice to back out, cut how often that app was opened by **57%** compared with their first week [4].
  Daytrace's nudges aim at the same moment without blocking anything: a gentle notice when an app pulls you away
  from a study block or keeps you up past your bedtime.
- **Local AI is good enough now.** A small open model (Gemma 4 E4B, about 5 GB) runs on our gaming PC (a 16 GB
  graphics card) and answers questions about the day in about 5 to 15 seconds, from your own numbers.
- **"Local" still has to be built with care.** In April 2026 a researcher showed that malware already running as the
  user can quietly pull everything Windows Recall captured [5]. Daytrace keeps far less: which app was in front and
  its window title, never screenshots or keystrokes, and never a sensitive title. Like any app on your computer, it
  can't protect its data from malware already running as you: the database isn't encrypted, and programs on the
  hub computer are trusted.

## What exists and the gap

- **Built-in Screen Time and Digital Wellbeing** each see one ecosystem, and give you numbers, not explanations.
- **Blockers** prove the demand. Opal passed 1 million daily users and $10 million a year in recurring revenue, and
  two thirds of its daily users are high school and college students [6]. But blockers block apps; they don't
  show you your day.
- **Time trackers:** ActivityWatch is open source and keeps its data local, on computers and on Android, but joining
  your devices into one view is still being worked on [7]. Many other trackers keep your activity in their own
  cloud.

**The gap:** nothing we found puts your phone and your computer on one timeline, explains the day in plain words,
nudges you at the right moment, and keeps all of it on your own network.

## Demo

Eight steps in three minutes, each with a fallback ([demo-script.md](demo-script.md)):

1. **Hook:** Today, one lane per device that has sent data, with the calendar, sleep and meals.
2. **Live:** the phone's lane grows as it is used.
3. **A nudge:** TikTok during a study block gets a gentle nudge.
4. **The AI, with the internet unplugged:** Ask answers from the numbers, and Privacy still says 0 internet
   connections.
5. **Streaks and a badge:** flames, goal rings and confetti.
6. **Insights:** the week, the apps and devices, and late nights against the next day's focus.
7. **Wrapped:** the week's card, saved or shared.
8. **Close:** your devices, your network, your AI.

**What is live today:** the hub, the dashboard and the local model are all real. The phone's part (steps 2 and 3)
is played by `daytrace_hub demo live` and `demo nudge` on the PC, because the Android app can't pair until PR #16
merges and showing nudges on the phone is DT-24. The nudge shows as a desktop notification. The Mac, iPhone and
browser lanes are seeded demo data.

**The backup recording**, if the live demo fails: [docs/img/demo.webm](img/demo.webm), about 2 minutes, captioned,
no sound, on the demo profile. It shows each step's screen. Two things happen off camera: the internet isn't
unplugged, and the nudge, which goes to the phone and the desktop rather than the dashboard, is quoted in its
caption.

The slides follow the sections of this page, and are kept outside the repo.

## How it works

- **Collectors** send events to the **hub** over your Wi-Fi. Today the Windows desktop tracker records real data.
  The Android app records and keeps app use, and waits for pairing (PR #16). iPhone Shortcuts, a Mac bridge, a
  macOS tracker and a browser extension are planned.
- **The hub** (Python and SQLite on any computer) turns events into sessions, and plain code works out every number:
  totals, focused time, pickups, sleep, streaks.
- **A local model** puts those numbers into words: the day's story, answers to your questions, Wrapped's lines.
  Every number it writes is checked against the facts, and if one doesn't match, you get a plain summary instead.
- **The dashboard** is a web app the hub serves itself, so it works in any browser, on any device.

The details: [architecture.md](architecture.md).

## Privacy

- **No cloud:** the hub keeps everything in one file on your computer.
- **No internet:** the hub listens only on this computer and your home network, and connects to nothing on the
  internet. Its one outgoing connection is to the local model, and it announces itself on the home network (mDNS)
  so phones can find it. Under that, a socket guard refuses any connection to an internet address. The Privacy page
  shows the count: 0 internet connections.
- **Sensitive titles are never stored:** banking, health portals, password managers and private windows. Words you
  add hide new events at once, and stored ones when you confirm.
- **Every device is paired** with a one-time code and can be revoked.
- **Yours to take or delete:** export everything, or delete everything, from the hub's own computer.
- **Not there yet:**
  - traffic on the home network is plain HTTP until HTTPS lands (DT-47);
  - a collector's token can read your data as well as send it, which a follow-up should narrow;
  - the database file isn't encrypted: your computer account protects it.

The details: [privacy.md](privacy.md).

## What's next

- **Every device:** pairing the Android app on real home networks (PR #16), then health and calendar from the phone
  (DT-23), live mode and nudges on the phone (DT-24), iPhone Shortcuts and the Mac bridge (DT-26 to DT-29), the
  macOS tracker (DT-17) and the browser extension (DT-18).
- **HTTPS at home** (DT-47): encrypted traffic on the home network, and a dashboard that installs and works offline
  on phones (DT-35). Then a signed Android app on GitHub Releases (DT-25).
- **Tighter tokens:** collectors that can send events but can't read them back.
- **Smarter without more data:** the local model sorts unknown apps into categories and reads meals from plain text
  (DT-42).

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
7. ActivityWatch, "Sync your activity between your devices (we're working on it)", activitywatch.net, checked
   September 2026. <https://activitywatch.net/>
