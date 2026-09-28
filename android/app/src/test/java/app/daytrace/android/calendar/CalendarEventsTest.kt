// DT-23: how calendar occurrences become hub events: what is left out, all-day days, and keys that stay put when
// an event moves.
package app.daytrace.android.calendar

import app.daytrace.android.data.PhoneEvent
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.Instant
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.ZoneOffset

class CalendarEventsTest {
    private val zone = ZoneId.of("America/St_Johns")
    private fun at(text: String) = LocalDateTime.parse(text).atZone(zone).toInstant().toEpochMilli()
    private fun utcMidnight(day: String) = LocalDateTime.parse("${day}T00:00").toInstant(ZoneOffset.UTC).toEpochMilli()
    private fun row(id: Long = 5, begin: Long = at("2026-09-27T10:00"), end: Long = at("2026-09-27T11:30"), title: String? = "Study: statistics") =
        CalendarRow(eventId = id, beginMs = begin, endMs = end, title = title, allDay = false, repeats = false)
    private fun one(row: CalendarRow): PhoneEvent = CalendarEvents.events(listOf(row), zone).single()

    @Test
    fun anEventIsItsTitleAndTimes() {
        val event = one(row())
        assertEquals("calendar_event", event.kind)
        assertEquals("calendar", event.source)
        assertEquals("cal:5", event.id)
        assertEquals("Study: statistics", event.title)
        assertEquals(at("2026-09-27T10:00") to at("2026-09-27T11:30"), event.startMs to event.endMs)
        assertFalse(JSONObject(event.data!!).getBoolean("all_day"))
    }

    @Test
    fun anEventMovedToAnotherTimeKeepsItsKey() {
        assertEquals(one(row()).id, one(row(begin = at("2026-09-27T15:00"), end = at("2026-09-27T16:00"))).id)
    }

    @Test
    fun eachOccurrenceOfARepeatingEventIsItsOwnEvent() {
        val monday = row(id = 9, begin = at("2026-09-28T09:00"), end = at("2026-09-28T09:15")).copy(repeats = true)
        val tuesday = monday.copy(beginMs = at("2026-09-29T09:00"), endMs = at("2026-09-29T09:15"))
        val evening = monday.copy(beginMs = at("2026-09-28T20:00"), endMs = at("2026-09-28T20:15")) // twice a day
        val keys = CalendarEvents.events(listOf(monday, evening, tuesday), zone).map { it.id }
        assertEquals(listOf("cal:9:${monday.beginMs}", "cal:9:${evening.beginMs}", "cal:9:${tuesday.beginMs}"), keys)
    }

    @Test
    fun anOccurrenceKeepsItsKeyInAnotherTimeZone() {
        val late = row(id = 9, begin = at("2026-09-28T23:30"), end = at("2026-09-28T23:45")).copy(repeats = true)
        assertEquals(one(late).id, CalendarEvents.events(listOf(late), ZoneId.of("Asia/Tokyo")).single().id) // after a flight east
    }

    @Test
    fun aChangedOccurrenceReplacesTheOneItWasMeantToBe() {
        // Monday's stand-up moved to the afternoon: the calendar makes it an event of its own (id 12).
        val monday = row(id = 9, begin = at("2026-09-28T09:00"), end = at("2026-09-28T09:15")).copy(repeats = true)
        val moved = row(id = 12, begin = at("2026-09-28T14:00"), end = at("2026-09-28T14:15"))
            .copy(originalId = 9, originalBeginMs = at("2026-09-28T09:00"))
        assertEquals(one(monday).id, one(moved).id)
    }

    @Test
    fun anAllDayEventCoversItsDaysFromLocalMidnight() {
        val holiday = CalendarRow(3, utcMidnight("2026-10-12"), utcMidnight("2026-10-13"), "Thanksgiving", allDay = true, repeats = true)
        val event = one(holiday)
        assertEquals(at("2026-10-12T00:00") to at("2026-10-13T00:00"), event.startMs to event.endMs)
        assertTrue(JSONObject(event.data!!).getBoolean("all_day"))
        assertEquals("cal:3:${utcMidnight("2026-10-12")}", event.id)
    }

    @Test
    fun cancelledAndDeclinedEventsAreLeftOut() {
        val events = CalendarEvents.events(listOf(row(id = 1).copy(cancelled = true), row(id = 2).copy(declined = true), row(id = 3)), zone)
        assertEquals(listOf("cal:3"), events.map { it.id })
    }

    @Test
    fun anUntitledEventHasNoTitleAndABrokenOneIsSkipped() {
        assertNull(one(row(title = "  ")).title)
        assertTrue(CalendarEvents.events(listOf(row(begin = at("2026-09-27T11:00"), end = at("2026-09-27T10:00"))), zone).isEmpty())
    }

    private val now = Instant.ofEpochMilli(at("2026-09-27T01:10"))

    @Test
    fun theWindowIsYesterdayTodayAndTomorrow() {
        assertEquals(at("2026-09-26T00:00") to at("2026-09-29T00:00"), CalendarEvents.window(now, zone))
        // Read 15 minutes ago: the same three days.
        assertEquals(at("2026-09-26T00:00") to at("2026-09-29T00:00"), CalendarEvents.window(now, zone, at("2026-09-27T00:55")))
    }

    @Test
    fun afterDaysWithoutASyncTheWindowStartsTheDayBeforeTheLastRead() {
        val lastRead = at("2026-09-21T18:00") // a week off Wi-Fi
        assertEquals(at("2026-09-20T00:00") to at("2026-09-29T00:00"), CalendarEvents.window(now, zone, lastRead))
        val longAgo = at("2026-06-01T12:00")
        assertEquals(at("2026-08-28T00:00"), CalendarEvents.window(now, zone, longAgo).first) // at most 30 days back
    }
}
