// DT-23: how Health Connect's sleep, steps and meals become hub events, keyed so a record read again is the same
// event.
package app.daytrace.android.health

import androidx.health.connect.client.records.MealType
import androidx.health.connect.client.records.SleepSessionRecord
import app.daytrace.android.data.PhoneEvent
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Duration
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId

class HealthEventsTest {
    private val zone = ZoneId.of("America/St_Johns") // half an hour off UTC: whole hours in UTC are not whole here
    private fun at(text: String) = LocalDateTime.parse(text).atZone(zone).toInstant().toEpochMilli()
    private fun data(event: PhoneEvent) = JSONObject(event.data!!)
    private fun minutes(n: Long) = n * 60_000

    @Test
    fun sleepStagesGetTheHubsNames() {
        val names = mapOf(
            SleepSessionRecord.STAGE_TYPE_UNKNOWN to "asleep",
            SleepSessionRecord.STAGE_TYPE_AWAKE to "awake",
            SleepSessionRecord.STAGE_TYPE_SLEEPING to "asleep",
            SleepSessionRecord.STAGE_TYPE_OUT_OF_BED to "awake",
            SleepSessionRecord.STAGE_TYPE_LIGHT to "core",
            SleepSessionRecord.STAGE_TYPE_DEEP to "deep",
            SleepSessionRecord.STAGE_TYPE_REM to "rem",
            SleepSessionRecord.STAGE_TYPE_AWAKE_IN_BED to "awake",
        )
        names.forEach { (type, name) -> assertEquals("stage $type", name, HealthEvents.stageName(type)) }
        assertEquals("asleep", HealthEvents.stageName(99)) // a stage added after this app: still sleep
    }

    @Test
    fun aNightWithStagesIsOneMeasuredEventPerStageInTimeOrder() {
        val bed = at("2026-09-26T23:40")
        val night = SleepRead(
            "abc", bed, bed + minutes(420),
            listOf(
                StageRead(bed + minutes(30), bed + minutes(120), SleepSessionRecord.STAGE_TYPE_DEEP),
                StageRead(bed, bed + minutes(30), SleepSessionRecord.STAGE_TYPE_LIGHT),
                StageRead(bed + minutes(120), bed + minutes(120), SleepSessionRecord.STAGE_TYPE_REM), // no length: left out
                StageRead(bed + minutes(120), bed + minutes(135), SleepSessionRecord.STAGE_TYPE_AWAKE),
            ),
        )
        val events = HealthEvents.sleep(listOf(night))
        assertEquals(listOf("hc:abc:0", "hc:abc:1", "hc:abc:2"), events.map { it.id })
        assertEquals(listOf("core", "deep", "awake"), events.map { data(it).getString("stage") })
        assertTrue(events.all { it.kind == "sleep" && it.source == "health_connect" && data(it).getBoolean("measured") })
        assertEquals(listOf(bed, bed + minutes(30), bed + minutes(120)), events.map { it.startMs })
        assertEquals(bed + minutes(135), events.last().endMs)
    }

    @Test
    fun aNightWithoutStagesIsOneSpanAsleep() {
        val bed = at("2026-09-26T23:40")
        val event = HealthEvents.sleep(listOf(SleepRead("xyz", bed, bed + minutes(400)))).single()
        assertEquals("hc:xyz", event.id)
        assertEquals(bed to bed + minutes(400), event.startMs to event.endMs)
        assertEquals("asleep", data(event).getString("stage"))
        assertTrue(HealthEvents.sleep(listOf(SleepRead("empty", bed, bed))).isEmpty()) // no length at all
    }

    @Test
    fun readingTheSameRecordsAgainGivesTheSameEvents() {
        val bed = at("2026-09-26T23:40")
        val night = SleepRead("abc", bed, bed + minutes(60), listOf(StageRead(bed, bed + minutes(60), SleepSessionRecord.STAGE_TYPE_DEEP)))
        assertEquals(HealthEvents.sleep(listOf(night)), HealthEvents.sleep(listOf(night.copy())))
        val food = FoodRead(bed, "Tea", MealType.MEAL_TYPE_SNACK)
        assertEquals(HealthEvents.meals(listOf(food)), HealthEvents.meals(listOf(food.copy())))
    }

    @Test
    fun aDayThatIsOverCountsItsStepsFromMidnightToMidnight() {
        val day = LocalDate.parse("2026-09-25")
        val event = HealthEvents.steps(listOf(DaySteps(day, 8123)), zone, at("2026-09-27T10:05")).single()
        assertEquals("steps:2026-09-25", event.id)
        assertEquals(at("2026-09-25T00:00") to at("2026-09-26T00:00"), event.startMs to event.endMs)
        assertEquals(8123, data(event).getInt("count"))
        assertEquals("steps", event.kind)
    }

    @Test
    fun todaysStepsRunToTheStartOfThisHourWhereYouAre() {
        val today = LocalDate.parse("2026-09-27")
        val event = HealthEvents.steps(listOf(DaySteps(today, 4200)), zone, at("2026-09-27T14:37")).single()
        assertEquals(at("2026-09-27T00:00") to at("2026-09-27T14:00"), event.startMs to event.endMs)
        // The same count later in the hour is the same event: nothing to send again.
        assertEquals(event, HealthEvents.steps(listOf(DaySteps(today, 4200)), zone, at("2026-09-27T14:59")).single())
        val justAfterMidnight = HealthEvents.steps(listOf(DaySteps(today, 12)), zone, at("2026-09-27T00:20")).single()
        assertEquals(justAfterMidnight.startMs, justAfterMidnight.endMs) // a moment, never an end before its start
    }

    @Test
    fun theDayTheClocksGoBackHas25Hours() {
        val event = HealthEvents.steps(listOf(DaySteps(LocalDate.parse("2026-11-01"), 100)), zone, at("2026-11-03T09:00")).single()
        assertEquals(Duration.ofHours(25).toMillis(), event.endMs!! - event.startMs)
    }

    @Test
    fun foodsLoggedTogetherAreOneMeal() {
        val lunch = at("2026-09-27T12:30")
        val events = HealthEvents.meals(
            listOf(
                FoodRead(lunch, "Roti", MealType.MEAL_TYPE_LUNCH),
                FoodRead(at("2026-09-27T08:00"), "Oats", MealType.MEAL_TYPE_BREAKFAST),
                FoodRead(lunch, "Dal", MealType.MEAL_TYPE_LUNCH),
                FoodRead(lunch, "Roti", MealType.MEAL_TYPE_LUNCH), // two rotis logged as two records: one item
            ),
        )
        assertEquals(listOf("meal:${at("2026-09-27T08:00")}:1", "meal:$lunch:2"), events.map { it.id })
        val meal = events.last()
        assertEquals("meal", meal.kind)
        assertEquals(lunch, meal.startMs)
        assertNull(meal.endMs) // a meal is a moment for the hub
        assertEquals(listOf("Roti", "Dal"), data(meal).getJSONArray("items").let { items -> List(items.length()) { items.getString(it) } })
        assertEquals("lunch", data(meal).getString("meal_type"))
    }

    @Test
    fun aMealWithoutATypeOrNamesStillSaysSomething() {
        val meal = HealthEvents.meals(listOf(FoodRead(at("2026-09-27T16:00"), null, MealType.MEAL_TYPE_UNKNOWN), FoodRead(at("2026-09-27T16:00"), " ", 0))).single()
        assertEquals("[\"Food\"]", data(meal).getJSONArray("items").toString())
        assertFalse(data(meal).has("meal_type")) // the hub guesses it from the time
    }

    @Test
    fun foodNamesAreCleanedToWhatTheHubAccepts() {
        val rlo = String(Character.toChars(0x202E))
        assertEquals("Chana masala", HealthEvents.itemName("  Chana\n\tmasala$rlo "))
        assertNull(HealthEvents.itemName("\n"))
        val emoji = String(Character.toChars(0x1F35B)).repeat(120)
        val cut = HealthEvents.itemName(emoji)!!
        assertEquals(100, cut.codePointCount(0, cut.length)) // 100 characters, none cut in half
        val many = (1..40).map { FoodRead(0, "Item $it", MealType.MEAL_TYPE_DINNER) }
        assertEquals(30, data(HealthEvents.meals(many).single()).getJSONArray("items").length())
    }

    // --- which days to read ---

    private val today = LocalDate.parse("2026-09-27")
    private fun day(text: String) = LocalDate.parse(text)

    @Test
    fun theFirstReadCoversTheLast30Days() {
        assertEquals(listOf(day("2026-08-28")..today), HealthEvents.daysToRead(today, changed = null))
    }

    @Test
    fun laterReadsCoverTodayYesterdayAndEveryChangedDay() {
        assertEquals(listOf(day("2026-09-26")..today), HealthEvents.daysToRead(today, changed = emptySet()))
        // A week off Wi-Fi: the changes list the nights in between, whatever the gap was.
        val away = (20..24).map { day("2026-09-$it") }.toSet()
        assertEquals(listOf(day("2026-09-20")..day("2026-09-24"), day("2026-09-26")..today), HealthEvents.daysToRead(today, away))
    }

    @Test
    fun oldNightsBackfilledLaterAreReadWithinThe30Days() {
        // Samsung Health started sharing after the first read, and wrote three weeks of history with its own dates.
        val backfill = setOf(day("2026-09-05"), day("2026-09-06"), day("2026-06-01"))
        assertEquals(
            listOf(day("2026-09-05")..day("2026-09-06"), day("2026-09-26")..today),
            HealthEvents.daysToRead(today, backfill), // June is further back than Health Connect lets an app read
        )
    }

    @Test
    fun aRecordFallsOnEveryDayItTouches() {
        val night = HealthEvents.daysOf(at("2026-09-26T23:40"), at("2026-09-27T07:10"), zone)
        assertEquals(setOf(day("2026-09-26"), day("2026-09-27")), night)
        assertEquals(setOf(day("2026-09-27")), HealthEvents.daysOf(at("2026-09-27T12:30"), at("2026-09-27T12:30"), zone))
        assertEquals(30, HealthEvents.daysOf(0, at("2026-09-27T00:00"), zone).size) // never unbounded
    }
}
