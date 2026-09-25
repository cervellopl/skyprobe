package net.skyprobe.app

import android.app.Notification
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationChannelCompat
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat

/**
 * Keeps the app process alive while an upload or analysis is running.
 *
 * AppViewModel drives the actual work (uploading, polling the server) in coroutines scoped to
 * the ViewModel, not to this service - without something in the foreground state, Android is
 * free to kill the whole background process for memory the moment the screen turns off or
 * another app comes forward, taking those coroutines down with it. A finished analysis on the
 * server is never lost either way, but the app would stop noticing until reopened. This service
 * does no work of its own; it only has to exist, with a visible notification, for the process
 * to be protected - the same trick a download manager uses.
 */
class AnalysisService : Service() {
    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        NotificationManagerCompat.from(this).createNotificationChannel(
            NotificationChannelCompat.Builder(CHANNEL, NotificationManagerCompat.IMPORTANCE_LOW)
                .setName("Analysis progress")
                .setDescription("Shown while an image is uploading or being analysed")
                .build(),
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val text = intent?.getStringExtra(EXTRA_TEXT) ?: "Working…"
        ServiceCompat.startForeground(this, NOTIFICATION_ID, notification(text), ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        return START_NOT_STICKY
    }

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0),
        )
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_brand)
            .setContentTitle("SkyProbe")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .build()
    }

    companion object {
        private const val CHANNEL = "analysis"
        private const val NOTIFICATION_ID = 1
        private const val EXTRA_TEXT = "text"

        /** Starts the service, or - if it is already running - just updates its notification. */
        fun start(context: Context, text: String) {
            val intent = Intent(context, AnalysisService::class.java).putExtra(EXTRA_TEXT, text)
            ContextCompat.startForegroundService(context, intent)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, AnalysisService::class.java))
        }
    }
}
