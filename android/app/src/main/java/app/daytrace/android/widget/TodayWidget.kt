// DT-58: the home-screen widget: today's screen time (phone and computers), the longest streak running with its flame
// and day count, and progress on the main goal. Every number comes from the hub (the same answers the dashboard
// shows), never worked out on the phone, read with the app's dashboard token. It refreshes after each background sync
// and every 30 minutes, over the hub's Wi-Fi only, and shows the last numbers it got (with their day and time) when
// the hub is out of reach. What it shows lives in each widget's Glance state, so a refresh redraws it at once.
package app.daytrace.android.widget

import android.content.Context
import androidx.compose.runtime.Composable
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.edit
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.Image
import androidx.glance.ImageProvider
import androidx.glance.action.actionStartActivity
import androidx.glance.action.clickable
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetManager
import androidx.glance.appwidget.LinearProgressIndicator
import androidx.glance.appwidget.provideContent
import androidx.glance.appwidget.state.updateAppWidgetState
import androidx.glance.background
import androidx.glance.currentState
import androidx.glance.layout.Alignment
import androidx.glance.layout.Column
import androidx.glance.layout.Row
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.fillMaxWidth
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.layout.size
import androidx.glance.layout.width
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import androidx.glance.unit.ColorProvider
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import app.daytrace.android.MainActivity
import app.daytrace.android.R
import app.daytrace.android.sync.HubClient
import app.daytrace.android.sync.HubDiscovery
import app.daytrace.android.sync.HubProof
import app.daytrace.android.sync.HubResult
import app.daytrace.android.sync.PairingStore
import app.daytrace.android.sync.WifiOnly
import app.daytrace.android.sync.findProvenHub
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONObject
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import java.util.concurrent.TimeUnit
import kotlin.math.roundToInt

// --- the numbers, in plain Kotlin so they are unit tested --------------------------------------------------------

/** What the widget shows, as the hub said it. */
data class WidgetNumbers(
    val screenMinutes: Int?,
    val phoneMinutes: Int?,
    val computerMinutes: Int?,
    val streakName: String?,
    val streakDays: Int,
    /** met, at_risk, missed or no_data: the flame is lit only once today counts. */
    val streakToday: String?,
    val goalLabel: String?,
    /** 0 to 100, toward the target (or how much of a limit is used). */
    val goalProgress: Int?,
    val goalText: String?,
    val updatedAtMs: Long,
    /** The day the numbers are for, as the hub said it ("2026-09-27"). */
    val day: String?,
) {
    fun toJson(): JSONObject = JSONObject()
        .putOpt("screen", screenMinutes).putOpt("phone", phoneMinutes).putOpt("computer", computerMinutes)
        .putOpt("streak", streakName).put("streak_days", streakDays).putOpt("streak_today", streakToday)
        .putOpt("goal", goalLabel).putOpt("goal_progress", goalProgress).putOpt("goal_text", goalText)
        .put("updated", updatedAtMs).putOpt("day", day)

    companion object {
        fun fromJson(json: JSONObject): WidgetNumbers? = runCatching {
            fun int(key: String) = if (json.has(key) && !json.isNull(key)) json.getInt(key) else null
            fun str(key: String) = if (json.has(key) && !json.isNull(key)) json.getString(key) else null
            WidgetNumbers(
                int("screen"), int("phone"), int("computer"), str("streak"), json.optInt("streak_days"), str("streak_today"),
                str("goal"), int("goal_progress"), str("goal_text"), json.getLong("updated"), str("day"),
            )
        }.getOrNull()
    }
}

/** What a widget shows: the hub's numbers, or none and why ([note]: [UNPAIRED], [PAIR_AGAIN], or null for "not yet"). */
data class WidgetView(val numbers: WidgetNumbers? = null, val note: String? = null) {
    fun toJson(): String = JSONObject().putOpt("numbers", numbers?.toJson()).putOpt("note", note).toString()

    companion object {
        /** Not paired, or the hub was forgotten: its numbers are gone from the widget too. */
        const val UNPAIRED = "unpaired"

        /** Paired before the app had a dashboard token: the widget reads with that token only. */
        const val PAIR_AGAIN = "pair_again"

        fun fromJson(text: String): WidgetView? = runCatching {
            val json = JSONObject(text)
            WidgetView(json.optJSONObject("numbers")?.let(WidgetNumbers::fromJson), if (json.isNull("note")) null else json.getString("note"))
        }.getOrNull()
    }
}

object WidgetRules {
    /** "6h 59m", "4h" or "42m", as the dashboard writes them. */
    fun duration(minutes: Int): String = when {
        minutes < 60 -> "${minutes}m"
        minutes % 60 == 0 -> "${minutes / 60}h"
        else -> "${minutes / 60}h ${minutes % 60}m"
    }

    /**
     * From the hub's answers: GET /insights/day, /streaks and /goals (docs/api.md). The streak shown is the longest
     * run going; the goal is the first one the hub lists (focused time).
     */
    fun numbers(day: JSONObject, streaks: JSONObject, goals: JSONObject, nowMs: Long): WidgetNumbers {
        fun minutes(key: String) = if (day.isNull(key)) null else day.getDouble(key).roundToInt()
        val runs = streaks.optJSONArray("streaks")?.let { array -> (0 until array.length()).map(array::getJSONObject) }.orEmpty()
        val streak = runs.maxByOrNull { it.optInt("current") }?.takeIf { it.optInt("current") > 0 }
        val goal = goals.optJSONArray("goals")?.optJSONObject(0)
        val today = goal?.optJSONObject("today")
        return WidgetNumbers(
            screenMinutes = minutes("screen_minutes"),
            phoneMinutes = minutes("phone_minutes"),
            computerMinutes = minutes("computer_minutes"),
            streakName = streak?.getString("name"),
            streakDays = streak?.optInt("current") ?: 0,
            streakToday = streak?.optString("today"),
            goalLabel = goal?.optString("label"),
            goalProgress = today?.takeIf { !it.isNull("progress") }?.getInt("progress"),
            goalText = goal?.let { goalText(it) },
            updatedAtMs = nowMs,
            day = if (day.isNull("date")) null else day.optString("date"),
        )
    }

    /** "Today" for today's numbers; numbers kept from another day say which ("Yesterday", "Sat 26 Sep"). */
    fun heading(day: String?, today: LocalDate, locale: Locale = Locale.getDefault()): String {
        val date = day?.let { runCatching { LocalDate.parse(it) }.getOrNull() } ?: return "Today"
        return when (date) {
            today -> "Today"
            today.minusDays(1) -> "Yesterday"
            else -> date.format(DateTimeFormatter.ofPattern("EEE d MMM", locale))
        }
    }

    /** "5h 36m of 4h" for a target in minutes, "23:25, by 23:30" for a clock time. */
    private fun goalText(goal: JSONObject): String? {
        val value = goal.optJSONObject("today")?.opt("value")?.takeIf { it != JSONObject.NULL } ?: return null
        val target = goal.opt("target")
        return if (goal.optString("unit") == "minutes" && value is Number && target is Number) {
            "${duration(value.toDouble().roundToInt())} of ${duration(target.toDouble().roundToInt())}"
        } else {
            "$value, goal $target"
        }
    }
}

// --- storing and refreshing ---------------------------------------------------------------------------------------

/** The latest view as JSON, so a widget placed later starts from it (each widget's own copy is its Glance state). */
class WidgetStore(context: Context) {
    private val prefs = context.getSharedPreferences("widget", Context.MODE_PRIVATE)

    fun load(): String? = prefs.getString(KEY, null)

    fun save(view: String) = prefs.edit(commit = true) { putString(KEY, view) }

    private companion object {
        const val KEY = "view"
    }
}

/** Each widget's own copy of what it shows (its Glance state), read in composition so an update redraws it. */
private val VIEW = stringPreferencesKey("view")

object WidgetRefresher {
    private val lock = Mutex() // one refresh at a time, and Forget always last

    /**
     * Asks the hub for today's numbers and redraws the widgets. Nothing is asked without a widget on the home screen,
     * off the hub's Wi-Fi, or when the hub doesn't prove it paired this phone (the token goes only to that hub; a hub
     * that moved is found as a sync finds it). The reads use the dashboard token: reading is not this phone sending
     * anything, so they never make it look live on the hub. Not paired, or paired without that token: the widget
     * says what to do instead.
     */
    suspend fun refresh(context: Context, zone: ZoneId = ZoneId.systemDefault()): Boolean = lock.withLock {
        if (GlanceAppWidgetManager(context).getGlanceIds(TodayWidget::class.java).isEmpty()) return@withLock false
        val store = PairingStore.get(context)
        val pairing = store.pairing() ?: return@withLock false.also { show(context, WidgetView(note = WidgetView.UNPAIRED)) }
        val viewer = pairing.viewerToken ?: return@withLock false.also { show(context, WidgetView(note = WidgetView.PAIR_AGAIN)) }
        val network = WifiOnly.network(context) ?: return@withLock false
        val http = HubClient.onNetwork(network)
        val found = findProvenHub(pairing.config, http, store, findHubs = { HubDiscovery(context).findNow() })
        val proof = found.proof
        if (proof !is HubResult.Ok || proof.value != HubProof.PAIRED) return@withLock false
        val reader = HubClient(found.config.copy(token = viewer), http)
        val tz = mapOf("tz" to zone.id)
        val day = reader.read("insights/day", tz) as? HubResult.Ok ?: return@withLock false
        val streaks = reader.read("streaks", tz + ("days" to "1")) as? HubResult.Ok ?: return@withLock false
        val goals = reader.read("goals", tz) as? HubResult.Ok ?: return@withLock false
        show(context, WidgetView(WidgetRules.numbers(day.value, streaks.value, goals.value, System.currentTimeMillis())))
        true
    }

    /** "Forget this hub": that hub's numbers leave the widget too, which now says to pair. */
    suspend fun forget(context: Context) = lock.withLock { show(context, WidgetView(note = WidgetView.UNPAIRED)) }

    private suspend fun show(context: Context, view: WidgetView) {
        val json = view.toJson()
        WidgetStore(context).save(json)
        GlanceAppWidgetManager(context).getGlanceIds(TodayWidget::class.java).forEach { id ->
            updateAppWidgetState(context, id) { state -> state[VIEW] = json }
            TodayWidget().update(context, id)
        }
    }
}

/** Every 30 minutes on Wi-Fi (with or without internet, never a VPN, as the sync), while a widget is on the home screen. */
class WidgetWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        runCatching { WidgetRefresher.refresh(applicationContext) }
        return Result.success()
    }

    companion object {
        internal const val PERIODIC = "daytrace-widget"

        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<WidgetWorker>(30, TimeUnit.MINUTES).setConstraints(WifiOnly.workConstraints()).build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP, request)
        }

        fun cancel(context: Context) {
            WorkManager.getInstance(context).cancelUniqueWork(PERIODIC)
        }
    }
}

// --- the widget ---------------------------------------------------------------------------------------------------

class TodayWidget : GlanceAppWidget() {
    override suspend fun provideGlance(context: Context, id: GlanceId) {
        // A widget placed after the last refresh starts from what the others show.
        val latest = WidgetStore(context).load()
        if (latest != null) updateAppWidgetState(context, id) { state -> if (state[VIEW] == null) state[VIEW] = latest }
        provideContent {
            val view = currentState<Preferences>()[VIEW]?.let(WidgetView::fromJson) ?: WidgetView()
            WidgetContent(view)
        }
    }
}

private val White = ColorProvider(Color.White)
private val Soft = ColorProvider(Color(0xDDFFFFFF))

@Composable
private fun WidgetContent(view: WidgetView) {
    val numbers = view.numbers
    Column(
        GlanceModifier.fillMaxSize().background(ImageProvider(R.drawable.widget_background)).padding(14.dp)
            .clickable(actionStartActivity<MainActivity>()),
    ) {
        Row(GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(
                WidgetRules.heading(numbers?.day, LocalDate.now()),
                style = TextStyle(color = Soft, fontSize = 13.sp, fontWeight = FontWeight.Medium),
            )
            Spacer(GlanceModifier.defaultWeight())
            if (numbers != null) Text(asOf(numbers.updatedAtMs), style = TextStyle(color = Soft, fontSize = 11.sp))
        }
        if (numbers == null) {
            Spacer(GlanceModifier.height(8.dp))
            val text = when (view.note) {
                WidgetView.UNPAIRED -> "Open Daytrace to pair with your hub."
                WidgetView.PAIR_AGAIN -> "Open Daytrace and pair with your hub again to see your day here."
                else -> "Your day shows here once this phone is on your hub's Wi-Fi."
            }
            Text(text, style = TextStyle(color = White, fontSize = 15.sp))
            return@Column
        }
        Text(numbers.screenMinutes?.let(WidgetRules::duration) ?: "-", style = TextStyle(color = White, fontSize = 30.sp, fontWeight = FontWeight.Bold))
        val split = listOfNotNull(
            numbers.phoneMinutes?.let { "${WidgetRules.duration(it)} phone" },
            numbers.computerMinutes?.let { "${WidgetRules.duration(it)} computers" },
        ).joinToString(" · ")
        if (split.isNotEmpty()) Text(split, style = TextStyle(color = Soft, fontSize = 12.sp))
        Spacer(GlanceModifier.height(10.dp))
        Row(GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            if (numbers.streakName != null) {
                Image(
                    ImageProvider(if (numbers.streakToday == "met") R.drawable.ic_widget_flame else R.drawable.ic_widget_flame_waiting),
                    contentDescription = null,
                    modifier = GlanceModifier.size(20.dp),
                )
                Spacer(GlanceModifier.width(4.dp))
                Text("${numbers.streakDays} ${if (numbers.streakDays == 1) "day" else "days"}", style = TextStyle(color = White, fontSize = 15.sp, fontWeight = FontWeight.Bold))
                Spacer(GlanceModifier.width(6.dp))
                Text(numbers.streakName, style = TextStyle(color = Soft, fontSize = 12.sp), maxLines = 1)
            } else {
                Text("No streak running yet", style = TextStyle(color = Soft, fontSize = 12.sp))
            }
        }
        if (numbers.goalLabel != null && numbers.goalProgress != null) {
            Spacer(GlanceModifier.height(8.dp))
            Text(
                listOfNotNull(numbers.goalLabel, numbers.goalText).joinToString(": "),
                style = TextStyle(color = Soft, fontSize = 12.sp),
                maxLines = 1,
            )
            Spacer(GlanceModifier.height(4.dp))
            LinearProgressIndicator(
                progress = numbers.goalProgress.coerceIn(0, 100) / 100f,
                modifier = GlanceModifier.fillMaxWidth().height(6.dp),
                color = White,
                backgroundColor = ColorProvider(Color(0x55FFFFFF)),
            )
        }
    }
}

private fun asOf(epochMs: Long): String =
    "as of " + Instant.ofEpochMilli(epochMs).atZone(ZoneId.systemDefault()).format(DateTimeFormatter.ofPattern("HH:mm"))
