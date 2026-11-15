package cz.minisearch.app

import android.content.Context
import android.util.Log
import com.chaquo.python.Python
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.CopyOnWriteArrayList

/** Real lifecycle states of the bundled server. */
enum class ServerState { STOPPED, STARTING, RUNNING, STOPPING, ERROR }

/**
 * Owns the bundled Python server's lifecycle for the whole process.
 *
 * A created Python thread is *not* proof the server is up, so [start] does not
 * report RUNNING until an actual HTTP readiness probe on ``/health`` succeeds,
 * within a bounded timeout. All the heavy work (importing the backend, DB
 * migration, loading the model, binding the socket) runs on a background thread,
 * never on the UI thread.
 *
 * The controller is deliberately idempotent: rotation, returning to the app or a
 * repeated service start must not spawn a second server, scheduler or worker
 * pool, so every transition is guarded by a lock and an in-flight flag.
 */
object ServerController {

    private const val TAG = "ServerController"
    private const val READY_TIMEOUT_MS = 60_000L
    private const val POLL_INTERVAL_MS = 250L

    const val HOST = "127.0.0.1"
    const val PORT = 8070
    const val BASE_URL = "http://$HOST:$PORT/"

    @Volatile
    var state: ServerState = ServerState.STOPPED
        private set

    @Volatile
    var lastError: String = ""
        private set

    @Volatile
    var backendLabel: String = ""
        private set

    private val listeners = CopyOnWriteArrayList<(ServerState) -> Unit>()
    private val lock = Any()
    private var inFlight = false

    fun addListener(listener: (ServerState) -> Unit) {
        listeners.add(listener)
        listener(state)
    }

    fun removeListener(listener: (ServerState) -> Unit) {
        listeners.remove(listener)
    }

    /** Surface an error raised outside the start path (e.g. the service). */
    fun reportError(message: String) {
        setState(ServerState.ERROR, message)
    }

    private fun setState(newState: ServerState, error: String = "") {
        synchronized(lock) {
            state = newState
            if (error.isNotEmpty()) lastError = error
            if (newState != ServerState.ERROR) lastError = ""
        }
        Log.i(TAG, "state -> $newState${if (error.isEmpty()) "" else " ($error)"}")
        for (listener in listeners) {
            try {
                listener(newState)
            } catch (t: Throwable) {
                Log.w(TAG, "listener failed", t)
            }
        }
    }

    /** Start the server if it is not already starting or running. */
    fun start(context: Context) {
        val appContext = context.applicationContext
        synchronized(lock) {
            if (inFlight || state == ServerState.RUNNING || state == ServerState.STARTING) {
                Log.i(TAG, "start ignored (state=$state, inFlight=$inFlight)")
                return
            }
            inFlight = true
        }
        setState(ServerState.STARTING)
        Thread({ doStart(appContext) }, "minisearch-start").start()
    }

    private fun doStart(context: Context) {
        try {
            EmbeddingBridge.setContext(context)
            SystemStatus.setContext(context)
            val entry = pythonEntry()
            val dataDir = context.filesDir.absolutePath
            val status = entry.callAttr("start", dataDir).toString()
            Log.i(TAG, "python start -> $status")
            if (!awaitReady()) {
                throw IllegalStateException(
                    "Server neodpověděl na http://$HOST:$PORT/health do ${READY_TIMEOUT_MS / 1000} s")
            }
            backendLabel = readBackendLabel(entry)
            setState(ServerState.RUNNING)
        } catch (t: Throwable) {
            Log.e(TAG, "server start failed", t)
            setState(ServerState.ERROR, describe(t))
        } finally {
            synchronized(lock) { inFlight = false }
        }
    }

    /** Stop the server and release its resources; safe to call when stopped. */
    fun stop() {
        synchronized(lock) {
            if (inFlight) {
                Log.i(TAG, "stop requested while busy; will retry")
            }
            if (state == ServerState.STOPPED) {
                return
            }
            inFlight = true
        }
        setState(ServerState.STOPPING)
        Thread({
            try {
                pythonEntry().callAttr("stop")
            } catch (t: Throwable) {
                Log.w(TAG, "python stop failed", t)
            } finally {
                EmbeddingBridge.reset()
                synchronized(lock) { inFlight = false }
                setState(ServerState.STOPPED)
            }
        }, "minisearch-stop").start()
    }

    /** Stop then start in the same process (the retry action). */
    fun restart(context: Context) {
        val appContext = context.applicationContext
        synchronized(lock) {
            if (inFlight) return
            inFlight = true
        }
        setState(ServerState.STARTING)
        Thread({
            try {
                val entry = pythonEntry()
                entry.callAttr("stop")
                EmbeddingBridge.reset()
                val dataDir = appContext.filesDir.absolutePath
                entry.callAttr("start", dataDir)
                if (!awaitReady()) {
                    throw IllegalStateException("Server se nepodařilo znovu spustit.")
                }
                backendLabel = readBackendLabel(entry)
                setState(ServerState.RUNNING)
            } catch (t: Throwable) {
                Log.e(TAG, "restart failed", t)
                setState(ServerState.ERROR, describe(t))
            } finally {
                synchronized(lock) { inFlight = false }
            }
        }, "minisearch-restart").start()
    }

    /**
     * Ask Python to import a Wikipedia dump that already sits in private
     * storage. Runs on a background thread and reports the JSON result (or a
     * failure message) through [onResult]; the caller must ensure the server is
     * running, because the import shares the backend's database.
     */
    fun importDump(path: String, onResult: (String?, String?) -> Unit) {
        Thread({
            try {
                val json = pythonEntry().callAttr("import_wiki_dump", path).toString()
                onResult(json, null)
            } catch (t: Throwable) {
                Log.e(TAG, "dump import failed", t)
                onResult(null, describe(t))
            }
        }, "minisearch-dump").start()
    }

    /** Poll ``/health`` until it answers or the timeout expires. */
    private fun awaitReady(): Boolean {
        val deadline = System.currentTimeMillis() + READY_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline) {
            if (probeHealth()) return true
            try {
                Thread.sleep(POLL_INTERVAL_MS)
            } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
                return false
            }
        }
        return false
    }

    private fun probeHealth(): Boolean {
        var connection: HttpURLConnection? = null
        return try {
            connection = (URL("${BASE_URL}health").openConnection() as HttpURLConnection).apply {
                connectTimeout = 1500
                readTimeout = 1500
                requestMethod = "GET"
            }
            connection.responseCode == 200
        } catch (t: Throwable) {
            false
        } finally {
            connection?.disconnect()
        }
    }

    private fun readBackendLabel(entry: com.chaquo.python.PyObject): String {
        return try {
            val json = entry.callAttr("status").toString()
            Regex("\"embeddings\"\\s*:\\s*\"([^\"]*)\"").find(json)?.groupValues?.get(1) ?: ""
        } catch (t: Throwable) {
            ""
        }
    }

    private fun pythonEntry(): com.chaquo.python.PyObject {
        // The interpreter is bootstrapped on a background thread by
        // MiniSearchApp; wait for it here (still off the main thread) instead of
        // assuming it is already up.
        PythonRuntime.awaitStarted()
        if (!Python.isStarted()) {
            throw IllegalStateException("Python runtime není inicializovaný")
        }
        return Python.getInstance().getModule("android_entry")
    }

    private fun describe(t: Throwable): String {
        val message = t.message ?: t.javaClass.simpleName
        return message.take(400)
    }
}
