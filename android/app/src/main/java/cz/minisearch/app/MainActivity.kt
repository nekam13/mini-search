package cz.minisearch.app

import android.os.Bundle
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import androidx.appcompat.app.AppCompatActivity

/**
 * Single-screen UI: a top bar with a search field and two tabs, one for the
 * public search page and one for the admin panel. Both are just WebViews
 * pointed at the local server; keeping the existing Clay UI means the phone
 * and the desktop look and behave the same.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var address: EditText
    private var onAdmin = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        ServerService.start(this)

        web = findViewById(R.id.web)
        address = findViewById(R.id.address)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.webViewClient = WebViewClient()

        findViewById<Button>(R.id.tab_search).setOnClickListener { showSearch() }
        findViewById<Button>(R.id.tab_admin).setOnClickListener { showAdmin() }

        showSearch()
    }

    private fun showSearch() {
        onAdmin = false
        load(BASE_URL)
    }

    private fun showAdmin() {
        onAdmin = true
        load("${BASE_URL}admin")
    }

    private fun load(url: String) {
        address.setText(url)
        web.loadUrl(url)
    }

    override fun onDestroy() {
        web.destroy()
        super.onDestroy()
    }

    companion object {
        // The bundled Flask server listens on loopback only.
        const val BASE_URL = "http://127.0.0.1:8070/"
    }
}
