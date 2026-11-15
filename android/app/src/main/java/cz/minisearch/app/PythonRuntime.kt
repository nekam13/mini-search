package cz.minisearch.app

import android.content.Context
import android.util.Log
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * Owns the one-time Chaquopy bootstrap.
 *
 * ``Python.start()`` must run exactly once per process and before any
 * ``Python.getInstance()`` call. It is also expensive (it unpacks the standard
 * library and starts the interpreter), so running it in
 * ``Application.onCreate`` on the main thread is wrong: it delays
 * ``Service.onStartCommand`` past the 20 s foreground-service start timeout and
 * Android kills the app with
 * ``ForegroundServiceDidNotStartInTimeException``.
 *
 * The interpreter is therefore started on a dedicated background thread and
 * every consumer waits on a latch through [awaitStarted] before touching
 * ``Python.getInstance()``. The work happens exactly once; a second caller only
 * waits for the first one to finish.
 */
object PythonRuntime {

    private const val TAG = "PythonRuntime"

    /** How long a caller waits for the interpreter before giving up. */
    const val INIT_TIMEOUT_MS = 120_000L

    private val lock = Object()

    @Volatile
    private var started = false

    @Volatile
    private var failure: Throwable? = null

    @Volatile
    private var initThread: Thread? = null

    /** Kick off the interpreter once; later calls are no-ops. */
    fun ensureStarted(context: Context) {
        val appContext = context.applicationContext
        synchronized(lock) {
            if (initThread != null) return
            if (Python.isStarted()) {
                started = true
                return
            }
            initThread = Thread({
                try {
                    if (!Python.isStarted()) {
                        Python.start(AndroidPlatform(appContext))
                    }
                    synchronized(lock) {
                        started = true
                        lock.notifyAll()
                    }
                    Log.i(TAG, "Python runtime started")
                } catch (t: Throwable) {
                    Log.e(TAG, "Python runtime failed to start", t)
                    synchronized(lock) {
                        failure = t
                        lock.notifyAll()
                    }
                }
            }, "minisearch-python-init").apply { isDaemon = true }.also { it.start() }
        }
    }

    /**
     * Block until the interpreter is usable, then return. Throws when it could
     * not be started or the timeout expired, so the caller can surface a real
     * error instead of an obscure ``Python.getInstance()`` failure.
     */
    fun awaitStarted(timeoutMs: Long = INIT_TIMEOUT_MS) {
        synchronized(lock) {
            val deadline = System.currentTimeMillis() + timeoutMs
            while (!started && failure == null) {
                val remaining = deadline - System.currentTimeMillis()
                if (remaining <= 0) {
                    throw IllegalStateException(
                        "Python se nepodařilo inicializovat do ${timeoutMs / 1000} s")
                }
                lock.wait(remaining)
            }
            failure?.let {
                throw IllegalStateException("Python nelze spustit: ${it.message}", it)
            }
        }
    }
}
