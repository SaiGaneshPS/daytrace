// DT-20: the usage rules: exact sessions, screen on/off, and nothing lost or doubled across checkpoints.
package app.daytrace.android.usage

import app.daytrace.android.data.PhoneEvent
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class UsageSessionizerTest {
    private val start = 1_000_000L
    private fun at(seconds: Int) = start + seconds * 1000L
    private fun ev(seconds: Int, pkg: String, type: UsageType, cls: String? = "Main") = RawUsageEvent(at(seconds), pkg, cls, type)
    private fun run(vararg raw: RawUsageEvent, until: Int = 3600, state: UsageState = UsageState(start), ignored: Set<String> = setOf("launcher")) =
        UsageSessionizer.collect(raw.toList(), state, at(until), ignored) { pkg -> pkg.replaceFirstChar { it.uppercase() } }
    private fun sessions(collected: Collected) = collected.events.filter { it.kind == "app_session" }
    private fun seconds(event: PhoneEvent) = (event.endMs!! - event.startMs) / 1000

    @Test
    fun twoMinutesInAnAppIsOneTwoMinuteSession() {
        val result = sessions(run(ev(10, "instagram", UsageType.RESUMED), ev(130, "instagram", UsageType.PAUSED)))
        assertEquals(1, result.size)
        assertEquals(120L, seconds(result[0]))
        assertEquals("Instagram", result[0].app)
        assertEquals("instagram", result[0].appId)
        assertEquals("usagestats", result[0].source)
    }

    @Test
    fun movingBetweenScreensOfOneAppStaysOneSession() {
        val result = sessions(run(
            ev(0, "whatsapp", UsageType.RESUMED, "Chats"),
            ev(30, "whatsapp", UsageType.RESUMED, "Conversation"), // the new screen resumes before the old one pauses
            ev(31, "whatsapp", UsageType.PAUSED, "Chats"),
            ev(90, "whatsapp", UsageType.PAUSED, "Conversation"),
        ))
        assertEquals(listOf(90L), result.map(::seconds))
    }

    @Test
    fun switchingAppsEndsOneAndStartsTheOther() {
        val result = sessions(run(
            ev(0, "youtube", UsageType.RESUMED),
            ev(60, "tiktok", UsageType.RESUMED),
            ev(61, "youtube", UsageType.PAUSED),
            ev(200, "tiktok", UsageType.PAUSED),
        ))
        assertEquals(listOf("youtube" to 61L, "tiktok" to 140L), result.map { it.appId to seconds(it) })
    }

    @Test
    fun aNightWithTheScreenOffGivesAScreenOffAndOnPair() {
        val result = run(
            ev(0, "instagram", UsageType.RESUMED),
            ev(100, "android", UsageType.SCREEN_OFF, null), // no paused event before the screen went off
            ev(3000, "android", UsageType.SCREEN_ON, null),
        )
        assertEquals(listOf("app_session", "screen_off", "screen_on"), result.events.map { it.kind })
        assertEquals(100L, seconds(sessions(result)[0]))
        assertTrue(result.state.open.isEmpty())
    }

    @Test
    fun anAppOpenAtTheCheckpointContinuesWithoutGapOrOverlap() {
        val first = run(ev(3000, "youtube", UsageType.RESUMED), until = 3600)
        assertEquals(listOf(at(3000) to at(3600)), sessions(first).map { it.startMs to it.endMs })
        assertEquals(mapOf("youtube" to at(3600)), first.state.open)

        val second = UsageSessionizer.collect(
            listOf(ev(3900, "youtube", UsageType.PAUSED)), first.state, at(7200), emptySet(),
        ) { it }
        assertEquals(listOf(at(3600) to at(3900)), sessions(second).map { it.startMs to it.endMs })
        val total = (sessions(first) + sessions(second)).sumOf { it.endMs!! - it.startMs }
        assertEquals(900_000L, total) // 3000 s to 3900 s, counted exactly once
    }

    @Test
    fun theHomeScreenIsNotAppTime() {
        val result = sessions(run(ev(0, "launcher", UsageType.RESUMED), ev(50, "launcher", UsageType.PAUSED)))
        assertTrue(result.isEmpty())
    }

    @Test
    fun eventsOutsideTheWindowAreIgnored() {
        val result = sessions(run(ev(-10, "old", UsageType.RESUMED), ev(4000, "future", UsageType.RESUMED)))
        assertTrue(result.isEmpty())
    }

    @Test
    fun aRepeatedResumeDoesNotStartASecondSession() {
        val result = sessions(run(
            ev(0, "chrome", UsageType.RESUMED),
            ev(20, "chrome", UsageType.RESUMED), // same activity resumed again (missing pause)
            ev(40, "chrome", UsageType.PAUSED),
        ))
        assertEquals(listOf(40L), result.map(::seconds))
    }

    @Test
    fun shuttingDownEndsEverything() {
        val result = run(ev(0, "maps", UsageType.RESUMED), ev(300, "android", UsageType.SHUTDOWN, null))
        assertEquals(listOf(300L), sessions(result).map(::seconds))
        assertTrue(result.state.open.isEmpty())
    }

    @Test
    fun todaysSummaryClipsSessionsToToday() {
        val now = System.currentTimeMillis()
        val events = listOf(
            PhoneEvent("app_session", "usagestats", now - 60_000, now - 30_000, "Instagram", "ig"),
            PhoneEvent("app_session", "usagestats", now - 20_000, now - 10_000, "YouTube", "yt"),
            PhoneEvent("app_session", "usagestats", now - 5 * 86_400_000L, now - 5 * 86_400_000L + 60_000, "Old", "old"),
            PhoneEvent("screen_on", "usagestats", now - 1000),
        )
        val summary = TodaySummary.from(events, now)
        assertEquals(40_000L, summary.totalMs)
        assertEquals(listOf("Instagram" to 30_000L, "YouTube" to 10_000L), summary.topApps)
    }
}
