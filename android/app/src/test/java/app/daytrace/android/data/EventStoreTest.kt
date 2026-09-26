// DT-20 / DT-21: the event store: nothing lost, nothing counted twice, numbered for the hub, old events moved in.
package app.daytrace.android.data

import android.app.Application
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File
import java.time.ZoneId

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class EventStoreTest {
    @get:Rule val folder = TemporaryFolder()
    private lateinit var db: AppDatabase
    private lateinit var store: EventStore

    @Before
    fun openDatabase() {
        db = Room.inMemoryDatabaseBuilder(ApplicationProvider.getApplicationContext(), AppDatabase::class.java)
            .allowMainThreadQueries()
            .build()
        store = EventStore(db)
    }

    @After
    fun closeDatabase() = db.close()

    private fun session(start: Long, end: Long, app: String = "yt") = PhoneEvent("app_session", "usagestats", start, end, app, app)
    private fun queued() = store.pending(1_000)

    @Test
    fun eventsAreNumberedFromZeroInTheOrderTheyAreStored() {
        store.add(listOf(session(200, 300), session(100, 150, "ig")))
        store.add(listOf(PhoneEvent("screen_off", "usagestats", 400)))
        assertEquals(listOf(0L to 200L, 1L to 100L, 2L to 400L), queued().map { it.seq to it.startMs })
        assertEquals(listOf(100L, 200L), store.sessionsEndingAfter(0).map { it.startMs }) // sessions in time order
    }

    @Test
    fun aRepeatedCollectionKeepsTheCopyThatEndsLast() {
        store.add(listOf(session(100, 200)))
        assertEquals(1, store.add(listOf(session(100, 260)))) // collected again after a crash, now known to run longer
        assertEquals(0, store.add(listOf(session(100, 200)))) // and an older copy again
        assertEquals(listOf(Triple(1L, 100L, 260L)), queued().map { Triple(it.seq, it.startMs, it.endMs) })
    }

    @Test
    fun anEventExtendedAfterItWasSentIsQueuedAgainUnderANewNumber() {
        store.add(listOf(session(100, 200)))
        val sent = queued()
        assertEquals(1, store.markSynced(sent))
        assertEquals(StoreCounts(waiting = 0, refused = 0), store.counts())

        store.add(listOf(session(100, 300)))
        assertEquals(listOf(1L to 300L), queued().map { it.seq to it.endMs })
        assertEquals(0, store.markSynced(sent)) // a sync that read the old copy cannot mark the new one as sent
        assertEquals(1, store.counts().waiting)
    }

    @Test
    fun afterAReinstallNewNumbersStartAboveTheHubs() {
        store.add(listOf(session(100, 200), session(300, 400)))
        store.raiseSeqFloor(41)
        assertEquals(listOf(42L, 43L), queued().map { it.seq })
        store.add(listOf(session(500, 600)))
        assertEquals(44L, queued().last().seq)
    }

    @Test
    fun theHubsNumbersAreRespectedEvenBeforeAnythingIsCollected() {
        store.raiseSeqFloor(9)
        store.add(listOf(session(100, 200)))
        assertEquals(listOf(10L), queued().map { it.seq })
    }

    @Test
    fun conflictingEventsGetNumbersAboveEverything() {
        store.add(listOf(session(100, 200), session(300, 400), session(500, 600)))
        store.renumber(queued().take(1), hubLastSeq = 99)
        assertEquals(listOf(1L, 2L, 100L), queued().map { it.seq })
        store.add(listOf(session(700, 800)))
        assertEquals(101L, queued().last().seq)
    }

    @Test
    fun refusedEventsAreKeptButNeverQueuedAgain() {
        store.add(listOf(session(100, 200)))
        assertEquals(1, store.markRejected(queued().single(), "invalid: bad"))
        assertEquals(StoreCounts(waiting = 0, refused = 1), store.counts())
        assertTrue(queued().isEmpty())
    }

    @Test
    fun todaysSessionsAreTheOnesEndingAfterMidnight() {
        store.add(listOf(session(100, 150, "ig"), session(180, 300), PhoneEvent("screen_on", "usagestats", 250)))
        assertEquals(listOf("yt"), store.sessionsEndingAfter(200).map { it.appId })
    }

    private fun writeLegacyFile(file: File) {
        file.writeText(
            listOf(session(100, 200), session(100, 260), session(300, 400, "ig")).joinToString("") { it.toStoredJson().toString() + "\n" } +
                "{\"kind\":\"app_ses\n", // a line cut off when the process died mid-write
        )
    }

    @Test
    fun eventsFromBeforeTheDatabaseAreMovedInOnce() {
        val file = File(folder.root, EventStore.LEGACY_FILE)
        writeLegacyFile(file)
        val moved = EventStore(db, file)
        assertEquals(listOf(Triple("yt", 100L, 260L), Triple("ig", 300L, 400L)), moved.pending(10).map { Triple(it.appId, it.startMs, it.endMs) })
        assertFalse(file.exists())
    }

    @Test
    fun movingTheOldFileAgainAfterACrashAddsNothing() {
        val file = File(folder.root, EventStore.LEGACY_FILE)
        writeLegacyFile(file)
        EventStore(db, file).counts()
        writeLegacyFile(file) // the app stopped after moving the events but before deleting the file
        assertEquals(StoreCounts(waiting = 2, refused = 0), EventStore(db, file).counts())
    }

    @Test
    fun timesAreWrittenForTheHubWithTheirOffset() {
        val event = session(1_790_307_924_094, 1_790_307_924_924)
        assertEquals("2026-09-25T01:15:24.094-02:30", event.isoStart(ZoneId.of("America/St_Johns")))
    }
}
