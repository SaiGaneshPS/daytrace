// DT-23: sleep (with its stages), daily steps and meals from Health Connect, into the event store. Samsung Health
// shares them with Health Connect once its sync is on (docs/install-android.md). The first read covers 30 days
// (as far back as Health Connect lets an app read), then every sync reads the last 3 days again: each record keeps
// its id, so a record read again is the same event, and one that changed (a step total that grew, an edited meal)
// replaces its copy on the phone and on the hub.
package app.daytrace.android.health

import android.content.Context
import androidx.core.content.edit
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.HealthConnectFeatures
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.MealType
import androidx.health.connect.client.records.NutritionRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.request.AggregateGroupByPeriodRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import app.daytrace.android.data.EventStore
import app.daytrace.android.data.PhoneEvent
import app.daytrace.android.sync.HubClient
import org.json.JSONArray
import org.json.JSONObject
import java.time.Duration
import java.time.Instant
import java.time.LocalDate
import java.time.Period
import java.time.ZoneId
import java.time.temporal.ChronoUnit
import kotlin.reflect.KClass

/** A sleep session as Health Connect keeps it, with its stages (Health Connect's stage numbers). */
data class SleepRead(val id: String, val startMs: Long, val endMs: Long, val stages: List<StageRead> = emptyList())

data class StageRead(val startMs: Long, val endMs: Long, val type: Int)

/** One food or drink someone logged, with Health Connect's meal type number. */
data class FoodRead(val startMs: Long, val name: String?, val mealType: Int)

data class DaySteps(val day: LocalDate, val count: Long)

/** How Health Connect's records become hub events (docs/api.md). Pure, so every rule has a test. */
object HealthEvents {
    const val SOURCE = "health_connect"
    const val MAX_ITEMS = 30 // the hub's limits for a meal's items
    const val MAX_ITEM = 100

    /** Health Connect's stage numbers as the hub names them; Samsung's "light" sleep is Apple's "core". */
    fun stageName(type: Int): String = when (type) {
        SleepSessionRecord.STAGE_TYPE_AWAKE, SleepSessionRecord.STAGE_TYPE_OUT_OF_BED,
        SleepSessionRecord.STAGE_TYPE_AWAKE_IN_BED -> "awake"
        SleepSessionRecord.STAGE_TYPE_LIGHT -> "core"
        SleepSessionRecord.STAGE_TYPE_DEEP -> "deep"
        SleepSessionRecord.STAGE_TYPE_REM -> "rem"
        else -> "asleep" // SLEEPING, UNKNOWN
    }

    fun mealTypeName(type: Int): String? = when (type) {
        MealType.MEAL_TYPE_BREAKFAST -> "breakfast"
        MealType.MEAL_TYPE_LUNCH -> "lunch"
        MealType.MEAL_TYPE_DINNER -> "dinner"
        MealType.MEAL_TYPE_SNACK -> "snack"
        else -> null // the hub guesses from the time
    }

    /**
     * Data as JSON with its keys in one order, so the same values always make the same text (the store compares
     * the text to tell whether a record changed).
     */
    fun json(values: Map<String, Any>): String = JSONObject(values.toSortedMap()).toString()

    /**
     * One event per stage (the hub counts the sleeping ones and takes the awake ones out), or one "asleep" event
     * for a session without stages. Measured, since a health app recorded it. Keyed hc:<record id>(:<stage number>).
     */
    fun sleep(sessions: List<SleepRead>): List<PhoneEvent> = sessions.filter { it.endMs > it.startMs }.flatMap { session ->
        val stages = session.stages.filter { it.endMs > it.startMs }.sortedBy { it.startMs }
        if (stages.isEmpty()) {
            listOf(sleepEvent(session.startMs, session.endMs, "asleep", "hc:${session.id}"))
        } else {
            stages.mapIndexed { index, stage -> sleepEvent(stage.startMs, stage.endMs, stageName(stage.type), "hc:${session.id}:$index") }
        }
    }

    private fun sleepEvent(startMs: Long, endMs: Long, stage: String, id: String) =
        PhoneEvent("sleep", SOURCE, startMs, endMs, data = json(mapOf("measured" to true, "stage" to stage)), id = id)

    /**
     * A day's step total, from local midnight to the end of the day, or for today to the start of this hour (so
     * the event changes at most hourly, besides the count). Each total replaces the day's earlier one on the hub.
     * Keyed steps:<yyyy-mm-dd>.
     */
    fun steps(days: List<DaySteps>, zone: ZoneId, nowMs: Long): List<PhoneEvent> = days.filter { it.count >= 0 }.map { day ->
        val start = day.day.atStartOfDay(zone).toInstant().toEpochMilli()
        val nextDay = day.day.plusDays(1).atStartOfDay(zone).toInstant().toEpochMilli()
        val thisHour = Instant.ofEpochMilli(nowMs).atZone(zone).truncatedTo(ChronoUnit.HOURS).toInstant().toEpochMilli()
        val end = minOf(nextDay, maxOf(thisHour, start))
        PhoneEvent("steps", SOURCE, start, end, data = json(mapOf("count" to day.count)), id = "steps:${day.day}")
    }

    /**
     * Foods logged at the same moment for the same meal are one meal, each food an item (Samsung Health logs a meal
     * as one record per food). Keyed meal:<time>:<meal type>, so a food added or removed later changes the meal in
     * place. A food without a name is "Food": the hub needs at least one item.
     */
    fun meals(foods: List<FoodRead>): List<PhoneEvent> =
        foods.groupBy { it.startMs to it.mealType }.entries.sortedWith(compareBy({ it.key.first }, { it.key.second })).map { (at, group) ->
            val (startMs, mealType) = at
            val items = group.mapNotNull { food -> food.name?.let(::itemName) }.distinct().take(MAX_ITEMS).ifEmpty { listOf("Food") }
            val data = buildMap<String, Any> {
                put("items", JSONArray(items))
                mealTypeName(mealType)?.let { put("meal_type", it) }
            }
            PhoneEvent("meal", SOURCE, startMs, null, data = json(data), id = "meal:$startMs:$mealType")
        }

    /** A food's name on one line, cleaned and cut to the hub's length like any text, or null if nothing is left. */
    fun itemName(name: String): String? = HubClient.cleanText(name.split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" "), MAX_ITEM)
}

class HealthCollector(private val context: Context, private val store: EventStore = EventStore.get(context)) {
    /**
     * Reads what Health Connect has and stores it. Returns how many events were added or changed. Reads nothing when
     * Health Connect isn't there or a permission is missing (onboarding asks for them). In the background this works
     * only with the "read in the background" permission; the next sync with the app open reads it otherwise.
     */
    suspend fun collect(now: Instant = Instant.now(), zone: ZoneId = ZoneId.systemDefault()): Int {
        if (HealthConnectClient.getSdkStatus(context, HEALTH_CONNECT_PACKAGE) != HealthConnectClient.SDK_AVAILABLE) return 0
        val client = HealthConnectClient.getOrCreate(context)
        val granted = client.permissionController.getGrantedPermissions()
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val first = !prefs.getBoolean(FIRST_READ_DONE, false)
        val from = now.minus(Duration.ofDays(if (first) FIRST_DAYS else LATER_DAYS))
        val events = buildList {
            if (canRead(granted, SleepSessionRecord::class)) {
                addAll(HealthEvents.sleep(readAll(client, SleepSessionRecord::class, from, now).map(::sleepRead)))
            }
            if (canRead(granted, StepsRecord::class)) {
                addAll(HealthEvents.steps(readSteps(client, from, now, zone), zone, now.toEpochMilli()))
            }
            if (canRead(granted, NutritionRecord::class)) {
                addAll(HealthEvents.meals(readAll(client, NutritionRecord::class, from, now).map(::foodRead)))
            }
        }
        val changed = store.add(events, zone)
        // Only once all three were read (a later grant of the others still gets its 30 days) and stored.
        if (first && HEALTH_READS.all { it in granted }) prefs.edit { putBoolean(FIRST_READ_DONE, true) }
        return changed
    }

    private fun canRead(granted: Set<String>, type: KClass<out Record>) = HealthPermission.getReadPermission(type) in granted

    private suspend fun <T : Record> readAll(client: HealthConnectClient, type: KClass<T>, from: Instant, to: Instant): List<T> {
        val records = mutableListOf<T>()
        var page: String? = null
        do {
            val response = client.readRecords(ReadRecordsRequest(type, TimeRangeFilter.between(from, to), pageToken = page))
            records += response.records
            page = response.pageToken?.takeIf { it.isNotEmpty() }
        } while (page != null && records.size < MAX_RECORDS)
        return records
    }

    /** Health Connect adds up each day itself, leaving out the steps two apps both recorded. */
    private suspend fun readSteps(client: HealthConnectClient, from: Instant, to: Instant, zone: ZoneId): List<DaySteps> {
        val start = from.atZone(zone).toLocalDate().atStartOfDay()
        val end = to.atZone(zone).toLocalDateTime()
        val days = client.aggregateGroupByPeriod(
            AggregateGroupByPeriodRequest(setOf(StepsRecord.COUNT_TOTAL), TimeRangeFilter.between(start, end), Period.ofDays(1)),
        )
        return days.mapNotNull { day -> day.result[StepsRecord.COUNT_TOTAL]?.let { DaySteps(day.startTime.toLocalDate(), it) } }
    }

    private fun sleepRead(record: SleepSessionRecord) = SleepRead(
        record.metadata.id,
        record.startTime.toEpochMilli(),
        record.endTime.toEpochMilli(),
        record.stages.map { StageRead(it.startTime.toEpochMilli(), it.endTime.toEpochMilli(), it.stage) },
    )

    private fun foodRead(record: NutritionRecord) = FoodRead(record.startTime.toEpochMilli(), record.name, record.mealType)

    companion object {
        const val HEALTH_CONNECT_PACKAGE = "com.google.android.apps.healthdata"
        private const val PREFS = "health"
        private const val FIRST_READ_DONE = "first_read_done"
        private const val FIRST_DAYS = 30L
        private const val LATER_DAYS = 3L
        private const val MAX_RECORDS = 5000 // per kind and read: far more than 30 days hold, never unbounded

        private val HEALTH_READS = listOf(SleepSessionRecord::class, StepsRecord::class, NutritionRecord::class)
            .map { HealthPermission.getReadPermission(it) }

        /**
         * Reading while the app is closed needs its own permission (Health Connect has it on Android 14 and newer,
         * and on older phones with a recent Health Connect). Asked for with the others where the phone has it.
         */
        fun backgroundPermission(context: Context): String? = runCatching {
            val features = HealthConnectClient.getOrCreate(context).features
            val status = features.getFeatureStatus(HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND)
            HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND.takeIf { status == HealthConnectFeatures.FEATURE_STATUS_AVAILABLE }
        }.getOrNull()
    }
}
