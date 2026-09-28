// DT-24 / DT-58: when Android ends live mode at its daily limit (onTimeout, Android 15 and newer), the app knows, so
// the status screen can say why live mode stopped instead of going quiet.
package app.daytrace.android.live

import android.app.Application
import android.content.pm.ServiceInfo
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [35])
class LiveModeTimeoutTest {
    @Test
    fun androidsDailyLimitIsRemembered() {
        val service = Robolectric.buildService(LiveModeService::class.java).create().get()
        assertFalse(LiveModeService.timedOut.value)
        service.onTimeout(1, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        assertTrue(LiveModeService.timedOut.value)
        assertFalse(LiveModeService.running.value)
    }
}
