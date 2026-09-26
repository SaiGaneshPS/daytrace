// DT-20: UsageStatsManager collector. Exact per-app sessions and screen on/off, from the last checkpoint to now.
package app.daytrace.android.usage

import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import app.daytrace.android.data.EventStore
import app.daytrace.android.data.PhoneEvent
import app.daytrace.android.ui.Permissions
import org.json.JSONObject
import java.time.LocalDate
import java.time.ZoneId

// --- the rules, in plain Kotlin so they are unit tested --------------------------------------------------------

enum class UsageType { RESUMED, PAUSED, SCREEN_ON, SCREEN_OFF, SHUTDOWN }

data class RawUsageEvent(val timeMs: Long, val packageName: String, val className: String?, val type: UsageType)

/** Where the last collection stopped, and which apps were still on screen then (package to the time they opened). */
data class UsageState(val checkpointMs: Long, val open: Map<String, Long> = emptyMap())

data class Collected(val events: List<PhoneEvent>, val state: UsageState)

object UsageSessionizer {
    const val SOURCE = "usagestats"
    private const val UNKNOWN_ACTIVITY = ""

    private class OpenApp(val startMs: Long, val activities: MutableSet<String> = mutableSetOf())

    /**
     * Turns the raw events between [UsageState.checkpointMs] and [untilMs] into sessions.
     *
     * - An app's session runs from its first activity resuming to its last activity pausing, so moving between
     *   screens of one app stays one session.
     * - The screen turning off (or the phone shutting down) ends every open session: a paused event can be
     *   missing then, and nobody uses an app with the screen off.
     * - An app still open at [untilMs] is closed there and reopened in the returned state, so the next collection
     *   continues it: no time is lost or counted twice (the hub joins the two pieces back together).
     * - [ignored] packages (the home screen, the status bar) are not app time.
     */
    fun collect(
        raw: List<RawUsageEvent>,
        state: UsageState,
        untilMs: Long,
        ignored: Set<String>,
        label: (String) -> String?,
    ): Collected {
        val events = mutableListOf<PhoneEvent>()
        val open = LinkedHashMap<String, OpenApp>()
        state.open.keys.forEach { pkg -> if (pkg !in ignored) open[pkg] = OpenApp(state.checkpointMs) }

        fun close(pkg: String, atMs: Long) {
            val app = open.remove(pkg) ?: return
            if (atMs > app.startMs) {
                events += PhoneEvent("app_session", SOURCE, app.startMs, atMs, label(pkg) ?: pkg, pkg)
            }
        }

        fun closeAll(atMs: Long) = open.keys.toList().forEach { close(it, atMs) }

        for (event in raw.sortedBy { it.timeMs }) {
            if (event.timeMs < state.checkpointMs || event.timeMs > untilMs) continue
            when (event.type) {
                UsageType.RESUMED -> if (event.packageName !in ignored) {
                    open.getOrPut(event.packageName) { OpenApp(event.timeMs) }.activities += event.className ?: UNKNOWN_ACTIVITY
                }
                UsageType.PAUSED -> open[event.packageName]?.let { app ->
                    app.activities.remove(event.className ?: UNKNOWN_ACTIVITY)
                    if (app.activities.isEmpty()) close(event.packageName, event.timeMs)
                }
                UsageType.SCREEN_OFF -> {
                    closeAll(event.timeMs)
                    events += PhoneEvent("screen_off", SOURCE, event.timeMs)
                }
                UsageType.SCREEN_ON -> events += PhoneEvent("screen_on", SOURCE, event.timeMs)
                UsageType.SHUTDOWN -> closeAll(event.timeMs)
            }
        }
        val stillOpen = open.keys.toList()
        closeAll(untilMs)
        return Collected(events.sortedBy { it.startMs }, UsageState(untilMs, stillOpen.associateWith { untilMs }))
    }
}

/** Today's app time on this phone, for the status screen (each session clipped to today). */
data class TodaySummary(val totalMs: Long, val topApps: List<Pair<String, Long>>) {
    companion object {
        fun from(events: List<PhoneEvent>, nowMs: Long, zone: ZoneId = ZoneId.systemDefault(), top: Int = 3): TodaySummary {
            val dayStart = LocalDate.now(zone).atStartOfDay(zone).toInstant().toEpochMilli()
            val byApp = HashMap<String, Long>()
            for (event in events) {
                val endMs = event.endMs ?: continue
                if (event.kind != "app_session") continue
                val start = maxOf(event.startMs, dayStart)
                val end = minOf(endMs, nowMs)
                if (end > start) byApp.merge(event.app ?: event.appId ?: "?", end - start, Long::plus)
            }
            val ranked = byApp.entries.sortedByDescending { it.value }.map { it.key to it.value }
            return TodaySummary(byApp.values.sum(), ranked.take(top))
        }
    }
}

// --- Android -------------------------------------------------------------------------------------------------

class UsageCollector(private val context: Context, private val store: EventStore = EventStore.get(context)) {
    private val prefs = context.getSharedPreferences("usage_collector", Context.MODE_PRIVATE)

    /** Collects everything new since the last run. Returns how many events were stored (0 without usage access). */
    fun collect(nowMs: Long = System.currentTimeMillis()): Int = synchronized(LOCK) {
        if (!Permissions.usageGranted(context)) return 0
        val state = loadState(nowMs)
        if (nowMs <= state.checkpointMs) {
            if (nowMs < state.checkpointMs) saveState(UsageState(nowMs)) // the clock went back: start again from now
            return 0
        }
        val usageStats = context.getSystemService(UsageStatsManager::class.java)
        val raw = read(usageStats.queryEvents(state.checkpointMs, nowMs))
        val collected = UsageSessionizer.collect(raw, state, nowMs, ignoredPackages(), ::label)
        store.add(collected.events) // stored before the checkpoint moves: a crash repeats work instead of losing it
        saveState(collected.state)
        return collected.events.size
    }

    private fun read(events: UsageEvents): List<RawUsageEvent> {
        val raw = mutableListOf<RawUsageEvent>()
        val event = UsageEvents.Event()
        while (events.hasNextEvent()) {
            events.getNextEvent(event)
            val type = when (event.eventType) {
                UsageEvents.Event.ACTIVITY_RESUMED -> UsageType.RESUMED
                UsageEvents.Event.ACTIVITY_PAUSED -> UsageType.PAUSED
                UsageEvents.Event.SCREEN_INTERACTIVE -> UsageType.SCREEN_ON
                UsageEvents.Event.SCREEN_NON_INTERACTIVE -> UsageType.SCREEN_OFF
                UsageEvents.Event.DEVICE_SHUTDOWN -> UsageType.SHUTDOWN
                else -> null
            } ?: continue
            raw += RawUsageEvent(event.timeStamp, event.packageName.orEmpty(), event.className, type)
        }
        return raw
    }

    /** The home screen app(s) and the system UI: time there is not time in an app. */
    private fun ignoredPackages(): Set<String> {
        val home = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME)
        val launchers = context.packageManager.queryIntentActivities(home, PackageManager.MATCH_ALL)
            .map { it.activityInfo.packageName }
        return (launchers + "com.android.systemui").toSet()
    }

    private val labels = HashMap<String, String?>()

    private fun label(pkg: String): String? = labels.getOrPut(pkg) {
        runCatching {
            val info = context.packageManager.getApplicationInfo(pkg, 0)
            context.packageManager.getApplicationLabel(info).toString()
        }.getOrNull()
    }

    private fun loadState(nowMs: Long): UsageState {
        val checkpoint = prefs.getLong(KEY_CHECKPOINT, -1)
        if (checkpoint < 0) {
            // First run: start from the beginning of yesterday, so the hub sees some history right away.
            val zone = ZoneId.systemDefault()
            val yesterday = LocalDate.now(zone).minusDays(1).atStartOfDay(zone).toInstant().toEpochMilli()
            return UsageState(yesterday.coerceAtMost(nowMs))
        }
        val open = JSONObject(prefs.getString(KEY_OPEN, "{}") ?: "{}")
        return UsageState(checkpoint, open.keys().asSequence().associateWith { open.getLong(it) })
    }

    private fun saveState(state: UsageState) {
        prefs.edit()
            .putLong(KEY_CHECKPOINT, state.checkpointMs)
            .putString(KEY_OPEN, JSONObject(state.open as Map<*, *>).toString())
            .commit() // commit, not apply: the checkpoint must be on disk before the next run reads it
    }

    private companion object {
        val LOCK = Any()
        const val KEY_CHECKPOINT = "checkpoint_ms"
        const val KEY_OPEN = "open_apps"
    }
}
