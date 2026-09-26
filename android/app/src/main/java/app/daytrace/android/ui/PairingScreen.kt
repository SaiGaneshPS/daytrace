// DT-22: pairing with the hub. Scan the QR code the PC shows, or pick the hub found on the Wi-Fi (or type its
// address) and type the 6-digit code. The token the hub gives back is kept encrypted (sync/PairingStore.kt).
package app.daytrace.android.ui

import android.content.Context
import android.os.Build
import android.provider.Settings
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.scaleIn
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
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
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.CheckCircle
import androidx.compose.material.icons.rounded.Home
import androidx.compose.material.icons.rounded.Warning
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import app.daytrace.android.sync.FoundHub
import app.daytrace.android.sync.HubClient
import app.daytrace.android.sync.HubConfig
import app.daytrace.android.sync.HubDiscovery
import app.daytrace.android.sync.HubResult
import app.daytrace.android.sync.Pairing
import app.daytrace.android.sync.PairingStore
import app.daytrace.android.sync.SyncStatusStore
import app.daytrace.android.sync.SyncWorker
import app.daytrace.android.sync.WifiOnly
import app.daytrace.android.ui.theme.DaytraceIcons
import app.daytrace.android.ui.theme.LocalDaytraceExtras
import app.daytrace.android.ui.theme.Mint
import app.daytrace.android.ui.theme.Sky
import com.google.android.gms.common.moduleinstall.ModuleInstall
import com.google.android.gms.common.moduleinstall.ModuleInstallRequest
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.onCompletion
import kotlinx.coroutines.launch
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import org.json.JSONObject

// --- what people type or scan, in plain Kotlin so it is unit tested -------------------------------------------

/** What the QR code on the PC says (docs/api.md, GET /pair/qr.png). */
data class PairingQr(val url: String, val code: String)

object PairingInput {
    const val DEFAULT_PORT = 8765

    /** "493817", "493 817" or "493-817" as the 6 digits, or null. */
    fun code(text: String): String? {
        val trimmed = text.trim()
        if (!trimmed.all { it in '0'..'9' || it == ' ' || it == '-' }) return null
        return trimmed.filter { it in '0'..'9' }.takeIf { it.length == 6 }
    }

    /**
     * A hub address as typed ("192.168.1.23", "192.168.1.23:8765", "http://my-pc.local:8766/") as a base URL
     * with the scheme and port spelled out (8765, the personal profile, when none is given), or null.
     */
    fun address(text: String): String? {
        val trimmed = text.trim()
        if (trimmed.isEmpty() || trimmed.any(Char::isWhitespace)) return null
        val withScheme = if ("://" in trimmed) trimmed else "http://$trimmed"
        val url = withScheme.toHttpUrlOrNull() ?: return null
        val authority = withScheme.substringAfter("://").substringBefore('/').substringAfterLast('@')
        val port = if (authority.substringAfterLast(']').contains(':')) url.port else DEFAULT_PORT
        return url.newBuilder().username("").password("").port(port).encodedPath("/").query(null).fragment(null)
            .build().toString().trimEnd('/')
    }

    /** The pairing QR code's text (`{"daytrace":1,"url":...,"code":...}`), or null for any other QR code. */
    fun qr(text: String): PairingQr? = runCatching {
        val json = JSONObject(text)
        if (json.optInt("daytrace") != 1) return null
        PairingQr(address(json.getString("url")) ?: return null, code(json.getString("code")) ?: return null)
    }.getOrNull()
}

// --- the screen --------------------------------------------------------------------------------------------------

private sealed interface PairState {
    data object Idle : PairState
    data object Working : PairState
    data class Failed(val message: String) : PairState
    data class Done(val pairing: Pairing) : PairState
}

/**
 * Pairing runs here rather than in the screen's scope: turning the phone or leaving the screen mid-way must not
 * lose a pairing the hub has already made (its code is spent by then). The screen only shows the state.
 */
private object Pairer {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val current = MutableStateFlow<PairState>(PairState.Idle)
    val state: StateFlow<PairState> = current

    fun pair(context: Context, url: String, code: String) {
        if (current.value == PairState.Working) return
        current.value = PairState.Working
        val app = context.applicationContext
        scope.launch {
            val result = runCatching { pairWith(app, url, code) }
                .getOrElse { PairState.Failed("Couldn't pair: ${it.message ?: it.javaClass.simpleName}") }
            current.value = result
            if (result is PairState.Done) SyncWorker.syncNow(app)
        }
    }

    fun fail(message: String) {
        if (current.value != PairState.Working) current.value = PairState.Failed(message)
    }

    /** A fresh screen starts from the beginning, unless a pairing is still on its way. */
    fun reset() {
        if (current.value != PairState.Working) current.value = PairState.Idle
    }
}

@Composable
fun PairingScreen(onClose: () -> Unit) {
    val context = LocalContext.current
    val discovery = rememberFoundHubs()
    val state by Pairer.state.collectAsStateWithLifecycle()
    var fresh by rememberSaveable { mutableStateOf(true) } // survives turning the phone, not closing the screen
    LaunchedEffect(Unit) {
        if (fresh) Pairer.reset()
        fresh = false
    }
    var chosen by rememberSaveable { mutableStateOf<String?>(null) }
    var typing by rememberSaveable { mutableStateOf(false) }
    var address by rememberSaveable { mutableStateOf("") }
    var code by rememberSaveable { mutableStateOf("") }

    // One hub on the Wi-Fi (the usual case): pick it, so only the code is left to type.
    LaunchedEffect(discovery.hubs) {
        if (chosen == null && discovery.hubs.size == 1) chosen = discovery.hubs.single().url
    }

    val hubUrl = if (typing) PairingInput.address(address) else chosen
    val validCode = PairingInput.code(code)
    val working = state == PairState.Working

    fun pair(url: String, pairingCode: String) = Pairer.pair(context, url, pairingCode)

    val scan = rememberQrScanner(
        onScanned = { text ->
            val qr = PairingInput.qr(text)
            if (qr != null) pair(qr.url, qr.code) else Pairer.fail("That QR code isn't a Daytrace pairing code.")
        },
        onError = { Pairer.fail(it) },
    )

    val done = state as? PairState.Done
    val steps = when {
        done != null -> 2
        hubUrl != null -> 1
        else -> 0
    }
    Surface(color = MaterialTheme.colorScheme.background, modifier = Modifier.fillMaxSize()) {
        LazyColumn(
            modifier = Modifier.fillMaxSize().imePadding().windowInsetsPadding(WindowInsets.safeDrawing.only(WindowInsetsSides.Horizontal)),
            contentPadding = PaddingValues(bottom = 24.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item(key = "header") {
                GradientHeader(
                    title = "Pair with your hub",
                    subtitle = "Your phone sends its events to the Daytrace hub on your PC, over your own Wi-Fi. Nothing goes to the internet.",
                    granted = steps,
                    possible = 2,
                    progressLabel = if (done != null) "Paired" else "Step ${steps + 1} of 2",
                )
            }
            if (done != null) {
                item(key = "done") { Box(Modifier.padding(horizontal = 16.dp)) { DoneCard(done.pairing, onClose) } }
            } else {
                item(key = "where") {
                    Text(
                        "On your PC, start pairing in the Daytrace dashboard (Devices page). It shows a QR code and a 6-digit code for 5 minutes.",
                        style = MaterialTheme.typography.bodyMedium,
                        modifier = Modifier.padding(horizontal = 20.dp),
                    )
                }
                item(key = "scan") { Box(Modifier.padding(horizontal = 16.dp)) { ScanCard(enabled = !working, onScan = scan) } }
                item(key = "code") {
                    Box(Modifier.padding(horizontal = 16.dp)) {
                        CodeCard(
                            discovery = discovery,
                            chosen = chosen,
                            onChoose = { chosen = it; typing = false },
                            typing = typing,
                            onTyping = { typing = true },
                            address = address,
                            onAddress = { address = it },
                            addressValid = PairingInput.address(address) != null,
                            code = code,
                            onCode = { code = it.take(7) },
                            canPair = hubUrl != null && validCode != null && !working,
                            working = working,
                            onPair = { if (hubUrl != null && validCode != null) pair(hubUrl, validCode) },
                        )
                    }
                }
                item(key = "problem") {
                    val failed = state as? PairState.Failed
                    AnimatedVisibility(visible = failed != null, enter = fadeIn() + expandVertically(), exit = fadeOut() + shrinkVertically()) {
                        Box(Modifier.padding(horizontal = 16.dp)) { ProblemCard(failed?.message.orEmpty()) }
                    }
                }
                item(key = "back") {
                    Box(Modifier.fillMaxWidth().navigationBarsPadding(), contentAlignment = Alignment.Center) {
                        TextButton(onClick = onClose) { Text("Not now") }
                    }
                }
            }
        }
    }
}

/** Hubs found on the Wi-Fi, and whether it has been long enough to suggest typing the address. */
private data class Discovery(val hubs: List<FoundHub>, val searching: Boolean, val slow: Boolean)

@Composable
private fun rememberFoundHubs(): Discovery {
    val context = LocalContext.current
    var hubs by remember { mutableStateOf(emptyList<FoundHub>()) }
    var searching by remember { mutableStateOf(true) }
    var slow by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) {
        launch {
            delay(10_000)
            slow = true
        }
        HubDiscovery(context).hubs()
            .catch { } // no discovery on this network: typing the address still works
            .onCompletion { searching = false }
            .collect { hubs = it }
    }
    return Discovery(hubs, searching, slow)
}

/**
 * Google's code scanner: on-device ML Kit inside Google Play services, with its own camera screen, so the app needs
 * no camera permission. Its module is downloaded by Play services the first time (a no-op after that).
 */
@Composable
private fun rememberQrScanner(onScanned: (String) -> Unit, onError: (String) -> Unit): () -> Unit {
    val context = LocalContext.current
    val scanned by rememberUpdatedState(onScanned)
    val failed by rememberUpdatedState(onError)
    return remember(context) {
        {
            val options = GmsBarcodeScannerOptions.Builder().setBarcodeFormats(Barcode.FORMAT_QR_CODE).build()
            val scanner = GmsBarcodeScanning.getClient(context, options)
            ModuleInstall.getClient(context).installModules(ModuleInstallRequest.newBuilder().addApi(scanner).build())
                .addOnSuccessListener {
                    scanner.startScan()
                        .addOnSuccessListener { barcode -> barcode.rawValue?.let(scanned) }
                        .addOnFailureListener { failed("The scanner stopped (${it.message}). Type the code instead.") }
                }
                .addOnFailureListener { failed("Google Play services couldn't set up the QR scanner. Type the code instead.") }
        }
    }
}

/** The name the hub shows in its device list: the phone's own name from Settings, or its model. */
private fun deviceName(context: Context): String {
    val named = runCatching { Settings.Global.getString(context.contentResolver, Settings.Global.DEVICE_NAME) }.getOrNull()
    val model = listOf(Build.MANUFACTURER.replaceFirstChar { it.uppercase() }, Build.MODEL).joinToString(" ")
    return HubClient.cleanText(named?.takeIf { it.isNotBlank() } ?: model, 64) ?: "Android phone"
}

/** Trades the code for a token over Wi-Fi and keeps it (encrypted). Runs off the main thread. */
private fun pairWith(context: Context, url: String, code: String): PairState {
    val wifi = WifiOnly.network(context) ?: return PairState.Failed("Connect this phone to the same Wi-Fi as your PC, then try again.")
    return when (val result = HubClient.claim(url, code, deviceName(context), HubClient.onNetwork(wifi))) {
        is HubResult.Ok -> {
            val paired = result.value
            val pairing = Pairing(HubConfig(url, paired.token, paired.deviceId), paired.profile, paired.name)
            try {
                PairingStore.get(context).save(pairing)
            } catch (e: Exception) { // the Keystore can fail on some phones; say so instead of crashing
                return PairState.Failed("This phone couldn't store the pairing securely (${e.javaClass.simpleName}). Start pairing again on the PC.")
            }
            SyncStatusStore(context).reset() // the last sync belonged to the old pairing
            PairState.Done(pairing)
        }
        is HubResult.Retry -> PairState.Failed("Couldn't pair with $url: ${result.message}")
        is HubResult.Blocked -> PairState.Failed(result.message)
        is HubResult.Unauthorized -> PairState.Failed(result.message)
        is HubResult.Split -> PairState.Failed(result.message)
    }
}

// --- cards -------------------------------------------------------------------------------------------------------

@Composable
private fun PairCard(content: @Composable () -> Unit) {
    Card(
        shape = RoundedCornerShape(20.dp),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        elevation = CardDefaults.cardElevation(defaultElevation = 2.dp),
    ) {
        Column(Modifier.padding(16.dp).fillMaxWidth()) { content() }
    }
}

@Composable
private fun ScanCard(enabled: Boolean, onScan: () -> Unit) = PairCard {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.size(44.dp).clip(CircleShape).background(Mint.copy(alpha = 0.18f)), contentAlignment = Alignment.Center) {
            Icon(DaytraceIcons.QrScanner, contentDescription = null, tint = Mint)
        }
        Spacer(Modifier.size(14.dp))
        Column(Modifier.weight(1f)) {
            Text("Scan the QR code", style = MaterialTheme.typography.titleMedium)
            Text(
                "The quickest way: it has the hub's address and the code in one.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
    Spacer(Modifier.height(12.dp))
    Button(onClick = onScan, enabled = enabled, modifier = Modifier.fillMaxWidth()) {
        Icon(DaytraceIcons.QrScanner, contentDescription = null, modifier = Modifier.size(18.dp))
        Spacer(Modifier.size(8.dp))
        Text("Scan")
    }
}

@Composable
private fun CodeCard(
    discovery: Discovery,
    chosen: String?,
    onChoose: (String) -> Unit,
    typing: Boolean,
    onTyping: () -> Unit,
    address: String,
    onAddress: (String) -> Unit,
    addressValid: Boolean,
    code: String,
    onCode: (String) -> Unit,
    canPair: Boolean,
    working: Boolean,
    onPair: () -> Unit,
) = PairCard {
    Text("Or type the code", style = MaterialTheme.typography.titleMedium)
    Spacer(Modifier.height(8.dp))
    Row(verticalAlignment = Alignment.CenterVertically) {
        if (discovery.searching) Radar(Modifier.size(22.dp)) else Icon(Icons.Rounded.Home, contentDescription = null, tint = Sky)
        Spacer(Modifier.size(10.dp))
        Text(
            when {
                discovery.hubs.isNotEmpty() -> "Hubs on this Wi-Fi"
                discovery.searching -> "Looking for your hub on this Wi-Fi..."
                else -> "Couldn't look for hubs on this network"
            },
            style = MaterialTheme.typography.bodyMedium,
        )
    }
    discovery.hubs.forEach { hub ->
        val selected = !typing && chosen == hub.url
        Row(
            Modifier
                .fillMaxWidth()
                .padding(top = 8.dp)
                .clip(RoundedCornerShape(14.dp))
                .border(1.dp, if (selected) Sky else MaterialTheme.colorScheme.outlineVariant, RoundedCornerShape(14.dp))
                .clickable { onChoose(hub.url) }
                .padding(horizontal = 8.dp, vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            RadioButton(selected = selected, onClick = { onChoose(hub.url) })
            Column(Modifier.weight(1f)) {
                Text(hub.name, style = MaterialTheme.typography.bodyLarge)
                Text(hub.url.removePrefix("http://"), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
    if (discovery.slow && discovery.hubs.isEmpty() && !typing) {
        Text(
            "Not showing up? Check the hub is running on your PC and this phone is on the same Wi-Fi, or type the address the PC shows.",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 8.dp),
        )
    }
    if (typing) {
        OutlinedTextField(
            value = address,
            onValueChange = onAddress,
            label = { Text("Hub address") },
            placeholder = { Text("192.168.1.23:8765") },
            singleLine = true,
            isError = address.isNotBlank() && !addressValid,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Next),
            modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
        )
    } else {
        TextButton(onClick = onTyping) { Text("Type the address instead") }
    }
    OutlinedTextField(
        value = code,
        onValueChange = onCode,
        label = { Text("6-digit code") },
        singleLine = true,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.NumberPassword, imeAction = ImeAction.Done),
        modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
    )
    Spacer(Modifier.height(12.dp))
    Button(onClick = onPair, enabled = canPair, modifier = Modifier.fillMaxWidth()) {
        if (working) {
            CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp)
            Spacer(Modifier.size(8.dp))
            Text("Pairing...")
        } else {
            Text("Pair")
        }
    }
}

/** A dot with a ring that keeps spreading out: searching. Animated in the draw phase only. */
@Composable
private fun Radar(modifier: Modifier = Modifier) {
    val wave = rememberInfiniteTransition(label = "radar").animateFloat(0f, 1f, infiniteRepeatable(tween(1_400)), label = "wave")
    Box(modifier, contentAlignment = Alignment.Center) {
        Box(
            Modifier.fillMaxSize().graphicsLayer {
                scaleX = 0.3f + wave.value * 0.7f
                scaleY = 0.3f + wave.value * 0.7f
                alpha = 1f - wave.value
            }.clip(CircleShape).background(Sky.copy(alpha = 0.5f)),
        )
        Box(Modifier.size(8.dp).clip(CircleShape).background(Sky))
    }
}

@Composable
private fun ProblemCard(message: String) {
    val needed = LocalDaytraceExtras.current.needed
    Card(shape = RoundedCornerShape(20.dp), colors = CardDefaults.cardColors(containerColor = needed.copy(alpha = 0.12f))) {
        Row(Modifier.padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Rounded.Warning, contentDescription = null, tint = needed)
            Spacer(Modifier.size(12.dp))
            Text(message, style = MaterialTheme.typography.bodyMedium)
        }
    }
}

@Composable
private fun DoneCard(pairing: Pairing, onClose: () -> Unit) = PairCard {
    val granted = LocalDaytraceExtras.current.granted
    Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
        var shown by remember { mutableStateOf(false) }
        LaunchedEffect(Unit) { shown = true }
        AnimatedVisibility(visible = shown, enter = scaleIn(spring(dampingRatio = Spring.DampingRatioMediumBouncy)) + fadeIn()) {
            Icon(Icons.Rounded.CheckCircle, contentDescription = null, tint = granted, modifier = Modifier.size(72.dp))
        }
        Spacer(Modifier.height(8.dp))
        Text("Paired!", style = MaterialTheme.typography.headlineSmall)
        Spacer(Modifier.height(4.dp))
        Text(
            "This phone is ${pairing.config.deviceId} on your hub's ${pairing.profile} profile. Your events are on their way.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(16.dp))
        Button(onClick = onClose, modifier = Modifier.fillMaxWidth()) { Text("Done") }
    }
}
