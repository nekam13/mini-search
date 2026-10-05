package cz.minisearch.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat

/**
 * Keeps the bundled Mini Search server alive while the app is in the
 * background. Android's Doze mode would otherwise suspend the crawler and the
 * scheduled jobs; a foreground service with a persistent notification keeps
 * the process running and gives the user a way to stop it.
 */
class ServerService : Service() {

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        createChannel()
        startForeground(NOTIFICATION_ID, buildNotification())
        startServer()
        return START_STICKY
    }

    private fun startServer() {
        try {
            val py = com.chaquo.python.Python.getInstance()
            val entry = py.getModule("android_entry")
            val result = entry.callAttr("start", filesDir.absolutePath).toString()
            android.util.Log.i(TAG, "Python server: $result")
        } catch (t: Throwable) {
            android.util.Log.e(TAG, "Python server failed to start", t)
        }
    }

    private fun buildNotification(): Notification {
        val open = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(getString(R.string.service_text))
            .setSmallIcon(android.R.drawable.ic_menu_search)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.service_channel),
                NotificationManager.IMPORTANCE_LOW
            )
            val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            manager.createNotificationChannel(channel)
        }
    }

    companion object {
        private const val TAG = "MiniSearchService"
        private const val CHANNEL_ID = "minisearch_server"
        private const val NOTIFICATION_ID = 1

        fun start(context: Context) {
            val intent = Intent(context, ServerService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, ServerService::class.java))
        }
    }
}
