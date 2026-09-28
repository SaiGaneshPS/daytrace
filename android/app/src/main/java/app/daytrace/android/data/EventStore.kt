// DT-20 / DT-21: the event store every collector writes to. Events wait in the phone's database (Room), numbered
// for the hub, until the hub has them. Events collected before DT-21 (a JSON-lines file) are moved in on first use.
package app.daytrace.android.data

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter

/** One thing that happened on this phone, before it gets a device id and a seq for the hub (docs/api.md). */
data class PhoneEvent(
    val kind: String,
    val source: String,
    val startMs: Long,
    val endMs: Long? = null,
    val app: String? = null,
    val appId: String? = null,
    /** DT-23: a calendar event's title. */
    val title: String? = null,
    /** DT-23: the event's data as compact JSON, or null. */
    val data: String? = null,
    /**
     * DT-23: the record's own id (a Health Connect record, a day's step total, a calendar event), when it has one.
     * It is the key: read again, the same record is the same event, and a changed record replaces its copy
     * (on the phone and, as the same external_id, on the hub).
     */
    val id: String? = null,
) {
    /** The same moment collected twice gives the same key, so a repeated collection never adds time twice. */
    val key: String get() = id ?: "$kind|$startMs|${appId ?: app.orEmpty()}"

    /** Whether [other] (the stored copy of the same record) says something else: a record that changed. */
    fun differsFrom(other: EventEntity): Boolean =
        startMs != other.startMs || endMs != other.endMs || title != other.title || data != other.data ||
            app != other.app || appId != other.appId || kind != other.kind

    /** The JSON-lines format of the store before DT-21 (read once to move old events into the database). */
    fun toStoredJson(): JSONObject = JSONObject()
        .put("kind", kind)
        .put("source", source)
        .put("start_ms", startMs)
        .putOpt("end_ms", endMs)
        .putOpt("app", app)
        .putOpt("app_id", appId)

    /** ISO 8601 with the offset in force at that moment, the way the hub wants its times. */
    fun isoStart(zone: ZoneId = ZoneId.systemDefault()): String = iso(startMs, zone)

    fun isoEnd(zone: ZoneId = ZoneId.systemDefault()): String? = endMs?.let { iso(it, zone) }

    companion object {
        /**
         * DT-23: an event's data as JSON with its keys in one order, so the same values always make the same text
         * (the store compares the text to tell whether a record changed).
         */
        fun dataJson(values: Map<String, Any>): String = JSONObject(values.toSortedMap()).toString()

        /** Whether [data] is a JSON object, as the hub needs it. */
        fun isDataJson(data: String): Boolean = runCatching { JSONObject(data) }.isSuccess

        fun iso(epochMs: Long, zone: ZoneId): String =
            OffsetDateTime.ofInstant(Instant.ofEpochMilli(epochMs), zone).format(DateTimeFormatter.ISO_OFFSET_DATE_TIME)

        fun fromStoredJson(json: JSONObject): PhoneEvent = PhoneEvent(
            kind = json.getString("kind"),
            source = json.getString("source"),
            startMs = json.getLong("start_ms"),
            endMs = if (json.has("end_ms")) json.getLong("end_ms") else null,
            app = json.optString("app").ifEmpty { null },
            appId = json.optString("app_id").ifEmpty { null },
        )
    }
}

/** How many events are still on their way to the hub, and how many it refused. */
data class StoreCounts(val waiting: Int, val refused: Int)

class EventStore(private val db: AppDatabase, private val legacyFile: File? = null) {
    private val dao = db.events()
    @Volatile private var legacyMoved = legacyFile == null

    /**
     * Stores newly collected events, each with the next seq, in [zone] (the zone now). The same moment collected
     * again (a collection repeated after a crash) is kept once: if the new copy ends later, the stored one is
     * extended and queued again under a new seq, so the hub replaces its copy; otherwise nothing changes. Returns
     * how many events were added or extended. Commits are on disk before this returns (see AppDatabase).
     */
    fun add(events: List<PhoneEvent>, zone: ZoneId = ZoneId.systemDefault()): Int {
        // Only this app's own code writes data: data that isn't JSON is a bug, caught here rather than as an event
        // the hub refuses for a missing field.
        events.forEach { event -> require(event.data == null || PhoneEvent.isDataJson(event.data)) { "${event.kind} data is not a JSON object" } }
        moveLegacyEvents()
        if (events.isEmpty()) return 0 // most collections find nothing new: no write transaction for that
        return transaction { addNow(events, zone.id) }
    }

    /** App sessions that end after [afterMs], oldest first (today's summary on the status screen). */
    fun sessionsEndingAfter(afterMs: Long): List<PhoneEvent> {
        moveLegacyEvents()
        return dao.sessionsEndingAfter(afterMs).map { it.toPhoneEvent() }
    }

    /** The next events to send, lowest seq first. */
    fun pending(limit: Int): List<EventEntity> {
        moveLegacyEvents()
        return dao.pending(limit)
    }

    fun counts(): StoreCounts {
        moveLegacyEvents()
        return StoreCounts(waiting = dao.count(SyncState.PENDING), refused = dao.count(SyncState.REJECTED))
    }

    /** The hub stored these. An event that changed since it was read (it has a new seq) stays queued. */
    fun markSynced(events: List<EventEntity>): Int = transaction { events.sumOf { dao.markSynced(it.id, it.seq) } }

    fun markRejected(event: EventEntity, reason: String): Int = dao.markRejected(event.id, event.seq, reason.take(500))

    /** Whether the hub's cursor for [deviceId] was read into this database (see [raiseSeqFloor]). */
    fun hubCursorRead(deviceId: String): Boolean = dao.meta(CURSOR_READ + deviceId) != null

    /**
     * DT-22: a pairing (a new one, or the same id again) reads the hub's cursor afresh before sending, since the hub
     * may hold higher seqs for that id than this phone knows of. Raising the floor never lowers anything, so reading
     * it again is always safe.
     */
    fun forgetHubCursor(deviceId: String) {
        dao.deleteMeta(CURSOR_READ + deviceId)
    }

    /**
     * The hub already holds seqs up to [hubLastSeq] (null: none) for [deviceId]; a reinstall starts counting from
     * 0 again. From now on every seq is higher, and queued events at or below it get new numbers above it, so the
     * hub treats them as the newest copies. Recorded in the same transaction, so a database that is recreated
     * reads the cursor again.
     */
    fun raiseSeqFloor(hubLastSeq: Long?, deviceId: String) = transaction {
        if (hubLastSeq != null) {
            if ((dao.meta(SEQ_FLOOR) ?: 0) <= hubLastSeq) dao.setMeta(MetaEntry(SEQ_FLOOR, hubLastSeq + 1))
            var next = nextSeq()
            dao.pendingUpTo(hubLastSeq).forEach { if (dao.renumber(it.id, it.seq, next) > 0) next++ }
        }
        dao.setMeta(MetaEntry(CURSOR_READ + deviceId, 1))
    }

    private fun addNow(events: List<PhoneEvent>, zone: String): Int {
        var next = nextSeq()
        var changed = 0
        for (event in events) {
            val stored = dao.byKey(event.key)
            if (stored == null) {
                dao.insert(
                    EventEntity(
                        seq = next++, key = event.key, kind = event.kind, source = event.source, startMs = event.startMs,
                        endMs = event.endMs, app = event.app, appId = event.appId, zone = zone, title = event.title,
                        data = event.data, record = event.id != null,
                    ),
                )
                changed++
            } else if (event.id != null) {
                // DT-23: a record read again (a step total that grew, an edited sleep or calendar event) replaces
                // its copy whatever changed, and goes to the hub again under a new seq; the same record adds nothing.
                if (event.differsFrom(stored)) {
                    dao.update(
                        stored.copy(
                            seq = next++, kind = event.kind, source = event.source, startMs = event.startMs, endMs = event.endMs,
                            app = event.app, appId = event.appId, title = event.title, data = event.data,
                            state = SyncState.PENDING, rejectReason = null,
                        ),
                    )
                    changed++
                }
            } else if ((event.endMs ?: Long.MIN_VALUE) > (stored.endMs ?: Long.MIN_VALUE)) {
                // The zone stays the one from the first collection, the closest to when the event happened.
                dao.update(
                    stored.copy(seq = next++, endMs = event.endMs, app = event.app ?: stored.app, state = SyncState.PENDING, rejectReason = null),
                )
                changed++
            }
        }
        return changed
    }

    private fun nextSeq(): Long = maxOf((dao.maxSeq() ?: -1) + 1, dao.meta(SEQ_FLOOR) ?: 0)

    private fun <T> transaction(body: () -> T): T = db.runInTransaction<T> { body() }

    /**
     * Moves the events stored before DT-21 into the database, once. The file is deleted only after the events
     * are committed; if the app stops in between, the next start moves them again and their keys keep them from
     * being stored twice.
     */
    private fun moveLegacyEvents() {
        if (legacyMoved) return
        synchronized(this) {
            if (legacyMoved) return
            val file = legacyFile!!
            if (file.exists()) {
                val events = readLegacy(file)
                transaction { addNow(events, ZoneId.systemDefault().id) }
                file.delete()
            }
            legacyMoved = true
        }
    }

    companion object {
        private const val SEQ_FLOOR = "seq_floor"
        private const val CURSOR_READ = "cursor_read:"
        const val LEGACY_FILE = "events-pending.jsonl"

        @Volatile private var instance: EventStore? = null

        fun get(context: Context): EventStore = instance ?: synchronized(this) {
            instance ?: EventStore(AppDatabase.get(context), File(context.filesDir, LEGACY_FILE)).also { instance = it }
        }

        /**
         * Every event in the old file once, in time order. A line cut off by a crash is skipped, and of two copies
         * of the same event (a collection repeated after a crash) the one that ends last is kept.
         */
        fun readLegacy(file: File): List<PhoneEvent> {
            val byKey = LinkedHashMap<String, PhoneEvent>()
            file.forEachLine { line ->
                val event = runCatching { PhoneEvent.fromStoredJson(JSONObject(line)) }.getOrNull() ?: return@forEachLine
                val kept = byKey[event.key]
                if (kept == null || (event.endMs ?: Long.MIN_VALUE) > (kept.endMs ?: Long.MIN_VALUE)) byKey[event.key] = event
            }
            return byKey.values.sortedBy { it.startMs }
        }
    }
}
