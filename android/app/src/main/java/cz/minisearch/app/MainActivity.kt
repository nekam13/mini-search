package cz.minisearch.app

import android.Manifest
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
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
 * The app shell. Everything is native: five Clay tabs (Domů / Hledat / Zdroje /
 * Weby / Nastavení), each a real Android view. There is no WebView anywhere —
 * search, sources, sites and settings are all rendered from the bundled Python
 * server through [ServerController], so the configuration and the index have a
 * single source of truth and no web page is ever shown inside the app.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var homeView: View
    private lateinit var searchView: View
    private lateinit var sourcesView: View
    private lateinit var sitesView: View
    private lateinit var settingsView: View
    private lateinit var status: TextView
    private lateinit var retry: Button

    // Home screen
    private lateinit var homeStatus: TextView
    private lateinit var homeBackend: TextView
    private lateinit var homeError: TextView
    private lateinit var homeStats: LinearLayout
    private lateinit var homeStart: Button
    private lateinit var homeStop: Button
    private lateinit var lanShare: CheckBox
    private lateinit var lanUrl: TextView
    private lateinit var lanCopy: Button

    // Search screen
    private lateinit var searchQuery: EditText
    private lateinit var searchFilter: Spinner
    private lateinit var searchStatus: TextView
    private lateinit var searchResults: LinearLayout
    private lateinit var searchNews: LinearLayout
    private lateinit var searchNewsTitle: TextView

    // Sources screen
    private lateinit var sourceList: LinearLayout
    private lateinit var sourceStatus: TextView
    private lateinit var sourceUrl: EditText
    private lateinit var sourceType: Spinner
    private lateinit var sourceAdd: Button

    // Sites screen
    private lateinit var sitesListPane: View
    private lateinit var sitesFilter: EditText
    private lateinit var sitesLocalOnly: CheckBox
    private lateinit var sitesStatus: TextView
    private lateinit var sitesList: LinearLayout
    private lateinit var siteDetailPane: View
    private lateinit var siteDetailTitle: TextView
    private lateinit var siteDetailStatus: TextView
    private lateinit var siteDetailBody: LinearLayout

    // Settings screen
    private lateinit var settingsGroups: LinearLayout
    private lateinit var settingsStatus: TextView
    private lateinit var settingsSave: Button
    private lateinit var settingsReset: Button

    private val tabs = mutableMapOf<Int, Button>()
    private var currentTab = R.id.tab_home
    private var settingsLoaded = false
    // Set while code reflects the backend state into the checkbox, so the
    // programmatic change does not look like a user tap and re-trigger a restart.
    private var syncingLanToggle = false
    private val settingFields = mutableMapOf<String, () -> Any>()
    // Widgets of the open site editor, keyed by field name.
    private val siteFields = mutableMapOf<String, () -> Any>()
    private var currentSiteId = -1

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

        homeView = findViewById(R.id.home_view)
        searchView = findViewById(R.id.search_view)
        sourcesView = findViewById(R.id.sources_view)
        sitesView = findViewById(R.id.sites_view)
        settingsView = findViewById(R.id.settings_view)
        status = findViewById(R.id.status)
        retry = findViewById(R.id.retry)

        homeStatus = findViewById(R.id.home_status)
        homeBackend = findViewById(R.id.home_backend)
        homeError = findViewById(R.id.home_error)
        homeStats = findViewById(R.id.home_stats)
        homeStart = findViewById(R.id.home_start)
        homeStop = findViewById(R.id.home_stop)
        lanShare = findViewById(R.id.lan_share)
        lanUrl = findViewById(R.id.lan_url)
        lanCopy = findViewById(R.id.lan_copy)

        searchQuery = findViewById(R.id.search_query)
        searchFilter = findViewById(R.id.search_filter)
        searchStatus = findViewById(R.id.search_status)
        searchResults = findViewById(R.id.search_results)
        searchNews = findViewById(R.id.search_news)
        searchNewsTitle = findViewById(R.id.search_news_title)

        sourceList = findViewById(R.id.source_list)
        sourceStatus = findViewById(R.id.source_status)
        sourceUrl = findViewById(R.id.source_url)
        sourceType = findViewById(R.id.source_type)
        sourceAdd = findViewById(R.id.source_add)

        sitesListPane = findViewById(R.id.sites_list_pane)
        sitesFilter = findViewById(R.id.sites_filter)
        sitesLocalOnly = findViewById(R.id.sites_local_only)
        sitesStatus = findViewById(R.id.sites_status)
        sitesList = findViewById(R.id.sites_list)
        siteDetailPane = findViewById(R.id.site_detail_pane)
        siteDetailTitle = findViewById(R.id.site_detail_title)
        siteDetailStatus = findViewById(R.id.site_detail_status)
        siteDetailBody = findViewById(R.id.site_detail_body)

        settingsGroups = findViewById(R.id.settings_groups)
        settingsStatus = findViewById(R.id.settings_status)
        settingsSave = findViewById(R.id.settings_save)
        settingsReset = findViewById(R.id.settings_reset)

        tabs[R.id.tab_home] = findViewById(R.id.tab_home)
        tabs[R.id.tab_search] = findViewById(R.id.tab_search)
        tabs[R.id.tab_sources] = findViewById(R.id.tab_sources)
        tabs[R.id.tab_web] = findViewById(R.id.tab_web)
        tabs[R.id.tab_settings] = findViewById(R.id.tab_settings)

        wireTabs()
        wireHome()
        wireSearch()
        wireSources()
        wireSites()
        wireSettings()

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
        searchView.visibility = if (id == R.id.tab_search) View.VISIBLE else View.GONE
        sourcesView.visibility = if (id == R.id.tab_sources) View.VISIBLE else View.GONE
        sitesView.visibility = if (id == R.id.tab_web) View.VISIBLE else View.GONE
        settingsView.visibility = if (id == R.id.tab_settings) View.VISIBLE else View.GONE

        when (id) {
            R.id.tab_home -> loadDashboard()
            R.id.tab_search -> loadNews()
            R.id.tab_sources -> loadSources()
            R.id.tab_web -> loadSites()
            R.id.tab_settings -> loadSettings()
        }
    }

    // ---------------------------------------------------------------- home ----

    private fun wireHome() {
        homeStart.setOnClickListener {
            UserState.serverEnabled = true
            ServerService.start(this)
            ServerController.start(this)
        }
        findViewById<Button>(R.id.home_restart).setOnClickListener {
            UserState.serverEnabled = true
            ServerService.start(this)
            ServerController.restart(this)
        }
        homeStop.setOnClickListener {
            UserState.serverEnabled = false
            ServerService.stop(this)
        }
        findViewById<Button>(R.id.home_recrawl_all).setOnClickListener {
            ServerController.recrawlAll { message, error ->
                runOnUiThread { toast(error?.let { getString(R.string.sources_error, it) } ?: message!!) }
            }
        }
        findViewById<Button>(R.id.home_pause_all).setOnClickListener {
            ServerController.pauseAll { message, error ->
                runOnUiThread {
                    toast(error?.let { getString(R.string.sources_error, it) } ?: message!!)
                    loadDashboard()
                }
            }
        }
        findViewById<Button>(R.id.home_resume_all).setOnClickListener {
            ServerController.resumeAll { message, error ->
                runOnUiThread {
                    toast(error?.let { getString(R.string.sources_error, it) } ?: message!!)
                    loadDashboard()
                }
            }
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
                syncLanToggle()
                toast(getString(R.string.lan_share_needs_running))
                return@setOnCheckedChangeListener
            }
            ServerController.setLanShare(this, checked)
        }
    }

    private fun loadDashboard() {
        if (ServerController.state != ServerState.RUNNING) {
            renderDashboard(null)
            return
        }
        ServerController.loadDashboard { json, _ ->
            runOnUiThread { renderDashboard(json) }
        }
    }

    private fun renderDashboard(json: String?) {
        homeStats.removeAllViews()
        if (json == null) return
        try {
            val stats = JSONObject(json).getJSONObject("stats")
            val entries = listOf(
                stats.optInt("sites") to getString(R.string.home_stat_sites),
                stats.optInt("pages") to getString(R.string.home_stat_pages),
                stats.optInt("pending") to getString(R.string.home_stat_pending),
                stats.optInt("errors") to getString(R.string.home_stat_errors),
                stats.optInt("sources") to getString(R.string.home_stat_sources))
            for ((value, label) in entries) {
                homeStats.addView(statCell(value.toString(), label))
            }
        } catch (t: Throwable) {
            Log.w(TAG, "dashboard parse failed", t)
        }
    }

    private fun statCell(value: String, label: String): View {
        val column = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            layoutParams = LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f)
        }
        column.addView(TextView(this).apply {
            text = value
            setTextColor(ContextCompat.getColor(context, R.color.clay_text))
            textSize = 18f
            setTypeface(typeface, Typeface.BOLD)
            gravity = Gravity.CENTER
        })
        column.addView(TextView(this).apply {
            text = label
            setTextColor(ContextCompat.getColor(context, R.color.clay_text_light))
            textSize = 11f
            gravity = Gravity.CENTER
        })
        return column
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

    // -------------------------------------------------------------- search ----

    private val searchFilterValues =
        listOf("all", "articles", "podcasts", "audio", "price")

    private fun wireSearch() {
        val labels = listOf(
            getString(R.string.filter_all), getString(R.string.filter_articles),
            getString(R.string.filter_podcasts), getString(R.string.filter_audio),
            getString(R.string.filter_price))
        searchFilter.adapter = ArrayAdapter(
            this, android.R.layout.simple_spinner_dropdown_item, labels)
        findViewById<Button>(R.id.search_button).setOnClickListener { doSearch() }
        searchQuery.setOnEditorActionListener { _, _, _ -> doSearch(); true }
    }

    private fun doSearch() {
        val query = searchQuery.text.toString().trim()
        if (query.isEmpty()) {
            showSearchStatus(getString(R.string.search_empty_prompt))
            searchResults.removeAllViews()
            loadNews()
            return
        }
        if (ServerController.state != ServerState.RUNNING) {
            showSearchStatus(getString(R.string.search_need_running))
            return
        }
        showSearchStatus(getString(R.string.search_button) + "…")
        val payload = JSONObject()
        payload.put("q", query)
        payload.put("filter", searchFilterValues[searchFilter.selectedItemPosition])
        payload.put("limit", 25)
        ServerController.search(payload.toString()) { json, error ->
            runOnUiThread {
                if (error != null || json == null) {
                    showSearchStatus(getString(R.string.search_error, error ?: ""))
                    return@runOnUiThread
                }
                renderSearch(json)
            }
        }
    }

    private fun renderSearch(json: String) {
        searchResults.removeAllViews()
        searchNews.visibility = View.GONE
        searchNewsTitle.visibility = View.GONE
        try {
            val obj = JSONObject(json)
            if (obj.has("error")) {
                showSearchStatus(getString(R.string.search_error, obj.optString("error")))
                return
            }
            val results = obj.optJSONArray("results") ?: JSONArray()
            val query = obj.optString("query")
            if (results.length() == 0) {
                showSearchStatus(getString(R.string.search_no_results, query))
                return
            }
            showSearchStatus(getString(R.string.search_result_count,
                results.length(), plural(results.length()), query))
            for (i in 0 until results.length()) {
                searchResults.addView(resultCard(results.getJSONObject(i)))
            }
        } catch (t: Throwable) {
            showSearchStatus(getString(R.string.search_error, t.message ?: ""))
        }
    }

    private fun resultCard(item: JSONObject): View {
        val card = card()
        card.addView(TextView(this).apply {
            text = item.optString("title")
            setTextColor(ContextCompat.getColor(context, R.color.clay_text))
            textSize = 16f
            setTypeface(typeface, Typeface.BOLD)
            setOnClickListener { openUrl(item.optString("url")) }
        })
        card.addView(TextView(this).apply {
            text = item.optString("domain") + item.optString("path")
            setTextColor(ContextCompat.getColor(context, R.color.clay_secondary_dark))
            textSize = 12f
            setPadding(0, dp(2), 0, 0)
        })
        val snippet = item.optString("snippet")
        if (snippet.isNotEmpty()) {
            card.addView(TextView(this).apply {
                text = snippet
                setTextColor(ContextCompat.getColor(context, R.color.clay_text_light))
                textSize = 13f
                setPadding(0, dp(4), 0, 0)
            })
        }
        val meta = buildString {
            append(getString(R.string.sites_indexed)).append(": ")
            append(item.optInt("relevance")).append("%")
            val date = item.optString("date")
            if (date.isNotEmpty()) append(" · ").append(date)
        }
        card.addView(TextView(this).apply {
            text = meta
            setTextColor(ContextCompat.getColor(context, R.color.clay_text_light))
            textSize = 11f
            setPadding(0, dp(4), 0, 0)
        })
        return card
    }

    private fun loadNews() {
        if (ServerController.state != ServerState.RUNNING) return
        ServerController.loadNews { json, _ ->
            runOnUiThread {
                if (json == null) return@runOnUiThread
                try {
                    val items = JSONObject(json).optJSONArray("items") ?: JSONArray()
                    searchNews.removeAllViews()
                    if (items.length() == 0) {
                        searchNews.visibility = View.GONE
                        searchNewsTitle.visibility = View.GONE
                        return@runOnUiThread
                    }
                    searchNewsTitle.visibility = View.VISIBLE
                    searchNews.visibility = View.VISIBLE
                    for (i in 0 until items.length()) {
                        val item = items.getJSONObject(i)
                        searchNews.addView(TextView(this).apply {
                            text = item.optString("title")
                            setTextColor(ContextCompat.getColor(context, R.color.clay_text))
                            textSize = 14f
                            setPadding(dp(4), dp(6), dp(4), dp(6))
                            setOnClickListener { openUrl(item.optString("url")) }
                        })
                    }
                } catch (t: Throwable) {
                    Log.w(TAG, "news parse failed", t)
                }
            }
        }
    }

    private fun showSearchStatus(text: String) {
        searchStatus.text = text
    }

    private fun plural(n: Int): String =
        if (n == 1) "výsledek" else if (n in 2..4) "výsledky" else "výsledků"

    private fun openUrl(url: String) {
        if (url.isEmpty()) return
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
        } catch (t: Throwable) {
            toast(getString(R.string.search_open_failed))
        }
    }

    // ------------------------------------------------------------ settings ----

    private fun wireSettings() {
        settingsSave.setOnClickListener { saveSettings() }
        settingsReset.setOnClickListener {
            buildConfirmDialog(getString(R.string.settings_reset_confirm)) { resetSettings() }
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
        for (g in 0 until groups.length()) {
            val group = groups.getJSONObject(g)
            val card = card()
            card.addView(sectionTitle(group.optString("label")))
            val items = group.optJSONArray("items") ?: JSONArray()
            for (i in 0 until items.length()) {
                card.addView(buildSettingRow(items.getJSONObject(i)))
            }
            settingsGroups.addView(card)
        }
    }

    private fun buildSettingRow(item: JSONObject): View {
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
            setTypeface(typeface, Typeface.BOLD)
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
                val index = values.indexOf(item.optString("value"))
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
        buildConfirmDialog(getString(R.string.sources_delete_confirm, label)) {
            ServerController.deleteSource(id) { message, error ->
                runOnUiThread {
                    if (error != null) {
                        showSourceStatus(getString(R.string.sources_error, error), false)
                    } else {
                        showSourceStatus(message ?: getString(R.string.sources_deleted), true)
                        loadSources()
                    }
                }
            }
        }
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

            val card = card()
            card.addView(TextView(this).apply {
                text = if (domain.isNotEmpty()) domain else url
                setTextColor(ContextCompat.getColor(context, R.color.clay_text))
                textSize = 15f
                setTypeface(typeface, Typeface.BOLD)
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
            card.addView(LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                setPadding(0, dp(8), 0, 0)
                addView(ghostButton(getString(R.string.sources_recrawl)) { recrawlSource(id) })
                addView(dangerButton(getString(R.string.sources_delete)) {
                    deleteSource(id, if (domain.isNotEmpty()) domain else url)
                })
            })
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

    // --------------------------------------------------------------- sites ----

    private fun wireSites() {
        sitesFilter.setOnEditorActionListener { _, _, _ -> loadSites(); true }
        sitesLocalOnly.setOnCheckedChangeListener { _, _ -> loadSites() }
        findViewById<Button>(R.id.site_back).setOnClickListener { showSiteList() }
    }

    private fun loadSites() {
        showSiteList()
        if (ServerController.state != ServerState.RUNNING) {
            renderSites(JSONArray())
            showSitesStatus(getString(R.string.sources_need_running), false)
            return
        }
        ServerController.loadSites(
            sitesFilter.text.toString().trim(), "", sitesLocalOnly.isChecked) { json, error ->
            runOnUiThread {
                if (error != null || json == null) {
                    renderSites(JSONArray())
                    showSitesStatus(getString(R.string.sites_load_error, error ?: ""), false)
                    return@runOnUiThread
                }
                try {
                    val obj = JSONObject(json)
                    if (obj.has("error")) {
                        renderSites(JSONArray())
                        showSitesStatus(getString(R.string.sites_load_error, obj.optString("error")), false)
                    } else {
                        renderSites(obj.optJSONArray("sites") ?: JSONArray())
                    }
                } catch (t: Throwable) {
                    renderSites(JSONArray())
                    showSitesStatus(getString(R.string.sites_load_error, t.message ?: ""), false)
                }
            }
        }
    }

    private fun showSiteList() {
        sitesListPane.visibility = View.VISIBLE
        siteDetailPane.visibility = View.GONE
        currentSiteId = -1
    }

    private fun renderSites(sites: JSONArray) {
        sitesList.removeAllViews()
        if (sites.length() == 0) {
            sitesList.addView(TextView(this).apply {
                text = getString(R.string.sites_empty)
                setTextColor(ContextCompat.getColor(context, R.color.clay_text_light))
                textSize = 14f
                setPadding(dp(4), dp(8), dp(4), dp(8))
            })
            return
        }
        for (i in 0 until sites.length()) {
            val site = sites.getJSONObject(i)
            val id = site.optInt("id", -1)
            val card = card()
            card.addView(TextView(this).apply {
                text = site.optString("url")
                setTextColor(ContextCompat.getColor(context, R.color.clay_text))
                textSize = 16f
                setTypeface(typeface, Typeface.BOLD)
                setOnClickListener { openSite(id) }
            })
            val badges = buildString {
                append(statusLabel(site.optString("status")))
                if (site.optBoolean("is_local")) append(" · ").append(getString(R.string.sites_local_badge))
            }
            card.addView(TextView(this).apply {
                text = badges
                setTextColor(ContextCompat.getColor(context, R.color.clay_secondary_dark))
                textSize = 12f
                setPadding(0, dp(2), 0, 0)
            })
            card.addView(TextView(this).apply {
                text = getString(R.string.sites_indexed) + ": " + site.optInt("indexed") +
                        " · " + getString(R.string.sites_pending) + ": " + site.optInt("pending") +
                        " · " + getString(R.string.sites_errors) + ": " + site.optInt("errors")
                setTextColor(ContextCompat.getColor(context, R.color.clay_text_light))
                textSize = 12f
                setPadding(0, dp(4), 0, 0)
            })
            card.addView(LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                setPadding(0, dp(8), 0, 0)
                addView(ghostButton(getString(R.string.sites_edit)) { openSite(id) })
            })
            sitesList.addView(card)
        }
    }

    private fun showSitesStatus(text: String, ok: Boolean) {
        if (text.isEmpty()) {
            sitesStatus.visibility = View.GONE
            return
        }
        sitesStatus.text = text
        sitesStatus.setTextColor(ContextCompat.getColor(
            this, if (ok) R.color.clay_ok_text else R.color.clay_err_text))
        sitesStatus.visibility = View.VISIBLE
    }

    private fun statusLabel(status: String): String = when (status) {
        "active" -> getString(R.string.sites_status_active)
        "paused" -> getString(R.string.sites_status_paused)
        "blocked" -> getString(R.string.sites_status_blocked)
        else -> status
    }

    private fun openSite(id: Int) {
        currentSiteId = id
        sitesListPane.visibility = View.GONE
        siteDetailPane.visibility = View.VISIBLE
        siteDetailTitle.text = "…"
        siteDetailStatus.text = ""
        siteDetailBody.removeAllViews()
        ServerController.loadSiteDetail(id) { json, error ->
            runOnUiThread {
                if (error != null || json == null) {
                    siteDetailStatus.text = getString(R.string.sites_detail_error, error ?: "")
                    return@runOnUiThread
                }
                renderSiteDetail(json)
            }
        }
    }

    private fun renderSiteDetail(json: String) {
        siteDetailBody.removeAllViews()
        siteFields.clear()
        try {
            val obj = JSONObject(json)
            if (obj.has("error")) {
                siteDetailStatus.text = getString(R.string.sites_detail_error, obj.optString("error"))
                return
            }
            val site = obj.getJSONObject("site")
            val siteId = site.optInt("id", currentSiteId)
            siteDetailTitle.text = site.optString("url")
            siteDetailStatus.text = getString(R.string.sites_last_crawled, site.optString("last_crawled"))

            val editor = card()
            editor.addView(sectionTitle(getString(R.string.sites_edit)))

            val maxPages = EditText(this).apply {
                inputType = InputType.TYPE_CLASS_NUMBER
                setText(site.optInt("max_pages").toString())
                setTextColor(ContextCompat.getColor(context, R.color.clay_text))
            }
            editor.addView(fieldLabel(getString(R.string.sites_edit_max_pages)))
            editor.addView(maxPages)
            siteFields["max_pages"] = { maxPages.text.toString().trim() }

            val statuses = obj.optJSONArray("statuses") ?: JSONArray()
            val statusValues = ArrayList<String>()
            val statusLabels = ArrayList<String>()
            for (s in 0 until statuses.length()) {
                val v = statuses.getString(s)
                statusValues.add(v)
                statusLabels.add(statusLabel(v))
            }
            val statusSpinner = Spinner(this)
            statusSpinner.adapter = ArrayAdapter(
                this, android.R.layout.simple_spinner_dropdown_item, statusLabels)
            val sIndex = statusValues.indexOf(site.optString("status"))
            if (sIndex >= 0) statusSpinner.setSelection(sIndex)
            editor.addView(fieldLabel(getString(R.string.sites_edit_status)))
            editor.addView(statusSpinner)
            siteFields["status"] = { statusValues[statusSpinner.selectedItemPosition] }

            val aliases = EditText(this).apply {
                setText(joinAliases(site.optJSONArray("aliases")))
                setTextColor(ContextCompat.getColor(context, R.color.clay_text))
            }
            editor.addView(fieldLabel(getString(R.string.sites_edit_aliases)))
            editor.addView(aliases)
            siteFields["aliases"] = { aliases.text.toString().trim() }

            val priority = EditText(this).apply {
                inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
                setText(site.optDouble("priority", 1.0).toString())
                setTextColor(ContextCompat.getColor(context, R.color.clay_text))
            }
            editor.addView(fieldLabel(getString(R.string.sites_edit_priority)))
            editor.addView(priority)
            siteFields["priority"] = { priority.text.toString().trim() }

            val local = CheckBox(this).apply {
                isChecked = site.optBoolean("is_local")
                setTextColor(ContextCompat.getColor(context, R.color.clay_text))
                text = getString(R.string.sites_edit_is_local)
            }
            editor.addView(local)
            siteFields["is_local"] = { local.isChecked }

            editor.addView(LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                setPadding(0, dp(8), 0, 0)
                addView(primaryButton(getString(R.string.settings_save)) { saveSite(siteId) })
                addView(ghostButton(getString(R.string.sites_recrawl)) { recrawlSite(siteId) })
                addView(dangerButton(getString(R.string.sites_delete)) {
                    deleteSite(siteId, site.optString("url"))
                })
            })
            siteDetailBody.addView(editor)

            siteDetailBody.addView(listCard(getString(R.string.sites_detail_sources),
                obj.optJSONArray("sources") ?: JSONArray()) { s ->
                sourceTypeLabel(s.optString("type")) + "\n" + s.optString("url")
            })
            siteDetailBody.addView(listCard(getString(R.string.sites_detail_pages),
                obj.optJSONArray("pages") ?: JSONArray()) { p ->
                p.optString("title") + "\n" + p.optString("indexed_at")
            })
            siteDetailBody.addView(listCard(getString(R.string.sites_detail_errors),
                obj.optJSONArray("errors") ?: JSONArray()) { e ->
                e.optString("url") + "\n" + e.optString("reason")
            })
        } catch (t: Throwable) {
            siteDetailStatus.text = getString(R.string.sites_detail_error, t.message ?: "")
        }
    }

    private fun joinAliases(aliases: JSONArray?): String {
        if (aliases == null) return ""
        val parts = ArrayList<String>()
        for (i in 0 until aliases.length()) parts.add(aliases.optString(i))
        return parts.joinToString(", ")
    }

    private fun listCard(title: String, items: JSONArray, line: (JSONObject) -> String): View {
        val card = card()
        card.addView(sectionTitle(title))
        if (items.length() == 0) {
            card.addView(TextView(this).apply {
                text = getString(R.string.sites_detail_none)
                setTextColor(ContextCompat.getColor(context, R.color.clay_text_light))
                textSize = 13f
            })
            return card
        }
        for (i in 0 until items.length()) {
            card.addView(TextView(this).apply {
                text = line(items.getJSONObject(i))
                setTextColor(ContextCompat.getColor(context, R.color.clay_text_light))
                textSize = 12f
                setPadding(0, dp(4), 0, 0)
            })
        }
        return card
    }

    private fun saveSite(siteId: Int) {
        val payload = JSONObject()
        payload.put("id", siteId)
        payload.put("max_pages", siteFields["max_pages"]?.invoke())
        payload.put("status", siteFields["status"]?.invoke())
        payload.put("aliases", siteFields["aliases"]?.invoke())
        payload.put("priority", siteFields["priority"]?.invoke())
        payload.put("is_local", siteFields["is_local"]?.invoke())
        ServerController.updateSite(payload.toString()) { message, error ->
            runOnUiThread {
                if (error != null) {
                    showSitesStatus(getString(R.string.sites_save_error, error), false)
                } else {
                    showSitesStatus(message ?: getString(R.string.sites_saved), true)
                    openSite(siteId)
                }
            }
        }
    }

    private fun deleteSite(siteId: Int, url: String) {
        buildConfirmDialog(getString(R.string.sites_delete_confirm, url)) {
            ServerController.deleteSite(siteId) { message, error ->
                runOnUiThread {
                    if (error != null) {
                        showSitesStatus(getString(R.string.sites_save_error, error), false)
                    } else {
                        showSiteList()
                        showSitesStatus(message ?: "", true)
                        loadSites()
                    }
                }
            }
        }
    }

    private fun recrawlSite(siteId: Int) {
        ServerController.recrawlSite(siteId) { message, error ->
            runOnUiThread {
                if (error != null) {
                    showSitesStatus(getString(R.string.sites_save_error, error), false)
                } else {
                    showSitesStatus(message ?: "", true)
                }
            }
        }
    }

    // ------------------------------------------------------- shared widgets ---

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private fun card(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setBackgroundResource(R.drawable.clay_card)
        setPadding(dp(18), dp(14), dp(18), dp(14))
        val lp = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        lp.bottomMargin = dp(10)
        layoutParams = lp
    }

    private fun sectionTitle(text: String): TextView = TextView(this).apply {
        this.text = text
        setTextColor(ContextCompat.getColor(context, R.color.clay_text))
        textSize = 15f
        setTypeface(typeface, Typeface.BOLD)
        setPadding(0, 0, 0, dp(6))
    }

    private fun fieldLabel(text: String): TextView = TextView(this).apply {
        this.text = text
        setTextColor(ContextCompat.getColor(context, R.color.clay_text))
        textSize = 12f
        setPadding(0, dp(8), 0, 0)
    }

    private fun ghostButton(text: String, onClick: () -> Unit): Button = Button(this).apply {
        this.text = text
        isAllCaps = false
        setBackgroundResource(R.drawable.clay_btn_ghost)
        setTextColor(ContextCompat.getColor(context, R.color.clay_text))
        setOnClickListener { onClick() }
    }

    private fun primaryButton(text: String, onClick: () -> Unit): Button = Button(this).apply {
        this.text = text
        isAllCaps = false
        setBackgroundResource(R.drawable.clay_btn_primary)
        setTextColor(Color.WHITE)
        setOnClickListener { onClick() }
    }

    private fun dangerButton(text: String, onClick: () -> Unit): Button = Button(this).apply {
        this.text = text
        isAllCaps = false
        setBackgroundResource(R.drawable.clay_btn_danger)
        setTextColor(Color.WHITE)
        setOnClickListener { onClick() }
    }

    /**
     * Show a native confirm dialog and run [onConfirm] when the user accepts.
     *
     * Kept as a separate, side-effect-light method so an instrumentation test
     * can drive the same AlertDialog the screens build.
     */
    internal fun buildConfirmDialog(message: String, onConfirm: () -> Unit): AlertDialog {
        val dialog = AlertDialog.Builder(this)
            .setMessage(message)
            .setCancelable(false)
            .setPositiveButton(android.R.string.ok) { _, _ -> onConfirm() }
            .setNegativeButton(android.R.string.cancel, null)
            .create()
        dialog.show()
        return dialog
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
        val running = state == ServerState.RUNNING
        homeStart.isEnabled = state == ServerState.STOPPED || state == ServerState.ERROR
        homeStop.isEnabled = running
        when (state) {
            ServerState.STARTING -> {
                status.text = getString(R.string.state_starting)
                retry.visibility = View.GONE
                setHomeStatus(getString(R.string.state_starting), true)
                homeError.visibility = View.GONE
            }
            ServerState.RUNNING -> {
                status.text = getString(R.string.state_running)
                retry.visibility = View.GONE
                setHomeStatus(getString(R.string.state_running), true)
                homeError.visibility = View.GONE
                if (ServerController.backendLabel.isNotEmpty()) {
                    homeBackend.text = getString(R.string.home_status_detail, ServerController.backendLabel)
                    homeBackend.visibility = View.VISIBLE
                } else {
                    homeBackend.visibility = View.GONE
                }
                syncLanToggle()
                when (currentTab) {
                    R.id.tab_home -> loadDashboard()
                    R.id.tab_search -> loadNews()
                    R.id.tab_sources -> loadSources()
                    R.id.tab_web -> loadSites()
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
                renderDashboard(null)
            }
            ServerState.ERROR -> {
                status.text = getString(R.string.state_error, ServerController.lastError)
                retry.visibility = View.VISIBLE
                setHomeStatus(getString(R.string.state_error, ServerController.lastError), false)
                homeError.text = ServerController.lastError
                homeError.visibility = View.VISIBLE
                syncLanToggle()
                renderDashboard(null)
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
        super.onDestroy()
    }

    companion object {
        private const val TAG = "MainActivity"
    }
}
