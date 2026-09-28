// DT-58: the widget shows the hub's numbers as the hub gave them: today's time, the longest streak going, the goal.
package app.daytrace.android.widget

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class WidgetRulesTest {
    private val day = JSONObject("""{"screen_minutes": 419.37, "phone_minutes": 41.67, "computer_minutes": 377.7}""")
    private val streaks = JSONObject(
        """{"streaks": [
            {"id": "focus_flame", "name": "Focus flame", "current": 3, "today": "met"},
            {"id": "logged_it", "name": "Logged it", "current": 14, "today": "met"},
            {"id": "balanced", "name": "Balanced", "current": 2, "today": "at_risk"}]}""",
    )
    private val goals = JSONObject(
        """{"goals": [
            {"id": "focus_target", "label": "Focused time", "unit": "minutes", "target": 240, "today": {"value": 336.4, "status": "met", "progress": 100}},
            {"id": "bedtime", "label": "Bedtime", "unit": "time", "target": "23:30", "today": {"value": "23:25", "status": "met", "progress": 97}}]}""",
    )

    @Test
    fun theWidgetShowsWhatTheHubSaid() {
        val numbers = WidgetRules.numbers(day, streaks, goals, nowMs = 5)
        assertEquals(WidgetNumbers(419, 42, 378, "Logged it", 14, "met", "Focused time", 100, "5h 36m of 4h", 5), numbers)
        assertEquals(numbers, WidgetNumbers.fromJson(numbers.toJson())) // kept between refreshes
    }

    @Test
    fun withNothingYetItSaysSo() {
        val empty = WidgetRules.numbers(
            JSONObject("""{"screen_minutes": null, "phone_minutes": null, "computer_minutes": null}"""),
            JSONObject("""{"streaks": [{"name": "Focus flame", "current": 0, "today": "no_data"}]}"""),
            JSONObject("""{"goals": [{"label": "Focused time", "unit": "minutes", "target": 240, "today": {"value": null, "status": "no_data", "progress": null}}]}"""),
            nowMs = 5,
        )
        assertNull(empty.screenMinutes)
        assertNull(empty.streakName) // no run going: no flame
        assertNull(empty.goalProgress)
        assertNull(empty.goalText)
        assertEquals(empty, WidgetNumbers.fromJson(empty.toJson()))
    }

    @Test
    fun durationsReadLikeTheDashboards() {
        assertEquals("6h 59m", WidgetRules.duration(419))
        assertEquals("42m", WidgetRules.duration(42))
        assertEquals("4h", WidgetRules.duration(240)) // whole hours as the dashboard writes them
    }
}
