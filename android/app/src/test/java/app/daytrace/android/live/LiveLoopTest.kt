// DT-24: live mode's timing: whatever is waiting goes at once, a failed sync waits a little, the screen off slows it
// down (but not once it's back on), a failed pass never ends it, and a phone that isn't paired any more stops it.
package app.daytrace.android.live

import android.app.Application
import app.daytrace.android.sync.SyncResult
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class) // a failed pass goes to the log
@Config(application = Application::class)
class LiveLoopTest {
    private class Stop : CancellationException("enough passes")

    /** What the loop did: the passes that synced, when each pass read, and whether it ended by itself. */
    private data class Ran(val synced: List<Int>, val reads: List<Long>, val ended: Boolean)

    /** Runs the loop on a fake clock for [passes] passes (a pass starts with its read). */
    private fun run(
        passes: Int,
        changes: List<Int> = emptyList(),
        waiting: (Int) -> Boolean = { false },
        results: (Int) -> LiveSync = { LiveSync.SENT },
        screenAt: (Long) -> Boolean = { true },
        collectFails: Set<Int> = emptySet(),
    ): Ran {
        val synced = mutableListOf<Int>()
        val reads = mutableListOf<Long>()
        var pass = -1
        var now = 0L
        var ended = false
        val loop = LiveLoop(
            collect = {
                pass++
                if (pass >= passes) throw Stop()
                reads += now
                if (pass in collectFails) throw NoSuchMethodError("an Error, not an Exception")
                changes.getOrElse(pass) { 0 }
            },
            waiting = { waiting(pass) },
            sync = { synced += pass; results(pass) },
            screenOn = { screenAt(now) },
            sleep = { ms -> now += ms },
            clock = { now },
        )
        runCatching { runBlocking { loop.run(); ended = true } }
        return Ran(synced, reads, ended)
    }

    @Test
    fun whateverIsWaitingGoesAtOnce() {
        // Pass 3 read something new; pass 6 found events left over from before (a sync that failed elsewhere).
        val ran = run(passes = 10, changes = listOf(0, 0, 0, 1), waiting = { it == 6 })
        assertEquals(listOf(3, 6), ran.synced)
        assertEquals(List(10) { it * LiveLoop.EVERY_MS }, ran.reads) // a read every 5 s
    }

    @Test
    fun aFailedSyncWaitsThirtySecondsEvenForNewEvents() {
        val ran = run(passes = 12, changes = List(12) { 1 }, results = { if (it == 0) LiveSync.LATER else LiveSync.SENT })
        assertEquals(listOf(0, 6, 7, 8, 9, 10, 11), ran.synced) // 30 s after the failure, then every pass again
    }

    @Test
    fun withTheScreenOffItReadsOnceAMinuteAndAtOnceWhenItComesBackOn() {
        assertEquals(listOf(0L, 60_000L, 120_000L), run(passes = 3, screenAt = { false }).reads)
        // Back on 20 s in: the next read is then, not a minute after the last one.
        assertEquals(listOf(0L, 20_000L, 25_000L), run(passes = 3, screenAt = { now -> now >= 20_000 }).reads)
    }

    @Test
    fun aFailedPassEvenAnErrorNeverEndsLiveMode() {
        val ran = run(passes = 4, changes = List(4) { 1 }, collectFails = setOf(1))
        assertEquals(listOf(0, 2, 3), ran.synced) // pass 1 read nothing, the rest still ran
    }

    @Test
    fun aPhoneThatIsNotPairedAnyMoreEndsLiveMode() {
        val ran = run(passes = 10, changes = List(10) { 1 }, results = { if (it == 2) LiveSync.STOP else LiveSync.SENT })
        assertTrue(ran.ended)
        assertEquals(listOf(0, 1, 2), ran.synced)
        assertEquals(LiveSync.STOP, LiveLoop.of(SyncResult.PAIR_AGAIN))
        assertEquals(LiveSync.STOP, LiveLoop.of(SyncResult.NOT_PAIRED))
        assertEquals(LiveSync.LATER, LiveLoop.of(SyncResult.UNREACHABLE))
        assertEquals(LiveSync.LATER, LiveLoop.of(SyncResult.NOT_ON_WIFI))
    }
}
