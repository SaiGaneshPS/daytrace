// DT-58: the Dashboard tab's rules: the hub's own address only, its pages in embed mode, and the token script.
package app.daytrace.android.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class DashboardRulesTest {
    private val hub = "http://192.168.137.1:8765"

    @Test
    fun theOriginIsSchemeHostAndPort() {
        assertEquals("http://192.168.137.1:8765", DashboardRules.origin(hub))
        assertEquals("http://192.168.137.1:8765", DashboardRules.origin("$hub/"))
        assertEquals("http://daytrace-pc.local:80", DashboardRules.origin("http://daytrace-pc.local"))
        assertNull(DashboardRules.origin("not an address"))
    }

    @Test
    fun theWebViewMayGoOnlyToTheHubItself() {
        assertTrue(DashboardRules.onHub("$hub/insights?tab=focus", hub))
        assertTrue(DashboardRules.onHub("$hub/assets/index.js", hub))
        assertFalse(DashboardRules.onHub("http://192.168.137.1:8766/", hub)) // another port: another server
        assertFalse(DashboardRules.onHub("https://192.168.137.1:8765/", hub)) // another scheme
        assertFalse(DashboardRules.onHub("http://192.168.137.2:8765/", hub))
        assertFalse(DashboardRules.onHub("https://fonts.googleapis.com/css", hub))
        assertFalse(DashboardRules.onHub("javascript:alert(1)", hub))
        assertFalse(DashboardRules.onHub("file:///sdcard/x.html", hub))
    }

    @Test
    fun pagesOpenInEmbedMode() {
        assertEquals("$hub/?embed=1", DashboardRules.pageUrl(hub, "/"))
        assertEquals("$hub/story?embed=1", DashboardRules.pageUrl("$hub/", "/story"))
        assertEquals("$hub/insights?tab=focus&embed=1", DashboardRules.pageUrl(hub, "/insights?tab=focus"))
    }

    @Test
    fun theTokenScriptQuotesTheToken() {
        val script = DashboardRules.tokenScript("dt_abc'\"-_")
        assertTrue(script, script.contains("localStorage.setItem('daytrace.token',\"dt_abc'\\\"-_\")"))
        assertFalse(script.contains("\n"))
    }
}
