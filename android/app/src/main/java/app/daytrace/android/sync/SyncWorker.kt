// DT-21: sending queued events to the hub. In the background every 15 minutes on Wi-Fi (an unmetered network),
// and right away when you tap "Sync now". Each run collects new usage first, then sends 200 events at a time.
package app.daytrace.android.sync

import android.content.Context
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
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit

enum class SyncResult { SENT, NOT_PAIRED, PAIR_AGAIN, BLOCKED, UNREACHABLE }

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

    /**
     * The device whose hub cursor was read (once per install and pairing, see [Syncer]). Losing it only means the
     * cursor is read once more; the numbers it set are kept in the database.
     */
    var cursorDevice: String?
        get() = prefs.getString(KEY_CURSOR_DEVICE, null)
        set(value) = prefs.edit { putString(KEY_CURSOR_DEVICE, value) }

    private companion object {
        const val KEY_SUCCESS = "last_success_ms"
        const val KEY_ATTEMPT = "last_attempt_ms"
        const val KEY_RESULT = "last_result"
        const val KEY_MESSAGE = "last_message"
        const val KEY_CURSOR_DEVICE = "cursor_device"
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
    private val clientFor: (HubConfig) -> HubClient = { HubClient(it) },
    private val clock: () -> Long = System::currentTimeMillis,
) {
    suspend fun sync(): SyncReport = LOCK.withLock {
        withContext(Dispatchers.IO) { syncLocked().also { status.save(it, clock()) } }
    }

    private fun syncLocked(): SyncReport {
        val hub = pairing.load() ?: return SyncReport(SyncResult.NOT_PAIRED, "Not paired with a hub yet")
        val client = clientFor(hub)
        // After a reinstall the phone counts from 0 again, but the hub may already hold higher seqs from before.
        if (status.cursorDevice != hub.deviceId) {
            when (val cursor = client.cursor()) {
                is HubResult.Ok -> {
                    cursor.value?.let(store::raiseSeqFloor)
                    status.cursorDevice = hub.deviceId
                }
                else -> return stopped(cursor, 0, 0)
            }
        }
        var size = BATCH
        var sent = 0
        var refused = 0
        var stuck = 0
        while (true) {
            val batch = store.pending(size)
            if (batch.isEmpty()) break
            when (val reply = client.send(batch)) {
                is HubResult.Ok -> {
                    val applied = apply(batch, reply.value)
                    sent += applied.sent
                    refused += applied.refused
                    if (applied.wrongDevice) return SyncReport(SyncResult.PAIR_AGAIN, PAIR_AGAIN_MESSAGE, sent, refused)
                    // Renumbered events go again; a hub that keeps refusing the same numbers is not looped forever.
                    stuck = if (applied.sent + applied.refused == 0) stuck + 1 else 0
                    if (stuck >= MAX_STUCK) return SyncReport(SyncResult.UNREACHABLE, "Your hub keeps refusing these events", sent, refused)
                    size = minOf(size * 2, BATCH)
                }
                is HubResult.Split -> if (batch.size == 1) {
                    refused += store.markRejected(batch[0], reply.message)
                } else {
                    size = batch.size / 2
                }
                else -> return stopped(reply, sent, refused)
            }
        }
        val message = if (sent == 0) "Everything was already on your hub" else "Sent ${events(sent)} to your hub"
        return SyncReport(SyncResult.SENT, message, sent, refused)
    }

    private class Applied(val sent: Int, val refused: Int, val wrongDevice: Boolean)

    private fun apply(batch: List<EventEntity>, reply: IngestReply): Applied {
        val byIndex = reply.rejected.associateBy { it.index }
        var refused = 0
        val conflicts = mutableListOf<EventEntity>()
        var wrongDevice = false
        batch.forEachIndexed { index, event ->
            val rejection = byIndex[index] ?: return@forEachIndexed
            when (rejection.code) {
                "seq_conflict" -> conflicts += event
                "wrong_device" -> wrongDevice = true // the token is for another device: re-pair, keep the events
                else -> refused += store.markRejected(event, "${rejection.code}: ${rejection.reason}")
            }
        }
        val sent = store.markSynced(batch.filterIndexed { index, _ -> index !in byIndex })
        if (conflicts.isNotEmpty()) store.renumber(conflicts, reply.lastSeq)
        return Applied(sent, refused, wrongDevice)
    }

    private fun stopped(result: HubResult<*>, sent: Int, refused: Int): SyncReport = when (result) {
        is HubResult.Unauthorized -> SyncReport(SyncResult.PAIR_AGAIN, PAIR_AGAIN_MESSAGE, sent, refused)
        is HubResult.Blocked -> SyncReport(SyncResult.BLOCKED, result.message, sent, refused)
        is HubResult.Retry -> SyncReport(SyncResult.UNREACHABLE, "Couldn't reach your hub (${result.message})", sent, refused)
        is HubResult.Split -> SyncReport(SyncResult.UNREACHABLE, result.message, sent, refused)
        is HubResult.Ok -> SyncReport(SyncResult.SENT, "", sent, refused)
    }

    companion object {
        const val BATCH = 200
        private const val MAX_STUCK = 3
        private const val PAIR_AGAIN_MESSAGE = "Your hub no longer accepts this phone. Pair again."
        private val LOCK = Mutex()

        @Volatile private var instance: Syncer? = null

        fun get(context: Context): Syncer = instance ?: synchronized(this) {
            instance ?: Syncer(EventStore.get(context), PairingStore, SyncStatusStore(context.applicationContext))
                .also { instance = it }
        }

        fun events(count: Int) = if (count == 1) "1 event" else "$count events"
    }
}

class SyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result {
        // New usage first. A failure there (no usage access, a full disk) never stops what is stored from going.
        withContext(Dispatchers.IO) { runCatching { UsageCollector(applicationContext).collect() } }
        val report = Syncer.get(applicationContext).sync()
        // Only the background sync retries (with backoff); after "Sync now" the next periodic run tries again.
        return if (report.result == SyncResult.UNREACHABLE && NOW !in tags) Result.retry() else Result.success()
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

        /** "Sync now": on any network, since you asked. A tap while one is running changes nothing. */
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
