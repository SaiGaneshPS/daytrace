// DT-58: the Dashboard tab's rules: the hub's own address only, its pages in embed mode, and the token script.
package app.daytrace.android.ui

import android.app.Application
import app.daytrace.android.sync.SyncResult
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowNetwork

@RunWith(RobolectricTestRunner::class) // for a real Network in HubGate.Ready
@Config(application = Application::class)
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

    @Test
    fun thePageTakesTheAppsOwnLightOrDark() {
        assertTrue(DashboardRules.themeScript(dark = true).contains("var t='dark'"))
        assertTrue(DashboardRules.themeScript(dark = false).contains("var t='light'"))
        assertTrue(DashboardRules.themeScript(dark = true).contains("setAttribute('data-theme',t)"))
        assertFalse(DashboardRules.themeScript(dark = true).contains("\n"))
    }

    @Test
    fun thePageScriptIsTheTokenThenTheThemeAsTwoWholeStatements() {
        val token = "dt_abc'\"-_);}"
        val script = DashboardRules.pageScript(token, dark = true)
        assertEquals(DashboardRules.tokenScript(token) + DashboardRules.themeScript(dark = true), script)
        // Each part closes every bracket it opens (outside its strings) and ends its statement, so neither can swallow
        // or break the other.
        for (part in listOf(DashboardRules.tokenScript(token), DashboardRules.themeScript(dark = false), script)) {
            assertEquals(part, 0, bracketDepth(part))
            assertTrue(part, part.endsWith("})();"))
            assertTrue(part, part.startsWith("(function(){try{"))
        }
    }

    /** How many brackets are still open at the end, skipping quoted strings (and failing on one that closes too early). */
    private fun bracketDepth(js: String): Int {
        var depth = 0
        var quote: Char? = null
        var i = 0
        while (i < js.length) {
            val c = js[i]
            when {
                quote != null && c == '\\' -> i++
                quote != null && c == quote -> quote = null
                quote != null -> {}
                c == '\'' || c == '"' -> quote = c
                c in "({[" -> depth++
                c in ")}]" -> { depth--; assertTrue(js, depth >= 0) }
            }
            i++
        }
        assertEquals(js, null, quote)
        return depth
    }

    @Test
    fun theTabsSayWhatNeedsYouOnThisPhone() {
        assertEquals("Usage access is off, so Daytrace can't see which apps you use.", DashboardRules.warning(false, SyncResult.SENT, "Sent 3 events"))
        assertEquals("Your hub no longer accepts this phone. Pair again.", DashboardRules.warning(true, SyncResult.PAIR_AGAIN, "Your hub no longer accepts this phone. Pair again."))
        assertEquals("not your hub", DashboardRules.warning(true, SyncResult.BLOCKED, "not your hub"))
        listOf(SyncResult.SENT, SyncResult.NOT_ON_WIFI, SyncResult.UNREACHABLE, SyncResult.NOT_PAIRED, null).forEach { result ->
            assertNull("for $result", DashboardRules.warning(true, result, "whatever")) // nothing for you to do
        }
    }

    @Test
    fun theReadyGateNeverPrintsTheToken() {
        val text = HubGate.Ready("http://192.168.1.23:8765", "dt_viewer_secret", ShadowNetwork.newInstance(1)).toString()
        assertFalse(text, "dt_viewer_secret" in text)
    }
}
