package cz.minisearch.app

import android.app.Application
import android.util.Log

/**
 * Process-wide setup.
 *
 * The Python runtime is bootstrapped through [PythonRuntime], which starts
 * Chaquopy on a *background* thread. Doing it inline in ``onCreate`` would block
 * the main thread long enough that ``ServerService`` misses the foreground
 * start deadline and the process is killed. Everything heavy (importing
 * ``app_combined``, DB migration, the embedding model) still happens on the
 * server thread that [ServerController] owns.
 */
class MiniSearchApp : Application() {

    override fun onCreate() {
        super.onCreate()
        PythonRuntime.ensureStarted(this)
        // Debug builds only: lets the instrumentation/smoke tests (and
        // chrome://inspect) drive the WebView, so the confirm()/alert() dialogs
        // the admin panel relies on can be exercised on a real device. It is
        // never enabled in a release build.
        val debuggable =
            (applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE) != 0
        if (debuggable) {
            android.webkit.WebView.setWebContentsDebuggingEnabled(true)
        }
        Log.i(TAG, "Application created; Python initialising in background")
    }

    companion object {
        private const val TAG = "MiniSearchApp"
    }
}
