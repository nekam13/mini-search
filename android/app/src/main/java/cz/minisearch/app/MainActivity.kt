package cz.minisearch.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.View
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat

/**
 * Single-screen UI: a top bar with the two tabs (search and admin), a status
 * strip that reflects the *real* server state, and a WebView that is only
 * pointed at loopback once ``/health`` actually answers.
 *
 * The WebView is locked down: JavaScript is enabled (the Clay UI needs it) but
 * file/content access is off, no JavaScript bridge is exposed and any external
 * link opens in the system browser instead of this privileged view.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var status: TextView
    private lateinit var retry: Button
    private lateinit var stopButton: Button
    private var onAdmin = false
    private var loadedOnce = false

    private val notificationPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    private val stateListener: (ServerState) -> Unit = { runOnUiThread { renderState(it) } }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        web = findViewById(R.id.web)
        status = findViewById(R.id.status)
        retry = findViewById(R.id.retry)
        stopButton = findViewById(R.id.stop_server)

        configureWebView()

        findViewById<Button>(R.id.tab_search).setOnClickListener { showSearch() }
        findViewById<Button>(R.id.tab_admin).setOnClickListener { showAdmin() }
        retry.setOnClickListener {
            UserState.serverEnabled = true
            ServerService.start(this)
            ServerController.restart(this)
        }
        stopButton.setOnClickListener {
            UserState.serverEnabled = false
            ServerService.stop(this)
        }

        requestNotificationPermissionIfNeeded()

        ServerController.addListener(stateListener)
        if (UserState.serverEnabled) {
            ServerService.start(this)
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView() {
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        // No local file or content access: the page is served over HTTP only.
        web.settings.allowFileAccess = false
        web.settings.allowContentAccess = false
        web.settings.javaScriptCanOpenWindowsAutomatically = false
        web.settings.setSupportMultipleWindows(false)
        web.settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        // Never add a JavaScript interface: that would expose native calls to
        // any page. Keep the WebView free of bridges.
        web.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                val url = request.url
                return if (isLocal(url)) {
                    false
                } else {
                    // External links leave the privileged WebView.
                    startActivity(Intent(Intent.ACTION_VIEW, url))
                    true
                }
            }

            override fun onPageFinished(view: WebView, url: String) {
                loadedOnce = true
            }
        }
    }

    private fun isLocal(uri: Uri): Boolean {
        val host = uri.host ?: return false
        return uri.scheme == "http" && (host == "127.0.0.1" || host == "localhost")
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val granted = ContextCompat.checkSelfPermission(
                this, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
            if (!granted) {
                notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
    }

    private fun showSearch() {
        onAdmin = false
        if (ServerController.state == ServerState.RUNNING) load(ServerController.BASE_URL)
    }

    private fun showAdmin() {
        onAdmin = true
        if (ServerController.state == ServerState.RUNNING) load("${ServerController.BASE_URL}admin")
    }

    private fun load(url: String) {
        web.loadUrl(url)
    }

    private fun renderState(state: ServerState) {
        when (state) {
            ServerState.STARTING -> {
                status.text = getString(R.string.state_starting)
                status.visibility = View.VISIBLE
                retry.visibility = View.GONE
            }
            ServerState.RUNNING -> {
                status.text = getString(R.string.state_running, ServerController.backendLabel)
                status.visibility = View.VISIBLE
                retry.visibility = View.GONE
                // Only now is localhost reachable.
                if (!loadedOnce || web.url == null) {
                    load(if (onAdmin) "${ServerController.BASE_URL}admin" else ServerController.BASE_URL)
                }
            }
            ServerState.STOPPING -> {
                status.text = getString(R.string.state_stopping)
                status.visibility = View.VISIBLE
            }
            ServerState.STOPPED -> {
                status.text = getString(R.string.state_stopped)
                status.visibility = View.VISIBLE
                retry.visibility = View.VISIBLE
            }
            ServerState.ERROR -> {
                status.text = getString(R.string.state_error, ServerController.lastError)
                status.visibility = View.VISIBLE
                retry.visibility = View.VISIBLE
            }
        }
    }

    override fun onDestroy() {
        ServerController.removeListener(stateListener)
        web.destroy()
        super.onDestroy()
    }
}
