// DT-58: the widget's 30-minute refresh runs on Wi-Fi as the sync does: a hub LAN may have no internet, and never a VPN.
package app.daytrace.android.widget

import android.app.Application
import android.content.Context
import android.net.NetworkCapabilities
import androidx.test.core.app.ApplicationProvider
import androidx.work.WorkManager
import androidx.work.testing.WorkManagerTestInitHelper
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class WidgetWorkerTest {
    private val context: Context = ApplicationProvider.getApplicationContext()

    @Test
    fun theRefreshWaitsForWifiButNotForTheInternet() {
        WorkManagerTestInitHelper.initializeTestWorkManager(context)
        WidgetWorker.schedule(context)
        val info = WorkManager.getInstance(context).getWorkInfosForUniqueWork(WidgetWorker.PERIODIC).get().single()
        val network = info.constraints.requiredNetworkRequest!!
        assertTrue(network.hasTransport(NetworkCapabilities.TRANSPORT_WIFI))
        assertTrue(network.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN))
        assertFalse(network.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET))
        assertEquals(30 * 60_000L, info.periodicityInfo!!.repeatIntervalMillis)
    }
}
