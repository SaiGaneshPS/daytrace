// DT-23: updating the app keeps every event already on the phone, including the ones not yet sent.
package app.daytrace.android.data

import android.app.Application
import android.database.sqlite.SQLiteDatabase
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class MigrationTest {
    /** The database as version 1 of the app (DT-21 and DT-22) made it: Room's own statements for that version. */
    private val version1 = listOf(
        "CREATE TABLE IF NOT EXISTS `events` (`id` INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, `seq` INTEGER NOT NULL, " +
            "`event_key` TEXT NOT NULL, `kind` TEXT NOT NULL, `source` TEXT NOT NULL, `start_ms` INTEGER NOT NULL, `end_ms` INTEGER, " +
            "`app` TEXT, `app_id` TEXT, `zone` TEXT NOT NULL, `state` INTEGER NOT NULL, `reject_reason` TEXT)",
        "CREATE UNIQUE INDEX IF NOT EXISTS `index_events_event_key` ON `events` (`event_key`)",
        "CREATE UNIQUE INDEX IF NOT EXISTS `index_events_seq` ON `events` (`seq`)",
        "CREATE INDEX IF NOT EXISTS `index_events_state_seq` ON `events` (`state`, `seq`)",
        "CREATE INDEX IF NOT EXISTS `index_events_end_ms` ON `events` (`end_ms`)",
        "CREATE TABLE IF NOT EXISTS `meta` (`name` TEXT NOT NULL, `value` INTEGER NOT NULL, PRIMARY KEY(`name`))",
        "CREATE TABLE IF NOT EXISTS room_master_table (id INTEGER PRIMARY KEY,identity_hash TEXT)",
        "INSERT OR REPLACE INTO room_master_table (id,identity_hash) VALUES(42, '5e67ca9106838abe0a1ebf423e277395')",
        "INSERT INTO events (seq, event_key, kind, source, start_ms, end_ms, app, app_id, zone, state) " +
            "VALUES (7, 'app_session|100|yt', 'app_session', 'usagestats', 100, 200, 'YouTube', 'yt', 'America/St_Johns', 0)",
        "INSERT INTO meta (name, value) VALUES ('seq_floor', 8)",
        "PRAGMA user_version = 1",
    )

    @Test
    fun anUpdateKeepsEveryQueuedEventAndItsNumbers() {
        val context = ApplicationProvider.getApplicationContext<Application>()
        val file = context.getDatabasePath("migration-test.db").apply { parentFile!!.mkdirs() }
        SQLiteDatabase.deleteDatabase(file) // anything a stopped run left behind
        SQLiteDatabase.openOrCreateDatabase(file, null).use { old -> version1.forEach(old::execSQL) }

        val db = Room.databaseBuilder(context, AppDatabase::class.java, file.absolutePath)
            .addMigrations(MIGRATION_1_2)
            .allowMainThreadQueries()
            .build()
        try {
            val store = EventStore(db)
            val kept = store.pending(10).single()
            assertEquals(7L to "YouTube", kept.seq to kept.app)
            assertNull(kept.title)
            assertNull(kept.data)
            assertNull(kept.toPhoneEvent().id) // an app session, not a record
            store.add(listOf(PhoneEvent("steps", "health_connect", 0, 900, data = "{\"count\":1}", id = "steps:2026-09-27")))
            assertEquals(listOf(7L, 8L), store.pending(10).map { it.seq }) // numbering carries on where it was
        } finally {
            db.close()
            SQLiteDatabase.deleteDatabase(file) // with its -wal, -shm and -journal files
        }
    }
}
