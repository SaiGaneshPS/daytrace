// DT-24: the nudge the hub sends back with a batch (DT-43), shown as a notification in the app's colors. The hub
// sends each nudge once (it logs it and rests the rule), so there is nothing to remember here: the notification's id
// comes from the nudge, and a repeat would only replace the same notification. Nothing shows when notifications,
// or just the Nudges channel, are off (the hub keeps its own log).
package app.daytrace.android.nudge

import android.annotation.SuppressLint
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationChannelCompat
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import app.daytrace.android.MainActivity
import app.daytrace.android.R
import app.daytrace.android.sync.Nudge
import app.daytrace.android.ui.Permissions

class NudgeNotifier(private val context: Context) {
    /** Shows [nudge]; false when notifications, or the Nudges channel, are off. */
    @SuppressLint("MissingPermission") // Permissions.notificationsGranted covers POST_NOTIFICATIONS on Android 13+
    fun show(nudge: Nudge): Boolean {
        if (!Permissions.notificationsGranted(context)) return false
        val manager = NotificationManagerCompat.from(context)
        manager.createNotificationChannel(
            NotificationChannelCompat.Builder(CHANNEL, NotificationManagerCompat.IMPORTANCE_HIGH)
                .setName("Nudges")
                .setDescription("A gentle nudge from your hub when a study block is slipping or it's past your bedtime.")
                .build(),
        )
        // Creating it again keeps what you chose for it: switched off, it stays off.
        if (manager.getNotificationChannelCompat(CHANNEL)?.importance == NotificationManagerCompat.IMPORTANCE_NONE) return false
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
        manager.notify(notificationId(nudge), notification)
        return true
    }

    companion object {
        const val CHANNEL = "nudges"
        /** The app's indigo (ui/theme: IndigoLight). */
        const val ACCENT = 0xFF6C5CE7.toInt()

        /** One per nudge; a nudge without its time (never from the hub) gets one of its own. */
        fun notificationId(nudge: Nudge): Int = (nudge.id ?: "${nudge.title}\n${nudge.body}\n${System.nanoTime()}").hashCode()
    }
}
