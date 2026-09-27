// DT-23: the phone's calendar, from yesterday to tomorrow, into the event store: the plan the timeline shows next
// to what really happened. Only the title and times leave the phone (never the place, notes or guests), and the hub
// hides titles its redaction rules match. Every sync reads the three days again: an event keeps its key, so a
// moved or renamed event replaces its copy.
package app.daytrace.android.calendar

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.provider.CalendarContract.Attendees
import android.provider.CalendarContract.Events
import android.provider.CalendarContract.Instances
import androidx.core.content.ContextCompat
import app.daytrace.android.data.EventStore
import app.daytrace.android.data.PhoneEvent
import app.daytrace.android.health.HealthEvents
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZoneOffset

/** One occurrence of a calendar event, as the calendar provider's Instances table has it. */
data class CalendarRow(
    val eventId: Long,
    val beginMs: Long,
    val endMs: Long,
    val title: String?,
    val allDay: Boolean,
    /** A repeating event: each occurrence is its own event on the hub. */
    val repeats: Boolean,
    /** For one changed occurrence of a repeating event: the event it belongs to and when it was meant to be. */
    val originalId: Long? = null,
    val originalBeginMs: Long? = null,
    val originalAllDay: Boolean = false,
    val cancelled: Boolean = false,
    val declined: Boolean = false,
)

/** How calendar occurrences become hub events. Pure, so every rule has a test. */
object CalendarEvents {
    const val SOURCE = "calendar"

    /**
     * Each occurrence the user still plans to go to, as a calendar_event keyed cal:<event id>, or for a repeating
     * event cal:<event id>:<day>. A changed occurrence keeps the key of the occurrence it replaced, so it replaces
     * that copy instead of standing next to it. An all-day event covers its days from local midnight.
     */
    fun events(rows: List<CalendarRow>, zone: ZoneId): List<PhoneEvent> = rows.filter { !it.cancelled && !it.declined }.mapNotNull { row ->
        val start = if (row.allDay) localMidnight(row.beginMs, zone) else row.beginMs
        val end = if (row.allDay) localMidnight(row.endMs, zone) else row.endMs
        if (end < start) return@mapNotNull null
        PhoneEvent(
            "calendar_event", SOURCE, start, end,
            title = row.title?.trim()?.ifEmpty { null },
            data = HealthEvents.json(mapOf("all_day" to row.allDay)),
            id = key(row, zone),
        )
    }

    fun key(row: CalendarRow, zone: ZoneId): String = when {
        row.originalId != null && row.originalBeginMs != null -> "cal:${row.originalId}:${day(row.originalBeginMs, row.originalAllDay, zone)}"
        row.repeats -> "cal:${row.eventId}:${day(row.beginMs, row.allDay, zone)}"
        else -> "cal:${row.eventId}"
    }

    /** The calendar keeps an all-day event's days as UTC midnights; the day itself is the same everywhere. */
    private fun day(ms: Long, allDay: Boolean, zone: ZoneId): LocalDate =
        Instant.ofEpochMilli(ms).atZone(if (allDay) ZoneOffset.UTC else zone).toLocalDate()

    private fun localMidnight(utcMidnightMs: Long, zone: ZoneId): Long =
        day(utcMidnightMs, allDay = true, zone).atStartOfDay(zone).toInstant().toEpochMilli()

    /** From the start of yesterday to the end of tomorrow, in [zone]. */
    fun window(now: Instant, zone: ZoneId): Pair<Long, Long> {
        val today = now.atZone(zone).toLocalDate()
        return today.minusDays(1).atStartOfDay(zone).toInstant().toEpochMilli() to
            today.plusDays(2).atStartOfDay(zone).toInstant().toEpochMilli()
    }
}

class CalendarCollector(private val context: Context, private val store: EventStore = EventStore.get(context)) {
    /** Returns how many events were added or changed; nothing without the calendar permission. */
    fun collect(now: Instant = Instant.now(), zone: ZoneId = ZoneId.systemDefault()): Int {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.READ_CALENDAR) != PackageManager.PERMISSION_GRANTED) return 0
        val (from, to) = CalendarEvents.window(now, zone)
        return store.add(CalendarEvents.events(read(from, to), zone), zone)
    }

    /** Every occurrence overlapping [from, to) in a calendar shown in the Calendar app. */
    private fun read(from: Long, to: Long): List<CalendarRow> {
        val uri = Instances.CONTENT_URI.buildUpon().also {
            ContentUris.appendId(it, from)
            ContentUris.appendId(it, to)
        }.build()
        val columns = arrayOf(
            Instances.EVENT_ID, Instances.BEGIN, Instances.END, Instances.TITLE, Instances.ALL_DAY, Instances.RRULE, Instances.RDATE,
            Instances.ORIGINAL_ID, Instances.ORIGINAL_INSTANCE_TIME, Instances.ORIGINAL_ALL_DAY, Instances.STATUS,
            Instances.SELF_ATTENDEE_STATUS,
        )
        val rows = mutableListOf<CalendarRow>()
        context.contentResolver.query(uri, columns, "${Instances.VISIBLE} = 1", null, "${Instances.BEGIN} ASC")?.use { cursor ->
            fun text(index: Int) = if (cursor.isNull(index)) null else cursor.getString(index)
            fun long(index: Int) = if (cursor.isNull(index)) null else cursor.getLong(index)
            while (cursor.moveToNext() && rows.size < MAX_ROWS) {
                rows += CalendarRow(
                    eventId = cursor.getLong(0),
                    beginMs = cursor.getLong(1),
                    endMs = long(2) ?: cursor.getLong(1),
                    title = text(3),
                    allDay = cursor.getInt(4) == 1,
                    repeats = !text(5).isNullOrEmpty() || !text(6).isNullOrEmpty(),
                    originalId = text(7)?.toLongOrNull(),
                    originalBeginMs = long(8),
                    originalAllDay = cursor.getInt(9) == 1,
                    cancelled = long(10)?.toInt() == Events.STATUS_CANCELED,
                    declined = long(11)?.toInt() == Attendees.ATTENDEE_STATUS_DECLINED,
                )
            }
        }
        return rows
    }

    private companion object {
        const val MAX_ROWS = 2000 // three days of even the busiest calendar, never unbounded
    }
}
