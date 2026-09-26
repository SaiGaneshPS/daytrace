# Android collector

Kotlin + Jetpack Compose. Distributed as an APK (a signed release on GitHub Releases from DT-25), never the Play Store.

- Package `app.daytrace.android`, minSdk 29 (Android 10), targetSdk 36, compileSdk 37.
- Android Gradle Plugin 9.4, Gradle 9.8 (wrapper, checksum pinned), Kotlin 2.4 (AGP bundles an older Kotlin; the root
  `build.gradle.kts` puts 2.4 on the classpath so the Compose compiler matches), Compose BOM 2026.09. All versions
  are in `gradle/libs.versions.toml`.
- No cloud backup: `allowBackup` is off and the backup rules exclude everything, so the event store and the hub token
  stay on the phone.

## Build

You need a JDK 17 or newer (Android Studio's bundled one works) and the Android SDK (`ANDROID_HOME`, on this PC
`D:\Hackathon\Android\Sdk`). Android Studio is optional: open this `android/` folder in it, or use the command line.

```powershell
# Windows
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
.\gradlew.bat assembleDebug testDebugUnitTest
```

```bash
# macOS / Linux
./gradlew assembleDebug testDebugUnitTest
```

The debug APK is `app/build/outputs/apk/debug/app-debug.apk`. CI (`.github/workflows/android.yml`) builds the same
APK on every PR and keeps it as an artifact for 14 days.

## Install on your phone

1. On the phone: Settings > About phone > Software information > tap **Build number** 7 times, then Settings >
   Developer options > turn on **USB debugging**.
2. Plug the phone in and accept **Allow USB debugging?** (tick "Always allow from this computer"). If no prompt
   appears: Developer options > **Revoke USB debugging authorizations**, then unplug and plug in again.
3. `adb install -r app/build/outputs/apk/debug/app-debug.apk` (adb is in `$ANDROID_HOME/platform-tools`).

Without a cable, copy the APK to the phone and open it (allow "Install unknown apps" for your file manager);
`docs/install-android.md` (DT-25) has the Samsung steps.

## Permissions (DT-19)

The onboarding screen walks through each one and the status screen shows them afterwards:

| Permission | Why | Required |
|---|---|---|
| Usage access (`PACKAGE_USAGE_STATS`, granted in Settings) | which apps you used and for how long | yes |
| Notifications | nudges (DT-24) and the live-mode notice | no |
| Calendar (read) | today's events on the timeline (DT-23) | no |
| Health Connect (read sleep, steps, nutrition) | sleep, steps and meals on the timeline (DT-23) | no |

`QUERY_ALL_PACKAGES` resolves app names from package IDs; it is fine because the app is not on the Play Store.

## Files by ticket

| File | Ticket |
|---|---|
| Gradle files and wrapper, `AndroidManifest.xml`, `MainActivity.kt`, `DaytraceApp.kt`, `ui/OnboardingScreen.kt`, `ui/StatusScreen.kt`, `ui/theme/*`, `res/*` | DT-19 |
| `usage/UsageCollector.kt` | DT-20 |
| `data/*`, `sync/HubClient.kt`, `sync/SyncWorker.kt` | DT-21 |
| `sync/HubDiscovery.kt`, `sync/PairingStore.kt`, `ui/PairingScreen.kt` | DT-22 |
| `health/HealthCollector.kt`, `calendar/CalendarCollector.kt` | DT-23 |
| `live/LiveModeService.kt`, `nudge/NudgeNotifier.kt` | DT-24 |
| `app/build.gradle.kts` signing config | DT-25 |
