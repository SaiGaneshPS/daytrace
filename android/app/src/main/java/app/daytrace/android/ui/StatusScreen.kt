// DT-19 / DT-21 / DT-22: permission and sync status: today's app time, the hub card (pairing, events waiting, the
// last sync, "Sync now" and "Forget this hub") and the permissions.
package app.daytrace.android.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
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
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Home
import androidx.compose.material.icons.rounded.Warning
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
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
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import app.daytrace.android.BuildConfig
import app.daytrace.android.data.EventStore
import app.daytrace.android.data.StoreCounts
import app.daytrace.android.sync.PairingInfo
import app.daytrace.android.sync.PairingStore
import app.daytrace.android.sync.SyncResult
import app.daytrace.android.sync.SyncStatus
import app.daytrace.android.sync.SyncStatusStore
import app.daytrace.android.sync.SyncWorker
import app.daytrace.android.sync.Syncer
import app.daytrace.android.ui.theme.Blush
import app.daytrace.android.ui.theme.LocalDaytraceExtras
import app.daytrace.android.ui.theme.Mint
import app.daytrace.android.ui.theme.Sky
import app.daytrace.android.ui.theme.Sunrise
import app.daytrace.android.usage.TodaySummary
import app.daytrace.android.usage.UsageCollector
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle

@Composable
fun StatusScreen(states: List<StepState>, onGrant: (StepState) -> Unit, onShowOnboarding: () -> Unit, onPair: () -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val usageOn = readyToContinue(states)
    val syncing by remember(context) { SyncWorker.running(context) }.collectAsStateWithLifecycle(initialValue = false)
    Surface(color = MaterialTheme.colorScheme.background, modifier = Modifier.fillMaxSize()) {
        LazyColumn(
            modifier = Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing.only(WindowInsetsSides.Horizontal)),
            contentPadding = PaddingValues(bottom = 16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item(key = "header") {
                GradientHeader(
                    title = "Daytrace",
                    subtitle = if (usageOn) {
                        "Usage access is on, so Daytrace can see which apps you use on this phone."
                    } else {
                        "Usage access is off, so Daytrace can't see which apps you use."
                    },
                    granted = grantedCount(states),
                    possible = possibleCount(states),
                    progressLabel = "${grantedCount(states)} of ${possibleCount(states)} permissions on",
                )
            }
            item(key = "warning") {
                AnimatedVisibility(
                    visible = !usageOn,
                    enter = fadeIn() + expandVertically(),
                    exit = fadeOut() + shrinkVertically(),
                ) {
                    Box(Modifier.padding(horizontal = 16.dp)) {
                        UsageOffBanner(onFix = { states.firstOrNull { it.step == Step.USAGE }?.let(onGrant) })
                    }
                }
            }
            if (usageOn) {
                item(key = "today") { Box(Modifier.padding(horizontal = 16.dp)) { TodayCard(rememberToday()) } }
            }
            item(key = "hub") {
                Box(Modifier.padding(horizontal = 16.dp)) {
                    HubCard(
                        rememberSyncCard(),
                        syncing,
                        onSyncNow = { SyncWorker.syncNow(context) },
                        onPair = onPair,
                        onForget = { scope.launch(Dispatchers.IO) { PairingStore.get(context).clear() } },
                    )
                }
            }
            item(key = "title") {
                Text(
                    "Permissions",
                    style = MaterialTheme.typography.titleLarge,
                    modifier = Modifier.padding(top = 8.dp, start = 20.dp),
                )
            }
            items(states, key = { it.step.name }) { state -> Box(Modifier.padding(horizontal = 16.dp)) { StepCard(state, onGrant) } }
            item(key = "footer") {
                Column(Modifier.fillMaxWidth().navigationBarsPadding(), horizontalAlignment = Alignment.CenterHorizontally) {
                    TextButton(onClick = onShowOnboarding) { Text("Show the welcome screen again") }
                    Text(
                        "Daytrace ${BuildConfig.VERSION_NAME}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
    }
}

/** What the Today card shows: still counting, today's numbers, or a problem reading them. */
private sealed interface Today {
    data object Counting : Today
    data class Ready(val summary: TodaySummary) : Today
    data object Failed : Today
}

/**
 * Collects what is new and sums up today, off the main thread, when the screen comes to the front and then every
 * minute while it stays there (so the card is live, and rolls over at midnight). A failure (a full disk, say)
 * shows a message on the card instead of crashing the app.
 */
@Composable
private fun rememberToday(): Today {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var today by remember { mutableStateOf<Today>(Today.Counting) }
    LifecycleResumeEffect(Unit) {
        val job = scope.launch {
            while (isActive) {
                today = withContext(Dispatchers.IO) {
                    runCatching {
                        UsageCollector(context).collect()
                        val now = System.currentTimeMillis()
                        TodaySummary.from(EventStore.get(context).sessionsEndingAfter(TodaySummary.startOfDay(now)), now)
                    }.fold({ Today.Ready(it) }, { Today.Failed })
                }
                delay(60_000)
            }
        }
        onPauseOrDispose { job.cancel() }
    }
    return today
}

private fun duration(ms: Long): String {
    val minutes = ms / 60_000
    return if (minutes >= 60) "${minutes / 60} h ${minutes % 60} min" else "$minutes min"
}

@Composable
private fun TodayCard(today: Today) {
    val summary = (today as? Today.Ready)?.summary
    val colors = listOf(Blush, Sunrise, Mint)
    Card(
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 2.dp),
    ) {
        Column(Modifier.padding(16.dp).fillMaxWidth()) {
            Text("Today on this phone", style = MaterialTheme.typography.titleMedium)
            Spacer(Modifier.height(4.dp))
            Text(
                when (today) {
                    Today.Counting -> "Counting..."
                    Today.Failed -> "Not right now"
                    is Today.Ready -> duration(today.summary.totalMs)
                },
                style = MaterialTheme.typography.displaySmall,
                color = MaterialTheme.colorScheme.primary,
            )
            val top = summary?.topApps.orEmpty()
            if (today is Today.Failed) {
                Text(
                    "Couldn't read today's usage. It will try again in a minute.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (summary != null && top.isEmpty()) {
                Text(
                    "Nothing yet today. Use a few apps and come back.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            top.forEachIndexed { index, (app, ms) ->
                val share by animateFloatAsState(
                    targetValue = if (summary!!.totalMs > 0) ms / summary.totalMs.toFloat() else 0f,
                    animationSpec = tween(700, delayMillis = 120 * index),
                    label = "share",
                )
                Spacer(Modifier.height(10.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(app, style = MaterialTheme.typography.bodyMedium, modifier = Modifier.weight(1f))
                    Text(duration(ms), style = MaterialTheme.typography.labelLarge)
                }
                Spacer(Modifier.height(4.dp))
                Box(Modifier.fillMaxWidth().height(8.dp).clip(RoundedCornerShape(50)).background(MaterialTheme.colorScheme.surfaceVariant)) {
                    Box(Modifier.fillMaxWidth(share).height(8.dp).clip(RoundedCornerShape(50)).background(colors[index % colors.size]))
                }
            }
        }
    }
}

@Composable
private fun UsageOffBanner(onFix: () -> Unit) {
    val needed = LocalDaytraceExtras.current.needed
    Card(
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(containerColor = needed.copy(alpha = 0.12f)),
        onClick = onFix,
    ) {
        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Rounded.Warning, contentDescription = null, tint = needed)
            Spacer(Modifier.size(12.dp))
            Column(Modifier.weight(1f)) {
                Text("Usage access is off", style = MaterialTheme.typography.titleMedium, color = needed)
                Text(
                    "Tap to turn it back on in Settings. Daytrace can't see which apps you use without it.",
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
    }
}

/** What the hub card shows: pairing, what is waiting to be sent, and how the last sync went. */
private data class SyncCard(val pairing: PairingInfo?, val counts: StoreCounts, val status: SyncStatus)

/** Read every few seconds while the screen is in front, so a sync's result shows up as soon as it finishes. */
@Composable
private fun rememberSyncCard(): SyncCard? {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var card by remember { mutableStateOf<SyncCard?>(null) }
    LifecycleResumeEffect(Unit) {
        val job = scope.launch {
            while (isActive) {
                withContext(Dispatchers.IO) {
                    runCatching {
                        SyncCard(PairingStore.get(context).info(), EventStore.get(context).counts(), SyncStatusStore(context).read())
                    }.getOrNull()
                }?.let { card = it }
                delay(3_000)
            }
        }
        onPauseOrDispose { job.cancel() }
    }
    return card
}

/** "today at 10:42" or a date and time. */
private fun whenText(epochMs: Long, nowMs: Long = System.currentTimeMillis()): String {
    val zone = ZoneId.systemDefault()
    val at = Instant.ofEpochMilli(epochMs).atZone(zone)
    return if (at.toLocalDate() == Instant.ofEpochMilli(nowMs).atZone(zone).toLocalDate()) {
        "today at " + at.format(DateTimeFormatter.ofLocalizedTime(FormatStyle.SHORT))
    } else {
        at.format(DateTimeFormatter.ofLocalizedDateTime(FormatStyle.MEDIUM, FormatStyle.SHORT))
    }
}

@Composable
private fun HubCard(card: SyncCard?, syncing: Boolean, onSyncNow: () -> Unit, onPair: () -> Unit, onForget: () -> Unit) {
    val pairing = card?.pairing
    val paired = pairing != null
    val status = card?.status
    var confirmForget by remember { mutableStateOf(false) }
    val healthy = paired && status?.result == SyncResult.SENT
    // The dot pulses while looking for the hub or syncing, and stays still once all is well. The value is read in
    // the draw phase (graphicsLayer), so the card does not recompose on every animation frame.
    val pulse = rememberInfiniteTransition(label = "hub").animateFloat(
        initialValue = 0.35f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(900), RepeatMode.Reverse),
        label = "pulse",
    )
    val still = healthy && !syncing
    val dot = if (healthy) Mint else Sky
    val needed = LocalDaytraceExtras.current.needed
    Card(
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 2.dp),
    ) {
        Column(Modifier.padding(16.dp).fillMaxWidth()) {
            Row(verticalAlignment = Alignment.Top) {
                Box(Modifier.size(44.dp).clip(CircleShape).background(Sky.copy(alpha = 0.18f)), contentAlignment = Alignment.Center) {
                    Icon(Icons.Rounded.Home, contentDescription = null, tint = Sky)
                }
                Spacer(Modifier.size(14.dp))
                Column(Modifier.weight(1f)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Text("Your hub", style = MaterialTheme.typography.titleMedium)
                        Spacer(Modifier.size(8.dp))
                        Box(Modifier.size(8.dp).graphicsLayer { alpha = if (still) 1f else pulse.value }.clip(CircleShape).background(dot))
                    }
                    Spacer(Modifier.height(4.dp))
                    Text(
                        if (pairing != null) {
                            "Paired with the hub on your PC (${pairing.profile} profile) as ${pairing.deviceId}, at ${pairing.baseUrl.removePrefix("http://")}."
                        } else {
                            "Not paired yet. Until you pair with the Daytrace hub on your PC, everything waits safely on this phone."
                        },
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            if (card != null) {
                val waiting = card.counts.waiting
                Spacer(Modifier.height(12.dp))
                Text(
                    when {
                        waiting == 0 && paired && status?.lastSuccessMs != null && card.counts.refused == 0 ->
                            "Everything on this phone is on your hub"
                        waiting == 0 -> "Nothing waiting to send"
                        paired -> "${Syncer.events(waiting)} waiting to go to your hub"
                        else -> "${Syncer.events(waiting)} saved on this phone, ready to send once you pair"
                    },
                    style = MaterialTheme.typography.bodyMedium,
                )
                val lastSync = status?.lastSuccessMs?.let { "Last synced ${whenText(it)}" } ?: if (paired) "Not synced yet" else null
                if (lastSync != null) {
                    Text(lastSync, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                val problem = status?.message?.takeIf { paired && status.result in PROBLEMS }
                if (problem != null) {
                    Spacer(Modifier.height(4.dp))
                    Text(problem, style = MaterialTheme.typography.bodySmall, color = needed)
                }
                if (paired && status?.result == SyncResult.NOT_ON_WIFI) {
                    Spacer(Modifier.height(4.dp))
                    Text(status.message.orEmpty(), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                if (card.counts.refused > 0) {
                    Text(
                        "Your hub refused ${Syncer.events(card.counts.refused)}. They stay on this phone.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Spacer(Modifier.height(12.dp))
                when {
                    !paired -> Button(onClick = onPair) { Text("Pair with your hub") }
                    status?.result == SyncResult.PAIR_AGAIN -> Row(verticalAlignment = Alignment.CenterVertically) {
                        Button(onClick = onPair) { Text("Pair again") }
                        Spacer(Modifier.size(8.dp))
                        TextButton(onClick = { confirmForget = true }) { Text("Forget this hub") }
                    }
                    else -> Row(verticalAlignment = Alignment.CenterVertically) {
                        FilledTonalButton(onClick = onSyncNow, enabled = !syncing) {
                            if (syncing) {
                                CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp)
                                Spacer(Modifier.size(8.dp))
                                Text("Syncing...")
                            } else {
                                Text("Sync now")
                            }
                        }
                        Spacer(Modifier.size(8.dp))
                        TextButton(onClick = { confirmForget = true }) { Text("Forget this hub") }
                    }
                }
            }
        }
    }
    if (confirmForget) {
        AlertDialog(
            onDismissRequest = { confirmForget = false },
            title = { Text("Forget this hub?") },
            text = {
                Text(
                    "This phone stops sending to it and deletes its token. Your events stay on the phone and go to the " +
                        "hub you pair with next; the hub keeps what it already has.",
                )
            },
            confirmButton = {
                TextButton(onClick = {
                    confirmForget = false
                    onForget()
                }) { Text("Forget") }
            },
            dismissButton = { TextButton(onClick = { confirmForget = false }) { Text("Cancel") } },
        )
    }
}

private val PROBLEMS = setOf(SyncResult.PAIR_AGAIN, SyncResult.BLOCKED, SyncResult.UNREACHABLE)
