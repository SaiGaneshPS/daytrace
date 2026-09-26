// DT-21: the phone's database (in the app's private storage, never backed up: see AndroidManifest.xml).
// A later schema change needs a Migration here; never use a destructive migration, it would drop events that
// have not reached the hub yet.
package app.daytrace.android.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.sqlite.db.SupportSQLiteDatabase

@Database(entities = [EventEntity::class, MetaEntry::class], version = 1, exportSchema = false)
abstract class AppDatabase : RoomDatabase() {
    abstract fun events(): EventDao

    companion object {
        @Volatile private var instance: AppDatabase? = null

        fun get(context: Context): AppDatabase = instance ?: synchronized(this) {
            instance ?: Room.databaseBuilder(context.applicationContext, AppDatabase::class.java, "daytrace.db")
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
