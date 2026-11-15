package cz.minisearch.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat

/**
 * Foreground service that keeps the bundled Mini Search server alive while the
 * app is in the background.
 *
 * Foreground service type: ``specialUse``. The service runs a *local* HTTP
 * server plus a crawler; it is not a data-transfer job (``dataSync``), and it is
 * not media playback, so ``specialUse`` is the honest classification. The type
 * requires ``FOREGROUND_SERVICE_SPECIAL_USE`` and a manifest ``property`` with a
 * justification (see ``AndroidManifest.xml``).
 *
 * A foreground service does *not* make the process immune to Doze or to the
 * system killing it under memory pressure; it only raises the priority. The UI
 * therefore explains the battery impact of continuous operation, and the user
 * can stop the server for good with the notification action or the in-app button.
 * After an explicit stop the service is not restarted by itself.
 */
class ServerService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopServerAndSelf()
            return START_NOT_STICKY
        }
        startInForeground()
        // If the system restarts us after a kill, bring the server back up; but
        // never after an explicit user stop (that path returns NOT_STICKY above).
        if (UserState.serverEnabled) {
            ServerController.start(this)
        }
        return START_STICKY
    }

    private fun startInForeground() {
        createChannel()
        val notification = buildNotification()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun stopServerAndSelf() {
        UserState.serverEnabled = false
        ServerController.stop()
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val stop = PendingIntent.getService(
            this, 1,
            Intent(this, ServerService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.service_text))
            .setSmallIcon(android.R.drawable.ic_menu_search)
            .setContentIntent(open)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel,
                getString(R.string.action_stop), stop)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.service_channel),
                NotificationManager.IMPORTANCE_LOW
            )
            channel.description = getString(R.string.service_channel_desc)
            val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            manager.createNotificationChannel(channel)
        }
    }

    companion object {
        private const val CHANNEL_ID = "minisearch_server"
        private const val NOTIFICATION_ID = 1
        const val ACTION_STOP = "cz.minisearch.app.STOP"

        fun start(context: Context) {
            val intent = Intent(context, ServerService::class.java)
            context.startForegroundService(intent)
        }

        fun stop(context: Context) {
            val intent = Intent(context, ServerService::class.java).setAction(ACTION_STOP)
            context.startService(intent)
        }

        /** True when notifications are permitted (Android 13+ needs a grant). */
        fun notificationsAllowed(context: Context): Boolean =
            NotificationManagerCompat.from(context).areNotificationsEnabled()
    }
}

/** Small process-wide flags the UI and service share. */
object UserState {
    /** False once the user explicitly stops the server; blocks auto-restart. */
    @Volatile
    var serverEnabled: Boolean = true
}
