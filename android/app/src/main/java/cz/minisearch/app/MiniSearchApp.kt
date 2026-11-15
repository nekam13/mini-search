package cz.minisearch.app

import android.app.Application
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * Process-wide setup for the bundled Python runtime.
 *
 * Chaquopy's [Python.start] must run exactly once per process, before anything
 * calls [Python.getInstance]. Doing it in [Application.onCreate] guarantees that:
 * the service, the activity and any restart path all share one already-started
 * interpreter instead of racing to initialise it.
 *
 * The startup work here is deliberately tiny — copying files and starting a
 * runtime, no crawling or model loading. Everything heavy (importing
 * ``app_combined``, DB migration, the embedding model) happens on the server
 * thread that [ServerService] owns, never on the main thread.
 */
class MiniSearchApp : Application() {

    override fun onCreate() {
        super.onCreate()
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
            Log.i(TAG, "Python runtime started")
        }
        // The ONNX bridge is initialised lazily on the server thread so the
        // first frame is not blocked by a 118 MB model load.
    }

    companion object {
        private const val TAG = "MiniSearchApp"
    }
}
