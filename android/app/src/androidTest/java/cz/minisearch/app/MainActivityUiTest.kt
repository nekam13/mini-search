package cz.minisearch.app

import android.view.View
import android.widget.Button
import androidx.appcompat.app.AlertDialog
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * On-device UI tests for the native-only Clay shell.
 *
 * The app has no WebView: all five tabs (Domů / Hledat / Zdroje / Weby /
 * Nastavení) are real Android views, and the search/sites/settings screens are
 * rendered from the Python bridge. These tests assert the native screens switch
 * correctly and that the confirm dialog used for destructive actions reports the
 * right outcome.
 *
 * The dialog buttons are clicked directly on the main thread rather than via
 * Espresso's view matching: the no-KVM emulator is slow enough that Espresso's
 * RESUMED-stage waits flake, while a direct ``performClick`` on the real
 * ``AlertDialog`` button exercises exactly the same callback the screens use.
 */
@RunWith(AndroidJUnit4::class)
class MainActivityUiTest {

    private fun launch(): ActivityScenario<MainActivity> =
        ActivityScenario.launch(MainActivity::class.java)

    /** Drain the main looper so button callbacks posted by the dialog run. */
    private fun idle() {
        InstrumentationRegistry.getInstrumentation().waitForIdleSync()
    }

    @Test
    fun nativeTabsArePresentAndSwitchScreens() {
        launch().use { scenario ->
            scenario.onActivity { activity ->
                // The five Clay tabs must exist and be labelled in Czech.
                for (id in listOf(R.id.tab_home, R.id.tab_search, R.id.tab_sources,
                                  R.id.tab_web, R.id.tab_settings)) {
                    val tab = activity.findViewById<Button>(id)
                    assertNotNull("tab $id missing", tab)
                    assertTrue("tab $id has no label", tab.text.isNotBlank())
                }
                // Home is the landing screen.
                val home = activity.findViewById<View>(R.id.home_view)
                assertTrue("home_view should be visible on launch",
                    home.visibility == View.VISIBLE)

                // Every tab shows its own native screen and hides the others.
                val panes = mapOf(
                    R.id.tab_home to R.id.home_view,
                    R.id.tab_search to R.id.search_view,
                    R.id.tab_sources to R.id.sources_view,
                    R.id.tab_web to R.id.sites_view,
                    R.id.tab_settings to R.id.settings_view)
                for ((tabId, paneId) in panes) {
                    activity.findViewById<Button>(tabId).performClick()
                    for ((_, otherId) in panes) {
                        val expected = if (otherId == paneId) View.VISIBLE else View.GONE
                        assertTrue("tab $tabId: pane $otherId visibility",
                            activity.findViewById<View>(otherId).visibility == expected)
                    }
                }
            }
        }
    }

    @Test
    fun thereIsNoWebViewAnywhere() {
        launch().use { scenario ->
            scenario.onActivity { activity ->
                // The app must not embed a browser: a WebView class in the view
                // tree would mean the public/admin pages leaked back in.
                val root = activity.findViewById<View>(android.R.id.content)
                assertTrue("no WebView may exist in the app",
                    !containsWebView(root))
            }
        }
    }

    private fun containsWebView(view: View): Boolean {
        if (view is android.webkit.WebView) return true
        if (view is android.view.ViewGroup) {
            for (i in 0 until view.childCount) {
                if (containsWebView(view.getChildAt(i))) return true
            }
        }
        return false
    }

    @Test
    fun confirmDialogShowsAndReportsConfirm() {
        launch().use { scenario ->
            val confirmed = AtomicBoolean(false)
            val dialog = AtomicReference<AlertDialog?>(null)

            scenario.onActivity { activity ->
                dialog.set(activity.buildConfirmDialog("Smazat web?") { confirmed.set(true) })
            }

            scenario.onActivity { _ ->
                val d = dialog.get()
                assertNotNull("dialog was not created", d)
                assertTrue("dialog should be showing", d!!.isShowing)
                assertTrue("confirm message missing",
                    d.findViewById<android.widget.TextView>(android.R.id.message)
                        ?.text?.toString()?.contains("Smazat web?") == true)
                assertNotNull("Cancel button missing",
                    d.getButton(AlertDialog.BUTTON_NEGATIVE))
                d.getButton(AlertDialog.BUTTON_POSITIVE).performClick()
            }
            idle()

            assertTrue("OK must report confirm", confirmed.get())
        }
    }

    @Test
    fun cancelButtonDoesNotConfirm() {
        launch().use { scenario ->
            val confirmed = AtomicBoolean(false)
            val dialog = AtomicReference<AlertDialog?>(null)

            scenario.onActivity { activity ->
                dialog.set(activity.buildConfirmDialog("Opravdu?") { confirmed.set(true) })
            }

            scenario.onActivity { _ ->
                val d = dialog.get()
                assertNotNull("dialog was not created", d)
                d!!.getButton(AlertDialog.BUTTON_NEGATIVE).performClick()
            }
            idle()

            assertTrue("Cancel must not fire confirm", !confirmed.get())
        }
    }
}
