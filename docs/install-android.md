# Installing the Android app

The Android app records which apps you use, your sleep, steps and meals (from Health Connect) and your calendar,
and sends them to your hub over your own Wi-Fi. It isn't on the Play Store: you install the APK yourself.

## Download the APK

- **A release (recommended):** open the repository's **Releases** page on the phone and download
  `daytrace-android-<version>.apk` from the newest **Daytrace for Android** release. Each release is signed with the
  same key, so a new one installs over the one before and keeps everything (the pairing, and events not sent yet).
  The release notes give the file's SHA-256 and the signing certificate's, if you want to check them.
- **A debug build, for development:** every pull request builds one (the PR's **Checks** tab, the `android` run, the
  `daytrace-debug-apk` artifact, kept for 14 days), or build it yourself (see [android/README.md](../android/README.md))
  and install it with `adb install -r app/build/outputs/apk/debug/app-debug.apk`.
- **Moving between builds:** a release and a debug build are signed with different keys, and so are debug builds
  from different CI runs (each makes its own debug key), so Android refuses to install one over another ("App not
  installed"). Sync first (**Sync now**), then uninstall and install the other. The phone then pairs as a new device
  (a reinstall always does); its history so far stays on the hub under the old one, which you can revoke on the
  Devices page. Releases always install over each other.

## Allow installing unknown apps (Samsung)

1. **Auto Blocker:** if it's on (Settings > Security and privacy > Auto Blocker), it blocks apps from outside the
   Galaxy Store and the Play Store, and installs over a USB cable too. Turn it off to install Daytrace, and back on
   afterwards if you like (an installed app keeps running); each new release needs it off again.
2. **Open the APK** from the download notification, or from **My Files** > **Downloads**.
3. **Allow the source once:** Android says the app you opened it from (Chrome, or My Files) can't install unknown
   apps. Tap **Settings**, turn on **Allow from this source**, then go back and tap **Install**. You can turn it off
   again under Settings > Apps > (menu) > Special access > Install unknown apps.
4. **Play Protect** may warn that it doesn't know the app (it isn't on the Play Store): tap **More details** >
   **Install anyway**. If it offers to send the app for a scan, you can say no.

## Permissions

The first screen walks through each permission, and the status screen shows them afterwards. Only usage access is
needed; the others add to what your timeline shows.

| Step | What Daytrace reads | What leaves the phone |
|---|---|---|
| **Usage access** (Settings > Usage access) | which apps you used, when, and screen on and off | the app's name and package, and the times |
| **Notifications** | nothing | nothing: it shows your hub's nudges and the live-mode notice |
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

## The Dashboard tab and the widget

Once the phone is paired, Daytrace opens on your dashboard: **Today**, **Story**, **Ask**, **Insights** and **More**
(Streaks and goals, Wrapped, Devices, Privacy, and **This phone**, with the sync, live mode and permissions).

- **The same dashboard as on the PC:** the pages come from your hub, with its numbers, inside the app. No browser
  opens.
- **Only your hub:** the app first checks that the hub is the one that paired this phone (as a sync does). It opens
  the page only on your hub's Wi-Fi, and refuses to go anywhere else. The page gets its own dashboard token from
  pairing, which can change settings (your goals, say) but not send events. It sends nothing to Google: the
  WebView's Safe Browsing checks and usage reports are off.
- **Paired before the dashboard came along?** The tab asks you to pair again: the phone keeps its id and history, and
  gets its dashboard token.
- **Moving around:** each tab opens at its start (tap it again to go back there), pull down to refresh, and Back goes
  back inside the page.
- **Something to fix on the phone?** When usage access is off, or the last sync needs you (pair again, or something
  that isn't your hub answered), a banner above every tab says so and opens **This phone**.
- **Wrapped:** the app can't save files, so save the card from Wrapped in your computer's browser.

**The widget:** long-press your home screen, then **Widgets**, then **Daytrace today**.
- **What it shows:** today's screen time (phone and computers), the longest streak going, with its flame and day
  count, and progress on your main goal. Tapping it opens the app.
- **Where the numbers come from:** your hub, the same answers as the dashboard. It refreshes after each background
  sync and every 30 minutes on your hub's Wi-Fi (internet or not), and shows the day and time of the numbers it has.
  It reads with the app's dashboard token, so a phone paired before the dashboard came along asks you to pair again,
  and forgetting the hub clears it.

## Live mode and nudges

The **Live mode** card (More, then This phone) is for demos, or any time you want the PC to see this phone at once.

- **While it's on:** Daytrace reads your app use every 5 seconds and sends anything new straight away. An app you open
  shows on the hub's timeline within about 5 seconds (5.5 s measured on a Galaxy S25 Ultra), and grows as you keep
  using it.
- **The notice:** "Live mode is on" stays in your notifications, with a **Stop** button.
- **Screen off:** it reads once a minute instead, and at once when the screen comes back on.
- **When it ends:** when you stop it, forget the hub, or swipe Daytrace away from your recent apps; when the hub no
  longer accepts this phone; or after Android's limit for this kind of background work (6 hours a day on Android 15
  and newer).
- **When the hub is out of reach:** it tries again every 30 seconds, and everything waits safely on the phone.
- **Without it:** the phone syncs every 15 minutes on Wi-Fi, and whenever you tap **Sync now**.

**Nudges** come from your hub, with the events that set them off. The hub's rules:
- **Focus time:** a social, video or game app during a study or work block on your calendar.
- **Late night:** social or video after your bedtime goal.
- **Streak at risk:** from 20:00, a streak not kept yet today.
- **Social limit:** past your daily social goal.

The phone shows each nudge once, in Daytrace's colors, for example "Time to focus: YouTube during "Study", which runs
until 22:15." It needs the **Notifications** permission; without it the nudge is only in the hub's log
(`GET /api/v1/nudges`). Nudges arrive with a sync: within seconds in live mode, otherwise at the next sync.

## Battery settings

Samsung phones put apps they think you don't use to sleep, which delays the 15-minute sync. So that Daytrace keeps
syncing (and live mode keeps running during a demo), change two settings. The names are from One UI on a Galaxy
S25 Ultra:

1. **Settings > Battery > Background usage limits > Never auto sleeping apps**. Tap **+** (Add apps), choose
   **Daytrace**, then **Add**. Older One UI versions call the list **Never sleeping apps**.
2. **Settings > Apps > Daytrace > Battery**: choose **Unrestricted**.

Check that Daytrace is not in **Sleeping apps** or **Deep sleeping apps** on the same Background usage limits page.
Other phone makers have similar settings, usually under Battery or App info.

## Google developer verification note

Google is starting to require that apps on certified Android phones come from a registered developer
([Android developer verification](https://developer.android.com/developer-verification), checked 2026-09-28):

- **When:** from September 30, 2026 in Brazil, Indonesia, Singapore and Thailand only, and on every certified
  Android phone in 2027.
- **What it means for Daytrace:** nothing yet elsewhere. Once it applies, a release needs either a registered
  developer or Google's free **limited distribution** account (for students and hobbyists: no fee, no ID, up to 20
  devices), which fits a hackathon team.
- **Without either:** Google keeps two ways in for your own phone: an "advanced flow" in the phone's settings (a
  one-time setup) and `adb install`, which works for developers as today
  ([Google's help page](https://support.google.com/android-developer-console/answer/16561738)).

## Making a release (maintainers)

The key is made once, kept off GitHub except as two secrets of the `android-release` environment, and never
committed:

1. **The key** (done on the hub PC): `D:\Hackathon\keys\daytrace-release.jks` (PKCS12, alias `daytrace`, RSA 4096,
   valid until 2054), with its password in `daytrace-release-password.txt` next to it. Its certificate's SHA-256,
   `35e83a98f1bd83b369ff3ce7febbe5902b8e4cda6e36086667794b4fe385119f`, is pinned in `android.yml`: a release signed
   any other way fails. **Back both files up somewhere safe**: without them no later release can install over an
   earlier one.
2. **The environment** `android-release` (Settings > Environments) takes only tags `android-v*`, so no branch or pull
   request run can read its secrets.
3. **The secrets** (once), in PowerShell from the repository folder, so the password never appears in a command line:

   ```powershell
   [Convert]::ToBase64String([IO.File]::ReadAllBytes("D:\Hackathon\keys\daytrace-release.jks")) | gh secret set DAYTRACE_KEYSTORE_BASE64 --env android-release
   Get-Content -Raw D:\Hackathon\keys\daytrace-release-password.txt | gh secret set DAYTRACE_KEYSTORE_PASSWORD --env android-release
   ```

4. **Before the first tag:** CI builds the shrunk release (R8) on every pull request, which shows it builds, not that
   it runs. So install a signed build on a phone once (step 6) and open every tab, the widget and live mode.
5. **A release:** tag a commit that is on `development` and push the tag. The `android release` job checks the
   commit is on `development` and the version is higher than every release so far, builds the APK (the version and
   its versionCode come from the tag), checks it is signed with the pinned key and publishes the GitHub Release:

   ```bash
   git tag android-v0.1.0 && git push origin android-v0.1.0
   ```

   Versions are `major.minor.patch` without leading zeros, at most `999.99.99` (versionCode is
   `major * 10000 + minor * 100 + patch`).
6. **On this PC** (optional): `android/keystore.properties` (ignored by git) with `storeFile` (with forward slashes,
   `D:/Hackathon/keys/daytrace-release.jks`: a backslash there is an escape), `storePassword` and `keyAlias`; or set
   `DAYTRACE_KEYSTORE`, `DAYTRACE_KEYSTORE_PASSWORD` and `DAYTRACE_KEY_ALIAS`. A signed build also needs
   `DAYTRACE_VERSION` (for example `0.1.0`), the release it is. Half a key, or a key without a version, fails the
   build. Without a key, the release build is unsigned (`app-release-unsigned.apk`).
