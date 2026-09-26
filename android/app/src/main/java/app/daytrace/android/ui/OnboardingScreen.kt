// DT-19: permission onboarding, and the one place that knows how to check and request each permission.
package app.daytrace.android.ui

import android.Manifest
import android.app.AppOpsManager
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Process
import android.provider.Settings
import androidx.activity.compose.LocalActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.BarChart
import androidx.compose.material.icons.rounded.CalendarMonth
import androidx.compose.material.icons.rounded.Favorite
import androidx.compose.material.icons.rounded.NotificationsActive
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.core.app.ActivityCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.core.content.edit
import androidx.core.net.toUri
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.PermissionController
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.NutritionRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.lifecycle.compose.LifecycleResumeEffect
import app.daytrace.android.ui.theme.Blush
import app.daytrace.android.ui.theme.LocalDaytraceExtras
import app.daytrace.android.ui.theme.Mint
import app.daytrace.android.ui.theme.PillText
import app.daytrace.android.ui.theme.Sky
import app.daytrace.android.ui.theme.Sunrise
import kotlinx.coroutines.launch

// --- the permission model -------------------------------------------------------------------------------------

enum class Step(val title: String, val why: String, val required: Boolean, val icon: ImageVector, val tint: Color) {
    USAGE(
        "Usage access",
        "Which apps you used and for how long. This is Daytrace's core: without it there is nothing to show.",
        required = true,
        icon = Icons.Rounded.BarChart,
        tint = Sky,
    ),
    NOTIFICATIONS(
        "Notifications",
        "Gentle nudges when a study block is slipping, and a small notice while live mode runs.",
        required = false,
        icon = Icons.Rounded.NotificationsActive,
        tint = Sunrise,
    ),
    CALENDAR(
        "Calendar",
        "Today's events, so your timeline can show the plan next to what really happened.",
        required = false,
        icon = Icons.Rounded.CalendarMonth,
        tint = Mint,
    ),
    HEALTH(
        "Health Connect",
        "Last night's sleep, your steps and meals, from Samsung Health or any app that shares with Health Connect.",
        required = false,
        icon = Icons.Rounded.Favorite,
        tint = Blush,
    ),
}

enum class Status { GRANTED, NEEDED, INSTALL, UNAVAILABLE, CHECKING }

data class StepState(val step: Step, val status: Status)

/** Onboarding can finish once every required step is granted; the optional ones can wait. */
fun readyToContinue(states: List<StepState>): Boolean =
    Step.entries.filter { it.required }.all { step -> states.any { it.step == step && it.status == Status.GRANTED } }

fun grantedCount(states: List<StepState>): Int = states.count { it.status == Status.GRANTED }

object Permissions {
    val HEALTH: Set<String> = setOf(
        HealthPermission.getReadPermission(SleepSessionRecord::class),
        HealthPermission.getReadPermission(StepsRecord::class),
        HealthPermission.getReadPermission(NutritionRecord::class),
    )
    private const val HEALTH_CONNECT_PACKAGE = "com.google.android.apps.healthdata"
    private const val PREFS = "daytrace"

    fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    @Suppress("DEPRECATION") // unsafeCheckOpNoThrow is the call that works on every version from Android 10 up
    fun usageGranted(context: Context): Boolean {
        val appOps = context.getSystemService(AppOpsManager::class.java)
        val mode = appOps.unsafeCheckOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), context.packageName)
        return if (mode == AppOpsManager.MODE_DEFAULT) {
            context.checkCallingOrSelfPermission(Manifest.permission.PACKAGE_USAGE_STATS) == PackageManager.PERMISSION_GRANTED
        } else {
            mode == AppOpsManager.MODE_ALLOWED
        }
    }

    fun notificationsGranted(context: Context): Boolean = NotificationManagerCompat.from(context).areNotificationsEnabled()

    fun calendarGranted(context: Context): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.READ_CALENDAR) == PackageManager.PERMISSION_GRANTED

    suspend fun healthStatus(context: Context): Status =
        when (HealthConnectClient.getSdkStatus(context, HEALTH_CONNECT_PACKAGE)) {
            HealthConnectClient.SDK_AVAILABLE -> {
                val granted = HealthConnectClient.getOrCreate(context).permissionController.getGrantedPermissions()
                if (granted.containsAll(HEALTH)) Status.GRANTED else Status.NEEDED
            }
            HealthConnectClient.SDK_UNAVAILABLE_PROVIDER_UPDATE_REQUIRED -> Status.INSTALL
            else -> Status.UNAVAILABLE
        }

    /** Everything that can be checked without waiting (Health Connect is filled in by [snapshot]). */
    fun quickSnapshot(context: Context, health: Status = Status.CHECKING): List<StepState> = listOf(
        StepState(Step.USAGE, if (usageGranted(context)) Status.GRANTED else Status.NEEDED),
        StepState(Step.NOTIFICATIONS, if (notificationsGranted(context)) Status.GRANTED else Status.NEEDED),
        StepState(Step.CALENDAR, if (calendarGranted(context)) Status.GRANTED else Status.NEEDED),
        StepState(Step.HEALTH, health),
    )

    suspend fun snapshot(context: Context): List<StepState> = quickSnapshot(context, healthStatus(context))

    fun usageAccessIntent(context: Context): Intent =
        Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS, "package:${context.packageName}".toUri())

    fun appSettingsIntent(context: Context): Intent =
        Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, "package:${context.packageName}".toUri())

    fun notificationSettingsIntent(context: Context): Intent =
        Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)

    fun installHealthConnectIntent(): Intent =
        Intent(Intent.ACTION_VIEW, "market://details?id=$HEALTH_CONNECT_PACKAGE&url=healthconnect%3A%2F%2Fonboarding".toUri())

    fun manageHealthIntent(context: Context): Intent =
        HealthConnectClient.getHealthConnectManageDataIntent(context, HEALTH_CONNECT_PACKAGE)
}

private fun Context.startSafely(vararg intents: Intent) {
    for (intent in intents) {
        try {
            startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            return
        } catch (_: ActivityNotFoundException) {
            // try the next, more general screen
        }
    }
}

/** The current state of every step, refreshed each time the app comes back to the front (after Settings). */
@Composable
fun rememberPermissionStates(): Pair<List<StepState>, () -> Unit> {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var states by remember { mutableStateOf(Permissions.quickSnapshot(context)) }
    val refresh: () -> Unit = { scope.launch { states = Permissions.snapshot(context) } }
    LifecycleResumeEffect(Unit) {
        refresh()
        onPauseOrDispose { }
    }
    return states to refresh
}

/**
 * What tapping a step does. Runtime permissions ask with the system dialog first; once Android stops showing
 * it (the user said no, twice or with "don't ask again"), the button opens the app's settings instead.
 */
@Composable
fun rememberPermissionRequester(onChanged: () -> Unit): (StepState) -> Unit {
    val context = LocalContext.current
    val activity = LocalActivity.current
    val prefs = remember { Permissions.prefs(context) }
    val notificationLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { onChanged() }
    val calendarLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { onChanged() }
    val healthLauncher = rememberLauncherForActivityResult(PermissionController.createRequestPermissionResultContract()) {
        prefs.edit { putInt("health_requests", prefs.getInt("health_requests", 0) + 1) }
        onChanged()
    }

    fun askOrOpenSettings(permission: String, launch: () -> Unit, settings: Intent) {
        val asked = prefs.getBoolean("asked_$permission", false)
        val canAsk = !asked || (activity != null && ActivityCompat.shouldShowRequestPermissionRationale(activity, permission))
        if (canAsk) {
            prefs.edit { putBoolean("asked_$permission", true) }
            launch()
        } else {
            context.startSafely(settings, Permissions.appSettingsIntent(context))
        }
    }

    return { state ->
        when (state.step) {
            Step.USAGE -> context.startSafely(Permissions.usageAccessIntent(context), Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS))
            Step.NOTIFICATIONS ->
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    askOrOpenSettings(
                        Manifest.permission.POST_NOTIFICATIONS,
                        { notificationLauncher.launch(Manifest.permission.POST_NOTIFICATIONS) },
                        Permissions.notificationSettingsIntent(context),
                    )
                } else {
                    context.startSafely(Permissions.notificationSettingsIntent(context), Permissions.appSettingsIntent(context))
                }
            Step.CALENDAR -> askOrOpenSettings(
                Manifest.permission.READ_CALENDAR,
                { calendarLauncher.launch(Manifest.permission.READ_CALENDAR) },
                Permissions.appSettingsIntent(context),
            )
            Step.HEALTH -> when (state.status) {
                Status.INSTALL -> context.startSafely(
                    Permissions.installHealthConnectIntent(),
                    Intent(Intent.ACTION_VIEW, "https://play.google.com/store/apps/details?id=com.google.android.apps.healthdata".toUri()),
                )
                // Health Connect stops showing its dialog after two refusals; then only its own settings can grant.
                else -> if (prefs.getInt("health_requests", 0) < 2) {
                    healthLauncher.launch(Permissions.HEALTH)
                } else {
                    context.startSafely(Permissions.manageHealthIntent(context))
                }
            }
        }
    }
}

// --- the screen -----------------------------------------------------------------------------------------------

@Composable
fun OnboardingScreen(states: List<StepState>, onGrant: (StepState) -> Unit, onContinue: () -> Unit) {
    val ready = readyToContinue(states)
    Surface(color = MaterialTheme.colorScheme.background, modifier = Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize()) {
            GradientHeader(
                title = "Welcome to Daytrace",
                subtitle = "See where your day goes, across your phone and computers. Everything stays on your own Wi-Fi.",
                progress = grantedCount(states) / Step.entries.size.toFloat(),
                progressLabel = "${grantedCount(states)} of ${Step.entries.size} set up",
            )
            LazyColumn(
                modifier = Modifier.weight(1f),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                items(states, key = { it.step.name }) { state -> StepCard(state, onGrant) }
            }
            Column(
                Modifier.fillMaxWidth().navigationBarsPadding().padding(16.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Button(onClick = onContinue, enabled = ready, modifier = Modifier.fillMaxWidth().height(52.dp)) {
                    Text(if (ready) "Let's go" else "Usage access is needed to continue")
                }
                if (ready && grantedCount(states) < Step.entries.size) {
                    Text(
                        "The optional ones can wait. You can turn them on later from the status screen.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        textAlign = TextAlign.Center,
                        modifier = Modifier.padding(top = 8.dp),
                    )
                }
            }
        }
    }
}

@Composable
fun GradientHeader(title: String, subtitle: String, progress: Float, progressLabel: String) {
    val animated by animateFloatAsState(progress, animationSpec = spring(dampingRatio = Spring.DampingRatioMediumBouncy), label = "progress")
    Box(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(bottomStart = 32.dp, bottomEnd = 32.dp))
            .background(LocalDaytraceExtras.current.headerGradient)
            .statusBarsPadding()
            .padding(horizontal = 24.dp, vertical = 20.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(title, style = MaterialTheme.typography.headlineMedium, color = Color.White)
                Spacer(Modifier.height(6.dp))
                Text(subtitle, style = MaterialTheme.typography.bodyMedium, color = Color.White.copy(alpha = 0.9f))
                Spacer(Modifier.height(10.dp))
                Text(progressLabel, style = PillText, color = Color.White)
            }
            Spacer(Modifier.size(16.dp))
            ProgressRing(animated, Modifier.size(64.dp))
        }
    }
}

@Composable
private fun ProgressRing(progress: Float, modifier: Modifier = Modifier) {
    Canvas(modifier) {
        val stroke = 8.dp.toPx()
        val arc = Size(size.width - stroke, size.height - stroke)
        val topLeft = Offset(stroke / 2, stroke / 2)
        drawArc(Color.White.copy(alpha = 0.25f), 0f, 360f, false, topLeft, arc, style = Stroke(stroke))
        drawArc(Color.White, -90f, 360f * progress, false, topLeft, arc, style = Stroke(stroke, cap = StrokeCap.Round))
    }
}

@Composable
fun StepCard(state: StepState, onGrant: (StepState) -> Unit) {
    val step = state.step
    Card(
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 2.dp),
    ) {
        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(
                Modifier.size(44.dp).clip(CircleShape).background(step.tint.copy(alpha = 0.18f)),
                contentAlignment = Alignment.Center,
            ) {
                Icon(step.icon, contentDescription = null, tint = step.tint)
            }
            Spacer(Modifier.size(14.dp))
            Column(Modifier.weight(1f)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(step.title, style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f, fill = false))
                    Spacer(Modifier.size(8.dp))
                    StatusPill(state)
                }
                Spacer(Modifier.height(4.dp))
                Text(step.why, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                if (state.status == Status.NEEDED || state.status == Status.INSTALL) {
                    Spacer(Modifier.height(8.dp))
                    FilledTonalButton(onClick = { onGrant(state) }) {
                        Text(if (state.status == Status.INSTALL) "Install Health Connect" else "Allow")
                    }
                }
            }
        }
    }
}

@Composable
fun StatusPill(state: StepState) {
    val extras = LocalDaytraceExtras.current
    val (label, color) = when (state.status) {
        Status.GRANTED -> "On" to extras.granted
        Status.NEEDED -> (if (state.step.required) "Needed" else "Optional") to
            (if (state.step.required) extras.needed else MaterialTheme.colorScheme.onSurfaceVariant)
        Status.INSTALL -> "Install" to MaterialTheme.colorScheme.onSurfaceVariant
        Status.UNAVAILABLE -> "Not on this phone" to MaterialTheme.colorScheme.onSurfaceVariant
        Status.CHECKING -> "Checking" to MaterialTheme.colorScheme.onSurfaceVariant
    }
    val animated by animateColorAsState(color, animationSpec = tween(400), label = "pill")
    Text(
        label,
        style = PillText,
        color = animated,
        modifier = Modifier.clip(RoundedCornerShape(50)).background(animated.copy(alpha = 0.14f)).padding(horizontal = 10.dp, vertical = 4.dp),
    )
}

/** Shown when Health Connect asks why Daytrace wants health data (its privacy-policy link opens this). */
@Composable
fun HealthRationaleScreen(onClose: () -> Unit) {
    Surface(color = MaterialTheme.colorScheme.background, modifier = Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize().statusBarsPadding().navigationBarsPadding().padding(24.dp)) {
            Text("Why Daytrace reads health data", style = MaterialTheme.typography.headlineMedium)
            Spacer(Modifier.height(16.dp))
            Text(
                "Daytrace reads your sleep sessions, daily step count and logged meals so your own hub can show them on " +
                    "your timeline next to your screen time. It only reads; it never writes to Health Connect.\n\n" +
                    "The data goes to the Daytrace hub on your own network and nowhere else: no cloud, no accounts, no " +
                    "ads, no analytics. You can turn this off at any time in Health Connect.",
                style = MaterialTheme.typography.bodyLarge,
            )
            Spacer(Modifier.weight(1f))
            TextButton(onClick = onClose, modifier = Modifier.align(Alignment.End)) { Text("Close") }
        }
    }
}

/** Whether onboarding was finished, so the app opens on the status screen next time. */
fun onboarded(context: Context): Boolean = Permissions.prefs(context).getBoolean("onboarding_done", false)

fun setOnboarded(context: Context, done: Boolean) {
    Permissions.prefs(context).edit { putBoolean("onboarding_done", done) }
}
