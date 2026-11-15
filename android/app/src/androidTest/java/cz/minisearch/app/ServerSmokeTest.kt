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
    private val entry get() = com.chaquo.python.Python.getInstance().getModule("android_entry")

    private fun waitForReady(timeoutMs: Long = 90_000): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (entry.callAttr("ready").toBoolean()) return true
            Thread.sleep(500)
        }
        return false
    }

    private fun httpGet(path: String): Pair<Int, String> {
        val connection = URL("http://127.0.0.1:8070$path").openConnection() as HttpURLConnection
        return try {
            connection.connectTimeout = 3000
            connection.readTimeout = 5000
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
}
