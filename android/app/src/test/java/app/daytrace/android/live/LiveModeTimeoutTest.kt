// DT-24 / DT-58: when Android ends live mode at its time limit (onTimeout, Android 15 and newer), the app keeps that on
// the phone, so the status screen can say why live mode stopped even after Android ended the app too; Stop clears it.
package app.daytrace.android.live

import android.app.Application
import android.content.Context
import android.content.pm.ServiceInfo
import androidx.test.core.app.ApplicationProvider
import org.junit.After
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
    private val context: Context = ApplicationProvider.getApplicationContext()

    @After
    fun tearDown() = LiveModeService.setTimedOut(context, false)

    @Test
    fun androidsTimeLimitIsKeptOnThePhoneUntilLiveModeIsStopped() {
        assertFalse(LiveModeService.timedOut(context).value)
        Robolectric.buildService(LiveModeService::class.java).create().get()
            .onTimeout(1, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        assertTrue(LiveModeService.timedOut(context).value)
        LiveModeService.forgetTimedOutRead() // as after Android ended the app: read back from the phone
        assertTrue(LiveModeService.timedOut(context).value)
        LiveModeService.stop(context) // Stop (and Forget, which stops it) clears it
        assertFalse(LiveModeService.timedOut(context).value)
    }
}
