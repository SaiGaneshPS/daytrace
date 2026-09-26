// DT-21: one event in the phone's database, numbered for the hub and marked once the hub has it.
package app.daytrace.android.data

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

/** Where an event is on its way to the hub. */
object SyncState {
    const val PENDING = 0
    const val SYNCED = 1
    /** The hub refused it for good and said why (rejectReason). Kept on the phone, never sent again. */
    const val REJECTED = 2
}

@Entity(
    tableName = "events",
    indices = [
        Index(value = ["event_key"], unique = true),
        Index(value = ["seq"], unique = true),
        Index(value = ["state", "seq"]),
        Index(value = ["end_ms"]),
    ],
)
data class EventEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    /**
     * The per-device number the hub keeps its cursor on (docs/api.md). An event that changes gets a new, higher
     * number, so the hub always keeps the newest copy and a sync that read the old copy cannot mark the new one.
     */
    val seq: Long,
    /** [PhoneEvent.key]: the same moment collected twice has the same key. The hub gets it as external_id. */
    @ColumnInfo(name = "event_key") val key: String,
    val kind: String,
    val source: String,
    @ColumnInfo(name = "start_ms") val startMs: Long,
    @ColumnInfo(name = "end_ms") val endMs: Long?,
    val app: String?,
    @ColumnInfo(name = "app_id") val appId: String?,
    /** The time zone when it was collected, so the hub gets the UTC offset in force then, not at sync time. */
    val zone: String,
    val state: Int = SyncState.PENDING,
    @ColumnInfo(name = "reject_reason") val rejectReason: String? = null,
) {
    fun toPhoneEvent() = PhoneEvent(kind, source, startMs, endMs, app, appId)
}

/** Named numbers kept next to the events and changed in the same transactions (the lowest free seq, say). */
@Entity(tableName = "meta")
data class MetaEntry(@PrimaryKey val name: String, val value: Long)
