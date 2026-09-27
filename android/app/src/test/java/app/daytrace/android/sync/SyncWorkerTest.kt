// DT-23: one collector failing never stops the others or the sync, and a stopped worker still stops.
package app.daytrace.android.sync

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class SyncWorkerTest {
    @Test
    fun aCollectorsFailureIsItsOwnEvenAnError() = runBlocking {
        val ran = mutableListOf<String>()
        SyncWorker.collecting { throw IllegalStateException("no usage access") }
        SyncWorker.collecting { throw NoSuchMethodError("Health Connect is updating") }
        SyncWorker.collecting { ran += "calendar" }
        assertEquals(listOf("calendar"), ran)
    }

    @Test
    fun aStoppedWorkerStillStops() = runBlocking {
        val thrown = runCatching { SyncWorker.collecting { throw CancellationException("stopped") } }.exceptionOrNull()
        assertTrue(thrown is CancellationException)
    }
}
