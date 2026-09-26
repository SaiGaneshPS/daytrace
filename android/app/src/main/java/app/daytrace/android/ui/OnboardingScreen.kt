// DT-19: permission onboarding, and the one place that knows how to check and request each permission.
package app.daytrace.android.ui

import android.Manifest
import android.app.Activity
import android.app.AppOpsManager
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Process
import android.os.SystemClock
import android.provider.Settings
import android.widget.Toast
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
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.WindowInsetsSides
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.DateRange
import androidx.compose.material.icons.rounded.Favorite
import androidx.compose.material.icons.rounded.Notifications
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
import androidx.compose.runtime.mutableLongStateOf
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
import app.daytrace.android.ui.theme.DaytraceIcons
import app.daytrace.android.ui.theme.LocalDaytraceExtras
import app.daytrace.android.ui.theme.Mint
import app.daytrace.android.ui.theme.PillText
import app.daytrace.android.ui.theme.Sky
import app.daytrace.android.ui.theme.Sunrise
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch

// --- the permission model -------------------------------------------------------------------------------------

enum class Step(val title: String, val why: String, val required: Boolean, val icon: ImageVector, val tint: Color) {
    USAGE(
        "Usage access",
        "Which apps you used and for how long. This is Daytrace's core: without it there is nothing to show.",
        required = true,
        icon = DaytraceIcons.BarChart,
        tint = Sky,
    ),
    NOTIFICATIONS(
        "Notifications",
        "Gentle nudges when a study block is slipping, and a small notice while live mode runs.",
        required = false,
        icon = Icons.Rounded.Notifications,
        tint = Sunrise,
    ),
    CALENDAR(
        "Calendar",
        "Today's events, so your timeline can show the plan next to what really happened.",
        required = false,
        icon = Icons.Rounded.DateRange,
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

/** Steps this phone can offer at all: a phone without Health Connect is fully set up without it. */
fun possibleCount(states: List<StepState>): Int = states.count { it.status != Status.UNAVAILABLE }

fun allPossibleGranted(states: List<StepState>): Boolean = grantedCount(states) == possibleCount(states)

object Permissions {
    val HEALTH: Set<String> = setOf(
        HealthPermission.getReadPermission(SleepSessionRecord::class),
        HealthPermission.getReadPermission(StepsRecord::class),
        HealthPermission.getReadPermission(NutritionRecord::class),
    )
    const val HEALTH_CONNECT_PACKAGE = "com.google.android.apps.healthdata"
    private const val PREFS = "daytrace"
    private const val HEALTH_BLOCKED = "blocked_health"

    fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun blockedKey(permission: String) = "blocked_$permission"

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

    /** Never throws: Health Connect is a separate app on Android 10 to 13 and can be updating or killed. */
    suspend fun healthStatus(context: Context): Status = try {
        when (HealthConnectClient.getSdkStatus(context, HEALTH_CONNECT_PACKAGE)) {
            HealthConnectClient.SDK_AVAILABLE -> {
                val granted = HealthConnectClient.getOrCreate(context).permissionController.getGrantedPermissions()
                if (granted.containsAll(HEALTH)) Status.GRANTED else Status.NEEDED
            }
            HealthConnectClient.SDK_UNAVAILABLE_PROVIDER_UPDATE_REQUIRED -> Status.INSTALL
            else -> Status.UNAVAILABLE
        }
    } catch (cancelled: CancellationException) {
        throw cancelled
    } catch (_: Exception) {
        Status.UNAVAILABLE // shown as "Not available"; the next refresh tries again
    }

    /** Everything that can be checked without waiting (Health Connect is filled in by [snapshot]). */
    fun quickSnapshot(context: Context, health: Status = Status.CHECKING): List<StepState> = listOf(
        StepState(Step.USAGE, if (usageGranted(context)) Status.GRANTED else Status.NEEDED),
        StepState(Step.NOTIFICATIONS, if (notificationsGranted(context)) Status.GRANTED else Status.NEEDED),
        StepState(Step.CALENDAR, if (calendarGranted(context)) Status.GRANTED else Status.NEEDED),
        StepState(Step.HEALTH, health),
    )

    suspend fun snapshot(context: Context): List<StepState> {
        val health = healthStatus(context) // read the quick ones after this, so they are the freshest
        return quickSnapshot(context, health).also { forgetBlocksOnceGranted(context, it) }
    }

    /** A permission granted in Settings is no longer "blocked", so a later reset asks with the dialog again. */
    private fun forgetBlocksOnceGranted(context: Context, states: List<StepState>) {
        val granted = states.filter { it.status == Status.GRANTED }.map { it.step }.toSet()
        prefs(context).edit {
            if (Step.NOTIFICATIONS in granted) remove(blockedKey(Manifest.permission.POST_NOTIFICATIONS))
            if (Step.CALENDAR in granted) remove(blockedKey(Manifest.permission.READ_CALENDAR))
            if (Step.HEALTH in granted) remove(HEALTH_BLOCKED)
        }
    }

    fun healthBlocked(context: Context) = prefs(context).getBoolean(HEALTH_BLOCKED, false)

    fun setHealthBlocked(context: Context, blocked: Boolean) = prefs(context).edit { putBoolean(HEALTH_BLOCKED, blocked) }

    fun usageAccessIntents(context: Context) = listOf(
        Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS, "package:${context.packageName}".toUri()),
        Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS),
    )

    fun appSettingsIntent(context: Context): Intent =
        Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, "package:${context.packageName}".toUri())

    fun notificationSettingsIntents(context: Context) = listOf(
        Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName),
        appSettingsIntent(context),
    )

    /** The Play Store itself (Samsung's Galaxy Store also answers market:// links but has no Health Connect). */
    fun installHealthConnectIntents() = listOf(
        Intent(Intent.ACTION_VIEW, "market://details?id=$HEALTH_CONNECT_PACKAGE&url=healthconnect%3A%2F%2Fonboarding".toUri())
            .setPackage("com.android.vending"),
        Intent(Intent.ACTION_VIEW, "https://play.google.com/store/apps/details?id=$HEALTH_CONNECT_PACKAGE".toUri()),
    )

    /** Daytrace's own page in Health Connect's permissions, where a blocked request can still be granted. */
    fun manageHealthPermissionsIntents(context: Context): List<Intent> = buildList {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            add(Intent("android.health.connect.action.MANAGE_HEALTH_PERMISSIONS").putExtra(Intent.EXTRA_PACKAGE_NAME, context.packageName))
        }
        add(
            Intent("androidx.health.ACTION_MANAGE_HEALTH_PERMISSIONS")
                .putExtra(Intent.EXTRA_PACKAGE_NAME, context.packageName)
                .setPackage(HEALTH_CONNECT_PACKAGE),
        )
        add(Intent(HealthConnectClient.ACTION_HEALTH_CONNECT_SETTINGS))
        add(HealthConnectClient.getHealthConnectManageDataIntent(context, HEALTH_CONNECT_PACKAGE))
    }
}

/**
 * Opens the first of these screens that exists. From an Activity the screen stacks on Daytrace's own task, so
 * Back returns here (and the resume refresh picks up the change). Tells the user if none can be opened.
 */
fun openFirst(context: Context, activity: Activity?, intents: List<Intent>) {
    for (intent in intents) {
        try {
            if (activity != null) activity.startActivity(intent) else context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            return
        } catch (_: ActivityNotFoundException) {
            // try the next, more general screen
        } catch (_: SecurityException) {
            // some phone makers protect their settings screens; try the next one
        }
    }
    Toast.makeText(context, "Couldn't open that settings screen. Please open Settings > Apps > Daytrace.", Toast.LENGTH_LONG).show()
}

/** The current state of every step, refreshed each time the app comes back to the front (after Settings). */
@Composable
fun rememberPermissionStates(): Pair<List<StepState>, () -> Unit> {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var states by remember { mutableStateOf(Permissions.quickSnapshot(context)) }
    val job = remember { arrayOfNulls<Job>(1) }
    val refresh: () -> Unit = {
        job[0]?.cancel() // a slow earlier check must never overwrite a newer answer
        job[0] = scope.launch { states = Permissions.snapshot(context) }
    }
    LifecycleResumeEffect(Unit) {
        refresh()
        onPauseOrDispose { }
    }
    return states to refresh
}

/** An answer faster than this came without a dialog: Android (or Health Connect) is blocking the request. */
private const val NO_DIALOG_MS = 400L

/**
 * What tapping a step does. It asks with the system dialog. When Android no longer shows that dialog (the user
 * chose "Don't allow" twice, or "don't ask again"), the answer comes back at once, and the tap opens the settings
 * screen where the permission can still be turned on. A dialog closed with Back is not a refusal: it asks again.
 */
@Composable
fun rememberPermissionRequester(onChanged: () -> Unit): (StepState) -> Unit {
    val context = LocalContext.current
    val activity = LocalActivity.current
    val prefs = remember { Permissions.prefs(context) }
    var askedAt by remember { mutableLongStateOf(0L) }
    fun answeredWithoutDialog() = SystemClock.elapsedRealtime() - askedAt < NO_DIALOG_MS

    fun onRuntimeResult(permission: String, granted: Boolean, settings: List<Intent>) {
        val blocked = !granted && answeredWithoutDialog()
        prefs.edit { putBoolean(Permissions.blockedKey(permission), blocked) }
        if (blocked) openFirst(context, activity, settings)
        onChanged()
    }

    val notificationLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        onRuntimeResult(Manifest.permission.POST_NOTIFICATIONS, granted, Permissions.notificationSettingsIntents(context))
    }
    val calendarLauncher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        onRuntimeResult(Manifest.permission.READ_CALENDAR, granted, listOf(Permissions.appSettingsIntent(context)))
    }
    val healthLauncher = rememberLauncherForActivityResult(PermissionController.createRequestPermissionResultContract()) { granted ->
        val blocked = !granted.containsAll(Permissions.HEALTH) && answeredWithoutDialog()
        Permissions.setHealthBlocked(context, blocked)
        if (blocked) openFirst(context, activity, Permissions.manageHealthPermissionsIntents(context))
        onChanged()
    }

    fun askOrOpen(permission: String, launch: () -> Unit, settings: List<Intent>) {
        if (prefs.getBoolean(Permissions.blockedKey(permission), false)) {
            openFirst(context, activity, settings)
        } else {
            askedAt = SystemClock.elapsedRealtime()
            launch()
        }
    }

    return { state ->
        when (state.step) {
            Step.USAGE -> openFirst(context, activity, Permissions.usageAccessIntents(context))
            Step.NOTIFICATIONS ->
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    askOrOpen(
                        Manifest.permission.POST_NOTIFICATIONS,
                        { notificationLauncher.launch(Manifest.permission.POST_NOTIFICATIONS) },
                        Permissions.notificationSettingsIntents(context),
                    )
                } else {
                    openFirst(context, activity, Permissions.notificationSettingsIntents(context)) // no dialog before 13
                }
            Step.CALENDAR -> askOrOpen(
                Manifest.permission.READ_CALENDAR,
                { calendarLauncher.launch(Manifest.permission.READ_CALENDAR) },
                listOf(Permissions.appSettingsIntent(context)),
            )
            Step.HEALTH -> when {
                state.status == Status.INSTALL -> openFirst(context, activity, Permissions.installHealthConnectIntents())
                Permissions.healthBlocked(context) -> openFirst(context, activity, Permissions.manageHealthPermissionsIntents(context))
                else -> {
                    askedAt = SystemClock.elapsedRealtime()
                    healthLauncher.launch(Permissions.HEALTH)
                }
            }
        }
    }
}

// --- the screen -----------------------------------------------------------------------------------------------

/** The whole screen scrolls (header included), so landscape and large text never squeeze the list away. */
@Composable
fun OnboardingScreen(states: List<StepState>, onGrant: (StepState) -> Unit, onContinue: () -> Unit) {
    val ready = readyToContinue(states)
    Surface(color = MaterialTheme.colorScheme.background, modifier = Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing.only(WindowInsetsSides.Horizontal))) {
            LazyColumn(
                modifier = Modifier.weight(1f),
                contentPadding = PaddingValues(bottom = 16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                item(key = "header") {
                    GradientHeader(
                        title = "Welcome to Daytrace",
                        subtitle = "See where your day goes, across your phone and computers. Everything stays on your own Wi-Fi.",
                        granted = grantedCount(states),
                        possible = possibleCount(states),
                        progressLabel = "${grantedCount(states)} of ${possibleCount(states)} set up",
                    )
                }
                items(states, key = { it.step.name }) { state ->
                    Box(Modifier.padding(horizontal = 16.dp)) { StepCard(state, onGrant) }
                }
                if (ready && !allPossibleGranted(states)) {
                    item(key = "hint") {
                        Text(
                            "The optional ones can wait. You can turn them on later from the status screen.",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            textAlign = TextAlign.Center,
                            modifier = Modifier.fillMaxWidth().padding(horizontal = 24.dp),
                        )
                    }
                }
            }
            Button(
                onClick = onContinue,
                enabled = ready,
                modifier = Modifier.fillMaxWidth().navigationBarsPadding().padding(16.dp).heightIn(min = 52.dp),
            ) {
                Text(if (ready) "Let's go" else "Usage access is needed to continue", textAlign = TextAlign.Center)
            }
        }
    }
}

@Composable
fun GradientHeader(title: String, subtitle: String, granted: Int, possible: Int, progressLabel: String) {
    val target = if (possible == 0) 0f else granted / possible.toFloat()
    val animated by animateFloatAsState(target, animationSpec = spring(dampingRatio = Spring.DampingRatioMediumBouncy), label = "progress")
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
        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.Top) {
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
                        Text(if (state.status == Status.INSTALL) "Install or update Health Connect" else "Allow")
                    }
                }
            }
        }
    }
}

@Composable
fun StatusPill(state: StepState) {
    val extras = LocalDaytraceExtras.current
    val quiet = MaterialTheme.colorScheme.onSurfaceVariant
    val (label, color) = when (state.status) {
        Status.GRANTED -> "On" to extras.granted
        Status.NEEDED -> if (state.step.required) "Needed" to extras.needed else "Optional" to quiet
        Status.INSTALL -> "Not installed" to quiet
        Status.UNAVAILABLE -> "Not available" to quiet
        Status.CHECKING -> "Checking" to quiet
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
