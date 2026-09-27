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

    // DT-23: reading health in the background is offered, never needed for the step.
    private val background = "android.permission.health.READ_HEALTH_DATA_IN_BACKGROUND"

    @Test
    fun theHealthStepIsDoneWithSleepStepsAndNutrition() {
        val sleepOnly = setOf("android.permission.health.READ_SLEEP")
        assertEquals(StepState(Step.HEALTH, Status.NEEDED), Permissions.healthStep(sleepOnly, background))
        assertEquals(StepState(Step.HEALTH, Status.GRANTED), Permissions.healthStep(Permissions.HEALTH + background, background))
        assertEquals(StepState(Step.HEALTH, Status.GRANTED), Permissions.healthStep(Permissions.HEALTH, background = null)) // not offered
    }

    @Test
    fun withoutBackgroundReadsTheHealthStepIsDoneAndSaysWhatThatMeans() {
        val step = Permissions.healthStep(Permissions.HEALTH, background)
        assertEquals(Status.GRANTED, step.status)
        assertEquals(Permissions.BACKGROUND_NOTE, step.note)
        assertTrue(allPossibleGranted(states(*Step.entries.toTypedArray()).map { if (it.step == Step.HEALTH) step else it }))
    }
}
