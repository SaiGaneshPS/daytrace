// DT-20: UsageStatsManager collector. Exact per-app sessions and screen on/off, from the last checkpoint to now.
package app.daytrace.android.usage

import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.SystemClock
import app.daytrace.android.data.EventStore
import app.daytrace.android.data.PhoneEvent
import app.daytrace.android.ui.Permissions
import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.time.ZoneId
import java.util.PriorityQueue
import kotlin.math.abs

// --- the rules, in plain Kotlin so they are unit tested --------------------------------------------------------

enum class UsageType { RESUMED, PAUSED, STOPPED, SCREEN_ON, SCREEN_OFF, SHUTDOWN, STARTUP }

data class RawUsageEvent(val timeMs: Long, val packageName: String, val className: String?, val type: UsageType)

/** An app on screen at a checkpoint: when its session started and which of its activities were resumed. */
data class OpenApp(val startMs: Long, val activities: Set<String> = emptySet())

/**
 * Where the last collection stopped, which apps were still on screen then, and the phone's uptime at that moment
 * (so a clock change between two collections can be detected and corrected).
 */
data class UsageState(val checkpointMs: Long, val open: Map<String, OpenApp> = emptyMap(), val checkpointUptimeMs: Long = -1)

data class Collected(val events: List<PhoneEvent>, val state: UsageState)

object UsageSessionizer {
    const val SOURCE = "usagestats"
    /** A pause followed by a resume of the same app within this long is a screen change inside the app. */
    const val SAME_APP_GRACE_MS = 1_000L
    private const val UNKNOWN_ACTIVITY = ""

    private class Open(val startMs: Long, val activities: MutableSet<String>)

    /**
     * Turns the raw events between [UsageState.checkpointMs] and [untilMs] into sessions.
     *
     * - A session runs from an app's first activity resuming to its last activity pausing. Android usually pauses
     *   the old screen before resuming the new one, so a pause is only final if the same app does not resume
     *   within [SAME_APP_GRACE_MS]: moving between screens of one app stays one session.
     * - An activity that stops without pausing (the app crashed or was killed) ends there too.
     * - The screen turning off, or a shutdown, ends every open session: a paused event can be missing then. After
     *   a restart, apps still open from before end at the last event seen before the restart, never counting the
     *   time the phone was off.
     * - An app still open at [untilMs] is closed there and carried into the returned state, so the next collection
     *   continues it with no gap and no double count (the hub joins the two pieces back together).
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
        val open = LinkedHashMap<String, Open>()
        state.open.forEach { (pkg, app) -> if (pkg !in ignored) open[pkg] = Open(state.checkpointMs, app.activities.toMutableSet()) }
        val closing = LinkedHashMap<String, Long>() // app -> when its last activity paused (not final yet)
        var lastSeen = state.checkpointMs

        fun emit(pkg: String, startMs: Long, endMs: Long) {
            if (endMs > startMs) events += PhoneEvent("app_session", SOURCE, startMs, endMs, label(pkg) ?: pkg, pkg)
        }

        fun finish(pkg: String) {
            val at = closing.remove(pkg) ?: return
            open.remove(pkg)?.let { emit(pkg, it.startMs, at) }
        }

        fun finishDue(nowMs: Long) = closing.filterValues { nowMs - it > SAME_APP_GRACE_MS }.keys.forEach(::finish)

        fun closeAll(atMs: Long) {
            closing.keys.toList().forEach(::finish)
            open.keys.toList().forEach { pkg -> open.remove(pkg)?.let { emit(pkg, it.startMs, maxOf(atMs, it.startMs)) } }
        }

        fun activityGone(pkg: String, activity: String, atMs: Long, stopped: Boolean) {
            val app = open[pkg] ?: return
            val wasResumed = app.activities.remove(activity)
            if (stopped && !wasResumed && app.activities.isNotEmpty()) return // the usual pause-then-stop: nothing new
            if (app.activities.isEmpty()) closing.putIfAbsent(pkg, atMs) // a later stop never moves the end
        }

        for (event in raw.sortedBy { it.timeMs }) {
            if (event.timeMs < state.checkpointMs || event.timeMs > untilMs) continue
            finishDue(event.timeMs)
            val activity = event.className ?: UNKNOWN_ACTIVITY
            when (event.type) {
                UsageType.RESUMED -> if (event.packageName !in ignored) {
                    closing.remove(event.packageName) // resumed again within the grace: the same session continues
                    open.getOrPut(event.packageName) { Open(event.timeMs, mutableSetOf()) }.activities += activity
                }
                UsageType.PAUSED -> activityGone(event.packageName, activity, event.timeMs, stopped = false)
                UsageType.STOPPED -> if (open[event.packageName]?.activities?.contains(activity) == true) {
                    activityGone(event.packageName, activity, event.timeMs, stopped = true)
                }
                UsageType.SCREEN_OFF -> {
                    closeAll(event.timeMs)
                    events += PhoneEvent("screen_off", SOURCE, event.timeMs)
                }
                UsageType.SCREEN_ON -> events += PhoneEvent("screen_on", SOURCE, event.timeMs)
                UsageType.SHUTDOWN -> closeAll(event.timeMs)
                UsageType.STARTUP -> closeAll(lastSeen) // whatever was open before the restart ended by then
            }
            lastSeen = event.timeMs
        }
        closing.keys.toList().forEach(::finish)
        val carried = open.mapValues { (_, app) -> OpenApp(untilMs, app.activities.toSet()) }
        open.forEach { (pkg, app) -> emit(pkg, app.startMs, untilMs) }
        return Collected(events.sortedBy { it.startMs }, UsageState(untilMs, carried))
    }
}

object ClockCheck {
    /** Changes smaller than this are ordinary network time adjustments. */
    const val THRESHOLD_MS = 2_000L

    /**
     * When the wall clock was changed since the last collection, Android shifts its stored usage events by the
     * same amount. Shifting the checkpoint (and the open apps) the same way keeps the next window exactly where
     * the last one ended: nothing is read twice and nothing is skipped. Uptime tells the real time that passed;
     * after a restart it starts again from zero, so no correction is possible and none is made.
     */
    fun correct(state: UsageState, nowWallMs: Long, nowUptimeMs: Long): UsageState {
        if (state.checkpointUptimeMs < 0 || nowUptimeMs < state.checkpointUptimeMs) return state
        val expectedWall = state.checkpointMs + (nowUptimeMs - state.checkpointUptimeMs)
        val shift = nowWallMs - expectedWall
        if (abs(shift) <= THRESHOLD_MS) return state
        return state.copy(
            checkpointMs = state.checkpointMs + shift,
            open = state.open.mapValues { (_, app) -> app.copy(startMs = app.startMs + shift) },
        )
    }
}

/**
 * Today's app time on this phone, for the status screen. Counted the way the hub counts it: each session clipped
 * to today, and overlapping sessions (split screen, pop-up windows) never counted twice; the most recently started
 * app owns the screen.
 */
data class TodaySummary(val totalMs: Long, val topApps: List<Pair<String, Long>>) {
    companion object {
        fun from(events: List<PhoneEvent>, nowMs: Long, zone: ZoneId = ZoneId.systemDefault(), top: Int = 3): TodaySummary {
            val dayStart = Instant.ofEpochMilli(nowMs).atZone(zone).toLocalDate().atStartOfDay(zone).toInstant().toEpochMilli()
            val sessions = events.mapNotNull { event ->
                val endMs = event.endMs ?: return@mapNotNull null
                if (event.kind != "app_session") return@mapNotNull null
                val start = maxOf(event.startMs, dayStart)
                val end = minOf(endMs, nowMs)
                if (end > start) Triple(start, end, event.app ?: event.appId ?: "?") else null
            }
            val byApp = resolveOverlaps(sessions)
            val ranked = byApp.entries.sortedByDescending { it.value }.map { it.key to it.value }
            return TodaySummary(byApp.values.sum(), ranked.take(top))
        }

        /** Milliseconds per app after giving each moment to the most recently started session (as the hub does). */
        fun resolveOverlaps(sessions: List<Triple<Long, Long, String>>): Map<String, Long> {
            val ordered = sessions.sortedBy { it.first }
            val points = (ordered.map { it.first } + ordered.map { it.second }).distinct().sorted()
            val active = PriorityQueue<Int>(compareByDescending<Int> { ordered[it].first }.thenByDescending { it })
            val byApp = HashMap<String, Long>()
            var next = 0
            for (i in 0 until points.size - 1) {
                val left = points[i]
                while (next < ordered.size && ordered[next].first <= left) active += next++
                while (active.isNotEmpty() && ordered[active.peek()!!].second <= left) active.poll()
                val owner = active.peek() ?: continue
                byApp.merge(ordered[owner].third, points[i + 1] - left, Long::plus)
            }
            return byApp
        }
    }
}

// --- Android -------------------------------------------------------------------------------------------------

class UsageCollector(private val context: Context, private val store: EventStore = EventStore.get(context)) {
    private val prefs = context.getSharedPreferences("usage_collector", Context.MODE_PRIVATE)

    /**
     * Collects everything new since the last run, up to [LATENESS_MS] ago: Android records usage events a moment
     * after they happen, so the newest few seconds are left for the next run instead of being skipped for good.
     * Returns how many events were stored (0 without usage access).
     */
    fun collect(nowMs: Long = System.currentTimeMillis(), nowUptimeMs: Long = SystemClock.elapsedRealtime()): Int = synchronized(LOCK) {
        if (!Permissions.usageGranted(context)) return 0
        val untilMs = nowMs - LATENESS_MS
        val state = ClockCheck.correct(loadState(untilMs), nowMs, nowUptimeMs)
        if (untilMs <= state.checkpointMs) {
            // Only after a restart combined with a clock set back: start again from here, keeping the open apps.
            if (untilMs < state.checkpointMs) saveState(state.copy(checkpointMs = untilMs), nowUptimeMs - LATENESS_MS)
            return 0
        }
        val usageStats = context.getSystemService(UsageStatsManager::class.java)
        val raw = read(usageStats.queryEvents(state.checkpointMs, untilMs))
        val collected = UsageSessionizer.collect(raw, state, untilMs, ignoredPackages(), ::label)
        store.add(collected.events) // on disk (synced) before the checkpoint moves: a crash repeats work, never loses it
        saveState(collected.state, nowUptimeMs - LATENESS_MS)
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
                UsageEvents.Event.ACTIVITY_STOPPED -> UsageType.STOPPED
                UsageEvents.Event.SCREEN_INTERACTIVE -> UsageType.SCREEN_ON
                UsageEvents.Event.SCREEN_NON_INTERACTIVE -> UsageType.SCREEN_OFF
                UsageEvents.Event.DEVICE_SHUTDOWN -> UsageType.SHUTDOWN
                UsageEvents.Event.DEVICE_STARTUP -> UsageType.STARTUP
                else -> null
            } ?: continue
            raw += RawUsageEvent(event.timeStamp, event.packageName.orEmpty(), event.className, type)
        }
        return raw
    }

    /**
     * The home screen and the system UI: time there is not time in an app. Only real launchers count; fallback
     * home screens with a negative priority (Settings registers one for first boot) are ordinary apps.
     */
    private fun ignoredPackages(): Set<String> {
        val home = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME)
        val pm = context.packageManager
        val default = pm.resolveActivity(home, PackageManager.MATCH_DEFAULT_ONLY)?.activityInfo?.packageName
        val launchers = pm.queryIntentActivities(home, 0).filter { it.priority >= 0 }.map { it.activityInfo.packageName }
        return (launchers + listOfNotNull(default?.takeIf { it != "android" }) + "com.android.systemui").toSet()
    }

    private val labels = HashMap<String, String?>()

    private fun label(pkg: String): String? = labels.getOrPut(pkg) {
        runCatching {
            val info = context.packageManager.getApplicationInfo(pkg, 0)
            context.packageManager.getApplicationLabel(info).toString()
        }.getOrNull()
    }

    private fun loadState(untilMs: Long): UsageState {
        val checkpoint = prefs.getLong(KEY_CHECKPOINT, -1)
        if (checkpoint < 0) {
            // First run: start from the beginning of yesterday, so the hub sees some history right away.
            val zone = ZoneId.systemDefault()
            val yesterday = Instant.ofEpochMilli(untilMs).atZone(zone).toLocalDate().minusDays(1).atStartOfDay(zone)
            return UsageState(yesterday.toInstant().toEpochMilli().coerceAtMost(untilMs))
        }
        return UsageState(checkpoint, parseOpen(prefs.getString(KEY_OPEN, "{}") ?: "{}"), prefs.getLong(KEY_UPTIME, -1))
    }

    private fun saveState(state: UsageState, checkpointUptimeMs: Long) {
        val open = JSONObject()
        state.open.forEach { (pkg, app) ->
            open.put(pkg, JSONObject().put("start", app.startMs).put("activities", JSONArray(app.activities.toList())))
        }
        val saved = prefs.edit()
            .putLong(KEY_CHECKPOINT, state.checkpointMs)
            .putLong(KEY_UPTIME, checkpointUptimeMs)
            .putString(KEY_OPEN, open.toString())
            .commit()
        check(saved) { "could not save the usage checkpoint" } // the next run repeats this window instead
    }

    private companion object {
        val LOCK = Any()
        const val LATENESS_MS = 15_000L
        const val KEY_CHECKPOINT = "checkpoint_ms"
        const val KEY_UPTIME = "checkpoint_uptime_ms"
        const val KEY_OPEN = "open_apps"

        /** Reads both the current format and the first one ({package: start}). */
        fun parseOpen(text: String): Map<String, OpenApp> {
            val json = runCatching { JSONObject(text) }.getOrElse { return emptyMap() }
            return json.keys().asSequence().associateWith { pkg ->
                when (val value = json.get(pkg)) {
                    is JSONObject -> {
                        val names = value.optJSONArray("activities") ?: JSONArray()
                        OpenApp(value.getLong("start"), (0 until names.length()).map { names.getString(it) }.toSet())
                    }
                    else -> OpenApp(json.getLong(pkg))
                }
            }
        }
    }
}
