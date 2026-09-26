// DT-19 / DT-21: permission and sync status. DT-21 adds the last sync and "Sync now"; DT-22 the hub pairing.
package app.daytrace.android.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
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
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Router
import androidx.compose.material.icons.rounded.WarningAmber
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import app.daytrace.android.BuildConfig
import app.daytrace.android.ui.theme.LocalDaytraceExtras
import app.daytrace.android.ui.theme.Sky

@Composable
fun StatusScreen(states: List<StepState>, onGrant: (StepState) -> Unit, onShowOnboarding: () -> Unit) {
    val collecting = readyToContinue(states)
    Surface(color = MaterialTheme.colorScheme.background, modifier = Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize()) {
            GradientHeader(
                title = "Daytrace",
                subtitle = if (collecting) {
                    "Usage access is on, so Daytrace can see which apps you use on this phone."
                } else {
                    "Usage access is off, so Daytrace can't see which apps you use."
                },
                progress = grantedCount(states) / Step.entries.size.toFloat(),
                progressLabel = "${grantedCount(states)} of ${Step.entries.size} permissions on",
            )
            LazyColumn(
                modifier = Modifier.weight(1f),
                contentPadding = PaddingValues(16.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                item(key = "warning") {
                    AnimatedVisibility(
                        visible = !collecting,
                        enter = fadeIn() + expandVertically(),
                        exit = fadeOut() + shrinkVertically(),
                    ) {
                        UsageOffBanner(onFix = { states.firstOrNull { it.step == Step.USAGE }?.let(onGrant) })
                    }
                }
                item(key = "hub") { HubCard() }
                item(key = "title") {
                    Text(
                        "Permissions",
                        style = MaterialTheme.typography.titleLarge,
                        modifier = Modifier.padding(top = 8.dp, start = 4.dp),
                    )
                }
                items(states, key = { it.step.name }) { state -> StepCard(state, onGrant) }
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
            Icon(Icons.Rounded.WarningAmber, contentDescription = null, tint = needed)
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

@Composable
private fun HubCard() {
    // A soft pulse on the dot says "looking for your hub" (discovery and pairing arrive in DT-22).
    val pulse by rememberInfiniteTransition(label = "hub").animateFloat(
        initialValue = 0.35f,
        targetValue = 1f,
        animationSpec = infiniteRepeatable(tween(900), RepeatMode.Reverse),
        label = "pulse",
    )
    Card(
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 2.dp),
    ) {
        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(44.dp).clip(CircleShape).background(Sky.copy(alpha = 0.18f)), contentAlignment = Alignment.Center) {
                Icon(Icons.Rounded.Router, contentDescription = null, tint = Sky)
            }
            Spacer(Modifier.size(14.dp))
            Column(Modifier.weight(1f)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text("Your hub", style = MaterialTheme.typography.titleMedium)
                    Spacer(Modifier.size(8.dp))
                    Box(Modifier.size(8.dp).alpha(pulse).clip(CircleShape).background(Sky))
                }
                Spacer(Modifier.height(4.dp))
                Text(
                    "Not paired yet. Pairing with the Daytrace hub on your PC is coming next.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}
