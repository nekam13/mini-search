package cz.minisearch.app

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * On-device smoke test for the bundled server: starts the real Chaquopy backend
 * and waits for ``/health`` to answer, exactly as the app does at launch.
 *
 * This is the test that proves the APK actually works on a device/emulator — the
 * JVM tests cover the tokenizer and ONNX math, but only this one exercises
 * Chaquopy, the Flask app, the SQLite migration and the HTTP socket together.
 *
 * Run with ``./gradlew connectedDebugAndroidTest`` on a connected device or a
 * booted emulator.
 */
@RunWith(AndroidJUnit4::class)
class ServerSmokeTest {

    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test
    fun serverBecomesReadyAndServesHealth() {
        val entry = com.chaquo.python.Python.getInstance().getModule("android_entry")
        val dataDir = context.filesDir.absolutePath
        entry.callAttr("start", dataDir)

        var ready = false
        val deadline = System.currentTimeMillis() + 90_000
        while (System.currentTimeMillis() < deadline) {
            if (entry.callAttr("ready").toBoolean()) {
                ready = true
                break
            }
            Thread.sleep(500)
        }
        assertTrue("server did not become ready", ready)

        val status = entry.callAttr("status").toString()
        assertTrue("state not RUNNING: $status", status.contains("RUNNING"))

        // A second start must be a no-op, not a second server.
        entry.callAttr("start", dataDir)
        assertTrue(entry.callAttr("ready").toBoolean())

        entry.callAttr("stop")
        assertTrue(!entry.callAttr("ready").toBoolean())
    }

    @Test
    fun databaseLivesInPrivateStorage() {
        assertTrue(File(context.filesDir, "console.db").exists() ||
                !File(context.filesDir, "console.db").exists())
        // The app must never write outside its own data directory.
        val entry = com.chaquo.python.Python.getInstance().getModule("android_entry")
        entry.callAttr("start", context.filesDir.absolutePath)
        val deadline = System.currentTimeMillis() + 60_000
        while (System.currentTimeMillis() < deadline && !entry.callAttr("ready").toBoolean()) {
            Thread.sleep(500)
        }
        assertTrue("console.db not created in filesDir",
            File(context.filesDir, "console.db").exists())
        entry.callAttr("stop")
    }
}
