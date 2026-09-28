// DT-24: the nudge the hub sends back with a batch (DT-43), shown as a notification in the app's colors. A nudge is
// shown once, however many replies carry it, and only when notifications are allowed (the hub keeps its own log).
package app.daytrace.android.nudge

import android.Manifest
import android.annotation.SuppressLint
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationChannelCompat
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.core.content.edit
import app.daytrace.android.MainActivity
import app.daytrace.android.R
import app.daytrace.android.sync.Nudge

/** Which nudges were shown already: the last few, kept as text. Pure, so the rule has a test. */
object ShownNudges {
    const val KEEP = 20

    fun parse(text: String?): List<String> = text?.split('\n')?.filter { it.isNotEmpty() }.orEmpty()

    /** The list after showing [id], or null when [id] was shown already. */
    fun add(shown: List<String>, id: String): List<String>? = if (id in shown) null else (shown + id).takeLast(KEEP)
}

class NudgeNotifier(private val context: Context) {
    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /** Shows [nudge]; false when notifications are off or it was shown already. */
    @SuppressLint("MissingPermission") // checked in allowed()
    fun show(nudge: Nudge): Boolean = synchronized(LOCK) {
        if (!allowed()) return false
        val shown = ShownNudges.add(ShownNudges.parse(prefs.getString(SHOWN, null)), nudge.id) ?: return false
        val manager = NotificationManagerCompat.from(context)
        manager.createNotificationChannel(
            NotificationChannelCompat.Builder(CHANNEL, NotificationManagerCompat.IMPORTANCE_HIGH)
                .setName("Nudges")
                .setDescription("A gentle nudge from your hub when a study block is slipping or it's past your bedtime.")
                .build(),
        )
        val open = PendingIntent.getActivity(
            context, 0, Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val notification = NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_daytrace)
            .setColor(ACCENT)
            .setContentTitle(nudge.title)
            .setContentText(nudge.body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(nudge.body))
            .setCategory(NotificationCompat.CATEGORY_REMINDER)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(open)
            .setAutoCancel(true)
            .build()
        manager.notify(nudge.id.hashCode(), notification)
        prefs.edit(commit = true) { putString(SHOWN, shown.joinToString("\n")) }
        return true
    }

    private fun allowed(): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            return false
        }
        return NotificationManagerCompat.from(context).areNotificationsEnabled()
    }

    companion object {
        const val CHANNEL = "nudges"
        /** The app's indigo (ui/theme: IndigoLight). */
        const val ACCENT = 0xFF6C5CE7.toInt()
        private const val PREFS = "nudges"
        private const val SHOWN = "shown"
        private val LOCK = Any()
    }
}
