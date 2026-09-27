// DT-23: sleep (with its stages), daily steps and meals from Health Connect, into the event store. Samsung Health
// shares them with Health Connect once its sync is on (docs/install-android.md). The first read covers 30 days (as
// far back as Health Connect lets an app read). After that, Health Connect's list of changes says which days changed
// since the last sync, however long ago that was and whatever dates the changed records have (a backfill of old
// nights, say), and those days are read again, with today and yesterday. Each record keeps its id, so a record read
// again is the same event, and one that changed (a step total that grew, an edited meal) replaces its copy on the
// phone and on the hub.
package app.daytrace.android.health

import android.content.Context
import androidx.core.content.edit
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.HealthConnectFeatures
import androidx.health.connect.client.changes.UpsertionChange
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.MealType
import androidx.health.connect.client.records.NutritionRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.request.AggregateGroupByPeriodRequest
import androidx.health.connect.client.request.ChangesTokenRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import app.daytrace.android.data.EventStore
import app.daytrace.android.data.PhoneEvent
import app.daytrace.android.sync.HubClient
import kotlinx.coroutines.CancellationException
import org.json.JSONArray
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

/** How Health Connect's records become hub events (docs/api.md), and which days to read. Pure, so every rule has a test. */
object HealthEvents {
    const val SOURCE = "health_connect"
    const val MAX_ITEMS = 30 // the hub's limits for a meal's items
    const val MAX_ITEM = 100
    /** As far back as Health Connect lets an app read (from before it was first allowed). */
    const val MAX_DAYS = 30L

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
        PhoneEvent("sleep", SOURCE, startMs, endMs, data = PhoneEvent.dataJson(mapOf("measured" to true, "stage" to stage)), id = id)

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
        PhoneEvent("steps", SOURCE, start, end, data = PhoneEvent.dataJson(mapOf("count" to day.count)), id = "steps:${day.day}")
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
            PhoneEvent("meal", SOURCE, startMs, null, data = PhoneEvent.dataJson(data), id = "meal:$startMs:$mealType")
        }

    /** A food's name on one line, cleaned and cut to the hub's length like any text, or null if nothing is left. */
    fun itemName(name: String): String? = HubClient.cleanText(name.split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" "), MAX_ITEM)

    /** The local days a record from [startMs] to [endMs] falls on (at most [MAX_DAYS] of them). */
    fun daysOf(startMs: Long, endMs: Long, zone: ZoneId): Set<LocalDate> {
        val first = Instant.ofEpochMilli(startMs).atZone(zone).toLocalDate()
        val last = Instant.ofEpochMilli(maxOf(startMs, endMs)).atZone(zone).toLocalDate()
        return generateSequence(first) { it.plusDays(1) }.takeWhile { !it.isAfter(last) }.take(MAX_DAYS.toInt()).toSet()
    }

    /**
     * The days to read, as runs of whole days, oldest first: every day of the last [MAX_DAYS] when there is no list
     * of changes to go by ([changed] null), otherwise today, yesterday and each changed day in that span (Health
     * Connect doesn't let an app read further back).
     */
    fun daysToRead(today: LocalDate, changed: Set<LocalDate>?): List<ClosedRange<LocalDate>> {
        val earliest = today.minusDays(MAX_DAYS)
        val days = if (changed == null) {
            generateSequence(earliest) { it.plusDays(1) }.takeWhile { !it.isAfter(today) }.toList()
        } else {
            (changed + today + today.minusDays(1)).filter { !it.isBefore(earliest) && !it.isAfter(today) }.sorted()
        }
        val runs = mutableListOf<ClosedRange<LocalDate>>()
        for (day in days) {
            val last = runs.lastOrNull()
            if (last != null && last.endInclusive.plusDays(1) == day) runs[runs.size - 1] = last.start..day else runs += day..day
        }
        return runs
    }
}

class HealthCollector(
    private val context: Context,
    private val store: EventStore = EventStore.get(context),
    /** Health Connect, or null where the phone doesn't have it (a test passes its own). */
    private val connect: () -> HealthConnectClient? = { available(context) },
) {
    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /** One kind of record, read by days. */
    private inner class Kind<T : Record>(
        val name: String,
        val type: KClass<T>,
        val read: suspend (HealthConnectClient, List<ClosedRange<LocalDate>>, Instant, ZoneId) -> List<PhoneEvent>,
    )

    private val kinds = listOf(
        Kind("sleep", SleepSessionRecord::class) { client, runs, now, zone ->
            // From the evening before too: a night that started then is that day's night.
            val nights = runs.flatMap { readAll(client, SleepSessionRecord::class, it.start.minusDays(1)..it.endInclusive, now, zone) }
            HealthEvents.sleep(nights.distinctBy { it.metadata.id }.map(::sleepRead))
        },
        Kind("steps", StepsRecord::class) { client, runs, now, zone ->
            HealthEvents.steps(runs.flatMap { readSteps(client, it, now, zone) }, zone, now.toEpochMilli())
        },
        Kind("nutrition", NutritionRecord::class) { client, runs, now, zone ->
            HealthEvents.meals(runs.flatMap { readAll(client, NutritionRecord::class, it, now, zone) }.distinctBy { it.metadata.id }.map(::foodRead))
        },
    )

    /**
     * Reads what changed in Health Connect and stores it. Returns how many events were added or changed. Reads
     * nothing when Health Connect isn't there, and only the kinds it was allowed to read (onboarding asks). In the
     * background this works only with the "read in the background" permission; the next sync with the app open
     * reads it otherwise. A kind that fails (Health Connect busy, a rate limit) is read again next time.
     */
    suspend fun collect(now: Instant = Instant.now(), zone: ZoneId = ZoneId.systemDefault()): Int {
        val client = connect() ?: return 0
        val granted = client.permissionController.getGrantedPermissions()
        var changed = 0
        for (kind in kinds.filter { HealthPermission.getReadPermission(it.type) in granted }) {
            try {
                changed += collect(client, kind, now, zone)
            } catch (cancelled: CancellationException) {
                throw cancelled
            } catch (_: Exception) {
                // its list of changes is kept as it was, so the same days are read next time
            }
        }
        return changed
    }

    private suspend fun <T : Record> collect(client: HealthConnectClient, kind: Kind<T>, now: Instant, zone: ZoneId): Int {
        val saved = prefs.getString(TOKEN + kind.name, null)
        val found = saved?.let { changedDays(client, it, zone) }
        // Without a list of changes (the first read, or one unused for 30 days), a new one starts before reading, so
        // nothing written meanwhile is missed.
        val token = found?.second ?: client.getChangesToken(ChangesTokenRequest(setOf(kind.type), emptySet()))
        val runs = HealthEvents.daysToRead(now.atZone(zone).toLocalDate(), found?.first)
        val added = store.add(kind.read(client, runs, now, zone), zone)
        prefs.edit(commit = true) { putString(TOKEN + kind.name, token) } // only once the events are stored
        return added
    }

    /** The days touched by records written since [token], and the token to use next time; null when it expired. */
    private suspend fun changedDays(client: HealthConnectClient, token: String, zone: ZoneId): Pair<Set<LocalDate>, String>? {
        val days = mutableSetOf<LocalDate>()
        var next = token
        var pages = 0
        do {
            val response = client.getChanges(next)
            if (response.changesTokenExpired) return null
            for (change in response.changes) {
                val (start, end) = timesOf((change as? UpsertionChange)?.record) ?: continue // a deletion has no date
                days += HealthEvents.daysOf(start.toEpochMilli(), end.toEpochMilli(), zone)
            }
            next = response.nextChangesToken
        } while (response.hasMore && ++pages < MAX_PAGES)
        return days to next
    }

    private suspend fun <T : Record> readAll(client: HealthConnectClient, type: KClass<T>, run: ClosedRange<LocalDate>, now: Instant, zone: ZoneId): List<T> {
        val (from, to) = span(run, now, zone)
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
    private suspend fun readSteps(client: HealthConnectClient, run: ClosedRange<LocalDate>, now: Instant, zone: ZoneId): List<DaySteps> {
        val (_, to) = span(run, now, zone)
        val days = client.aggregateGroupByPeriod(
            AggregateGroupByPeriodRequest(
                setOf(StepsRecord.COUNT_TOTAL),
                TimeRangeFilter.between(run.start.atStartOfDay(), to.atZone(zone).toLocalDateTime()),
                Period.ofDays(1),
                emptySet(),
            ),
        )
        return days.mapNotNull { day -> day.result[StepsRecord.COUNT_TOTAL]?.let { DaySteps(day.startTime.toLocalDate(), it) } }
    }

    /** From the first day's midnight to the midnight after the last, or now if that is sooner. */
    private fun span(run: ClosedRange<LocalDate>, now: Instant, zone: ZoneId): Pair<Instant, Instant> =
        run.start.atStartOfDay(zone).toInstant() to minOf(run.endInclusive.plusDays(1).atStartOfDay(zone).toInstant(), now)

    private fun timesOf(record: Record?): Pair<Instant, Instant>? = when (record) {
        is SleepSessionRecord -> record.startTime to record.endTime
        is StepsRecord -> record.startTime to record.endTime
        is NutritionRecord -> record.startTime to record.endTime
        else -> null
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

        /** What Daytrace reads from Health Connect (onboarding asks for exactly these). */
        val READS: Set<String> = setOf(SleepSessionRecord::class, StepsRecord::class, NutritionRecord::class)
            .map { HealthPermission.getReadPermission(it) }.toSet()

        private const val PREFS = "health"
        private const val TOKEN = "changes_token:"
        private const val MAX_RECORDS = 5000 // per kind and run of days: far more than 30 days hold, never unbounded
        private const val MAX_PAGES = 50 // of changes per sync; the rest waits for the next one

        fun available(context: Context): HealthConnectClient? =
            if (HealthConnectClient.getSdkStatus(context, HEALTH_CONNECT_PACKAGE) == HealthConnectClient.SDK_AVAILABLE) {
                HealthConnectClient.getOrCreate(context)
            } else {
                null
            }

        /**
         * Reading while the app is closed needs its own permission (Health Connect has it on Android 14 and newer,
         * and on older phones with a recent Health Connect). Null where the phone doesn't offer it.
         */
        fun backgroundPermission(context: Context): String? = runCatching {
            val features = HealthConnectClient.getOrCreate(context).features
            val status = features.getFeatureStatus(HealthConnectFeatures.FEATURE_READ_HEALTH_DATA_IN_BACKGROUND)
            HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND.takeIf { status == HealthConnectFeatures.FEATURE_STATUS_AVAILABLE }
        }.getOrNull()
    }
}
