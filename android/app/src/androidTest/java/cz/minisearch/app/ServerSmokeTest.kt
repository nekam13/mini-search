package cz.minisearch.app

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

/**
 * On-device smoke tests for the bundled server: they start the real Chaquopy
 * backend and wait for ``/health`` to answer, exactly as the app does at launch.
 *
 * These are the tests that prove the APK actually works on a device/emulator —
 * the JVM tests cover the tokenizer and ONNX math, but only these exercise
 * Chaquopy, the Flask app, the SQLite migration and the HTTP socket together.
 *
 * Run with ``./gradlew connectedDebugAndroidTest`` on a connected device or a
 * booted emulator.
 */
@RunWith(AndroidJUnit4::class)
class ServerSmokeTest {

    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext

    /**
     * The interpreter is bootstrapped on a background thread by the app; wait
     * for it before asking Chaquopy for a module.
     */
    private val entry get(): com.chaquo.python.PyObject {
        PythonRuntime.awaitStarted()
        return com.chaquo.python.Python.getInstance().getModule("android_entry")
    }

    private fun waitForReady(timeoutMs: Long = 90_000): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (entry.callAttr("ready").toBoolean()) return true
            Thread.sleep(500)
        }
        return false
    }

    private fun httpGet(path: String, timeoutMs: Int = 30_000): Pair<Int, String> {
        val connection = URL("http://127.0.0.1:8070$path").openConnection() as HttpURLConnection
        return try {
            connection.connectTimeout = timeoutMs
            connection.readTimeout = timeoutMs
            connection.requestMethod = "GET"
            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            code to (stream?.use { it.readBytes().toString(Charsets.UTF_8) } ?: "")
        } finally {
            connection.disconnect()
        }
    }

    @Test
    fun serverBecomesReadyAndServesHealth() {
        entry.callAttr("start", context.filesDir.absolutePath)
        assertTrue("server did not become ready", waitForReady())

        val status = entry.callAttr("status").toString()
        assertTrue("state not RUNNING: $status", status.contains("RUNNING"))

        val (code, body) = httpGet("/health")
        assertEquals("GET /health should be 200", 200, code)
        assertTrue("health body should report RUNNING: $body", body.contains("RUNNING"))
    }

    @Test
    fun startIsIdempotentAndDoesNotDuplicateWorkers() {
        entry.callAttr("start", context.filesDir.absolutePath)
        assertTrue("server did not become ready", waitForReady())

        val workersBefore = Regex("\"workers\"\\s*:\\s*(\\d+)")
            .find(entry.callAttr("status").toString())?.groupValues?.get(1)?.toInt() ?: -1

        // A second start must be a no-op, not a second server or worker pool.
        entry.callAttr("start", context.filesDir.absolutePath)
        assertTrue("server lost readiness after repeated start", waitForReady(15_000))

        val workersAfter = Regex("\"workers\"\\s*:\\s*(\\d+)")
            .find(entry.callAttr("status").toString())?.groupValues?.get(1)?.toInt() ?: -1
        assertEquals("worker count changed after repeated start", workersBefore, workersAfter)

        entry.callAttr("stop")
        assertFalse("server still ready after stop", entry.callAttr("ready").toBoolean())
    }

    @Test
    fun searchAndAdminPagesLoad() {
        entry.callAttr("start", context.filesDir.absolutePath)
        assertTrue("server did not become ready", waitForReady())

        val (searchCode, searchBody) = httpGet("/")
        assertEquals("GET / should be 200", 200, searchCode)
        assertTrue("search page should be HTML", searchBody.contains("<html"))

        val (adminCode, _) = httpGet("/admin")
        assertEquals("GET /admin should be 200", 200, adminCode)

        entry.callAttr("stop")
    }

    @Test
    fun stopAndRestartWorkInTheSameProcess() {
        entry.callAttr("start", context.filesDir.absolutePath)
        assertTrue("server did not become ready", waitForReady())

        entry.callAttr("stop")
        assertFalse("server still ready after stop", entry.callAttr("ready").toBoolean())

        // A fresh start in the same process must succeed, not leave ERROR behind.
        entry.callAttr("start", context.filesDir.absolutePath)
        assertTrue("server did not restart in the same process", waitForReady())
        val status = entry.callAttr("status").toString()
        assertTrue("state not RUNNING after restart: $status", status.contains("RUNNING"))

        entry.callAttr("stop")
    }

    @Test
    fun databaseLivesInPrivateStorage() {
        // The app must never write outside its own data directory.
        entry.callAttr("start", context.filesDir.absolutePath)
        assertTrue("server did not become ready", waitForReady())
        assertTrue("console.db not created in filesDir",
            File(context.filesDir, "console.db").exists())
        entry.callAttr("stop")
    }

    @Test
    fun pagesSurviveStopAndRestart() {
        // A stop/start cycle must not lose indexed data (WAL checkpoint, not a
        // fresh database).
        entry.callAttr("start", context.filesDir.absolutePath)
        assertTrue("server did not become ready", waitForReady())
        val pageCountBefore = entry.callAttr("page_count").toInt()

        entry.callAttr("stop")
        entry.callAttr("start", context.filesDir.absolutePath)
        assertTrue("server did not restart", waitForReady())

        val pageCountAfter = entry.callAttr("page_count").toInt()
        assertEquals("page count changed across a restart",
            pageCountBefore, pageCountAfter)
        entry.callAttr("stop")
    }

    @Test
    fun mutatingAdminRoutesRejectGet() {
        // State-changing admin endpoints must not be reachable via GET, so a
        // stray link or prefetch cannot trigger them.
        entry.callAttr("start", context.filesDir.absolutePath)
        assertTrue("server did not become ready", waitForReady())

        for (path in listOf("/admin/recrawl-all", "/admin/pause-all",
                            "/admin/resume-all", "/admin/check-update")) {
            val (code, _) = httpGet(path)
            assertEquals("GET $path must not be 200", true, code == 405 || code == 404)
        }
        entry.callAttr("stop")
    }

    /**
     * Raw-socket HTTP GET. ``HttpURLConnection`` refuses cleartext to anything
     * but 127.0.0.1 (the app's network security config), which is exactly the
     * policy we want for the WebView; but here we need to prove the *server*
     * answers on the LAN interface, so we speak HTTP over a plain socket.
     */
    private fun rawHttpGetOn(host: String, path: String, timeoutMs: Int = 30_000): Int {
        return try {
            java.net.Socket().use { socket ->
                socket.connect(java.net.InetSocketAddress(host, 8070), timeoutMs)
                socket.soTimeout = timeoutMs
                socket.getOutputStream().write(
                    ("GET $path HTTP/1.0\r\nHost: $host:8070\r\nConnection: close\r\n\r\n")
                        .toByteArray(Charsets.UTF_8))
                socket.getOutputStream().flush()
                val line = socket.getInputStream().bufferedReader().readLine() ?: return -1
                // "HTTP/1.0 200 OK"
                line.split(" ").getOrNull(1)?.toIntOrNull() ?: -1
            }
        } catch (t: Throwable) {
            -1
        }
    }

    private fun deviceLanAddress(): String? {
        // The address a PC on the same network would dial; null when offline.
        return try {
            java.net.NetworkInterface.getNetworkInterfaces().toList()
                .filter { it.isUp && !it.isLoopback }
                .flatMap { it.inetAddresses.toList() }
                .firstOrNull { it is java.net.Inet4Address && !it.isLoopbackAddress }
                ?.hostAddress
        } catch (t: Throwable) {
            null
        }
    }

    @Test
    fun lanSharingRebindsAndExposesTheAddress() {
        // LAN sharing must be off by default and, once enabled, bind beyond
        // loopback and report an address a PC on the network can open.
        entry.callAttr("start", context.filesDir.absolutePath)
        assertTrue("server did not become ready", waitForReady())

        entry.callAttr("set_lan_share", false)
        entry.callAttr("stop")
        entry.callAttr("start", context.filesDir.absolutePath)
        assertTrue("server did not become ready after disabling", waitForReady())
        var status = entry.callAttr("status").toString()
        assertTrue("lan_share should be false: $status", status.contains("\"lan_share\": false"))

        entry.callAttr("set_lan_share", true)
        entry.callAttr("stop")
        entry.callAttr("start", context.filesDir.absolutePath)
        assertTrue("server did not become ready after enabling", waitForReady())
        status = entry.callAttr("status").toString()
        assertTrue("lan_share should be true: $status", status.contains("\"lan_share\": true"))
        // A real device/emulator on Wi-Fi reports an address; an offline one is
        // allowed to report an empty lan_url, which the UI explains.
        assertTrue("status should carry a lan_url field: $status",
            status.contains("\"lan_url\""))

        // With LAN sharing on, the search page must answer on the device's own
        // network address while the admin panel must refuse it.
        val lan = deviceLanAddress()
        if (lan != null) {
            val searchCode = rawHttpGetOn(lan, "/")
            assertEquals("GET / on $lan should be 200", 200, searchCode)

            val adminCode = rawHttpGetOn(lan, "/admin")
            assertEquals("GET /admin on $lan must be refused", 403, adminCode)
        }

        entry.callAttr("set_lan_share", false)
        entry.callAttr("stop")
    }
}
