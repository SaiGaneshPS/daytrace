// DT-21 / DT-22: sending queued events to the hub, only over Wi-Fi (the phone and the hub on the same Wi-Fi). In the
// background every 15 minutes on an unmetered network, and right away when you tap "Sync now". Each run collects
// new usage first, checks the hub proves it paired this phone, then sends 200 events at a time.
package app.daytrace.android.sync

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import androidx.core.content.edit
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkInfo
import androidx.work.WorkManager
import androidx.work.WorkQuery
import androidx.work.WorkerParameters
import app.daytrace.android.data.EventEntity
import app.daytrace.android.data.EventStore
import app.daytrace.android.usage.UsageCollector
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import java.util.concurrent.TimeUnit

enum class SyncResult { SENT, NOT_PAIRED, NOT_ON_WIFI, PAIR_AGAIN, BLOCKED, UNREACHABLE }

/**
 * The phone talks to the hub only over Wi-Fi. The hub accepts only devices on its own network, and every request
 * is made on the Wi-Fi network itself (never cellular or a VPN), so a sync happens only when the phone is on the
 * same Wi-Fi as the hub; the hub's proof then confirms it is the right one.
 */
object WifiOnly {
    /** The Wi-Fi network the phone is on right now, or null. Found even when Android prefers another network. */
    fun network(context: Context): Network? {
        val connectivity = context.getSystemService(ConnectivityManager::class.java)
        fun isWifi(network: Network) = connectivity.getNetworkCapabilities(network)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
        connectivity.activeNetwork?.takeIf(::isWifi)?.let { return it }
        @Suppress("DEPRECATION") // its replacement is a callback; a one-off look is all that is needed here
        return connectivity.allNetworks.firstOrNull(::isWifi)
    }

    const val WAITING = "Waiting for Wi-Fi: this phone syncs only when it is on the same Wi-Fi as your hub"
}

data class SyncReport(val result: SyncResult, val message: String, val sent: Int = 0, val refused: Int = 0)

/** The last sync, for the status screen. */
data class SyncStatus(val lastSuccessMs: Long?, val lastAttemptMs: Long?, val result: SyncResult?, val message: String?)

class SyncStatusStore(context: Context) {
    private val prefs = context.getSharedPreferences("sync_status", Context.MODE_PRIVATE)

    fun read() = SyncStatus(
        lastSuccessMs = prefs.getLong(KEY_SUCCESS, -1).takeIf { it >= 0 },
        lastAttemptMs = prefs.getLong(KEY_ATTEMPT, -1).takeIf { it >= 0 },
        result = prefs.getString(KEY_RESULT, null)?.let { name -> SyncResult.entries.firstOrNull { it.name == name } },
        message = prefs.getString(KEY_MESSAGE, null),
    )

    fun save(report: SyncReport, nowMs: Long) = prefs.edit {
        putLong(KEY_ATTEMPT, nowMs)
        putString(KEY_RESULT, report.result.name)
        putString(KEY_MESSAGE, report.message)
        if (report.result == SyncResult.SENT) putLong(KEY_SUCCESS, nowMs)
    }

    private companion object {
        const val KEY_SUCCESS = "last_success_ms"
        const val KEY_ATTEMPT = "last_attempt_ms"
        const val KEY_RESULT = "last_result"
        const val KEY_MESSAGE = "last_message"
    }
}

/**
 * Sends everything queued, lowest seq first. An event is marked sent only after the hub answered 200, so a sync
 * cut off at any point (the app killed, the Wi-Fi gone) just sends it again, and the hub ignores the repeat
 * (same external_id, same values). One sync runs at a time.
 */
class Syncer(
    private val store: EventStore,
    private val pairing: HubConfigSource,
    private val status: SyncStatusStore,
    /** An HTTP client bound to the Wi-Fi network, or null when the phone is not on Wi-Fi (see [WifiOnly]). */
    private val wifi: () -> OkHttpClient?,
    private val clientFor: (HubConfig, OkHttpClient) -> HubClient = { config, http -> HubClient(config, http) },
    private val clock: () -> Long = System::currentTimeMillis,
) {
    suspend fun sync(): SyncReport = LOCK.withLock {
        withContext(Dispatchers.IO) { syncLocked().also { status.save(it, clock()) } }
    }

    private suspend fun syncLocked(): SyncReport {
        val hub = pairing.load() ?: return SyncReport(SyncResult.NOT_PAIRED, "Not paired with a hub yet")
        val http = wifi() ?: return SyncReport(SyncResult.NOT_ON_WIFI, WifiOnly.WAITING)
        val client = clientFor(hub, http)
        // Is this really the hub that paired this phone? Asked without the token, which only goes out after a yes.
        when (val proof = client.proveHub()) {
            is HubResult.Ok -> if (!proof.value) return SyncReport(SyncResult.BLOCKED, NOT_YOUR_HUB_MESSAGE)
            else -> return stopped(proof, 0, 0)
        }
        // After a reinstall the phone counts from 0 again, but the hub may already hold higher seqs from before.
        if (!store.hubCursorRead(hub.deviceId)) {
            when (val cursor = client.cursor()) {
                is HubResult.Ok -> store.raiseSeqFloor(cursor.value, hub.deviceId)
                else -> return stopped(cursor, 0, 0)
            }
        }
        var size = BATCH
        var sent = 0
        var refused = 0
        while (true) {
            currentCoroutineContext().ensureActive() // a stopped worker sends nothing more
            val batch = store.pending(size)
            if (batch.isEmpty()) break
            when (val reply = client.send(batch)) {
                is HubResult.Ok -> {
                    val applied = apply(batch, reply.value)
                    sent += applied.sent
                    refused += applied.refused
                    if (applied.wrongDevice) return SyncReport(SyncResult.PAIR_AGAIN, PAIR_AGAIN_MESSAGE, sent, refused)
                    size = minOf(size * 2, BATCH)
                }
                // One event always fits (the hub caps its size), so a 413 for a single event means the request
                // went somewhere that is not behaving like the hub: stop and keep it, never refuse it for good.
                is HubResult.Split -> if (batch.size > 1) size = batch.size / 2 else return stopped(HubResult.Retry(reply.message), sent, refused)
                else -> return stopped(reply, sent, refused)
            }
        }
        return SyncReport(SyncResult.SENT, doneMessage(sent, refused), sent, refused)
    }

    private class Applied(val sent: Int, val refused: Int, val wrongDevice: Boolean)

    /**
     * Refused events (invalid, or any code this app does not know) are kept on the phone and not sent again.
     * wrong_device means the token belongs to another device: those events stay queued until you pair again.
     * seq_conflict cannot happen: every event carries an external_id, which the hub deduplicates on instead.
     */
    private fun apply(batch: List<EventEntity>, reply: IngestReply): Applied {
        val byIndex = reply.rejected.associateBy { it.index }
        var refused = 0
        var wrongDevice = false
        batch.forEachIndexed { index, event ->
            val rejection = byIndex[index] ?: return@forEachIndexed
            if (rejection.code == "wrong_device") {
                wrongDevice = true
            } else {
                refused += store.markRejected(event, "${rejection.code}: ${rejection.reason}")
            }
        }
        val sent = store.markSynced(batch.filterIndexed { index, _ -> index !in byIndex })
        return Applied(sent, refused, wrongDevice)
    }

    private fun stopped(result: HubResult<*>, sent: Int, refused: Int): SyncReport = when (result) {
        is HubResult.Unauthorized -> SyncReport(SyncResult.PAIR_AGAIN, PAIR_AGAIN_MESSAGE, sent, refused)
        is HubResult.Blocked -> SyncReport(SyncResult.BLOCKED, result.message, sent, refused)
        is HubResult.Retry -> SyncReport(SyncResult.UNREACHABLE, "Couldn't reach your hub (${result.message})", sent, refused)
        is HubResult.Split -> SyncReport(SyncResult.UNREACHABLE, "Couldn't reach your hub (${result.message})", sent, refused)
        is HubResult.Ok -> SyncReport(SyncResult.SENT, doneMessage(sent, refused), sent, refused)
    }

    private fun doneMessage(sent: Int, refused: Int) = when {
        sent > 0 && refused > 0 -> "Sent ${events(sent)} to your hub; it refused ${events(refused)}"
        sent > 0 -> "Sent ${events(sent)} to your hub"
        refused > 0 -> "Your hub refused ${events(refused)}"
        else -> "Everything was already on your hub"
    }

    companion object {
        const val BATCH = 200
        private const val PAIR_AGAIN_MESSAGE = "Your hub no longer accepts this phone. Pair again."
        private const val NOT_YOUR_HUB_MESSAGE =
            "Something answered at your hub's address but could not prove it is your hub, so nothing was sent. Are you on your home Wi-Fi?"
        private val LOCK = Mutex()

        @Volatile private var instance: Syncer? = null

        fun get(context: Context): Syncer = instance ?: synchronized(this) {
            instance ?: run {
                val app = context.applicationContext
                Syncer(EventStore.get(app), PairingStore.get(app), SyncStatusStore(app), wifi = { WifiOnly.network(app)?.let(HubClient::onNetwork) })
            }.also { instance = it }
        }

        fun events(count: Int) = if (count == 1) "1 event" else "$count events"
    }
}

class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        // New usage first. A failure there (no usage access, a full disk) never stops what is stored from going.
        withContext(Dispatchers.IO) { runCatching { UsageCollector(applicationContext).collect() } }
        Syncer.get(applicationContext).sync()
        // Always a success, even when the hub was out of reach: a retry would swap the 15-minute period for
        // WorkManager's backoff (up to 5 hours), and the next run tries again anyway. The result is on the screen.
        return Result.success()
    }

    companion object {
        internal const val PERIODIC = "daytrace-sync"
        internal const val NOW = "daytrace-sync-now"

        /** Every 15 minutes (Android's shortest period) while on an unmetered network. Kept across restarts. */
        fun schedule(context: Context) {
            val request = PeriodicWorkRequestBuilder<SyncWorker>(15, TimeUnit.MINUTES)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.UNMETERED).build())
                .build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(PERIODIC, ExistingPeriodicWorkPolicy.KEEP, request)
        }

        /**
         * "Sync now": runs right away on any connection, so off Wi-Fi the screen says it is waiting for Wi-Fi
         * instead of nothing happening. A tap while one is running changes nothing.
         */
        fun syncNow(context: Context) {
            val request = OneTimeWorkRequestBuilder<SyncWorker>()
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build())
                .addTag(NOW)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(NOW, ExistingWorkPolicy.KEEP, request)
        }

        /** True while a sync (background or "Sync now") is running. */
        fun running(context: Context): Flow<Boolean> =
            WorkManager.getInstance(context).getWorkInfosFlow(WorkQuery.fromUniqueWorkNames(PERIODIC, NOW))
                .map { infos -> infos.any { it.state == WorkInfo.State.RUNNING } }
    }
}
