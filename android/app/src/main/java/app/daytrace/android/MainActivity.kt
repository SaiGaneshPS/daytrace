// DT-19 / DT-22 / DT-58: entry activity. Onboarding until usage access is granted, then the status screen, which opens
// pairing with the hub. Once paired, the dashboard's tabs, with the status screen under More, This phone.
package app.daytrace.android

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import app.daytrace.android.sync.PairingStore
import app.daytrace.android.ui.AppShell
import app.daytrace.android.ui.HealthRationaleScreen
import app.daytrace.android.ui.OnboardingScreen
import app.daytrace.android.ui.PairingScreen
import app.daytrace.android.ui.StatusScreen
import app.daytrace.android.ui.onboarded
import app.daytrace.android.ui.rememberPermissionRequester
import app.daytrace.android.ui.rememberPermissionStates
import app.daytrace.android.ui.setOnboarded
import app.daytrace.android.ui.theme.DaytraceTheme
import kotlinx.coroutines.delay

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
    var showPairing by rememberSaveable { mutableStateOf(false) }
    val (states, refresh) = rememberPermissionStates()
    val grant = rememberPermissionRequester(onChanged = refresh)
    // Reopened from the status screen: Back returns there instead of closing the app.
    BackHandler(enabled = showOnboarding && onboarded(context)) { showOnboarding = false }
    BackHandler(enabled = showPairing && !showOnboarding) { showPairing = false }
    // Paired or not, looked at every couple of seconds while the app is in front ("Forget this hub" changes it).
    var paired by remember { mutableStateOf(PairingStore.get(context).info() != null) }
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    LaunchedEffect(lifecycle) {
        lifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
            while (true) {
                paired = PairingStore.get(context).info() != null
                delay(2_000)
            }
        }
    }
    val phone: @Composable () -> Unit = {
        StatusScreen(states, grant, onShowOnboarding = { showOnboarding = true }, onPair = { showPairing = true })
    }
    when {
        showOnboarding -> OnboardingScreen(states, grant, onContinue = {
            setOnboarded(context, true)
            showOnboarding = false
        })
        showPairing -> PairingScreen(onClose = {
            showPairing = false
            paired = PairingStore.get(context).info() != null
        })
        paired -> AppShell(phoneScreen = phone, onPairAgain = { showPairing = true })
        else -> phone()
    }
}
