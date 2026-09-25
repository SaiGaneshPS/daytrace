# Android collector

Kotlin + Jetpack Compose. Distributed as a signed APK on GitHub Releases (no Play Store).

## DT-19: generating the Gradle files

The Kotlin files, manifest and resources below are part of the fixed layout. The Gradle build files are
placeholders until DT-19, so the versions come from Android Studio's own template:

1. In Android Studio: **New Project > Empty Activity**, package `app.daytrace.android`, minSdk 29,
   saved to a temporary folder.
2. Copy `settings.gradle.kts`, `build.gradle.kts`, `gradle.properties`, `gradle/` (including `wrapper/`),
   `gradlew`, `gradlew.bat` and `app/build.gradle.kts` from it into this folder.
3. Keep the files already in `app/src/main/` (merge the generated `MainActivity` into ours).
4. Open this `android/` folder in Android Studio and build.

| File | Ticket |
|---|---|
| Gradle files, `AndroidManifest.xml`, `MainActivity.kt`, `DaytraceApp.kt`, `ui/OnboardingScreen.kt`, `ui/StatusScreen.kt`, `res/xml/network_security_config.xml` | DT-19 |
| `usage/UsageCollector.kt` | DT-20 |
| `data/*`, `sync/HubClient.kt`, `sync/SyncWorker.kt` | DT-21 |
| `sync/HubDiscovery.kt`, `sync/PairingStore.kt`, `ui/PairingScreen.kt` | DT-22 |
| `health/HealthCollector.kt`, `calendar/CalendarCollector.kt` | DT-23 |
| `live/LiveModeService.kt`, `nudge/NudgeNotifier.kt` | DT-24 |
| `app/build.gradle.kts` signing config | DT-25 |
