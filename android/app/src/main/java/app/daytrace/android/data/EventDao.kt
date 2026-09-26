// DT-21: the queries behind EventStore. Every change to a queued event is checked against its seq, so a sync
// that read an older copy of an event can never mark the newer copy as sent.
package app.daytrace.android.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Update

@Dao
interface EventDao {
    @Insert
    fun insert(event: EventEntity): Long

    @Update
    fun update(event: EventEntity)

    @Query("SELECT * FROM events WHERE event_key = :key")
    fun byKey(key: String): EventEntity?

    @Query("SELECT MAX(seq) FROM events")
    fun maxSeq(): Long?

    @Query("SELECT * FROM events WHERE state = ${SyncState.PENDING} ORDER BY seq LIMIT :limit")
    fun pending(limit: Int): List<EventEntity>

    @Query("SELECT * FROM events WHERE state = ${SyncState.PENDING} AND seq <= :atMost ORDER BY seq")
    fun pendingUpTo(atMost: Long): List<EventEntity>

    @Query("SELECT COUNT(*) FROM events WHERE state = :state")
    fun count(state: Int): Int

    @Query("UPDATE events SET state = ${SyncState.SYNCED} WHERE id = :id AND seq = :seq AND state = ${SyncState.PENDING}")
    fun markSynced(id: Long, seq: Long): Int

    @Query(
        "UPDATE events SET state = ${SyncState.REJECTED}, reject_reason = :reason" +
            " WHERE id = :id AND seq = :seq AND state = ${SyncState.PENDING}",
    )
    fun markRejected(id: Long, seq: Long, reason: String): Int

    @Query("UPDATE events SET seq = :newSeq WHERE id = :id AND seq = :oldSeq AND state = ${SyncState.PENDING}")
    fun renumber(id: Long, oldSeq: Long, newSeq: Long): Int

    @Query("SELECT * FROM events WHERE kind = 'app_session' AND end_ms > :afterMs ORDER BY start_ms")
    fun sessionsEndingAfter(afterMs: Long): List<EventEntity>

    @Query("SELECT value FROM meta WHERE name = :name")
    fun meta(name: String): Long?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun setMeta(entry: MetaEntry)
}
