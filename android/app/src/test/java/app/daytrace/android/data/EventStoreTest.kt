// DT-20: the file-backed store: nothing lost, nothing counted twice, a crash mid-write recovered.
package app.daytrace.android.data

import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.time.ZoneId

class EventStoreTest {
    @get:Rule val folder = TemporaryFolder()

    private fun session(start: Long, end: Long, app: String = "yt") = PhoneEvent("app_session", "usagestats", start, end, app, app)

    @Test
    fun eventsComeBackInTimeOrder() {
        val store = EventStore(File(folder.root, "events.jsonl"))
        store.add(listOf(session(200, 300), session(100, 150, "ig")))
        store.add(listOf(PhoneEvent("screen_off", "usagestats", 400)))
        assertEquals(listOf(100L, 200L, 400L), store.all().map { it.startMs })
    }

    @Test
    fun aRepeatedCollectionKeepsTheCopyThatEndsLast() {
        val store = EventStore(File(folder.root, "events.jsonl"))
        store.add(listOf(session(100, 200)))
        store.add(listOf(session(100, 260))) // the same session, collected again after a crash, now known to run longer
        store.add(listOf(session(100, 200))) // and an older copy again
        assertEquals(listOf(100L to 260L), store.all().map { it.startMs to it.endMs })
    }

    @Test
    fun aLineCutOffByACrashDoesNotSwallowTheNextEvent() {
        val file = File(folder.root, "events.jsonl")
        val store = EventStore(file)
        store.add(listOf(session(100, 200)))
        file.appendText("{\"kind\":\"app_ses") // the process died mid-write
        store.add(listOf(session(300, 400, "ig")))
        assertEquals(listOf("yt", "ig"), store.all().map { it.appId })
    }

    @Test
    fun timesAreWrittenForTheHubWithTheirOffset() {
        val event = session(1_790_307_924_094, 1_790_307_924_924)
        assertEquals("2026-09-25T01:15:24.094-02:30", event.isoStart(ZoneId.of("America/St_Johns")))
    }
}
