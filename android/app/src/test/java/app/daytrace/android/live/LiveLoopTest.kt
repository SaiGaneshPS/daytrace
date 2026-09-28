// DT-24: live mode's timing: what is new goes at once, a quiet phone syncs now and then, the screen off slows it
// down, and a failed pass never ends it.
package app.daytrace.android.live

import android.app.Application
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class) // a failed pass goes to the log
@Config(application = Application::class)
class LiveLoopTest {
    private class Stop : CancellationException("enough passes")

    /** Runs [passes] passes: each returns the next of [changes] from collect, then the loop sleeps. */
    private fun run(passes: Int, changes: List<Int>, screen: (Int) -> Boolean = { true }, collectFails: Set<Int> = emptySet(), syncFails: Boolean = false): Pair<List<Int>, List<Long>> {
        val synced = mutableListOf<Int>()
        val sleeps = mutableListOf<Long>()
        var pass = 0
        var now = 0L
        val loop = LiveLoop(
            collect = { if (pass in collectFails) error("usage access was taken away") else changes.getOrElse(pass) { 0 } },
            sync = { synced += pass; if (syncFails) error("the hub is off") },
            screenOn = { screen(pass) },
            sleep = { ms -> sleeps += ms; now += ms; pass++; if (pass == passes) throw Stop() },
            clock = { now },
        )
        runCatching { runBlocking { loop.run() } }
        return synced to sleeps
    }

    @Test
    fun somethingNewIsSentAtOnceAndAQuietPhoneOnlyNowAndThen() {
        // pass 0 syncs to start with; 3 and 4 found something; nothing else changes, so the next sync is a minute on
        val (synced, sleeps) = run(passes = 20, changes = listOf(0, 0, 0, 1, 2))
        assertEquals(listOf(0, 3, 4, 16), synced)
        assertEquals(List(20) { LiveLoop.EVERY_MS }, sleeps)
    }

    @Test
    fun withTheScreenOffItReadsOnceAMinute() {
        val (_, sleeps) = run(passes = 3, changes = emptyList(), screen = { it != 1 })
        assertEquals(listOf(LiveLoop.EVERY_MS, LiveLoop.SCREEN_OFF_EVERY_MS, LiveLoop.EVERY_MS), sleeps)
    }

    @Test
    fun aFailedReadOrSyncNeverEndsLiveMode() {
        val (synced, sleeps) = run(passes = 4, changes = listOf(1, 1, 1, 1), collectFails = setOf(1), syncFails = true)
        assertEquals(listOf(0, 2, 3), synced) // pass 1 read nothing, the rest still ran
        assertEquals(4, sleeps.size)
    }
}
