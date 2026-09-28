// DT-58: the home-screen widget: today's screen time (phone and computers), the longest streak running with its flame
// and day count, and progress on the main goal. Every number comes from the hub (the same answers the dashboard
// shows), never worked out on the phone. It refreshes after each background sync and every 30 minutes, over the
// hub's Wi-Fi only, and shows the last numbers it got (with their time) when the hub is out of reach.
package app.daytrace.android.widget

import android.content.Context
import androidx.compose.runtime.Composable
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
import androidx.glance.appwidget.updateAll
import androidx.glance.background
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
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import app.daytrace.android.MainActivity
import app.daytrace.android.R
import app.daytrace.android.sync.HubClient
import app.daytrace.android.sync.HubProof
import app.daytrace.android.sync.HubResult
import app.daytrace.android.sync.PairingStore
import app.daytrace.android.sync.WifiOnly
import org.json.JSONObject
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
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
) {
    fun toJson(): String = JSONObject()
        .putOpt("screen", screenMinutes).putOpt("phone", phoneMinutes).putOpt("computer", computerMinutes)
        .putOpt("streak", streakName).put("streak_days", streakDays).putOpt("streak_today", streakToday)
        .putOpt("goal", goalLabel).putOpt("goal_progress", goalProgress).putOpt("goal_text", goalText)
        .put("updated", updatedAtMs).toString()

    companion object {
        fun fromJson(text: String): WidgetNumbers? = runCatching {
            val json = JSONObject(text)
            fun int(key: String) = if (json.has(key) && !json.isNull(key)) json.getInt(key) else null
            fun str(key: String) = if (json.has(key) && !json.isNull(key)) json.getString(key) else null
            WidgetNumbers(
                int("screen"), int("phone"), int("computer"), str("streak"), json.optInt("streak_days"), str("streak_today"),
                str("goal"), int("goal_progress"), str("goal_text"), json.getLong("updated"),
            )
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
        )
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

class WidgetStore(context: Context) {
    private val prefs = context.getSharedPreferences("widget", Context.MODE_PRIVATE)

    fun load(): WidgetNumbers? = prefs.getString(KEY, null)?.let(WidgetNumbers::fromJson)

    fun save(numbers: WidgetNumbers) = prefs.edit(commit = true) { putString(KEY, numbers.toJson()) }

    private companion object {
        const val KEY = "numbers"
    }
}

object WidgetRefresher {
    /**
     * Asks the hub for today's numbers and redraws the widget. Nothing happens without a widget on the home screen,
     * off the hub's Wi-Fi, or when the hub doesn't prove it paired this phone (the token goes only to that hub).
     */
    suspend fun refresh(context: Context, zone: ZoneId = ZoneId.systemDefault()): Boolean {
        if (GlanceAppWidgetManager(context).getGlanceIds(TodayWidget::class.java).isEmpty()) return false
        val config = PairingStore.get(context).load() ?: return false
        val network = WifiOnly.network(context) ?: return false
        val client = HubClient(config, HubClient.onNetwork(network))
        val proof = client.proveHub()
        if (proof !is HubResult.Ok || proof.value != HubProof.PAIRED) return false
        val tz = mapOf("tz" to zone.id)
        val day = client.read("insights/day", tz) as? HubResult.Ok ?: return false
        val streaks = client.read("streaks", tz + ("days" to "1")) as? HubResult.Ok ?: return false
        val goals = client.read("goals", tz) as? HubResult.Ok ?: return false
        WidgetStore(context).save(WidgetRules.numbers(day.value, streaks.value, goals.value, System.currentTimeMillis()))
        TodayWidget().updateAll(context)
        return true
    }
}

/** Every 30 minutes on Wi-Fi, while a widget is on the home screen. */
class WidgetWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        runCatching { WidgetRefresher.refresh(applicationContext) }
        return Result.success()
    }

    companion object {
        private const val PERIODIC = "daytrace-widget"

        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<WidgetWorker>(30, TimeUnit.MINUTES)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.UNMETERED).build())
                .build()
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
        val numbers = WidgetStore(context).load()
        provideContent { WidgetContent(numbers) }
    }
}

private val White = ColorProvider(Color.White)
private val Soft = ColorProvider(Color(0xDDFFFFFF))

@Composable
private fun WidgetContent(numbers: WidgetNumbers?) {
    Column(
        GlanceModifier.fillMaxSize().background(ImageProvider(R.drawable.widget_background)).padding(14.dp)
            .clickable(actionStartActivity<MainActivity>()),
    ) {
        Row(GlanceModifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text("Today", style = TextStyle(color = Soft, fontSize = 13.sp, fontWeight = FontWeight.Medium))
            Spacer(GlanceModifier.defaultWeight())
            if (numbers != null) Text(asOf(numbers.updatedAtMs), style = TextStyle(color = Soft, fontSize = 11.sp))
        }
        if (numbers == null) {
            Spacer(GlanceModifier.height(8.dp))
            Text("Open Daytrace to pair with your hub.", style = TextStyle(color = White, fontSize = 15.sp))
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
