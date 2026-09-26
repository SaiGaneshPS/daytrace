// DT-20 / DT-21: event store used by all collectors. DT-21 moves it to Room (numbered events with a synced flag);
// until then events are appended to a JSON-lines file, so nothing collected is lost between runs.
package app.daytrace.android.data

import android.content.Context
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.RandomAccessFile
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
    /**
     * Appends in one write and waits until it is on disk (fsync), so a checkpoint saved afterwards never gets ahead
     * of the events it covers. A line cut off by a crash is closed off first, so it cannot swallow the next event.
     */
    @Synchronized
    fun add(events: List<PhoneEvent>) {
        if (events.isEmpty()) return
        file.parentFile?.mkdirs()
        val text = buildString {
            if (endsMidLine()) append(NEWLINE)
            events.forEach { append(it.toStoredJson().toString()).append(NEWLINE) }
        }
        FileOutputStream(file, true).use { out ->
            out.write(text.toByteArray(Charsets.UTF_8))
            out.fd.sync()
        }
    }

    /**
     * Every stored event once, in time order. A collection repeated after a crash stores the same session again
     * with an end at least as late, so the copy that ends last is kept.
     */
    @Synchronized
    fun all(): List<PhoneEvent> {
        if (!file.exists()) return emptyList()
        val byKey = LinkedHashMap<String, PhoneEvent>()
        file.forEachLine { line ->
            val event = runCatching { PhoneEvent.fromStoredJson(JSONObject(line)) }.getOrNull() ?: return@forEachLine
            val kept = byKey[event.key]
            if (kept == null || (event.endMs ?: Long.MIN_VALUE) > (kept.endMs ?: Long.MIN_VALUE)) byKey[event.key] = event
        }
        return byKey.values.sortedBy { it.startMs }
    }

    private fun endsMidLine(): Boolean {
        if (!file.exists() || file.length() == 0L) return false
        RandomAccessFile(file, "r").use { raf ->
            raf.seek(file.length() - 1)
            return raf.read() != NEWLINE.code
        }
    }

    companion object {
        private const val NEWLINE = '\n'

        @Volatile private var instance: EventStore? = null

        fun get(context: Context): EventStore = instance ?: synchronized(this) {
            instance ?: EventStore(File(context.filesDir, "events-pending.jsonl")).also { instance = it }
        }
    }
}
