// DT-24: live mode, for demos. A foreground service (with its notice in the notifications) reads usage every 5 s
// and syncs as soon as anything is waiting, so opening an app shows on the hub's timeline within seconds. With the
// screen off it reads once a minute, and at once when the screen comes back on. It never runs by itself: you turn
// it on, and it ends when you turn it off, forget the hub, swipe the app away from recents, when the hub no longer
// accepts this phone, or after Android's limit for this kind of service (6 hours a day).
package app.daytrace.android.live

import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import androidx.core.app.NotificationChannelCompat
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import androidx.core.content.edit
import app.daytrace.android.MainActivity
import app.daytrace.android.R
import app.daytrace.android.data.EventStore
import app.daytrace.android.nudge.NudgeNotifier
import app.daytrace.android.sync.SyncResult
import app.daytrace.android.sync.Syncer
import app.daytrace.android.usage.UsageCollector
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/** What a sync means for live mode: go on, try again a little later, or stop (the phone isn't paired any more). */
enum class LiveSync { SENT, LATER, STOP }

/**
 * One pass after another: read what is new, send whatever is waiting, wait. A failed read or sync never ends the
 * loop (the next pass tries again, a failed sync after [RETRY_MS]); only [LiveSync.STOP] or cancelling does. Every
 * input is passed in, so the timing rules have tests.
 */
class LiveLoop(
    /** Reads usage; returns how many events were added or changed. */
    private val collect: () -> Int,
    /** Whether anything is still waiting to go to the hub. */
    private val waiting: () -> Boolean,
    private val sync: suspend () -> LiveSync,
    private val screenOn: () -> Boolean,
    private val sleep: suspend (Long) -> Unit = { delay(it) },
    /** Time since boot: setting the wall clock never changes the waits. */
    private val clock: () -> Long = SystemClock::elapsedRealtime,
) {
    suspend fun run() {
        var failedAt: Long? = null
        while (true) {
            currentCoroutineContext().ensureActive()
            val changed = attempt { collect() } ?: 0
            val due = failedAt?.let { clock() - it >= RETRY_MS } ?: true
            if (due && (changed > 0 || attempt { waiting() } == true)) {
                when (attempt { sync() } ?: LiveSync.LATER) {
                    LiveSync.SENT -> failedAt = null
                    LiveSync.LATER -> failedAt = clock()
                    LiveSync.STOP -> return
                }
            }
            pause()
        }
    }

    /** [EVERY_MS]; with the screen off, up to [SCREEN_OFF_EVERY_MS], but no longer once it comes back on. */
    private suspend fun pause() {
        var waited = 0L
        do {
            sleep(EVERY_MS)
            waited += EVERY_MS
        } while (waited < SCREEN_OFF_EVERY_MS && attempt { screenOn() } == false)
    }

    private inline fun <T> attempt(block: () -> T): T? = try {
        block()
    } catch (cancelled: CancellationException) {
        throw cancelled
    } catch (failure: Throwable) {
        Log.w(TAG, "Live mode: a pass failed; the next one tries again", failure)
        null
    }

    companion object {
        const val EVERY_MS = 5_000L
        const val SCREEN_OFF_EVERY_MS = 60_000L
        /** After a failed sync (the hub away, the phone off its Wi-Fi), the next try waits this long. */
        const val RETRY_MS = 30_000L
        /**
         * How far behind now live mode reads. Android stamps usage events as it reports them, so they are there
         * within milliseconds; one recorded later than this is never read, and its app's session then ends at the
         * next screen-off instead (the background sync reads 15 s behind, but can't go back past live mode's reads).
         */
        const val LATENESS_MS = 3_000L
        private const val TAG = "Daytrace"

        fun of(result: SyncResult): LiveSync = when (result) {
            SyncResult.SENT -> LiveSync.SENT
            SyncResult.NOT_PAIRED, SyncResult.PAIR_AGAIN -> LiveSync.STOP
            SyncResult.NOT_ON_WIFI, SyncResult.BLOCKED, SyncResult.UNREACHABLE -> LiveSync.LATER
        }
    }
}

class LiveModeService : Service() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var loop: Job? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            shutDown()
            return START_NOT_STICKY
        }
        try {
            ServiceCompat.startForeground(this, NOTIFICATION_ID, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } catch (refused: Exception) {
            // Android refuses a foreground service started from the background, or past today's limit.
            Log.w(TAG, "Live mode could not start", refused)
            refusedState.value = true
            shutDown()
            return START_NOT_STICKY
        }
        refusedState.value = false
        setTimedOut(this, false)
        state.value = true
        if (loop == null) {
            val app = applicationContext
            val power = app.getSystemService(PowerManager::class.java)
            val usage = UsageCollector(app) // one for the whole session: it keeps its app names and home screens
            val store = EventStore.get(app)
            val syncer = Syncer.get(app)
            loop = scope.launch {
                LiveLoop(
                    collect = { usage.collect(latenessMs = LiveLoop.LATENESS_MS) },
                    waiting = { store.counts().waiting > 0 },
                    sync = { LiveLoop.of(syncer.sync().result) },
                    screenOn = { power.isInteractive },
                ).run()
                ContextCompat.getMainExecutor(app).execute { shutDown() } // the phone isn't paired any more
            }
        }
        return START_NOT_STICKY // after the app is killed, live mode stays off until you turn it on again
    }

    /** Android 15 and newer end a data-sync service after 6 hours in a day; the status screen then says so. */
    override fun onTimeout(startId: Int, fgsType: Int) {
        setTimedOut(this, true)
        shutDown()
    }

    override fun onTaskRemoved(rootIntent: Intent?) = shutDown()

    override fun onDestroy() {
        scope.cancel()
        state.value = false
        super.onDestroy()
    }

    private fun shutDown() {
        state.value = false
        loop?.cancel()
        loop = null
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun notification() = run {
        NotificationManagerCompat.from(this).createNotificationChannel(
            NotificationChannelCompat.Builder(CHANNEL, NotificationManagerCompat.IMPORTANCE_LOW)
                .setName("Live mode")
                .setDescription("Shown while live mode sends this phone's app use to your hub within seconds.")
                .build(),
        )
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, LiveModeService::class.java).setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE,
        )
        NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_daytrace)
            .setColor(NudgeNotifier.ACCENT)
            .setContentTitle("Live mode is on")
            .setContentText("Your hub sees this phone's app use within seconds.")
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setContentIntent(open)
            .addAction(0, "Stop", stop)
            .build()
    }

    companion object {
        private const val CHANNEL = "live"
        private const val NOTIFICATION_ID = 24
        private const val ACTION_STOP = "app.daytrace.android.live.STOP"
        private const val TAG = "Daytrace"
        private val state = MutableStateFlow(false)
        private val refusedState = MutableStateFlow(false)
        private val timedOutState = MutableStateFlow(false)

        /** True while live mode runs. */
        val running: StateFlow<Boolean> get() = state

        /** True when Android refused the last start (shown on the status screen until the next one works). */
        val refused: StateFlow<Boolean> get() = refusedState

        /**
         * True when Android ended live mode at its time limit, until the next start (or Stop, or Forget). Kept on the
         * phone: after a timeout overnight Android has usually ended the app too, and the screen must still say why.
         */
        fun timedOut(context: Context): StateFlow<Boolean> {
            if (!timedOutRead) {
                timedOutState.value = prefs(context).getBoolean(KEY_TIMED_OUT, false)
                timedOutRead = true
            }
            return timedOutState
        }

        internal fun setTimedOut(context: Context, value: Boolean) {
            prefs(context).edit(commit = true) { putBoolean(KEY_TIMED_OUT, value) }
            timedOutState.value = value
            timedOutRead = true
        }

        /** For tests: forget what was read, as a new process would. */
        internal fun forgetTimedOutRead() {
            timedOutRead = false
        }

        private fun prefs(context: Context) = context.applicationContext.getSharedPreferences("live_mode", Context.MODE_PRIVATE)
        private const val KEY_TIMED_OUT = "timed_out"
        @Volatile private var timedOutRead = false

        /** From the screen (the app in front): Android lets a foreground service start only then. */
        fun start(context: Context) {
            runCatching { ContextCompat.startForegroundService(context, Intent(context, LiveModeService::class.java)) }
                .onFailure {
                    Log.w(TAG, "Live mode could not start", it)
                    refusedState.value = true
                }
        }

        fun stop(context: Context) {
            setTimedOut(context, false) // stopped on purpose (or Forget): nothing left to explain
            context.stopService(Intent(context, LiveModeService::class.java))
        }
    }
}
