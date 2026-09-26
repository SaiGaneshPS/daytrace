// DT-19 / DT-21: Application class. Schedules the background sync (WorkManager keeps it across restarts).
package app.daytrace.android

import android.app.Application
import app.daytrace.android.sync.SyncWorker

class DaytraceApp : Application() {
    override fun onCreate() {
        super.onCreate()
        SyncWorker.schedule(this)
    }
}
