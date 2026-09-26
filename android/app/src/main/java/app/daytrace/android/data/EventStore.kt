// DT-20 / DT-21: event store used by all collectors. DT-21 moves it to Room (numbered events with a synced flag);
// until then events are appended to a JSON-lines file, so nothing collected is lost between runs.
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
) {
    /** The same moment collected twice gives the same key, so a repeated collection never adds time twice. */
    val key: String get() = "$kind|$startMs|${appId ?: app.orEmpty()}"

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

class EventStore(private val file: File) {
    /** Appends in one write; a line cut off by a crash is skipped when reading. */
    @Synchronized
    fun add(events: List<PhoneEvent>) {
        if (events.isEmpty()) return
        file.parentFile?.mkdirs()
        file.appendText(events.joinToString(separator = "") { it.toStoredJson().toString() + "\n" })
    }

    /** Every stored event once, in time order. */
    @Synchronized
    fun all(): List<PhoneEvent> {
        if (!file.exists()) return emptyList()
        val byKey = LinkedHashMap<String, PhoneEvent>()
        file.forEachLine { line ->
            val event = runCatching { PhoneEvent.fromStoredJson(JSONObject(line)) }.getOrNull() ?: return@forEachLine
            byKey.putIfAbsent(event.key, event)
        }
        return byKey.values.sortedBy { it.startMs }
    }

    companion object {
        @Volatile private var instance: EventStore? = null

        fun get(context: Context): EventStore = instance ?: synchronized(this) {
            instance ?: EventStore(File(context.filesDir, "events-pending.jsonl")).also { instance = it }
        }
    }
}
