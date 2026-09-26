// DT-19: entry activity. Onboarding until usage access is granted, then the status screen.
package app.daytrace.android

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import app.daytrace.android.ui.HealthRationaleScreen
import app.daytrace.android.ui.OnboardingScreen
import app.daytrace.android.ui.StatusScreen
import app.daytrace.android.ui.onboarded
import app.daytrace.android.ui.rememberPermissionRequester
import app.daytrace.android.ui.rememberPermissionStates
import app.daytrace.android.ui.setOnboarded
import app.daytrace.android.ui.theme.DaytraceTheme

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val showHealthRationale = intent?.action in HEALTH_RATIONALE_ACTIONS
        setContent {
            DaytraceTheme {
                if (showHealthRationale) HealthRationaleScreen(onClose = ::finish) else DaytraceRoot()
            }
        }
    }

    private companion object {
        val HEALTH_RATIONALE_ACTIONS = setOf(
            "androidx.health.ACTION_SHOW_PERMISSIONS_RATIONALE",
            Intent.ACTION_VIEW_PERMISSION_USAGE,
        )
    }
}

@Composable
private fun DaytraceRoot() {
    val context = LocalContext.current
    var showOnboarding by rememberSaveable { mutableStateOf(!onboarded(context)) }
    val (states, refresh) = rememberPermissionStates()
    val grant = rememberPermissionRequester(onChanged = refresh)
    // Reopened from the status screen: Back returns there instead of closing the app.
    BackHandler(enabled = showOnboarding && onboarded(context)) { showOnboarding = false }
    if (showOnboarding) {
        OnboardingScreen(states, grant, onContinue = {
            setOnboarded(context, true)
            showOnboarding = false
        })
    } else {
        StatusScreen(states, grant, onShowOnboarding = { showOnboarding = true })
    }
}
