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
        assertEquals(listOf("cal:9:2026-09-28", "cal:9:2026-09-29"), CalendarEvents.events(listOf(monday, tuesday), zone).map { it.id })
        // Late in the evening here it is already the next day in UTC: the day is the local one.
        assertEquals("cal:9:2026-09-28", one(monday.copy(beginMs = at("2026-09-28T22:30"), endMs = at("2026-09-28T23:00"))).id)
    }

    @Test
    fun aChangedOccurrenceReplacesTheOneItWasMeantToBe() {
        // Monday's stand-up moved to the afternoon: the calendar makes it an event of its own (id 12).
        val moved = row(id = 12, begin = at("2026-09-28T14:00"), end = at("2026-09-28T14:15"))
            .copy(originalId = 9, originalBeginMs = at("2026-09-28T09:00"))
        assertEquals("cal:9:2026-09-28", one(moved).id)
    }

    @Test
    fun anAllDayEventCoversItsDaysFromLocalMidnight() {
        val holiday = CalendarRow(3, utcMidnight("2026-10-12"), utcMidnight("2026-10-13"), "Thanksgiving", allDay = true, repeats = true)
        val event = one(holiday)
        assertEquals(at("2026-10-12T00:00") to at("2026-10-13T00:00"), event.startMs to event.endMs)
        assertTrue(JSONObject(event.data!!).getBoolean("all_day"))
        assertEquals("cal:3:2026-10-12", event.id) // the calendar's own day, not the local day of its UTC midnight
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

    @Test
    fun theWindowIsYesterdayTodayAndTomorrow() {
        val (from, to) = CalendarEvents.window(Instant.ofEpochMilli(at("2026-09-27T01:10")), zone)
        assertEquals(at("2026-09-26T00:00") to at("2026-09-29T00:00"), from to to)
    }
}
