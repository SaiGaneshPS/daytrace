// DT-24: the hub's nudge on the phone: read from the reply, shown once in the app's colors, and never without
// notifications allowed.
package app.daytrace.android.nudge

import android.Manifest
import android.app.Application
import android.app.NotificationManager
import android.content.Context
import androidx.core.app.NotificationCompat
import androidx.test.core.app.ApplicationProvider
import app.daytrace.android.sync.HubClient
import app.daytrace.android.sync.Nudge
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class NudgeNotifierTest {
    private val context: Application = ApplicationProvider.getApplicationContext()
    private val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
    private val nudge = Nudge("focus_block", "Time to focus", "TikTok during \"Study: statistics\", which runs until 20:52.", "2026-09-27T22:37:30Z")

    @Before
    fun allow() {
        context.getSharedPreferences("nudges", 0).edit().clear().commit()
        shadowOf(context).grantPermissions(Manifest.permission.POST_NOTIFICATIONS)
        shadowOf(manager).setNotificationsEnabled(true)
    }

    @Test
    fun theHubsNudgeIsReadFromTheReplyAndCleaned() {
        val rlo = String(Character.toChars(0x202E))
        val reply = JSONObject("""{"accepted": 1, "rejected": [], "nudge": {"rule": "late_scroll", "title": "Past your ${rlo}bedtime", "body": "It's 23:50.", "created_at": "2026-09-27T02:20:00Z"}}""")
        assertEquals(Nudge("late_scroll", "Past your bedtime", "It's 23:50.", "2026-09-27T02:20:00Z"), HubClient.nudgeOf(reply))
        assertNull(HubClient.nudgeOf(JSONObject("""{"accepted": 1, "nudge": null}""")))
        assertNull(HubClient.nudgeOf(JSONObject("""{"accepted": 1, "nudge": {"rule": "x"}}"""))) // unreadable: dropped, not the reply
    }

    @Test
    fun aNudgeIsShownOnceInTheAppsColors() {
        assertTrue(NudgeNotifier(context).show(nudge))
        val shown = shadowOf(manager).allNotifications.single()
        assertEquals("Time to focus", shown.extras.getString(NotificationCompat.EXTRA_TITLE))
        assertEquals(nudge.body, shown.extras.getCharSequence(NotificationCompat.EXTRA_BIG_TEXT).toString())
        assertEquals(NudgeNotifier.ACCENT, shown.color)
        assertEquals(NudgeNotifier.CHANNEL, shown.channelId)
        assertFalse(NudgeNotifier(context).show(nudge)) // the same nudge in a second reply
        assertEquals(1, shadowOf(manager).allNotifications.size)
        assertTrue(NudgeNotifier(context).show(nudge.copy(createdAt = "2026-09-27T23:00:00Z"))) // a later one shows
    }

    @Test
    fun withoutNotificationsNothingIsShown() {
        shadowOf(manager).setNotificationsEnabled(false)
        assertFalse(NudgeNotifier(context).show(nudge))
        shadowOf(manager).setNotificationsEnabled(true)
        assertTrue(NudgeNotifier(context).show(nudge)) // not marked as shown while it couldn't be
    }

    @Test
    fun onlyTheLastFewAreRemembered() {
        var shown = emptyList<String>()
        (1..25).forEach { shown = ShownNudges.add(shown, "n$it")!! }
        assertEquals(ShownNudges.KEEP, shown.size)
        assertEquals("n25", shown.last())
        assertNull(ShownNudges.add(shown, "n25"))
        assertEquals(shown, ShownNudges.parse(shown.joinToString("\n")))
    }
}
