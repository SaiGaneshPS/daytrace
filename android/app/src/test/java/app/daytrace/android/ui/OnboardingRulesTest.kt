// DT-19: onboarding can finish once the required steps are granted; the optional ones can wait.
package app.daytrace.android.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class OnboardingRulesTest {
    private fun states(vararg granted: Step): List<StepState> =
        Step.entries.map { StepState(it, if (it in granted) Status.GRANTED else Status.NEEDED) }

    @Test
    fun usageAccessIsTheOnlyRequiredStep() {
        assertEquals(listOf(Step.USAGE), Step.entries.filter { it.required })
    }

    @Test
    fun onboardingNeedsUsageAccess() {
        assertFalse(readyToContinue(states()))
        assertFalse(readyToContinue(states(Step.NOTIFICATIONS, Step.CALENDAR, Step.HEALTH)))
        assertTrue(readyToContinue(states(Step.USAGE)))
    }

    @Test
    fun healthConnectMissingFromThePhoneDoesNotBlockOnboarding() {
        val withoutHealth = states(Step.USAGE).map { if (it.step == Step.HEALTH) it.copy(status = Status.UNAVAILABLE) else it }
        assertTrue(readyToContinue(withoutHealth))
        assertEquals(1, grantedCount(withoutHealth))
    }

    @Test
    fun aMissingStepIsNotTreatedAsGranted() {
        assertFalse(readyToContinue(emptyList()))
    }
}
