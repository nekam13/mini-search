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
 * On-device UI tests for the native Clay shell (Home/Search/Admin/Settings) and
 * for the JS-dialog handling that the admin forms depend on.
 *
 * The dialog tests drive the exact [MainActivity.buildJsDialog] the
 * ``WebChromeClient`` calls. They deliberately do **not** attach a DevTools/CDP
 * client: an attached client intercepts ``window.confirm()``/``window.alert()``
 * itself, so ``onJsConfirm`` is never invoked and the app's dialog never shows.
 * Testing through the real method (no CDP) is what actually proves the dialog
 * path, which a CDP-driven ``Runtime.evaluate`` cannot.
 *
 * The dialog's buttons are clicked directly on the main thread rather than via
 * Espresso's view matching: the no-KVM emulator is slow enough that Espresso's
 * RESUMED-stage waits flake, while a direct ``performClick`` on the real
 * ``AlertDialog`` button exercises exactly the same ``onConfirm``/``onCancel``
 * callbacks the WebView would.
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
                // The four Clay tabs must exist and be labelled in Czech.
                for (id in listOf(R.id.tab_home, R.id.tab_search,
                                  R.id.tab_admin, R.id.tab_settings)) {
                    val tab = activity.findViewById<Button>(id)
                    assertNotNull("tab $id missing", tab)
                    assertTrue("tab $id has no label", tab.text.isNotBlank())
                }
                // Home is the landing screen: its status line is visible.
                val home = activity.findViewById<View>(R.id.home_view)
                assertTrue("home_view should be visible on launch",
                    home.visibility == View.VISIBLE)

                // Selecting the Admin tab hides Home and shows the web container.
                activity.findViewById<Button>(R.id.tab_admin).performClick()
                val web = activity.findViewById<View>(R.id.web_container)
                assertTrue("web_container should show for the admin tab",
                    web.visibility == View.VISIBLE)
                assertTrue("home_view should hide for the admin tab",
                    home.visibility == View.GONE)
            }
        }
    }

    @Test
    fun confirmDialogShowsAndReportsConfirm() {
        launch().use { scenario ->
            val confirmed = AtomicBoolean(false)
            val cancelled = AtomicBoolean(false)
            val dialog = AtomicReference<AlertDialog?>(null)

            scenario.onActivity { activity ->
                dialog.set(activity.buildJsDialog(
                    message = "Smazat web?",
                    confirm = true,
                    onConfirm = { confirmed.set(true) },
                    onCancel = { cancelled.set(true) },
                ))
            }

            scenario.onActivity { activity ->
                val d = dialog.get()
                assertNotNull("dialog was not created", d)
                assertTrue("dialog should be showing", d!!.isShowing)
                // The Czech message and both buttons are present.
                assertTrue("confirm message missing",
                    d.findViewById<android.widget.TextView>(android.R.id.message)
                        ?.text?.toString()?.contains("Smazat web?") == true)
                val ok = d.getButton(AlertDialog.BUTTON_POSITIVE)
                assertNotNull("OK button missing", ok)
                assertNotNull("Cancel button missing",
                    d.getButton(AlertDialog.BUTTON_NEGATIVE))
                ok.performClick()
            }
            idle()

            assertTrue("OK must report confirm()", confirmed.get())
            assertTrue("cancel must not fire on OK", !cancelled.get())
        }
    }

    @Test
    fun cancelButtonReportsCancel() {
        launch().use { scenario ->
            val confirmed = AtomicBoolean(false)
            val cancelled = AtomicBoolean(false)
            val dialog = AtomicReference<AlertDialog?>(null)

            scenario.onActivity { activity ->
                dialog.set(activity.buildJsDialog(
                    message = "Opravdu?",
                    confirm = true,
                    onConfirm = { confirmed.set(true) },
                    onCancel = { cancelled.set(true) },
                ))
            }

            scenario.onActivity { activity ->
                val d = dialog.get()
                assertNotNull("dialog was not created", d)
                d!!.getButton(AlertDialog.BUTTON_NEGATIVE).performClick()
            }
            idle()

            assertTrue("Cancel must report cancel()", cancelled.get())
            assertTrue("confirm must not fire on Cancel", !confirmed.get())
        }
    }

    @Test
    fun alertDialogHasOnlyOkButton() {
        launch().use { scenario ->
            val confirmed = AtomicBoolean(false)
            val dialog = AtomicReference<AlertDialog?>(null)
            scenario.onActivity { activity ->
                dialog.set(activity.buildJsDialog(
                    message = "Hotovo",
                    confirm = false,
                    onConfirm = { confirmed.set(true) },
                ))
            }
            scenario.onActivity { activity ->
                val d = dialog.get()
                assertNotNull("dialog was not created", d)
                // An alert offers only OK: no visible cancel button.
                val negative = d!!.getButton(AlertDialog.BUTTON_NEGATIVE)
                assertTrue("alert must not offer a visible cancel button",
                    negative == null || negative.visibility != View.VISIBLE)
                d.getButton(AlertDialog.BUTTON_POSITIVE).performClick()
            }
            idle()
            assertTrue("alert OK must fire", confirmed.get())
        }
    }
}
