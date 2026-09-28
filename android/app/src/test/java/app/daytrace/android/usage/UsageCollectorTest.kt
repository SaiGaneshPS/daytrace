// DT-24: the collector as live mode uses it: 2 s behind now, one growing session per open app, and a background
// read (15 s behind) right after never winds the checkpoint back.
package app.daytrace.android.usage

import android.Manifest
import android.app.AppOpsManager
import android.app.Application
import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.os.Process
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import app.daytrace.android.data.AppDatabase
import app.daytrace.android.data.EventStore
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class UsageCollectorTest {
    private val context: Application = ApplicationProvider.getApplicationContext()
    private lateinit var db: AppDatabase
    private lateinit var store: EventStore
    private val now = 1_790_553_600_000L // 21:30 on the demo evening
    private val uptime = 50_000_000L
    private val tiktok = "com.zhiliaoapp.musically"

    @Before
    fun setUp() {
        context.getSharedPreferences("usage_collector", 0).edit().clear().commit()
        shadowOf(context).grantPermissions(Manifest.permission.PACKAGE_USAGE_STATS)
        shadowOf(context.getSystemService(AppOpsManager::class.java))
            .setMode(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), context.packageName, AppOpsManager.MODE_ALLOWED)
        db = Room.inMemoryDatabaseBuilder(context, AppDatabase::class.java).allowMainThreadQueries().build()
        store = EventStore(db)
    }

    @After
    fun tearDown() = db.close()

    private fun opened(pkg: String, atMs: Long) =
        shadowOf(context.getSystemService(UsageStatsManager::class.java)).addEvent(pkg, atMs, UsageEvents.Event.ACTIVITY_RESUMED)

    private fun sessions() = store.pending(100).filter { it.kind == "app_session" }

    @Test
    fun liveModeSeesAnAppOpenedSecondsAgoAsOneGrowingSession() {
        val collector = UsageCollector(context, store)
        opened(tiktok, now - 4_000)
        assertEquals(1, collector.collect(now, uptime, latenessMs = 2_000)) // opened 4 s ago: already there
        assertEquals(listOf((now - 4_000) to (now - 2_000)), sessions().map { it.startMs to it.endMs })

        assertEquals(1, collector.collect(now + 5_000, uptime + 5_000, latenessMs = 2_000)) // 5 s on: longer
        // The 15-minute background read right after: nothing new, and live mode's checkpoint stays where it was.
        assertEquals(0, collector.collect(now + 6_000, uptime + 6_000))
        assertEquals(1, collector.collect(now + 10_000, uptime + 10_000, latenessMs = 2_000))

        val session = sessions().single() // three reads, one session, never counted twice
        assertEquals((now - 4_000) to (now + 8_000), session.startMs to session.endMs)
        assertEquals(tiktok, session.appId)
    }
}
