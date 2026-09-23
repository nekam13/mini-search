/* Mini Search - admin client logic (vanilla JS + fetch) */
(function () {
    'use strict';

    function showMessage(text, ok) {
        var box = document.getElementById('flash');
        if (!box) return;
        box.className = 'msg ' + (ok ? 'msg-ok' : 'msg-err');
        box.textContent = text;
        box.style.display = 'block';
        window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    async function api(url, options) {
        var response = await fetch(url, Object.assign({
            headers: { 'Content-Type': 'application/json' }
        }, options || {}));
        var data = null;
        try { data = await response.json(); } catch (e) { data = {}; }
        if (!response.ok) {
            throw new Error((data && data.error) || ('HTTP ' + response.status));
        }
        return data;
    }

    /* ---------- Source form ---------- */
    function initSourceForm() {
        var form = document.getElementById('source-form');
        if (!form) return;

        var typeSelect = form.querySelector('[name="source_type"]');
        var urlInput = form.querySelector('[name="url"]');
        var detected = false;

        if (typeSelect && urlInput) {
            urlInput.addEventListener('input', function () {
                if (detected || typeSelect.value !== 'url') return;
                var value = urlInput.value.toLowerCase();
                var guess = null;
                if (value.indexOf('sitemap') !== -1 || /\.xml(\.gz)?$/.test(value)) {
                    guess = 'sitemap';
                } else if (value.indexOf('feed') !== -1 || value.indexOf('/rss') !== -1 || /\.(rss|atom)$/.test(value)) {
                    guess = 'rss';
                }
                if (guess) {
                    typeSelect.value = guess;
                    detected = true;
                }
            });
        }

        form.addEventListener('submit', async function (event) {
            event.preventDefault();
            var payload = {};
            new FormData(form).forEach(function (value, key) {
                if (value !== '') payload[key] = value;
            });
            try {
                var result = await api(form.dataset.endpoint, {
                    method: form.dataset.method || 'POST',
                    body: JSON.stringify(payload)
                });
                showMessage((result && result.message) || 'Uloženo', true);
                setTimeout(function () { window.location.href = form.dataset.redirect; }, 700);
            } catch (err) {
                showMessage(err.message, false);
            }
        });
    }

    /* ---------- Delete buttons ---------- */
    function initDeleteButtons() {
        document.querySelectorAll('[data-delete-source]').forEach(function (button) {
            button.addEventListener('click', async function (event) {
                event.preventDefault();
                if (!window.confirm('Opravdu smazat tento zdroj?')) return;
                try {
                    await api('/admin/api/sources/' + button.dataset.deleteSource, { method: 'DELETE' });
                    showMessage('Zdroj smazán', true);
                    setTimeout(function () { window.location.reload(); }, 400);
                } catch (err) {
                    showMessage(err.message, false);
                }
            });
        });
    }

    /* ---------- Quick status toggle ---------- */
    function initStatusToggles() {
        document.querySelectorAll('[data-toggle-status]').forEach(function (button) {
            button.addEventListener('click', async function (event) {
                event.preventDefault();
                var next = button.dataset.toggleStatus;
                try {
                    await api('/admin/api/sites/' + button.dataset.siteId, {
                        method: 'PUT',
                        body: JSON.stringify({ status: next })
                    });
                    showMessage('Stav upraven', true);
                    setTimeout(function () { window.location.reload(); }, 400);
                } catch (err) {
                    showMessage(err.message, false);
                }
            });
        });
    }

    /* ---------- Live stats refresh ---------- */
    function initStatsRefresh() {
        var target = document.getElementById('global-stats');
        if (!target) return;
        setInterval(async function () {
            try {
                var stats = await api('/admin/api/stats');
                Object.keys(stats).forEach(function (key) {
                    var el = target.querySelector('[data-stat="' + key + '"]');
                    if (el) el.textContent = stats[key];
                });
            } catch (e) { /* ignore transient errors */ }
        }, 20000);
    }

    document.addEventListener('DOMContentLoaded', function () {
        initSourceForm();
        initDeleteButtons();
        initStatusToggles();
        initStatsRefresh();
    });
})();
