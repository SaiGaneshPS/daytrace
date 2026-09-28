// DT-24: live mode, for demos. A foreground service (with its notice in the notifications) reads usage every 5 s
// and syncs as soon as anything new is stored, so opening an app shows on the hub's timeline within seconds. With
// the screen off it reads once a minute. It never runs by itself: you turn it on, and it ends when you turn it off,
// when the app is closed from recents, or after Android's limit for this kind of service (6 hours a day).
package app.daytrace.android.live

import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationChannelCompat
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import app.daytrace.android.MainActivity
import app.daytrace.android.R
import app.daytrace.android.nudge.NudgeNotifier
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

/**
 * One pass after another: read what is new, send it if anything was stored, wait. A failed read or sync never ends
 * the loop (the next pass tries again); only cancelling it does. Everything it touches is passed in, so the timing
 * rules have tests.
 */
class LiveLoop(
    /** Reads usage; returns how many events were added or changed. */
    private val collect: () -> Int,
    private val sync: suspend () -> Unit,
    private val screenOn: () -> Boolean,
    private val sleep: suspend (Long) -> Unit = { delay(it) },
    private val clock: () -> Long = System::currentTimeMillis,
) {
    suspend fun run() {
        var lastSync = Long.MIN_VALUE
        while (true) {
            currentCoroutineContext().ensureActive()
            val changed = attempt { collect() } ?: 0
            val now = clock()
            // Something new goes at once; otherwise a sync now and then keeps the hub's "last seen" fresh.
            if (changed > 0 || lastSync == Long.MIN_VALUE || now - lastSync >= KEEP_ALIVE_MS) {
                attempt { sync() }
                lastSync = now
            }
            sleep(if (screenOn()) EVERY_MS else SCREEN_OFF_EVERY_MS)
        }
    }

    private inline fun <T> attempt(block: () -> T): T? = try {
        block()
    } catch (cancelled: CancellationException) {
        throw cancelled
    } catch (failure: Exception) {
        Log.w(TAG, "Live mode: a pass failed; the next one tries again", failure)
        null
    }

    companion object {
        const val EVERY_MS = 5_000L
        const val SCREEN_OFF_EVERY_MS = 60_000L
        const val KEEP_ALIVE_MS = 60_000L
        /** How far behind now live mode reads: Android records usage events within milliseconds. */
        const val LATENESS_MS = 2_000L
        private const val TAG = "Daytrace"
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
            // Android refuses a foreground service started while the app is in the background (or past today's
            // limit): stay off, and say so on the status screen.
            Log.w(TAG, "Live mode could not start", refused)
            shutDown()
            return START_NOT_STICKY
        }
        state.value = true
        if (loop == null) {
            val app = applicationContext
            val power = app.getSystemService(PowerManager::class.java)
            val syncer = Syncer.get(app)
            loop = scope.launch {
                LiveLoop(
                    collect = { UsageCollector(app).collect(latenessMs = LiveLoop.LATENESS_MS) },
                    sync = { syncer.sync() },
                    screenOn = { power.isInteractive },
                ).run()
            }
        }
        return START_NOT_STICKY // after the app is killed, live mode stays off until you turn it on again
    }

    /** Android 15 and newer end a data-sync service after 6 hours in a day. */
    override fun onTimeout(startId: Int, fgsType: Int) = shutDown()

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

        /** True while live mode runs. */
        val running: StateFlow<Boolean> get() = state

        /** From the screen (the app in front): Android lets a foreground service start only then. */
        fun start(context: Context): Boolean = runCatching {
            ContextCompat.startForegroundService(context, Intent(context, LiveModeService::class.java))
        }.onFailure { Log.w(TAG, "Live mode could not start", it) }.isSuccess

        fun stop(context: Context) {
            context.stopService(Intent(context, LiveModeService::class.java))
        }
    }
}
