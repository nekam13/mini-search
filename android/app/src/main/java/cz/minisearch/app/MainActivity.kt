package cz.minisearch.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.JsResult
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import org.json.JSONArray
import org.json.JSONObject

/**
 * The app shell: a native Clay home screen and a native Clay settings screen,
 * plus the WebView for the two browser-based surfaces (public search and admin).
 *
 * The native screens are not a skin over the web UI — the home screen owns the
 * real server controls (start/stop/restart, LAN sharing, dump import) and the
 * settings screen renders ``SETTINGS_SPEC`` straight from the backend, so the
 * configuration never lives in two places.
 *
 * The WebView is locked down: JavaScript on (the Clay UI needs it), no file or
 * content access, no JavaScript bridge, external links leave for the system
 * browser, and a [WebChromeClient] renders ``confirm``/``alert`` so the admin
 * forms that guard destructive actions actually work.
 */
class MainActivity : AppCompatActivity() {

    // Created lazily: see ensureWebView(). Null until the user first opens a
    // web-backed tab, so Activity.onCreate stays cheap and the foreground
    // service meets its start deadline even on a slow device.
    private var web: WebView? = null
    private lateinit var webContainer: android.widget.FrameLayout
    private lateinit var homeView: View
    private lateinit var settingsView: View
    private lateinit var sourcesView: View
    private lateinit var status: TextView
    private lateinit var retry: Button

    // Home screen widgets
    private lateinit var homeStatus: TextView
    private lateinit var homeBackend: TextView
    private lateinit var homeError: TextView
    private lateinit var lanShare: CheckBox
    private lateinit var lanUrl: TextView
    private lateinit var lanCopy: Button

    // Settings screen widgets
    private lateinit var settingsGroups: LinearLayout
    private lateinit var settingsStatus: TextView
    private lateinit var settingsSave: Button
    private lateinit var settingsReset: Button

    // Sources screen widgets
    private lateinit var sourceList: LinearLayout
    private lateinit var sourceStatus: TextView
    private lateinit var sourceUrl: EditText
    private lateinit var sourceType: Spinner
    private lateinit var sourceAdd: Button

    private val tabs = mutableMapOf<Int, Button>()
    private var currentTab = R.id.tab_home
    private var onAdmin = false
    private var loadedOnce = false
    private var settingsLoaded = false
    // Set while the code reflects the backend state into the checkbox, so the
    // programmatic change does not look like a user tap and re-trigger a restart.
    private var syncingLanToggle = false
    // Maps a setting key to the widget that holds its current value, so saving
    // can read every field without re-parsing the layout.
    private val settingFields = mutableMapOf<String, () -> Any>()

    private val notificationPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    // The Wikipedia dump is chosen through the Storage Access Framework: the
    // picker hands back a content:// URI, which Kotlin copies into private
    // storage before Python ever sees a real path.
    private val dumpPicker =
        registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
            if (uri != null) copyAndImportDump(uri)
        }

    private val stateListener: (ServerState) -> Unit = { runOnUiThread { renderState(it) } }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webContainer = findViewById(R.id.web_container)
        homeView = findViewById(R.id.home_view)
        settingsView = findViewById(R.id.settings_view)
        sourcesView = findViewById(R.id.sources_view)
        status = findViewById(R.id.status)
        retry = findViewById(R.id.retry)

        homeStatus = findViewById(R.id.home_status)
        homeBackend = findViewById(R.id.home_backend)
        homeError = findViewById(R.id.home_error)
        lanShare = findViewById(R.id.lan_share)
        lanUrl = findViewById(R.id.lan_url)
        lanCopy = findViewById(R.id.lan_copy)

        settingsGroups = findViewById(R.id.settings_groups)
        settingsStatus = findViewById(R.id.settings_status)
        settingsSave = findViewById(R.id.settings_save)
        settingsReset = findViewById(R.id.settings_reset)

        sourceList = findViewById(R.id.source_list)
        sourceStatus = findViewById(R.id.source_status)
        sourceUrl = findViewById(R.id.source_url)
        sourceType = findViewById(R.id.source_type)
        sourceAdd = findViewById(R.id.source_add)

        tabs[R.id.tab_home] = findViewById(R.id.tab_home)
        tabs[R.id.tab_search] = findViewById(R.id.tab_search)
        tabs[R.id.tab_sources] = findViewById(R.id.tab_sources)
        tabs[R.id.tab_admin] = findViewById(R.id.tab_admin)
        tabs[R.id.tab_settings] = findViewById(R.id.tab_settings)

        wireTabs()
        wireHome()
        wireSettings()
        wireSources()

        findViewById<Button>(R.id.import_dump).setOnClickListener { pickDump() }
        retry.setOnClickListener {
            UserState.serverEnabled = true
            ServerService.start(this)
            ServerController.restart(this)
        }

        requestNotificationPermissionIfNeeded()

        ServerController.addListener(stateListener)
        // The controller may already be RUNNING (rotation, return from
        // background), in which case no new state event fires: reflect the
        // current state once so the screens are correct immediately.
        renderState(ServerController.state)
        selectTab(currentTab)
        if (UserState.serverEnabled) {
            ServerService.start(this)
        }
    }

    // ---------------------------------------------------------------- tabs ----

    private fun wireTabs() {
        for ((id, button) in tabs) {
            button.setOnClickListener { selectTab(id) }
        }
    }

    private fun selectTab(id: Int) {
        currentTab = id
        for ((tabId, button) in tabs) {
            button.isSelected = tabId == id
        }
        homeView.visibility = if (id == R.id.tab_home) View.VISIBLE else View.GONE
        settingsView.visibility = if (id == R.id.tab_settings) View.VISIBLE else View.GONE
        sourcesView.visibility = if (id == R.id.tab_sources) View.VISIBLE else View.GONE
        val webTab = id == R.id.tab_search || id == R.id.tab_admin
        webContainer.visibility = if (webTab) View.VISIBLE else View.GONE

        when (id) {
            R.id.tab_search -> {
                onAdmin = false
                ensureWebView()
                if (ServerController.state == ServerState.RUNNING) load(ServerController.BASE_URL)
            }
            R.id.tab_admin -> {
                onAdmin = true
                ensureWebView()
                if (ServerController.state == ServerState.RUNNING) load("${ServerController.BASE_URL}admin")
            }
            R.id.tab_sources -> loadSources()
            R.id.tab_settings -> loadSettings()
        }
    }

    // ---------------------------------------------------------------- home ----

    private fun wireHome() {
        findViewById<Button>(R.id.home_open_web).setOnClickListener {
            if (ServerController.state != ServerState.RUNNING) {
                toast(getString(R.string.dump_need_running))
                return@setOnClickListener
            }
            selectTab(R.id.tab_search)
        }
        findViewById<Button>(R.id.home_restart).setOnClickListener {
            UserState.serverEnabled = true
            ServerService.start(this)
            ServerController.restart(this)
        }
        findViewById<Button>(R.id.home_stop).setOnClickListener {
            UserState.serverEnabled = false
            ServerService.stop(this)
        }
        lanCopy.setOnClickListener {
            val url = ServerController.lanUrl
            if (url.isNotEmpty()) {
                val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                clipboard.setPrimaryClip(ClipData.newPlainText("Mini Search", url))
                toast(getString(R.string.home_copied))
            }
        }
        lanShare.setOnCheckedChangeListener { _, checked ->
            if (syncingLanToggle) return@setOnCheckedChangeListener
            if (ServerController.state != ServerState.RUNNING) {
                // Nothing to re-bind; restore the switch to the real state.
                syncLanToggle()
                toast(getString(R.string.lan_share_needs_running))
                return@setOnCheckedChangeListener
            }
            if (checked) {
                toast(getString(R.string.lan_share_hint))
            }
            ServerController.setLanShare(this, checked)
        }
    }

    /** Reflect the backend's LAN state into the checkbox and the address label. */
    private fun syncLanToggle() {
        syncingLanToggle = true
        lanShare.isChecked = ServerController.lanShareEnabled
        syncingLanToggle = false
        val url = ServerController.lanUrl
        if (ServerController.lanShareEnabled && url.isNotEmpty()) {
            lanUrl.text = getString(R.string.lan_share_url, url)
            lanUrl.visibility = View.VISIBLE
            lanCopy.visibility = View.VISIBLE
        } else if (ServerController.lanShareEnabled) {
            lanUrl.text = getString(R.string.lan_share_no_address)
            lanUrl.visibility = View.VISIBLE
            lanCopy.visibility = View.GONE
        } else {
            lanUrl.visibility = View.GONE
            lanCopy.visibility = View.GONE
        }
    }

    // ------------------------------------------------------------ settings ----

    private fun wireSettings() {
        settingsSave.setOnClickListener { saveSettings() }
        settingsReset.setOnClickListener {
            AlertDialog.Builder(this)
                .setMessage(getString(R.string.settings_reset_confirm))
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(android.R.string.ok) { _, _ -> resetSettings() }
                .show()
        }
    }

    private fun loadSettings() {
        if (settingsLoaded) return
        settingsSave.isEnabled = false
        settingsReset.isEnabled = false
        ServerController.loadSettings { json, error ->
            runOnUiThread {
                if (error != null || json == null) {
                    showSettingsStatus(getString(R.string.settings_load_error, error ?: ""), false)
                    return@runOnUiThread
                }
                try {
                    buildSettingsUi(JSONObject(json).getJSONArray("groups"))
                    settingsLoaded = true
                    settingsSave.isEnabled = true
                    settingsReset.isEnabled = true
                    settingsStatus.visibility = View.GONE
                } catch (t: Throwable) {
                    showSettingsStatus(getString(R.string.settings_load_error, t.message ?: ""), false)
                }
            }
        }
    }

    /** Build one card per settings group, one row per item, from the backend JSON. */
    private fun buildSettingsUi(groups: JSONArray) {
        settingsGroups.removeAllViews()
        settingFields.clear()
        val density = resources.displayMetrics.density
        fun dp(value: Int) = (value * density).toInt()

        for (g in 0 until groups.length()) {
            val group = groups.getJSONObject(g)
            val card = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setBackgroundResource(R.drawable.clay_card)
                setPadding(dp(18), dp(18), dp(18), dp(18))
                val lp = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
                lp.bottomMargin = dp(14)
                layoutParams = lp
            }
            card.addView(TextView(this).apply {
                text = group.optString("label")
                setTextColor(ContextCompat.getColor(context, R.color.clay_text))
                textSize = 16f
                setTypeface(typeface, android.graphics.Typeface.BOLD)
                setPadding(0, 0, 0, dp(8))
            })

            val items = group.optJSONArray("items") ?: JSONArray()
            for (i in 0 until items.length()) {
                card.addView(buildSettingRow(items.getJSONObject(i), ::dp))
            }
            settingsGroups.addView(card)
        }
    }

    private fun buildSettingRow(item: JSONObject, dp: (Int) -> Int): View {
        val key = item.optString("key")
        val kind = item.optString("kind")
        val label = item.optString("label")
        val help = item.optString("help")
        val env = item.optString("env")
        val restart = item.optBoolean("restart", false)

        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(6), 0, dp(10))
        }

        val labelRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        labelRow.addView(TextView(this).apply {
            text = label
            setTextColor(ContextCompat.getColor(context, R.color.clay_text))
            textSize = 14f
            setTypeface(typeface, android.graphics.Typeface.BOLD)
        })
        if (restart) {
            labelRow.addView(TextView(this).apply {
                text = " " + getString(R.string.settings_restart_badge)
                setTextColor(ContextCompat.getColor(context, R.color.clay_accent))
                textSize = 11f
                setPadding(dp(6), 0, 0, 0)
            })
        }
        column.addView(labelRow)

        when (kind) {
            "bool" -> {
                val check = CheckBox(this).apply {
                    text = getString(R.string.settings_on)
                    isChecked = item.optBoolean("value", false)
                    setTextColor(ContextCompat.getColor(context, R.color.clay_text))
                }
                column.addView(check)
                settingFields[key] = { check.isChecked }
            }
            "choice" -> {
                val choices = item.optJSONArray("choices") ?: JSONArray()
                val labels = ArrayList<String>()
                val values = ArrayList<String>()
                for (c in 0 until choices.length()) {
                    val value = choices.getString(c)
                    values.add(value)
                    labels.add(choiceLabel(value))
                }
                val spinner = Spinner(this)
                spinner.adapter = ArrayAdapter(
                    this, android.R.layout.simple_spinner_dropdown_item, labels)
                val current = item.optString("value")
                val index = values.indexOf(current)
                if (index >= 0) spinner.setSelection(index)
                column.addView(spinner)
                settingFields[key] = { values[spinner.selectedItemPosition] }
            }
            "int" -> {
                val edit = EditText(this).apply {
                    inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_SIGNED
                    setText(item.optString("value"))
                    setTextColor(ContextCompat.getColor(context, R.color.clay_text))
                }
                column.addView(edit)
                settingFields[key] = { edit.text.toString().trim() }
            }
            else -> {
                val edit = EditText(this).apply {
                    setText(item.optString("value"))
                    setTextColor(ContextCompat.getColor(context, R.color.clay_text))
                }
                column.addView(edit)
                settingFields[key] = { edit.text.toString().trim() }
            }
        }

        val helpText = buildString {
            append(help)
            if (env.isNotEmpty()) {
                append("\n")
                append(getString(R.string.settings_env_hint, env))
            }
        }
        column.addView(TextView(this).apply {
            text = helpText
            setTextColor(ContextCompat.getColor(context, R.color.clay_text_light))
            textSize = 12f
            setPadding(0, dp(4), 0, 0)
        })
        return column
    }

    private fun choiceLabel(value: String): String = when (value) {
        "auto" -> getString(R.string.settings_choice_auto)
        "1" -> getString(R.string.settings_choice_on)
        "0" -> getString(R.string.settings_choice_off)
        else -> value
    }

    private fun saveSettings() {
        val settings = JSONObject()
        for ((key, reader) in settingFields) {
            settings.put(key, reader())
        }
        settingsSave.isEnabled = false
        settingsReset.isEnabled = false
        showSettingsStatus(getString(R.string.settings_save) + "…", true)
        ServerController.saveSettings(settings.toString(), this) { message, error, restarted ->
            runOnUiThread {
                settingsSave.isEnabled = true
                settingsReset.isEnabled = true
                if (error != null) {
                    showSettingsStatus(getString(R.string.settings_error, error), false)
                    return@runOnUiThread
                }
                val text = if (restarted) getString(R.string.settings_saved_restart)
                           else (message ?: getString(R.string.settings_saved))
                showSettingsStatus(text, true)
                // The values may have been clamped; reload so the UI shows what
                // was actually stored.
                settingsLoaded = false
                if (restarted) loadSettings()
            }
        }
    }

    private fun resetSettings() {
        settingsSave.isEnabled = false
        settingsReset.isEnabled = false
        ServerController.resetSettings(this) { _, error ->
            runOnUiThread {
                settingsSave.isEnabled = true
                settingsReset.isEnabled = true
                if (error != null) {
                    showSettingsStatus(getString(R.string.settings_error, error), false)
                } else {
                    showSettingsStatus(getString(R.string.settings_reset_done), true)
                    settingsLoaded = false
                    loadSettings()
                }
            }
        }
    }

    private fun showSettingsStatus(text: String, ok: Boolean) {
        settingsStatus.text = text
        settingsStatus.setBackgroundResource(
            if (ok) R.drawable.clay_status_ok else R.drawable.clay_status_err)
        settingsStatus.setTextColor(ContextCompat.getColor(
            this, if (ok) R.color.clay_ok_text else R.color.clay_err_text))
        settingsStatus.visibility = View.VISIBLE
    }

    // ------------------------------------------------------------- sources ----

    private fun wireSources() {
        val labels = listOf(
            getString(R.string.sources_type_auto),
            getString(R.string.sources_type_domain),
            getString(R.string.sources_type_url),
            getString(R.string.sources_type_sitemap),
            getString(R.string.sources_type_rss),
            getString(R.string.sources_type_wiki))
        sourceType.adapter = ArrayAdapter(
            this, android.R.layout.simple_spinner_dropdown_item, labels)
        sourceAdd.setOnClickListener { addSource() }
    }

    /** The backend source-type value matching the spinner position. */
    private fun selectedSourceType(): String = when (sourceType.selectedItemPosition) {
        1 -> "domain"
        2 -> "url"
        3 -> "sitemap"
        4 -> "rss"
        5 -> "wiki"
        else -> "auto"
    }

    private fun loadSources() {
        if (ServerController.state != ServerState.RUNNING) {
            renderSources(JSONArray())
            showSourceStatus(getString(R.string.sources_need_running), false)
            return
        }
        sourceAdd.isEnabled = false
        ServerController.loadSources { json, error ->
            runOnUiThread {
                sourceAdd.isEnabled = true
                if (error != null || json == null) {
                    renderSources(JSONArray())
                    showSourceStatus(
                        getString(R.string.sources_load_error, error ?: ""), false)
                    return@runOnUiThread
                }
                try {
                    val obj = JSONObject(json)
                    if (obj.has("error")) {
                        renderSources(JSONArray())
                        showSourceStatus(getString(
                            R.string.sources_load_error, obj.optString("error")), false)
                    } else {
                        renderSources(obj.optJSONArray("sources") ?: JSONArray())
                    }
                } catch (t: Throwable) {
                    renderSources(JSONArray())
                    showSourceStatus(getString(
                        R.string.sources_load_error, t.message ?: ""), false)
                }
            }
        }
    }

    private fun addSource() {
        val url = sourceUrl.text.toString().trim()
        if (url.isEmpty()) {
            showSourceStatus(getString(R.string.sources_error, ""), false)
            return
        }
        if (ServerController.state != ServerState.RUNNING) {
            showSourceStatus(getString(R.string.sources_need_running), false)
            return
        }
        val payload = JSONObject()
        payload.put("url", url)
        payload.put("type", selectedSourceType())
        sourceAdd.isEnabled = false
        showSourceStatus(getString(R.string.sources_add) + "…", true)
        ServerController.addSource(payload.toString()) { message, error ->
            runOnUiThread {
                sourceAdd.isEnabled = true
                if (error != null) {
                    showSourceStatus(getString(R.string.sources_error, error), false)
                } else {
                    sourceUrl.setText("")
                    showSourceStatus(message ?: getString(R.string.sources_added), true)
                    loadSources()
                }
            }
        }
    }

    private fun deleteSource(id: Int, label: String) {
        AlertDialog.Builder(this)
            .setMessage(getString(R.string.sources_delete_confirm, label))
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(android.R.string.ok) { _, _ ->
                ServerController.deleteSource(id) { message, error ->
                    runOnUiThread {
                        if (error != null) {
                            showSourceStatus(getString(R.string.sources_error, error), false)
                        } else {
                            showSourceStatus(
                                message ?: getString(R.string.sources_deleted), true)
                            loadSources()
                        }
                    }
                }
            }
            .show()
    }

    private fun recrawlSource(id: Int) {
        ServerController.recrawlSource(id) { message, error ->
            runOnUiThread {
                if (error != null) {
                    showSourceStatus(getString(R.string.sources_error, error), false)
                } else {
                    showSourceStatus(message ?: "", true)
                }
            }
        }
    }

    /** Render one card per source, built from the backend JSON. */
    private fun renderSources(sources: JSONArray) {
        sourceList.removeAllViews()
        val density = resources.displayMetrics.density
        fun dp(value: Int) = (value * density).toInt()

        if (sources.length() == 0) {
            sourceList.addView(TextView(this).apply {
                text = getString(R.string.sources_empty)
                setTextColor(ContextCompat.getColor(context, R.color.clay_text_light))
                textSize = 14f
                setPadding(dp(4), dp(8), dp(4), dp(8))
            })
            return
        }

        for (i in 0 until sources.length()) {
            val source = sources.getJSONObject(i)
            val id = source.optInt("id", -1)
            val url = source.optString("url")
            val domain = source.optString("domain")
            val type = source.optString("type")
            val lastChecked = source.optString("last_checked")

            val card = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setBackgroundResource(R.drawable.clay_card)
                setPadding(dp(18), dp(14), dp(18), dp(14))
                val lp = LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
                lp.bottomMargin = dp(10)
                layoutParams = lp
            }

            card.addView(TextView(this).apply {
                text = if (domain.isNotEmpty()) domain else url
                setTextColor(ContextCompat.getColor(context, R.color.clay_text))
                textSize = 15f
                setTypeface(typeface, android.graphics.Typeface.BOLD)
            })
            card.addView(TextView(this).apply {
                text = sourceTypeLabel(type) + "\n" + url
                setTextColor(ContextCompat.getColor(context, R.color.clay_text_light))
                textSize = 12f
                setPadding(0, dp(2), 0, 0)
            })
            if (lastChecked.isNotEmpty()) {
                card.addView(TextView(this).apply {
                    text = getString(R.string.sources_last_checked, lastChecked)
                    setTextColor(ContextCompat.getColor(context, R.color.clay_text_light))
                    textSize = 12f
                    setPadding(0, dp(2), 0, 0)
                })
            }

            val actions = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                setPadding(0, dp(8), 0, 0)
            }
            actions.addView(Button(this).apply {
                text = getString(R.string.sources_recrawl)
                isAllCaps = false
                setBackgroundResource(R.drawable.clay_btn_ghost)
                setTextColor(ContextCompat.getColor(context, R.color.clay_text))
                setOnClickListener { recrawlSource(id) }
            })
            actions.addView(Button(this).apply {
                text = getString(R.string.sources_delete)
                isAllCaps = false
                setBackgroundResource(R.drawable.clay_btn_danger)
                setTextColor(android.graphics.Color.WHITE)
                setOnClickListener {
                    deleteSource(id, if (domain.isNotEmpty()) domain else url)
                }
            })
            card.addView(actions)

            sourceList.addView(card)
        }
    }

    private fun sourceTypeLabel(type: String): String = when (type) {
        "domain" -> getString(R.string.sources_type_domain)
        "url" -> getString(R.string.sources_type_url)
        "sitemap" -> getString(R.string.sources_type_sitemap)
        "rss", "atom", "feed" -> getString(R.string.sources_type_rss)
        "wiki" -> getString(R.string.sources_type_wiki)
        else -> type
    }

    private fun showSourceStatus(text: String, ok: Boolean) {
        if (text.isEmpty()) {
            sourceStatus.visibility = View.GONE
            return
        }
        sourceStatus.text = text
        sourceStatus.setBackgroundResource(
            if (ok) R.drawable.clay_status_ok else R.drawable.clay_status_err)
        sourceStatus.setTextColor(ContextCompat.getColor(
            this, if (ok) R.color.clay_ok_text else R.color.clay_err_text))
        sourceStatus.visibility = View.VISIBLE
    }

    // ------------------------------------------------------------- webview ----

    /**
     * Create and configure the WebView the first time a web-backed tab is
     * opened. Doing this in onCreate made activity creation heavy enough to
     * miss the foreground-service start deadline on a slow device, so it is
     * deferred until it is actually needed.
     */
    @SuppressLint("SetJavaScriptEnabled")
    private fun ensureWebView(): WebView {
        web?.let { return it }
        val view = WebView(this)
        view.layoutParams = android.widget.FrameLayout.LayoutParams(
            android.view.ViewGroup.LayoutParams.MATCH_PARENT,
            android.view.ViewGroup.LayoutParams.MATCH_PARENT)
        webContainer.addView(view)
        web = view
        view.settings.javaScriptEnabled = true
        view.settings.domStorageEnabled = true
        // No local file or content access: the page is served over HTTP only.
        view.settings.allowFileAccess = false
        view.settings.allowContentAccess = false
        view.settings.javaScriptCanOpenWindowsAutomatically = false
        view.settings.setSupportMultipleWindows(false)
        view.settings.mixedContentMode = WebSettings.MIXED_CONTENT_NEVER_ALLOW
        // The Clay CSS is light-only. Android's "algorithmic darkening" would
        // invert the page in system dark mode and paint it black, so it is
        // turned off explicitly (the theme pins light colours as well).
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            view.settings.isAlgorithmicDarkeningAllowed = false
        } else {
            @Suppress("DEPRECATION")
            run { view.settings.forceDark = WebSettings.FORCE_DARK_OFF }
        }
        view.setBackgroundColor(android.graphics.Color.parseColor("#F5F5F5"))
        // Never add a JavaScript interface: that would expose native calls to
        // any page. Keep the WebView free of bridges.
        //
        // A WebChromeClient *is* required, though: without one the WebView
        // silently drops window.confirm()/window.alert() (onJsConfirm returns
        // false by default), so every admin form guarded by a confirm() — delete
        // a site, recrawl all, pause/resume all, reset settings — would look
        // like a dead button. Rendering the dialogs is what makes those work.
        view.webChromeClient = object : WebChromeClient() {
            override fun onJsConfirm(
                view: WebView?, url: String?, message: String?, result: JsResult?
            ): Boolean {
                if (message == null || result == null) return super.onJsConfirm(view, url, message, result)
                buildJsDialog(message, confirm = true,
                    onConfirm = { result.confirm() },
                    onCancel = { result.cancel() })
                return true
            }

            override fun onJsAlert(
                view: WebView?, url: String?, message: String?, result: JsResult?
            ): Boolean {
                if (message == null || result == null) return super.onJsAlert(view, url, message, result)
                buildJsDialog(message, confirm = false, onConfirm = { result.confirm() })
                return true
            }
        }
        view.webViewClient = object : WebViewClient() {
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

            /**
             * A renderer crash (typically the system killing the sandboxed
             * renderer under memory pressure) would otherwise take the whole
             * process down. Recover by reloading the local page instead.
             */
            override fun onRenderProcessGone(view: WebView, detail: RenderProcessGoneDetail): Boolean {
                Log.w(TAG, "WebView renderer gone (crashed=${detail.didCrash()})")
                loadedOnce = false
                if (ServerController.state == ServerState.RUNNING) {
                    view.postDelayed({
                        load(if (onAdmin) "${ServerController.BASE_URL}admin"
                             else ServerController.BASE_URL)
                    }, 500)
                }
                return true
            }
        }
        return view
    }

    /**
     * Show a JS dialog and hand the outcome back through [onConfirm]/[onCancel].
     *
     * Kept as a separate, side-effect-light method so an instrumentation test
     * can drive the same AlertDialog the WebChromeClient builds without a real
     * DevTools session attached (an attached DevTools client intercepts
     * window.confirm()/window.alert() itself, so onJsConfirm is never called).
     */
    internal fun buildJsDialog(
        message: String,
        confirm: Boolean,
        onConfirm: () -> Unit,
        onCancel: () -> Unit = {},
    ): AlertDialog {
        val builder = AlertDialog.Builder(this)
            .setMessage(message)
            .setCancelable(false)
            .setPositiveButton(android.R.string.ok) { _, _ -> onConfirm() }
        if (confirm) {
            builder.setNegativeButton(android.R.string.cancel) { _, _ -> onCancel() }
        }
        val dialog = builder.create()
        dialog.show()
        return dialog
    }

    private fun isLocal(uri: Uri): Boolean {
        val host = uri.host ?: return false
        return uri.scheme == "http" && (host == "127.0.0.1" || host == "localhost")
    }

    private fun load(url: String) {
        ensureWebView().loadUrl(url)
    }

    // ---------------------------------------------------------- dump import ----

    private fun pickDump() {
        if (ServerController.state != ServerState.RUNNING) {
            toast(getString(R.string.dump_need_running))
            return
        }
        // A multistream dump is an XML file, often bzip2-compressed; accept both
        // generic binaries and XML so the picker does not hide the download.
        dumpPicker.launch(arrayOf("application/x-bzip2", "application/gzip",
            "application/xml", "text/xml", "application/octet-stream"))
    }

    private fun copyAndImportDump(uri: Uri) {
        toast(getString(R.string.dump_copying))
        Thread({
            when (val result = DumpImporter.copyToPrivate(this, uri)) {
                is DumpImporter.Result.Success -> {
                    ServerController.importDump(result.path) { json, error ->
                        runOnUiThread {
                            if (error != null) {
                                toast(getString(R.string.dump_import_error, error))
                            } else if (json != null && json.contains("\"error\"")) {
                                toast(getString(R.string.dump_import_error, json))
                            } else {
                                toast(getString(R.string.dump_copied))
                            }
                        }
                    }
                }
                is DumpImporter.Result.Failure ->
                    runOnUiThread { toast(getString(R.string.dump_import_error, result.message)) }
            }
        }, "minisearch-dump-copy").start()
    }

    // ------------------------------------------------------------ rendering ----

    private fun renderState(state: ServerState) {
        when (state) {
            ServerState.STARTING -> {
                status.text = getString(R.string.state_starting)
                retry.visibility = View.GONE
                setHomeStatus(getString(R.string.state_starting), true)
                homeError.visibility = View.GONE
            }
            ServerState.RUNNING -> {
                status.text = getString(R.string.state_running, ServerController.backendLabel)
                retry.visibility = View.GONE
                setHomeStatus(getString(R.string.state_running, ServerController.backendLabel), true)
                homeError.visibility = View.GONE
                if (ServerController.backendLabel.isNotEmpty()) {
                    homeBackend.text = getString(R.string.home_status_detail, ServerController.backendLabel)
                    homeBackend.visibility = View.VISIBLE
                } else {
                    homeBackend.visibility = View.GONE
                }
                syncLanToggle()
                // Only now is localhost reachable.
                if ((currentTab == R.id.tab_search || currentTab == R.id.tab_admin) &&
                    (!loadedOnce || web?.url == null)) {
                    load(if (onAdmin) "${ServerController.BASE_URL}admin" else ServerController.BASE_URL)
                }
                if (currentTab == R.id.tab_sources) {
                    loadSources()
                }
            }
            ServerState.STOPPING -> {
                status.text = getString(R.string.state_stopping)
                setHomeStatus(getString(R.string.state_stopping), true)
            }
            ServerState.STOPPED -> {
                status.text = getString(R.string.state_stopped)
                retry.visibility = View.VISIBLE
                setHomeStatus(getString(R.string.state_stopped), false)
                homeError.visibility = View.GONE
                syncLanToggle()
            }
            ServerState.ERROR -> {
                status.text = getString(R.string.state_error, ServerController.lastError)
                retry.visibility = View.VISIBLE
                setHomeStatus(getString(R.string.state_error, ServerController.lastError), false)
                homeError.text = ServerController.lastError
                homeError.visibility = View.VISIBLE
                syncLanToggle()
            }
        }
    }

    private fun setHomeStatus(text: String, ok: Boolean) {
        homeStatus.text = text
        homeStatus.setBackgroundResource(
            if (ok) R.drawable.clay_status_ok else R.drawable.clay_status_err)
        homeStatus.setTextColor(ContextCompat.getColor(
            this, if (ok) R.color.clay_ok_text else R.color.clay_err_text))
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

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    }

    override fun onDestroy() {
        ServerController.removeListener(stateListener)
        web?.destroy()
        web = null
        super.onDestroy()
    }

    companion object {
        private const val TAG = "MainActivity"
    }
}
