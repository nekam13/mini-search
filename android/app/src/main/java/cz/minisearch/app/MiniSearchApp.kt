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
        Log.i(TAG, "Application created; Python initialising in background")
    }

    companion object {
        private const val TAG = "MiniSearchApp"
    }
}
