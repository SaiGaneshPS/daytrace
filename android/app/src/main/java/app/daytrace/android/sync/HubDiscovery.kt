// DT-22: finding the hub on the Wi-Fi. The hub advertises _daytrace._tcp over mDNS (hub/daytrace_hub/discovery.py)
// and Android's NsdManager finds and resolves it, so pairing only needs the 6-digit code shown on the PC.
package app.daytrace.android.sync

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import java.io.ByteArrayOutputStream
import java.net.Inet4Address
import java.net.InetAddress
import kotlin.coroutines.resume

/** A hub found on the Wi-Fi: its advertised name ("Daytrace hub (personal)"), address and profile. */
data class FoundHub(val name: String, val url: String, val profile: String?)

object HubAddresses {
    /**
     * The address to use for a resolved hub: a private IPv4 address (the hub listens on IPv4 only), never
     * loopback, and never anything the sync would refuse anyway.
     */
    fun bestUrl(addresses: List<InetAddress>, port: Int): String? {
        val pick = addresses.firstOrNull { it is Inet4Address && !it.isLoopbackAddress && PrivateNetwork.isPrivate(it.address) }
        return pick?.let { "http://${it.hostAddress}:$port" }
    }

    /** DNS-SD names can arrive escaped ("Daytrace\032hub"): \DDD is a byte in decimal, \x is x. */
    fun unescape(name: String): String {
        val out = ByteArrayOutputStream()
        var i = 0
        while (i < name.length) {
            val digits = name.substring(minOf(i + 1, name.length), minOf(i + 4, name.length))
            if (name[i] == '\\' && digits.length == 3 && digits.all { it in '0'..'9' } && digits.toInt() <= 255) {
                out.write(digits.toInt())
                i += 4
            } else {
                if (name[i] == '\\' && i + 1 < name.length) i++ // \x stands for x
                val codePoint = name.codePointAt(i)
                out.write(String(Character.toChars(codePoint)).toByteArray(Charsets.UTF_8))
                i += Character.charCount(codePoint)
            }
        }
        return out.toString(Charsets.UTF_8.name())
    }
}

class HubDiscovery(context: Context) {
    private val nsd = context.getSystemService(NsdManager::class.java)

    /**
     * The hubs on this Wi-Fi, updated as they come and go, for as long as the flow is collected. Services are
     * resolved one at a time (older Android allows only one resolve at a time). The flow ends if Android cannot
     * start discovery; the screen then offers typing the address.
     */
    fun hubs(): Flow<List<FoundHub>> = callbackFlow {
        val found = LinkedHashMap<String, FoundHub>()
        val changes = Channel<Pair<Boolean, NsdServiceInfo>>(Channel.UNLIMITED) // (found or lost, service)
        val listener = object : NsdManager.DiscoveryListener {
            override fun onServiceFound(info: NsdServiceInfo) {
                changes.trySend(true to info)
            }

            override fun onServiceLost(info: NsdServiceInfo) {
                changes.trySend(false to info)
            }

            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                close()
            }

            override fun onDiscoveryStarted(serviceType: String) = Unit
            override fun onDiscoveryStopped(serviceType: String) = Unit
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) = Unit
        }
        launch {
            send(emptyList())
            for ((isFound, info) in changes) {
                val key = info.serviceName
                if (isFound) resolve(info)?.let { found[key] = it } else found.remove(key)
                send(found.values.toList())
            }
        }
        nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
        awaitClose {
            runCatching { nsd.stopServiceDiscovery(listener) }
            changes.close()
        }
    }

    /** The hub addresses found within [windowMs], for the sync when the saved address stops answering. */
    suspend fun findNow(windowMs: Long = FIND_WINDOW_MS): List<String> {
        var latest = emptyList<FoundHub>()
        withTimeoutOrNull(windowMs) { hubs().collect { latest = it } }
        return latest.map { it.url }
    }

    private suspend fun resolve(info: NsdServiceInfo): FoundHub? = withTimeoutOrNull(RESOLVE_TIMEOUT_MS) {
        suspendCancellableCoroutine { continuation ->
            val listener = object : NsdManager.ResolveListener {
                override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                    if (continuation.isActive) continuation.resume(null)
                }

                override fun onServiceResolved(serviceInfo: NsdServiceInfo) {
                    if (continuation.isActive) continuation.resume(toHub(serviceInfo))
                }
            }
            @Suppress("DEPRECATION") // its replacement needs Android 14; this works on every version
            nsd.resolveService(info, listener)
            // A resolve that timed out must be stopped, or later ones fail. Android 13 and older cannot stop one;
            // there a hub that never resolves may hide the others until the screen is opened again, and typing
            // the address still works.
            continuation.invokeOnCancellation {
                if (Build.VERSION.SDK_INT >= 34) runCatching { nsd.stopServiceResolution(listener) }
            }
        }
    }

    private fun toHub(info: NsdServiceInfo): FoundHub? {
        @Suppress("DEPRECATION") // host is replaced by hostAddresses on Android 14
        val addresses = if (Build.VERSION.SDK_INT >= 34) info.hostAddresses else listOfNotNull(info.host)
        val url = HubAddresses.bestUrl(addresses, info.port) ?: return null
        val profile = info.attributes["profile"]?.let { String(it, Charsets.UTF_8) }
        return FoundHub(HubAddresses.unescape(info.serviceName), url, profile)
    }

    private companion object {
        const val SERVICE_TYPE = "_daytrace._tcp"
        const val RESOLVE_TIMEOUT_MS = 5_000L
        const val FIND_WINDOW_MS = 6_000L
    }
}
