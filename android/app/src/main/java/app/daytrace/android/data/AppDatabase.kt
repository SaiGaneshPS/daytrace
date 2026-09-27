// DT-21: the phone's database (in the app's private storage, never backed up: see AndroidManifest.xml).
// A later schema change needs a Migration here; never use a destructive migration, it would drop events that
// have not reached the hub yet.
package app.daytrace.android.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

/** DT-23: health and calendar events carry a title, data and their own id. Every event already queued stays. */
val MIGRATION_1_2 = object : Migration(1, 2) {
    override fun migrate(db: SupportSQLiteDatabase) {
        db.execSQL("ALTER TABLE events ADD COLUMN title TEXT")
        db.execSQL("ALTER TABLE events ADD COLUMN data TEXT")
        db.execSQL("ALTER TABLE events ADD COLUMN is_record INTEGER NOT NULL DEFAULT 0")
    }
}

@Database(entities = [EventEntity::class, MetaEntry::class], version = 2, exportSchema = false)
abstract class AppDatabase : RoomDatabase() {
    abstract fun events(): EventDao

    companion object {
        @Volatile private var instance: AppDatabase? = null

        fun get(context: Context): AppDatabase = instance ?: synchronized(this) {
            instance ?: Room.databaseBuilder(context.applicationContext, AppDatabase::class.java, "daytrace.db")
                .addMigrations(MIGRATION_1_2)
                .addCallback(DurableCommits)
                .build()
                .also { instance = it }
        }
    }
}

/**
 * Every commit waits until it is on disk (SQLite synchronous=FULL; Android's default for WAL can lose the last
 * commits on a power cut). The usage checkpoint is saved right after its events, so the events must never be the
 * part that goes missing. There is about one commit a minute, so the cost is nothing.
 */
private object DurableCommits : RoomDatabase.Callback() {
    override fun onOpen(db: SupportSQLiteDatabase) {
        db.query("PRAGMA synchronous = FULL").close()
    }
}
