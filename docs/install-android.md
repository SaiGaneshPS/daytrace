# Installing the Android app

The Android app records which apps you use, your sleep, steps and meals (from Health Connect) and your calendar,
and sends them to your hub over your own Wi-Fi. It isn't on the Play Store: you install the APK yourself.

## Download the APK

Signed releases on GitHub Releases come with DT-25. Until then, use the debug APK:

- **From CI:** every pull request builds it. Open the PR's **Checks** tab, then the `android` run, and download
  the `daytrace-debug-apk` artifact (kept for 14 days).
- **Build it yourself:** see [android/README.md](../android/README.md) (`./gradlew assembleDebug`), then install it
  with `adb install -r app/build/outputs/apk/debug/app-debug.apk`.

## Allow installing unknown apps (Samsung)

TODO (DT-25)

## Permissions

The first screen walks through each permission, and the status screen shows them afterwards. Only usage access is
needed; the others add to what your timeline shows.

| Step | What Daytrace reads | What leaves the phone |
|---|---|---|
| **Usage access** (Settings > Usage access) | which apps you used, when, and screen on and off | the app's name and package, and the times |
| **Notifications** | nothing | nothing (nudges and the live-mode notice, DT-24) |
| **Calendar** | the calendars shown in your Calendar app, from yesterday to tomorrow | each event's title and times, and whether it is all day. Never the place, notes or guests |
| **Health Connect** | sleep with its stages, daily step totals, and the foods you logged | the times, sleep stages, step counts, food names and meal type. Never calories or other nutrients |

On the **Health Connect** step, Health Connect opens its own permission screen:

1. Allow **Sleep**, **Steps** and **Nutrition**.
2. If it also offers **Access data in the background** (Android 14 and newer), allow it too. Syncs run in the
   background every 15 minutes, and Health Connect only lets an app read while it is open unless this is allowed.
   Without it, health data still arrives, but only when you sync with the app open. The Health Connect card then
   says so, with an **Allow in the background** button.

The hub hides calendar titles that match its redaction rules (banking, health portals and the words you add on the
dashboard's Privacy page) before anything is stored.

## Samsung Health to Health Connect

Samsung Health keeps sleep, steps and food in its own app. Daytrace reads them through Health Connect, so Samsung
Health has to share them there first:

1. Open **Samsung Health**.
2. Tap the menu (three dots at the top right), then **Settings**.
3. Tap **Health Connect**. This opens Health Connect's page for Samsung Health.
4. Under **Allow Samsung Health to write data** (the names can differ slightly between versions), turn on
   **Sleep**, **Steps** and **Nutrition**, or **Allow all**.
5. Open Samsung Health once more. It shares new data with Health Connect from then on, in the background.

To check that it worked: in Health Connect, open **Data and access** (or **Browse data**), then **Sleep**. Last
night should be listed with Samsung Health as its source.

Food only reaches Health Connect when you log it in Samsung Health (**Food** > **Record meal**). Foods logged
together are one meal on your timeline, each food an item.

### What Daytrace reads, and when

- **The first time:** the last 30 days. Health Connect lets an app read at most 30 days from before it was first
  allowed, so older nights can't be read.
- **Every sync after that:** today and yesterday, and every day Health Connect says changed since the last sync,
  within the last 30 days. That covers a week without Wi-Fi, a night Samsung Health adds late, a meal you edit, and
  the history Samsung Health shares if you turn its sharing on after allowing Daytrace. Reading the same data again
  never adds anything twice: each record keeps its key, and a changed one replaces its copy on the hub.
- **Steps:** one total per day, as Health Connect adds them up (steps counted by two apps are counted once). Today's
  total goes up during the day.
- **Calendar:** yesterday, today and tomorrow, every sync, and after days without a sync, from the day before the
  last one (at most 30 days back). A moved or renamed event replaces its copy, and so does one occurrence of a
  repeating event that you change. Declined and cancelled events are left out.

### Known limits

- **Deleting doesn't reach the hub yet.** An event you delete from your calendar, a meal you delete in Samsung
  Health, or a calendar event you decline after it was sent stays on the hub. So do the old copies of changes that
  give a record a new key:
  - a meal whose time or meal type you change (a meal is keyed by both);
  - every occurrence of a repeating event whose time you change for the whole series;
  - the extra stages of a night that Samsung Health later splits into fewer stages.

## Battery settings (Never sleeping apps)

TODO (DT-24)

## Google developer verification note

TODO (DT-25)
