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

    /** URL a PC on the same network can open, or "" when LAN sharing is off. */
    @Volatile
    var lanUrl: String = ""
        private set

    /** True when the public search page is currently shared on the LAN. */
    @Volatile
    var lanShareEnabled: Boolean = false
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
            refreshLanInfo(entry)
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
                refreshLanInfo(entry)
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
     * Turn LAN sharing on or off and restart the server so the new bind address
     * takes effect. The value is persisted by Python (``app_settings``), so it
     * survives a restart. Runs on a background thread; the UI follows the state
     * listener.
     */
    fun setLanShare(context: Context, enabled: Boolean) {
        val appContext = context.applicationContext
        synchronized(lock) {
            if (inFlight) {
                Log.i(TAG, "setLanShare ignored, a transition is in flight")
                return
            }
            inFlight = true
        }
        setState(ServerState.STARTING)
        Thread({
            try {
                val entry = pythonEntry()
                entry.callAttr("set_lan_share", enabled)
                // The bind address is only read at start, so re-bind now.
                entry.callAttr("stop")
                EmbeddingBridge.reset()
                val dataDir = appContext.filesDir.absolutePath
                entry.callAttr("start", dataDir)
                if (!awaitReady()) {
                    throw IllegalStateException("Server se nepodařilo znovu spustit.")
                }
                backendLabel = readBackendLabel(entry)
                refreshLanInfo(entry)
                setState(ServerState.RUNNING)
            } catch (t: Throwable) {
                Log.e(TAG, "LAN share change failed", t)
                setState(ServerState.ERROR, describe(t))
            } finally {
                synchronized(lock) { inFlight = false }
            }
        }, "minisearch-lan").start()
    }

    /**
     * Load the grouped settings JSON from the backend (``SETTINGS_SPEC`` driven)
     * and hand it to [onResult] on a background thread. A null JSON plus a
     * non-null error means the call failed.
     */
    fun loadSettings(onResult: (String?, String?) -> Unit) {
        Thread({
            try {
                val json = pythonEntry().callAttr("settings_json").toString()
                onResult(json, null)
            } catch (t: Throwable) {
                Log.e(TAG, "settings load failed", t)
                onResult(null, describe(t))
            }
        }, "minisearch-settings-load").start()
    }

    /**
     * Persist the settings the native screen collected.
     *
     * When the backend reports ``restart_needed`` (a saved item is flagged
     * ``restart`` in ``SETTINGS_SPEC`` and its value actually changed), the
     * server is stopped and started again in the same process so the change
     * takes effect instead of silently doing nothing. [onResult] receives
     * ``(message, error, restarted)``.
     */
    fun saveSettings(settingsJson: String, context: Context,
                     onResult: (String?, String?, Boolean) -> Unit) {
        val appContext = context.applicationContext
        synchronized(lock) {
            if (inFlight) {
                onResult(null, "Probíhá jiná operace se serverem, zkuste to za okamžik.", false)
                return
            }
            inFlight = true
        }
        Thread({
            var restartNeeded = false
            try {
                val entry = pythonEntry()
                val payload = org.json.JSONObject()
                payload.put("settings", org.json.JSONObject(settingsJson))
                val result = org.json.JSONObject(
                    entry.callAttr("save_settings_json", payload.toString()).toString())
                if (result.has("error")) {
                    onResult(null, result.optString("error"), false)
                    return@Thread
                }
                restartNeeded = result.optBoolean("restart_needed", false)
                if (restartNeeded) {
                    // Re-bind so the change applies; the caller (UI) follows the
                    // state listener while this runs.
                    setState(ServerState.STARTING)
                    entry.callAttr("stop")
                    EmbeddingBridge.reset()
                    entry.callAttr("start", appContext.filesDir.absolutePath)
                    if (!awaitReady()) {
                        throw IllegalStateException("Server se nepodařilo po změně nastavení znovu spustit.")
                    }
                    backendLabel = readBackendLabel(entry)
                    refreshLanInfo(entry)
                    setState(ServerState.RUNNING)
                }
                onResult(result.optString("message", "Nastavení uloženo"), null, restartNeeded)
            } catch (t: Throwable) {
                Log.e(TAG, "settings save failed", t)
                onResult(null, describe(t), restartNeeded)
            } finally {
                synchronized(lock) { inFlight = false }
            }
        }, "minisearch-settings-save").start()
    }

    /**
     * Reset every stored setting to its environment default, then restart so the
     * defaults are in effect. [onResult] receives ``(message, error)``.
     */
    fun resetSettings(context: Context, onResult: (String?, String?) -> Unit) {
        val appContext = context.applicationContext
        synchronized(lock) {
            if (inFlight) {
                onResult(null, "Probíhá jiná operace se serverem, zkuste to za okamžik.")
                return
            }
            inFlight = true
        }
        Thread({
            try {
                val entry = pythonEntry()
                entry.callAttr("reset_settings_json")
                setState(ServerState.STARTING)
                entry.callAttr("stop")
                EmbeddingBridge.reset()
                entry.callAttr("start", appContext.filesDir.absolutePath)
                if (!awaitReady()) {
                    throw IllegalStateException("Server se nepodařilo po obnovení nastavení znovu spustit.")
                }
                backendLabel = readBackendLabel(entry)
                refreshLanInfo(entry)
                setState(ServerState.RUNNING)
                onResult("Nastavení obnoveno na výchozí hodnoty.", null)
            } catch (t: Throwable) {
                Log.e(TAG, "settings reset failed", t)
                onResult(null, describe(t))
            } finally {
                synchronized(lock) { inFlight = false }
            }
        }, "minisearch-settings-reset").start()
    }

    /** Read ``lan_share`` / ``lan_url`` from the backend status JSON. */
    private fun refreshLanInfo(entry: com.chaquo.python.PyObject) {
        try {
            val json = entry.callAttr("status").toString()
            lanShareEnabled = Regex("\"lan_share\"\\s*:\\s*(true|false)")
                .find(json)?.groupValues?.get(1) == "true"
            lanUrl = Regex("\"lan_url\"\\s*:\\s*\"([^\"]*)\"")
                .find(json)?.groupValues?.get(1) ?: ""
        } catch (t: Throwable) {
            Log.w(TAG, "lan info read failed", t)
            lanShareEnabled = false
            lanUrl = ""
        }
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

    /**
     * Load the source list for the native "Zdroje" screen. Runs on a background
     * thread and hands the JSON (or an error) to [onResult].
     */
    fun loadSources(onResult: (String?, String?) -> Unit) {
        Thread({
            try {
                val json = pythonEntry().callAttr("sources_json").toString()
                onResult(json, null)
            } catch (t: Throwable) {
                Log.e(TAG, "sources load failed", t)
                onResult(null, describe(t))
            }
        }, "minisearch-sources-load").start()
    }

    /**
     * Register a source typed on the native screen. [payloadJson] is
     * ``{"url": "...", "type": "auto"|...}``; indexing starts on the backend.
     * [onResult] receives the parsed ``(message, error)``.
     */
    fun addSource(payloadJson: String, onResult: (String?, String?) -> Unit) {
        Thread({
            try {
                val json = pythonEntry().callAttr("add_source_json", payloadJson).toString()
                reportSourceResult(json, onResult)
            } catch (t: Throwable) {
                Log.e(TAG, "source add failed", t)
                onResult(null, describe(t))
            }
        }, "minisearch-source-add").start()
    }

    /** Delete a source (or its whole domain) and report ``(message, error)``. */
    fun deleteSource(sourceId: Int, onResult: (String?, String?) -> Unit) {
        Thread({
            try {
                val json = pythonEntry().callAttr("delete_source_json", sourceId).toString()
                reportSourceResult(json, onResult)
            } catch (t: Throwable) {
                Log.e(TAG, "source delete failed", t)
                onResult(null, describe(t))
            }
        }, "minisearch-source-delete").start()
    }

    /** Queue an immediate re-crawl of one source's site. */
    fun recrawlSource(sourceId: Int, onResult: (String?, String?) -> Unit) {
        callJson("recrawl_source_json", sourceId, tag = "source recrawl", onResult = onResult)
    }

    // ------------------------------------------------- native screens --------

    /**
     * Load the overview (global stats + sites) for the native home screen.
     * [onResult] receives the raw JSON or an error; the screen parses it.
     */
    fun loadDashboard(onResult: (String?, String?) -> Unit) {
        callJson("dashboard_json", tag = "dashboard load", raw = true, onResult = onResult)
    }

    /** Load the filtered site list for the native "Weby" screen. */
    fun loadSites(search: String, status: String, localOnly: Boolean,
                  onResult: (String?, String?) -> Unit) {
        callJson("sites_json", search, status, localOnly,
            tag = "sites load", raw = true, onResult = onResult)
    }

    /** Load one site with its sources, recent pages and errors. */
    fun loadSiteDetail(siteId: Int, onResult: (String?, String?) -> Unit) {
        callJson("site_detail_json", siteId, tag = "site detail", raw = true, onResult = onResult)
    }

    /** Save the editable fields of a site. [onResult] gets ``(message, error)``. */
    fun updateSite(payloadJson: String, onResult: (String?, String?) -> Unit) {
        callJson("update_site_json", payloadJson, tag = "site update", onResult = onResult)
    }

    /** Delete a site and everything under it. */
    fun deleteSite(siteId: Int, onResult: (String?, String?) -> Unit) {
        callJson("delete_site_json", siteId, tag = "site delete", onResult = onResult)
    }

    /** Queue a re-crawl of a whole site. */
    fun recrawlSite(siteId: Int, onResult: (String?, String?) -> Unit) {
        callJson("recrawl_site_json", siteId, tag = "site recrawl", onResult = onResult)
    }

    /** Pause every active site. */
    fun pauseAll(onResult: (String?, String?) -> Unit) {
        callJson("pause_all_json", tag = "pause all", onResult = onResult)
    }

    /** Resume every paused site. */
    fun resumeAll(onResult: (String?, String?) -> Unit) {
        callJson("resume_all_json", tag = "resume all", onResult = onResult)
    }

    /** Queue a re-crawl of every active site. */
    fun recrawlAll(onResult: (String?, String?) -> Unit) {
        callJson("recrawl_all_json", tag = "recrawl all", onResult = onResult)
    }

    /**
     * Run a hybrid search for the native search screen. [payloadJson] is
     * ``{"q": ..., "filter": ..., "limit": ...}``; [onResult] receives the
     * result JSON (not split into message/error) for the screen to render.
     */
    fun search(payloadJson: String, onResult: (String?, String?) -> Unit) {
        callJson("search_json", payloadJson, tag = "search", raw = true, onResult = onResult)
    }

    /** Load the most recently indexed pages for the search screen's empty state. */
    fun loadNews(onResult: (String?, String?) -> Unit) {
        callJson("news_json", tag = "news load", raw = true, onResult = onResult)
    }

    /**
     * Run a bridge function off the UI thread and report the outcome.
     *
     * With [raw] the reply is handed back verbatim (the caller renders it);
     * otherwise a ``{message}|{error}`` reply is split into ``(message, error)``.
     */
    private fun callJson(
        name: String,
        vararg args: Any?,
        tag: String,
        raw: Boolean = false,
        onResult: (String?, String?) -> Unit,
    ) {
        Thread({
            try {
                val json = pythonEntry().callAttr(name, *args).toString()
                if (raw) onResult(json, null) else reportSourceResult(json, onResult)
            } catch (t: Throwable) {
                Log.e(TAG, "$tag failed", t)
                onResult(null, describe(t))
            }
        }, "minisearch-$tag".replace(' ', '-')).start()
    }

    /** Split a Python ``{message}|{error}`` JSON reply into ``(message, error)``. */
    private fun reportSourceResult(json: String, onResult: (String?, String?) -> Unit) {
        val obj = org.json.JSONObject(json)
        if (obj.has("error")) {
            onResult(null, obj.optString("error"))
        } else {
            onResult(obj.optString("message", "Hotovo"), null)
        }
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
