// DT-23: one collector failing never stops the others or the sync, and a stopped worker still stops.
package app.daytrace.android.sync

import android.app.Application
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowLog

@RunWith(RobolectricTestRunner::class) // the failure goes to the log
@Config(application = Application::class)
class SyncWorkerTest {
    @Test
    fun aCollectorsFailureIsItsOwnEvenAnError() = runBlocking {
        val ran = mutableListOf<String>()
        SyncWorker.collecting { throw IllegalStateException("no usage access") }
        SyncWorker.collecting { throw NoSuchMethodError("Health Connect is updating") }
        SyncWorker.collecting { ran += "calendar" }
        assertEquals(listOf("calendar"), ran)
        assertEquals(2, ShadowLog.getLogsForTag("Daytrace").size) // each failure says what failed
    }

    @Test
    fun aStoppedWorkerStillStops() = runBlocking {
        val thrown = runCatching { SyncWorker.collecting { throw CancellationException("stopped") } }.exceptionOrNull()
        assertTrue(thrown is CancellationException)
    }
}
