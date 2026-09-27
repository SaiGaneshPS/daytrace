// DT-23: the phone's calendar, from yesterday to tomorrow, into the event store: the plan the timeline shows next
// to what really happened. Only the title and times leave the phone (never the place, notes or guests), and the hub
// hides titles its redaction rules match. Every sync reads those days again, and the days since the last read
// (after a week off Wi-Fi, say): an event keeps its key, so a moved or renamed event replaces its copy.
package app.daytrace.android.calendar

import android.content.ContentUris
import android.content.Context
import android.provider.CalendarContract.Attendees
import android.provider.CalendarContract.Events
import android.provider.CalendarContract.Instances
import androidx.core.content.edit
import app.daytrace.android.data.EventStore
import app.daytrace.android.data.PhoneEvent
import app.daytrace.android.ui.Permissions
import java.time.Instant
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
    /** For one changed occurrence of a repeating event: the event it belongs to and when it was meant to start. */
    val originalId: Long? = null,
    val originalBeginMs: Long? = null,
    val cancelled: Boolean = false,
    val declined: Boolean = false,
)

/** How calendar occurrences become hub events. Pure, so every rule has a test. */
object CalendarEvents {
    const val SOURCE = "calendar"

    /**
     * Each occurrence the user still plans to go to, as a calendar_event keyed cal:<event id>, or for a repeating
     * event cal:<event id>:<start in ms> (a time, so two on one day stay two, whatever the phone's zone). A changed
     * occurrence keeps the key of the one it replaced (the calendar keeps when that was meant to start), so it
     * replaces that copy instead of standing next to it. An all-day event covers its days from local midnight.
     */
    fun events(rows: List<CalendarRow>, zone: ZoneId): List<PhoneEvent> = rows.filter { !it.cancelled && !it.declined }.mapNotNull { row ->
        val start = if (row.allDay) localMidnight(row.beginMs, zone) else row.beginMs
        val end = if (row.allDay) localMidnight(row.endMs, zone) else row.endMs
        if (end < start) return@mapNotNull null
        PhoneEvent(
            "calendar_event", SOURCE, start, end,
            title = row.title?.trim()?.ifEmpty { null },
            data = PhoneEvent.dataJson(mapOf("all_day" to row.allDay)),
            id = key(row),
        )
    }

    fun key(row: CalendarRow): String = when {
        row.originalId != null && row.originalBeginMs != null -> "cal:${row.originalId}:${row.originalBeginMs}"
        row.repeats -> "cal:${row.eventId}:${row.beginMs}"
        else -> "cal:${row.eventId}"
    }

    /** The calendar keeps an all-day event's days as UTC midnights; the day itself is the same everywhere. */
    private fun localMidnight(utcMidnightMs: Long, zone: ZoneId): Long =
        Instant.ofEpochMilli(utcMidnightMs).atZone(ZoneOffset.UTC).toLocalDate().atStartOfDay(zone).toInstant().toEpochMilli()

    /** At most this many days back, after a long time without a sync. */
    const val MAX_DAYS_BACK = 30L

    /**
     * From the start of yesterday to the end of tomorrow, in [zone], starting earlier when the last read
     * ([lastReadMs]) was before yesterday: from the day before it, at most [MAX_DAYS_BACK] days back.
     */
    fun window(now: Instant, zone: ZoneId, lastReadMs: Long? = null): Pair<Long, Long> {
        val today = now.atZone(zone).toLocalDate()
        val sinceLastRead = lastReadMs?.let { Instant.ofEpochMilli(it).atZone(zone).toLocalDate().minusDays(1) } ?: today
        val first = maxOf(minOf(today.minusDays(1), sinceLastRead), today.minusDays(MAX_DAYS_BACK))
        return first.atStartOfDay(zone).toInstant().toEpochMilli() to today.plusDays(2).atStartOfDay(zone).toInstant().toEpochMilli()
    }
}

class CalendarCollector(private val context: Context, private val store: EventStore = EventStore.get(context)) {
    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /** Returns how many events were added or changed; nothing without the calendar permission. */
    fun collect(now: Instant = Instant.now(), zone: ZoneId = ZoneId.systemDefault()): Int {
        if (!Permissions.calendarGranted(context)) return 0
        val (from, to) = CalendarEvents.window(now, zone, prefs.getLong(LAST_READ, -1).takeIf { it >= 0 })
        val added = store.add(CalendarEvents.events(read(from, to), zone), zone)
        prefs.edit(commit = true) { putLong(LAST_READ, now.toEpochMilli()) } // only once the events are stored
        return added
    }

    /** Every occurrence overlapping [from, to) in a calendar shown in the Calendar app. */
    private fun read(from: Long, to: Long): List<CalendarRow> {
        val uri = Instances.CONTENT_URI.buildUpon().also {
            ContentUris.appendId(it, from)
            ContentUris.appendId(it, to)
        }.build()
        val columns = arrayOf(
            Instances.EVENT_ID, Instances.BEGIN, Instances.END, Instances.TITLE, Instances.ALL_DAY, Instances.RRULE, Instances.RDATE,
            Instances.ORIGINAL_ID, Instances.ORIGINAL_INSTANCE_TIME, Instances.STATUS, Instances.SELF_ATTENDEE_STATUS,
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
                    cancelled = long(9)?.toInt() == Events.STATUS_CANCELED,
                    declined = long(10)?.toInt() == Attendees.ATTENDEE_STATUS_DECLINED,
                )
            }
        }
        return rows
    }

    private companion object {
        const val PREFS = "calendar"
        const val LAST_READ = "last_read_ms"
        const val MAX_ROWS = 5000 // a month of even a busy calendar, never unbounded
    }
}
