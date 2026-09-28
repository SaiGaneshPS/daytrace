// DT-23: what the health collector asks Health Connect for, sync after sync: 30 days the first time, then the days
// its list of changes names (after a week away, or a backfill of old nights), and what one failing kind leaves alone.
package app.daytrace.android.health

import android.app.Application
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.PermissionController
import androidx.health.connect.client.aggregate.AggregationResult
import androidx.health.connect.client.aggregate.AggregationResultGroupedByDuration
import androidx.health.connect.client.aggregate.AggregationResultGroupedByPeriod
import androidx.health.connect.client.changes.Change
import androidx.health.connect.client.changes.UpsertionChange
import androidx.health.connect.client.records.MealType
import androidx.health.connect.client.records.NutritionRecord
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.metadata.Metadata
import androidx.health.connect.client.request.AggregateGroupByDurationRequest
import androidx.health.connect.client.request.AggregateGroupByPeriodRequest
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.request.ChangesTokenRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.response.ChangesResponse
import androidx.health.connect.client.response.InsertRecordsResponse
import androidx.health.connect.client.response.ReadRecordResponse
import androidx.health.connect.client.response.ReadRecordsResponse
import androidx.health.connect.client.time.TimeRangeFilter
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import app.daytrace.android.data.AppDatabase
import app.daytrace.android.data.EventStore
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.time.Instant
import java.time.LocalDateTime
import java.time.ZoneId
import kotlin.reflect.KClass

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class HealthCollectorTest {
    private val zone = ZoneId.of("America/St_Johns")
    private fun at(text: String): Instant = LocalDateTime.parse(text).atZone(zone).toInstant()
    private val now = at("2026-09-27T09:00")

    private lateinit var db: AppDatabase
    private lateinit var store: EventStore
    private val fake = FakeHealthConnect()

    @Before
    fun open() {
        val context = ApplicationProvider.getApplicationContext<Application>()
        context.getSharedPreferences("health", 0).edit().clear().commit()
        db = Room.inMemoryDatabaseBuilder(context, AppDatabase::class.java).allowMainThreadQueries().build()
        store = EventStore(db)
    }

    @After
    fun close() = db.close()

    private fun collect(at: Instant = now) = runBlocking {
        HealthCollector(ApplicationProvider.getApplicationContext(), store) { fake }.collect(at, zone)
    }

    private fun night(id: String, bed: String, up: String) = SleepSessionRecord(
        startTime = at(bed), startZoneOffset = null, endTime = at(up), endZoneOffset = null, metadata = Metadata.manualEntryWithId(id),
    )

    private fun food(id: String, time: String, name: String) = NutritionRecord(
        startTime = at(time), startZoneOffset = null, endTime = at(time).plusSeconds(60), endZoneOffset = null,
        metadata = Metadata.manualEntryWithId(id), name = name, mealType = MealType.MEAL_TYPE_LUNCH,
    )

    /** Each sleep read as its first day and where it ends (sleep reads start the evening before each run). */
    private fun sleepReads() = fake.reads.filter { it.first == SleepSessionRecord::class }.map { (_, filter) ->
        filter.startTime!!.atZone(zone).toLocalDate().toString() to filter.endTime!!.atZone(zone).toLocalDateTime().toString()
    }

    @Test
    fun theFirstSyncReads30DaysAndKeepsAListOfChangesForNextTime() {
        fake.sleep += night("n1", "2026-09-26T23:30", "2026-09-27T07:00")
        assertEquals(1, collect())
        assertEquals(listOf("2026-08-27" to "2026-09-27T09:00"), sleepReads()) // from the day before Aug 28, to now
        assertEquals(listOf("sleep", "steps", "nutrition"), fake.tokensGiven)
        assertEquals("hc:n1", store.pending(10).single().key)
    }

    @Test
    fun afterAWeekAwayTheChangesNameTheDaysToRead() {
        collect(at("2026-09-19T21:00")) // the last sync before a week off Wi-Fi
        fake.reads.clear()
        val away = night("n2", "2026-09-21T23:00", "2026-09-22T06:30")
        fake.sleep += away
        fake.changes["token-sleep"] = listOf(UpsertionChange(away))
        collect()
        assertEquals(listOf("2026-09-20" to "2026-09-23T00:00", "2026-09-25" to "2026-09-27T09:00"), sleepReads())
        assertEquals(listOf("hc:n2"), store.pending(10).map { it.key })
        collect()
        assertEquals(listOf("token-sleep", "token-sleep-next"), fake.asked("sleep")) // each sync goes on from the last
    }

    @Test
    fun anExpiredListOfChangesMeansReadingThe30DaysAgain() {
        collect(at("2026-08-20T09:00"))
        fake.reads.clear()
        fake.expired += "token-sleep"
        collect()
        assertEquals("2026-08-27", sleepReads().single().first)
    }

    @Test
    fun aKindThatFailsIsReadAgainNextTimeAndTheOthersStillArrive() {
        collect(at("2026-09-26T09:00"))
        fake.failing += NutritionRecord::class
        fake.sleep += night("n3", "2026-09-26T23:30", "2026-09-27T07:00")
        fake.food += food("f1", "2026-09-27T08:00", "Roti")
        collect()
        assertEquals(listOf("hc:n3"), store.pending(10).map { it.key })
        fake.failing.clear()
        collect()
        assertEquals(listOf("token-nutrition", "token-nutrition"), fake.asked("nutrition")) // not moved on after the failure
        assertEquals(1, store.pending(10).count { it.kind == "meal" })
    }

    @Test
    fun onlyTheKindsItMayReadAreRead() {
        fake.granted = setOf("android.permission.health.READ_SLEEP")
        collect()
        assertEquals(listOf("sleep"), fake.tokensGiven)
        assertEquals(setOf(SleepSessionRecord::class), fake.reads.map { it.first }.toSet())
    }
}

/** Health Connect as far as the collector uses it: records, reads, and one list of changes per kind. */
private class FakeHealthConnect : HealthConnectClient {
    var granted: Set<String> = HealthCollector.READS
    val sleep = mutableListOf<SleepSessionRecord>()
    val food = mutableListOf<NutritionRecord>()
    val reads = mutableListOf<Pair<KClass<*>, TimeRangeFilter>>()
    val tokensGiven = mutableListOf<String>()
    val changes = mutableMapOf<String, List<Change>>()
    val expired = mutableSetOf<String>()
    val failing = mutableSetOf<KClass<*>>()
    private val askedWith = mutableListOf<String>()

    /** The tokens each sync asked for changes with, for one kind. */
    fun asked(kind: String): List<String> = askedWith.filter { it.removePrefix("token-").removeSuffix("-next") == kind }

    override val permissionController = object : PermissionController {
        override suspend fun getGrantedPermissions(): Set<String> = granted
        override suspend fun revokeAllPermissions() = Unit
    }

    private fun kindOf(type: KClass<*>) = when (type) {
        SleepSessionRecord::class -> "sleep"
        NutritionRecord::class -> "nutrition"
        else -> "steps"
    }

    override suspend fun getChangesToken(request: ChangesTokenRequest): String {
        val kind = kindOf(request.recordTypes.single())
        tokensGiven += kind
        return "token-$kind"
    }

    override suspend fun getChanges(changesToken: String): ChangesResponse {
        askedWith += changesToken
        if (changesToken in expired) return ChangesResponse(emptyList(), changesToken, false, true)
        val kind = changesToken.removePrefix("token-").removeSuffix("-next")
        return ChangesResponse(changes[changesToken].orEmpty(), "token-$kind-next", false, false)
    }

    @Suppress("UNCHECKED_CAST")
    override suspend fun <T : Record> readRecords(request: ReadRecordsRequest<T>): ReadRecordsResponse<T> {
        if (request.recordType in failing) throw IllegalStateException("rate limited")
        val filter = request.timeRangeFilter
        reads += request.recordType to filter
        val all: List<Record> = if (request.recordType == SleepSessionRecord::class) sleep else food
        val inRange = all.filter { record ->
            val (start, end) = when (record) {
                is SleepSessionRecord -> record.startTime to record.endTime
                is NutritionRecord -> record.startTime to record.endTime
                else -> error("not read here")
            }
            start < filter.endTime && end > filter.startTime
        }
        return ReadRecordsResponse(inRange as List<T>, null)
    }

    override suspend fun aggregateGroupByPeriod(request: AggregateGroupByPeriodRequest): List<AggregationResultGroupedByPeriod> = emptyList()

    override suspend fun insertRecords(records: List<Record>): InsertRecordsResponse = error("read only")
    override suspend fun updateRecords(records: List<Record>) = error("read only")
    override suspend fun deleteRecords(recordType: KClass<out Record>, recordIdsList: List<String>, clientRecordIdsList: List<String>) = error("read only")
    override suspend fun deleteRecords(recordType: KClass<out Record>, timeRangeFilter: TimeRangeFilter) = error("read only")
    override suspend fun <T : Record> readRecord(recordType: KClass<T>, recordId: String): ReadRecordResponse<T> = error("not used")
    override suspend fun aggregate(request: AggregateRequest): AggregationResult = error("not used")
    override suspend fun aggregateGroupByDuration(request: AggregateGroupByDurationRequest): List<AggregationResultGroupedByDuration> = error("not used")
}
