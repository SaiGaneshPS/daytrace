// DT-20: the usage rules: exact sessions, screen on/off, and nothing lost or doubled across checkpoints.
package app.daytrace.android.usage

import app.daytrace.android.data.PhoneEvent
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDateTime
import java.time.ZoneId

class UsageSessionizerTest {
    private val start = 1_000_000L
    private fun at(seconds: Int, millis: Int = 0) = start + seconds * 1000L + millis
    private fun ev(seconds: Int, pkg: String, type: UsageType, cls: String? = "Main", millis: Int = 0) =
        RawUsageEvent(at(seconds, millis), pkg, cls, type)
    private fun run(vararg raw: RawUsageEvent, until: Int = 3600, state: UsageState = UsageState(start), ignored: Set<String> = setOf("launcher")) =
        UsageSessionizer.collect(raw.toList(), state, at(until), ignored) { pkg -> pkg.replaceFirstChar { it.uppercase() } }
    private fun sessions(collected: Collected) = collected.events.filter { it.kind == "app_session" }
    private fun seconds(event: PhoneEvent) = (event.endMs!! - event.startMs) / 1000.0

    @Test
    fun twoMinutesInAnAppIsOneTwoMinuteSession() {
        val result = sessions(run(ev(10, "instagram", UsageType.RESUMED), ev(130, "instagram", UsageType.PAUSED)))
        assertEquals(1, result.size)
        assertEquals(120.0, seconds(result[0]), 0.0)
        assertEquals("Instagram", result[0].app)
        assertEquals("instagram", result[0].appId)
        assertEquals("usagestats", result[0].source)
    }

    @Test
    fun movingBetweenScreensStaysOneSessionWhicheverOrderAndroidLogsThem() {
        val resumeFirst = sessions(run(
            ev(0, "whatsapp", UsageType.RESUMED, "Chats"),
            ev(30, "whatsapp", UsageType.RESUMED, "Conversation"),
            ev(31, "whatsapp", UsageType.PAUSED, "Chats"),
            ev(90, "whatsapp", UsageType.PAUSED, "Conversation"),
        ))
        val pauseFirst = sessions(run( // the usual order: the old screen pauses 11 ms before the new one resumes
            ev(0, "whatsapp", UsageType.RESUMED, "Chats"),
            ev(30, "whatsapp", UsageType.PAUSED, "Chats"),
            ev(30, "whatsapp", UsageType.RESUMED, "Conversation", millis = 11),
            ev(90, "whatsapp", UsageType.PAUSED, "Conversation"),
        ))
        assertEquals(listOf(90.0), resumeFirst.map(::seconds))
        assertEquals(listOf(90.0), pauseFirst.map(::seconds))
    }

    @Test
    fun comingBackToAnAppLaterIsANewSession() {
        val result = sessions(run(
            ev(0, "maps", UsageType.RESUMED), ev(10, "maps", UsageType.PAUSED),
            ev(15, "maps", UsageType.RESUMED), ev(25, "maps", UsageType.PAUSED),
        ))
        assertEquals(listOf(10.0, 10.0), result.map(::seconds))
    }

    @Test
    fun switchingAppsEndsOneAndStartsTheOther() {
        val result = sessions(run(
            ev(0, "youtube", UsageType.RESUMED),
            ev(60, "youtube", UsageType.PAUSED),
            ev(60, "tiktok", UsageType.RESUMED, millis = 5),
            ev(200, "tiktok", UsageType.PAUSED),
        ))
        assertEquals(listOf(at(0) to at(60), at(60, 5) to at(200)), result.map { it.startMs to it.endMs })
    }

    @Test
    fun anAppKilledWhileOnScreenEndsWhenItStops() {
        // force-stop, a crash or an update logs ACTIVITY_STOPPED without ACTIVITY_PAUSED
        val result = sessions(run(ev(0, "daytrace", UsageType.RESUMED), ev(100, "daytrace", UsageType.STOPPED)))
        assertEquals(listOf(100.0), result.map(::seconds))
    }

    @Test
    fun theUsualStopAfterAPauseDoesNotMoveTheEnd() {
        val result = sessions(run(
            ev(0, "chrome", UsageType.RESUMED),
            ev(60, "chrome", UsageType.PAUSED),
            ev(61, "chrome", UsageType.STOPPED),
        ))
        assertEquals(listOf(60.0), result.map(::seconds))
    }

    @Test
    fun aNightWithTheScreenOffGivesAScreenOffAndOnPair() {
        val result = run(
            ev(0, "instagram", UsageType.RESUMED),
            ev(100, "android", UsageType.SCREEN_OFF, null), // no paused event before the screen went off
            ev(3000, "android", UsageType.SCREEN_ON, null),
        )
        assertEquals(listOf("app_session", "screen_off", "screen_on"), result.events.map { it.kind })
        assertEquals(100.0, seconds(sessions(result)[0]), 0.0)
        assertTrue(result.state.open.isEmpty())
    }

    @Test
    fun anAppOpenAtTheCheckpointContinuesWithoutGapOrOverlap() {
        val first = run(ev(3000, "youtube", UsageType.RESUMED), until = 3600)
        assertEquals(listOf(at(3000) to at(3600)), sessions(first).map { it.startMs to it.endMs })
        assertEquals(mapOf("youtube" to OpenApp(at(3600), setOf("Main"))), first.state.open)

        val second = UsageSessionizer.collect(listOf(ev(3900, "youtube", UsageType.PAUSED)), first.state, at(7200), emptySet()) { it }
        assertEquals(listOf(at(3600) to at(3900)), sessions(second).map { it.startMs to it.endMs })
        val total = (sessions(first) + sessions(second)).sumOf { it.endMs!! - it.startMs }
        assertEquals(900_000L, total) // 3000 s to 3900 s, counted exactly once
    }

    @Test
    fun anAppWithTwoScreensOpenAtTheCheckpointEndsWhenTheLastPauses() {
        val state = UsageState(start, mapOf("whatsapp" to OpenApp(start, setOf("Chats", "Conversation"))))
        val result = sessions(run(
            ev(10, "whatsapp", UsageType.PAUSED, "Chats"),
            ev(20, "whatsapp", UsageType.PAUSED, "Conversation"),
            state = state,
        ))
        assertEquals(listOf(20.0), result.map(::seconds))
    }

    @Test
    fun aRestartNeverCountsTheTimeThePhoneWasOff() {
        val state = UsageState(start, mapOf("daytrace" to OpenApp(start, setOf("Main"))))
        val result = sessions(run(
            ev(900, "android", UsageType.STARTUP, null), // the phone froze and was restarted: no pause, no shutdown
            ev(960, "maps", UsageType.RESUMED),
            ev(1020, "maps", UsageType.PAUSED),
            state = state,
        ))
        assertEquals(listOf("maps" to 60.0), result.map { it.appId to seconds(it) })
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
        assertEquals(listOf(40.0), result.map(::seconds))
    }

    @Test
    fun shuttingDownEndsEverything() {
        val result = run(ev(0, "maps", UsageType.RESUMED), ev(300, "android", UsageType.SHUTDOWN, null))
        assertEquals(listOf(300.0), sessions(result).map(::seconds))
        assertTrue(result.state.open.isEmpty())
    }

    // --- clock changes ---

    @Test
    fun aClockSetForwardShiftsTheCheckpointWithAndroidsEvents() {
        val state = UsageState(checkpointMs = 10_000_000, open = mapOf("yt" to OpenApp(10_000_000)), checkpointUptimeMs = 500_000)
        val corrected = ClockCheck.correct(state, nowWallMs = 10_060_000 + 30_000, nowUptimeMs = 560_000) // 60 s later, clock +30 s
        assertEquals(10_030_000L, corrected.checkpointMs)
        assertEquals(10_030_000L, corrected.open.getValue("yt").startMs)
    }

    @Test
    fun aClockSetBackShiftsTheCheckpointBack() {
        val state = UsageState(checkpointMs = 10_000_000, checkpointUptimeMs = 500_000)
        val corrected = ClockCheck.correct(state, nowWallMs = 10_600_000 - 300_000, nowUptimeMs = 1_100_000) // 10 min later, -5 min
        assertEquals(9_700_000L, corrected.checkpointMs)
    }

    @Test
    fun smallDriftAndRestartsAreLeftAlone() {
        val state = UsageState(checkpointMs = 10_000_000, checkpointUptimeMs = 500_000)
        assertEquals(state, ClockCheck.correct(state, nowWallMs = 10_061_500, nowUptimeMs = 560_000)) // 1.5 s drift
        assertEquals(state, ClockCheck.correct(state, nowWallMs = 99_000_000, nowUptimeMs = 1_000)) // uptime restarted
        assertEquals(UsageState(5), ClockCheck.correct(UsageState(5), 99_000_000, 1_000)) // first run: no uptime saved
    }

    // --- today's summary ---

    @Test
    fun todaysSummaryClipsSessionsToToday() {
        val zone = ZoneId.of("America/St_Johns")
        val now = LocalDateTime.of(2026, 9, 26, 0, 0, 30).atZone(zone).toInstant().toEpochMilli() // 30 s after midnight
        val events = listOf(
            PhoneEvent("app_session", "usagestats", now - 60_000, now - 10_000, "Instagram", "ig"), // 20 s of it is today
            PhoneEvent("app_session", "usagestats", now - 5_000, now - 1_000, "YouTube", "yt"),
            PhoneEvent("screen_on", "usagestats", now - 1_000),
        )
        val summary = TodaySummary.from(events, now, zone)
        assertEquals(24_000L, summary.totalMs)
        assertEquals(listOf("Instagram" to 20_000L, "YouTube" to 4_000L), summary.topApps)
    }

    @Test
    fun splitScreenTimeIsNotCountedTwice() {
        val zone = ZoneId.of("UTC")
        val now = LocalDateTime.of(2026, 9, 26, 12, 0).atZone(zone).toInstant().toEpochMilli()
        val half = 30 * 60_000L
        val events = listOf(
            PhoneEvent("app_session", "usagestats", now - 2 * half, now - half, "YouTube", "yt"),
            PhoneEvent("app_session", "usagestats", now - 2 * half + 60_000, now - half, "Chrome", "chrome"), // side by side
        )
        val summary = TodaySummary.from(events, now, zone)
        assertEquals(half, summary.totalMs)
        assertEquals(listOf("Chrome" to half - 60_000, "YouTube" to 60_000L), summary.topApps) // the newer app owns the screen
    }
}
