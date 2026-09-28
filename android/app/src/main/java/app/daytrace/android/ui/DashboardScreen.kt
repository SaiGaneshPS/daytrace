// DT-58: the dashboard inside the app. Native bottom tabs (Today, Story, Ask, Insights, More), each showing the
// matching dashboard page in one embedded WebView, in the dashboard's embed mode (?embed=1: no navigation of its own).
// The WebView loads only the paired hub's address: first the hub proves it paired this phone (as for a sync), then the
// page opens on the Wi-Fi only, with the app's dashboard token put into the page's storage for the hub's origin only.
// Anything else it tries to open is refused. More holds this phone's own screen (sync, live mode, permissions) and
// the rest of the dashboard's pages.
package app.daytrace.android.ui

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Bitmap
import android.net.ConnectivityManager
import android.net.Network
import android.view.ViewGroup
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebStorage
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.rounded.ArrowBack
import androidx.compose.material.icons.rounded.Edit
import androidx.compose.material.icons.rounded.Home
import androidx.compose.material.icons.rounded.Menu
import androidx.compose.material.icons.rounded.Search
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import androidx.webkit.ScriptHandler
import androidx.webkit.ServiceWorkerClientCompat
import androidx.webkit.ServiceWorkerControllerCompat
import androidx.webkit.WebSettingsCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import app.daytrace.android.sync.HubClient
import app.daytrace.android.sync.HubDiscovery
import app.daytrace.android.sync.HubProof
import app.daytrace.android.sync.HubResult
import app.daytrace.android.sync.PairingStore
import app.daytrace.android.sync.SyncResult
import app.daytrace.android.sync.WifiOnly
import app.daytrace.android.sync.findProvenHub
import app.daytrace.android.ui.theme.DaytraceIcons
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import org.json.JSONObject

// --- the rules, in plain Kotlin so they are unit tested --------------------------------------------------------

enum class DashboardTab(val label: String, val path: String?) {
    TODAY("Today", "/"),
    STORY("Story", "/story"),
    ASK("Ask", "/ask"),
    INSIGHTS("Insights", "/insights"),
    MORE("More", null),
}

/** More's list: this phone's own screen, then the dashboard's other pages. */
enum class MorePage(val label: String, val detail: String, val path: String?) {
    PHONE("This phone", "Sync, live mode and permissions", null),
    STREAKS("Streaks and goals", "Your streaks, today's goals and badges", "/streaks"),
    WRAPPED("Wrapped", "The week on one card", "/wrapped"),
    DEVICES("Devices", "Everything paired with your hub", "/devices"),
    PRIVACY("Privacy", "Where your data lives, and what leaves it: nothing", "/privacy"),
}

object DashboardRules {
    /** "http://192.168.1.23:8765": scheme, host and port, as a WebView origin rule wants it. Null for a bad address. */
    fun origin(baseUrl: String): String? = baseUrl.toHttpUrlOrNull()?.let { "${it.scheme}://${it.host}:${it.port}" }

    /** Whether [url] is on the hub itself (same scheme, host and port): the only place the WebView may go. */
    fun onHub(url: String, baseUrl: String): Boolean {
        val target = url.toHttpUrlOrNull() ?: return false
        val hub = baseUrl.toHttpUrlOrNull() ?: return false
        return target.scheme == hub.scheme && target.host == hub.host && target.port == hub.port
    }

    /** The hub's [path] in embed mode. */
    fun pageUrl(baseUrl: String, path: String): String {
        val hub = baseUrl.trimEnd('/')
        return hub + path + (if ('?' in path) "&" else "?") + "embed=1"
    }

    /** The script that puts the dashboard token where the dashboard reads it (api/client.ts), before the page runs. */
    fun tokenScript(token: String): String =
        "(function(){try{localStorage.setItem('daytrace.token'," + JSONObject.quote(token) + ")}catch(e){}})();"

    /**
     * What needs you on this phone, shown above every tab (before the tabs, the app opened on the status screen,
     * which says these): usage access off, or a last sync that needs you (pair again, or not your hub). Null: nothing.
     */
    fun warning(usageOn: Boolean, lastSync: SyncResult?, lastMessage: String?): String? = when {
        !usageOn -> "Usage access is off, so Daytrace can't see which apps you use."
        lastSync == SyncResult.PAIR_AGAIN -> lastMessage ?: "Your hub no longer accepts this phone. Pair again."
        lastSync == SyncResult.BLOCKED -> lastMessage ?: "Something at your hub's address couldn't prove it is your hub, so nothing was sent."
        else -> null
    }
}

/** Whether the Dashboard tab can open, and if not, why (each has its own card). */
sealed interface HubGate {
    data object Checking : HubGate
    data class Ready(val baseUrl: String, val viewerToken: String, val network: Network) : HubGate {
        override fun toString() = "Ready(baseUrl=$baseUrl, network=$network)" // never print the token
    }
    /** Paired before the app had a dashboard (or with an older hub): pairing again gives the token. */
    data object NoDashboardToken : HubGate
    data object NotOnWifi : HubGate
    data object Unreachable : HubGate
    data object PairAgain : HubGate
}

/**
 * Proves the hub first (the token goes only to the hub that paired this phone), exactly as a sync does: a hub whose
 * address changed is found on the Wi-Fi and followed.
 */
suspend fun checkGate(context: Context): HubGate = withContext(Dispatchers.IO) {
    val store = PairingStore.get(context)
    val pairing = store.pairing() ?: return@withContext HubGate.PairAgain
    val viewer = pairing.viewerToken ?: return@withContext HubGate.NoDashboardToken
    val network = WifiOnly.network(context) ?: return@withContext HubGate.NotOnWifi
    val found = findProvenHub(pairing.config, HubClient.onNetwork(network), store, findHubs = { HubDiscovery(context).findNow() })
    when (val proof = found.proof) {
        is HubResult.Ok -> when (proof.value) {
            HubProof.PAIRED -> HubGate.Ready(found.config.baseUrl, viewer, network)
            HubProof.REVOKED -> HubGate.PairAgain
            HubProof.NOT_PROVEN -> HubGate.Unreachable
        }
        else -> HubGate.Unreachable
    }
}

/** DT-58: forgetting the hub also forgets the dashboard's copy of the token in the WebView's storage. */
fun forgetDashboardStorage() = runCatching { WebStorage.getInstance().deleteAllData() }

// --- the screen ------------------------------------------------------------------------------------------------

/**
 * The app once paired: the dashboard's tabs, with this phone's own screen under More. [warning] (see
 * [DashboardRules.warning]) shows above every tab and opens This phone.
 */
@Composable
fun AppShell(phoneScreen: @Composable () -> Unit, warning: String?, onPairAgain: () -> Unit) {
    var tab by rememberSaveable { mutableStateOf(DashboardTab.TODAY) }
    var more by rememberSaveable { mutableStateOf<MorePage?>(null) }
    // Each tap on a tab (or a page under More) is a new visit: the page opens at its start, with no history before it.
    var visit by rememberSaveable { mutableIntStateOf(0) }
    val path = when (tab) {
        DashboardTab.MORE -> more?.path
        else -> tab.path
    }
    val onPhone = tab == DashboardTab.MORE && more == MorePage.PHONE
    Scaffold(
        bottomBar = {
            NavigationBar {
                DashboardTab.entries.forEach { item ->
                    NavigationBarItem(
                        selected = tab == item,
                        onClick = {
                            if (tab == item && item == DashboardTab.MORE) more = null // tapping More again: back to its list
                            tab = item
                            visit++
                        },
                        icon = { Icon(iconFor(item), contentDescription = null) },
                        label = { Text(item.label) },
                    )
                }
            }
        },
    ) { padding ->
        // The Scaffold's padding already keeps clear of the system bars: the pages inside mustn't add them again.
        Column(Modifier.fillMaxSize().padding(padding).consumeWindowInsets(padding)) {
            if (warning != null && !onPhone) {
                WarningBanner(warning) {
                    tab = DashboardTab.MORE
                    more = MorePage.PHONE
                }
            }
            Box(Modifier.weight(1f)) {
                when {
                    path != null -> DashboardPage(path, visit, onPairAgain, onBack = if (tab == DashboardTab.MORE) ({ more = null }) else null)
                    onPhone -> Column(Modifier.fillMaxSize()) {
                        BackBar("This phone") { more = null }
                        Box(Modifier.weight(1f)) { phoneScreen() }
                    }
                    else -> MoreList(onOpen = {
                        more = it
                        visit++
                    })
                }
            }
        }
    }
    BackHandler(enabled = tab == DashboardTab.MORE && more != null) { more = null }
}

@Composable
private fun WarningBanner(text: String, onOpen: () -> Unit) {
    Surface(color = MaterialTheme.colorScheme.errorContainer, modifier = Modifier.fillMaxWidth()) {
        Row(Modifier.padding(start = 16.dp, end = 8.dp, top = 4.dp, bottom = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            Text(
                text,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onErrorContainer,
                modifier = Modifier.weight(1f),
            )
            Spacer(Modifier.width(8.dp))
            TextButton(onClick = onOpen) { Text("Open") }
        }
    }
}

private fun iconFor(tab: DashboardTab): ImageVector = when (tab) {
    DashboardTab.TODAY -> Icons.Rounded.Home
    DashboardTab.STORY -> Icons.Rounded.Edit
    DashboardTab.ASK -> Icons.Rounded.Search
    DashboardTab.INSIGHTS -> DaytraceIcons.BarChart
    DashboardTab.MORE -> Icons.Rounded.Menu
}

@Composable
private fun BackBar(title: String, onBack: () -> Unit) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) { // the Scaffold keeps it clear of the system bars
        IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Rounded.ArrowBack, contentDescription = "Back") }
        Text(title, style = MaterialTheme.typography.titleLarge)
    }
}

@Composable
private fun MoreList(onOpen: (MorePage) -> Unit) {
    Surface(color = MaterialTheme.colorScheme.background, modifier = Modifier.fillMaxSize()) {
        Column(
            Modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text("More", style = MaterialTheme.typography.headlineMedium)
            MorePage.entries.forEach { page ->
                Card(
                    shape = RoundedCornerShape(20.dp),
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
                    modifier = Modifier.fillMaxWidth().clickable { onOpen(page) },
                ) {
                    Column(Modifier.padding(16.dp)) {
                        Text(page.label, style = MaterialTheme.typography.titleMedium)
                        Text(page.detail, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
            }
        }
    }
}

/** One dashboard page: the hub proven first, then the WebView, or a card saying what to do. */
@Composable
private fun DashboardPage(path: String, visit: Int, onPairAgain: () -> Unit, onBack: (() -> Unit)?) {
    val context = LocalContext.current
    var attempt by remember { mutableIntStateOf(0) }
    var gate by remember { mutableStateOf<HubGate>(HubGate.Checking) }
    LaunchedEffect(attempt) {
        gate = HubGate.Checking
        gate = checkGate(context)
    }
    Column(Modifier.fillMaxSize()) {
        if (onBack != null) BackBar(MorePage.entries.firstOrNull { it.path == path }?.label.orEmpty(), onBack)
        Box(Modifier.weight(1f)) {
            when (val state = gate) {
                HubGate.Checking -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { CircularProgressIndicator() }
                is HubGate.Ready -> DashboardWebView(state, path, visit, onUnreachable = { gate = HubGate.Unreachable }, onRetry = { attempt++ })
                HubGate.NotOnWifi -> GateCard(
                    "Not on your hub's Wi-Fi",
                    "The dashboard opens on the same Wi-Fi as your hub, like the sync. Connect to it, then try again.",
                    "Try again",
                ) { attempt++ }
                HubGate.Unreachable -> GateCard(
                    "Couldn't reach your hub",
                    "Is the Daytrace hub running on your PC, and is this phone on the same Wi-Fi? Everything waits safely here.",
                    "Try again",
                ) { attempt++ }
                HubGate.PairAgain -> GateCard("Pair again", "Your hub no longer accepts this phone. Pair with it again to see your dashboard.", "Pair again", onPairAgain)
                HubGate.NoDashboardToken -> GateCard(
                    "Pair again for your dashboard",
                    "This phone paired before the app had a dashboard of its own. Pair with your hub again: this phone keeps its id and history, and gets its dashboard.",
                    "Pair again",
                    onPairAgain,
                )
            }
        }
    }
}

@Composable
private fun GateCard(title: String, text: String, action: String, onAction: () -> Unit) {
    Box(Modifier.fillMaxSize().padding(24.dp), contentAlignment = Alignment.Center) {
        Card(shape = RoundedCornerShape(20.dp), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
            Column(Modifier.padding(20.dp)) {
                Text(title, style = MaterialTheme.typography.titleMedium)
                Spacer(Modifier.height(6.dp))
                Text(text, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(14.dp))
                Button(onClick = onAction) { Text(action) }
            }
        }
    }
}

/**
 * The WebView itself: the process is bound to the hub's Wi-Fi while it shows (so its requests never leave on
 * cellular), the token is set for the hub's origin only, every request to anywhere else is refused (a service
 * worker's too), and Safe Browsing is off (it would send the addresses to Google). A new [visit] opens [path] at
 * its start and forgets the history before it, so Back never crosses tabs.
 */
@SuppressLint("SetJavaScriptEnabled") // the dashboard is a JavaScript app, from the hub that just proved itself
@Composable
private fun DashboardWebView(gate: HubGate.Ready, path: String, visit: Int, onUnreachable: () -> Unit, onRetry: () -> Unit) {
    val context = LocalContext.current
    val origin = remember(gate.baseUrl) { DashboardRules.origin(gate.baseUrl) }
    if (origin == null || !WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
        GateCard("Update Android System WebView", "The dashboard needs a newer Android System WebView. Update it from the Play Store, then try again.", "Try again", onRetry)
        return
    }
    var progress by remember { mutableIntStateOf(0) }
    var canGoBack by remember { mutableStateOf(false) }
    val holder = remember { arrayOfNulls<WebView>(1) }
    val script = remember { arrayOfNulls<ScriptHandler>(1) }
    val forgetHistory = remember { booleanArrayOf(false) } // once the next page has loaded
    val refuse = { url: String -> if (DashboardRules.onHub(url, gate.baseUrl)) null else WebResourceResponse("text/plain", "utf-8", 403, "Refused", emptyMap(), null) }

    DisposableEffect(gate.network) {
        val connectivity = context.getSystemService(ConnectivityManager::class.java)
        connectivity.bindProcessToNetwork(gate.network)
        onDispose { connectivity.bindProcessToNetwork(null) }
    }
    Column(Modifier.fillMaxSize()) {
        if (progress in 1..99) LinearProgressIndicator(progress = { progress / 100f }, modifier = Modifier.fillMaxWidth().height(2.dp))
        AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { viewContext ->
                val web = WebView(viewContext).apply {
                    layoutParams = ViewGroup.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
                    settings.javaScriptEnabled = true
                    settings.domStorageEnabled = true
                    settings.allowFileAccess = false
                    settings.allowContentAccess = false
                    settings.setGeolocationEnabled(false)
                    settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
                    settings.setSupportMultipleWindows(false)
                    if (WebViewFeature.isFeatureSupported(WebViewFeature.SAFE_BROWSING_ENABLE)) WebSettingsCompat.setSafeBrowsingEnabled(settings, false)
                }
                script[0] = WebViewCompat.addDocumentStartJavaScript(web, DashboardRules.tokenScript(gate.viewerToken), setOf(origin))
                // A service worker's requests skip the WebView's own checks: they get the same ones. (The dashboard
                // registers none inside the app, and removes one an earlier version registered.)
                if (WebViewFeature.isFeatureSupported(WebViewFeature.SERVICE_WORKER_BASIC_USAGE) &&
                    WebViewFeature.isFeatureSupported(WebViewFeature.SERVICE_WORKER_SHOULD_INTERCEPT_REQUEST)
                ) {
                    ServiceWorkerControllerCompat.getInstance().setServiceWorkerClient(object : ServiceWorkerClientCompat() {
                        override fun shouldInterceptRequest(request: WebResourceRequest): WebResourceResponse? = refuse(request.url.toString())
                    })
                }
                SwipeRefreshLayout(viewContext).apply {
                    setOnRefreshListener { web.reload() }
                    setOnChildScrollUpCallback { _, _ -> web.canScrollVertically(-1) }
                    web.webViewClient = object : WebViewClient() {
                        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean =
                            !DashboardRules.onHub(request.url.toString(), gate.baseUrl) // anywhere else: refused, no browser opens

                        override fun shouldInterceptRequest(view: WebView, request: WebResourceRequest): WebResourceResponse? =
                            refuse(request.url.toString())

                        override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
                            progress = 1
                        }

                        override fun onPageFinished(view: WebView, url: String?) {
                            progress = 100
                            isRefreshing = false
                            if (forgetHistory[0]) {
                                forgetHistory[0] = false
                                view.clearHistory() // everything before this tab's page
                                canGoBack = view.canGoBack()
                            }
                        }

                        override fun doUpdateVisitedHistory(view: WebView, url: String?, isReload: Boolean) {
                            canGoBack = view.canGoBack()
                        }

                        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                            if (request.isForMainFrame) {
                                isRefreshing = false
                                onUnreachable()
                            }
                        }
                    }
                    web.webChromeClient = object : android.webkit.WebChromeClient() {
                        override fun onProgressChanged(view: WebView, newProgress: Int) {
                            progress = newProgress
                        }
                    }
                    addView(web)
                    holder[0] = web
                    web.tag = "$visit $path"
                    web.loadUrl(DashboardRules.pageUrl(gate.baseUrl, path))
                }
            },
            update = {
                val web = holder[0] ?: return@AndroidView
                val wanted = "$visit $path"
                if (web.tag != wanted) { // another tab, or the same one tapped again
                    web.tag = wanted
                    forgetHistory[0] = true
                    web.loadUrl(DashboardRules.pageUrl(gate.baseUrl, path))
                }
            },
            onRelease = {
                script[0]?.remove()
                holder[0]?.destroy()
                holder[0] = null
            },
        )
    }
    BackHandler(enabled = canGoBack) { holder[0]?.goBack() }
}
