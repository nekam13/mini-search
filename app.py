import sqlite3
import time
import json
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import crawler_engine
import threading
import os

app = Flask(__name__)

# Initialize database
crawler_engine.init_db()

# Start workers
worker_threads = crawler_engine.start_workers()


def auto_recrawl():
    """Auto-recrawl all sites every 12 hours"""
    conn = crawler_engine.get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, canonical_url, max_pages FROM sites WHERE status = 'active'")
    sites = cursor.fetchall()
    conn.close()
    
    for site_id, site_url, max_pages in sites:
        print(f"[Auto-recrawl] Re-crawling: {site_url}")
        crawler_engine.recrawl_site(site_id)


def get_site_stats(site_id):
    """Get statistics for a site"""
    conn = crawler_engine.get_db()
    cursor = conn.cursor()
    
    # Get total and done counts
    cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE site_id = ?", (site_id,))
    total = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE site_id = ? AND status = 'done'", (site_id,))
    done = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE site_id = ? AND status = 'error'", (site_id,))
    errors = cursor.fetchone()[0]
    
    conn.close()
    
    return total, done, errors


def get_error_details(site_id):
    """Get error details for a site"""
    conn = crawler_engine.get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT url, error_reason, retry_count 
        FROM crawl_queue 
        WHERE site_id = ? AND status = 'error' 
        ORDER BY id DESC 
        LIMIT 10
    """, (site_id,))
    errors = cursor.fetchall()
    
    conn.close()
    
    return errors


def get_sitemaps_feeds(site_id):
    """Get sitemaps and feeds for a site"""
    conn = crawler_engine.get_db()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, url, type, last_checked 
        FROM sitemaps_feeds 
        WHERE site_id = ? 
        ORDER BY type, url
    """, (site_id,))
    items = cursor.fetchall()
    
    conn.close()
    
    return items


def has_active_crawls():
    """Check if there are any active crawls"""
    conn = crawler_engine.get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE status IN ('pending', 'locked')")
    count = cursor.fetchone()[0]
    
    conn.close()
    
    return count > 0


HTML_CONSOLE = '''
<!DOCTYPE html>
<html lang="cs">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="5" />
    <title>Mini Search - Správcovská konzole</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; 
            background: #f8f9fa; 
            color: #202124; 
            line-height: 1.6; 
        }
        .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
        .header { background: white; padding: 20px 0; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .header-content { max-width: 1200px; margin: 0 auto; padding: 0 20px; display: flex; justify-content: space-between; align-items: center; }
        .logo { font-size: 1.8rem; font-weight: 500; color: #1a73e8; text-decoration: none; }
        .status-indicator { display: flex; align-items: center; gap: 10px; font-size: 0.9rem; }
        .status-dot { width: 10px; height: 10px; border-radius: 50%; background: #0f9d58; }
        .status-dot.idle { background: #db4437; }
        
        .card { 
            background: white; 
            border-radius: 8px; 
            box-shadow: 0 1px 3px rgba(0,0,0,0.12); 
            margin-bottom: 20px; 
            padding: 24px; 
        }
        
        h1 { font-size: 1.5rem; font-weight: 500; margin-bottom: 20px; color: #202124; }
        h2 { font-size: 1.2rem; font-weight: 500; margin-bottom: 16px; color: #202124; }
        
        .form-group { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
        input[type="text"], input[type="number"] { 
            padding: 10px 14px; 
            border: 1px solid #dadce0; 
            border-radius: 4px; 
            font-size: 1rem; 
            flex: 1; 
            min-width: 250px; 
        }
        input[type="text"]:focus, input[type="number"]:focus { 
            outline: none; 
            border-color: #1a73e8; 
            box-shadow: 0 0 0 2px rgba(26, 115, 232, 0.2); 
        }
        
        .btn { 
            background: #1a73e8; 
            color: white; 
            border: none; 
            padding: 10px 20px; 
            border-radius: 4px; 
            cursor: pointer; 
            font-size: 0.95rem; 
            text-decoration: none; 
            display: inline-block; 
            transition: background 0.2s; 
        }
        .btn:hover { background: #1557b0; }
        .btn:disabled { background: #9aa0a6; cursor: not-allowed; }
        
        .btn-secondary { background: #f1f3f4; color: #3c4043; }
        .btn-secondary:hover { background: #e8eaed; }
        
        .btn-danger { background: #db4437; }
        .btn-danger:hover { background: #c1351b; }
        
        .btn-success { background: #0f9d58; }
        .btn-success:hover { background: #0b8043; }
        
        .btn-sm { padding: 6px 12px; font-size: 0.85rem; }
        
        table { width: 100%; border-collapse: collapse; }
        th, td { text-align: left; padding: 12px 16px; border-bottom: 1px solid #e0e0e0; }
        th { 
            background: #f1f3f4; 
            font-weight: 500; 
            font-size: 0.85rem; 
            text-transform: uppercase; 
            color: #5f6368; 
        }
        tr:hover { background: #f8f9fa; }
        
        .status-badge { 
            display: inline-block; 
            padding: 4px 8px; 
            border-radius: 12px; 
            font-size: 0.8rem; 
            font-weight: 500; 
        }
        .status-ok { background: #e6f4ea; color: #0f9d58; }
        .status-warning { background: #fff1e6; color: #f4b400; }
        .status-error { background: #ffebee; color: #db4437; }
        .status-blocked { background: #f8f9fa; color: #9aa0a6; }
        
        .progress-bar { 
            width: 150px; 
            height: 20px; 
            background: #e0e0e0; 
            border-radius: 10px; 
            overflow: hidden; 
        }
        .progress-fill { 
            height: 100%; 
            background: #1a73e8; 
            transition: width 0.3s; 
        }
        
        .aliases-list { 
            font-size: 0.85rem; 
            color: #5f6368; 
            max-width: 300px; 
        }
        .aliases-list span { 
            display: inline-block; 
            margin-right: 4px; 
            background: #f1f3f4; 
            padding: 2px 6px; 
            border-radius: 4px; 
        }
        
        .action-buttons { display: flex; gap: 8px; }
        
        /* Sitemaps & Feeds */
        .sitemaps-section { margin-top: 20px; padding-top: 20px; border-top: 1px solid #e0e0e0; }
        .sitemap-item { 
            display: flex; 
            align-items: center; 
            gap: 12px; 
            padding: 8px 0; 
            border-bottom: 1px solid #f1f3f4; 
        }
        .sitemap-item:last-child { border-bottom: none; }
        .type-badge { 
            padding: 2px 8px; 
            border-radius: 4px; 
            font-size: 0.75rem; 
            font-weight: 500; 
        }
        .type-sitemap { background: #e8f0fe; color: #1a73e8; }
        .type-rss { background: #ffe0b2; color: #e65100; }
        .type-atom { background: #f3e5f5; color: #7b1fa2; }
        .sitemap-link { color: #1a73e8; text-decoration: none; }
        .sitemap-link:hover { text-decoration: underline; }
        .sitemap-date { font-size: 0.8rem; color: #70757a; }
        
        /* Error Modal */
        .modal { 
            display: none; 
            position: fixed; 
            z-index: 1000; 
            left: 0; 
            top: 0; 
            width: 100%; 
            height: 100%; 
            background: rgba(0,0,0,0.5); 
        }
        .modal-content { 
            background: white; 
            margin: 10% auto; 
            padding: 24px; 
            border-radius: 8px; 
            max-width: 600px; 
            max-height: 80vh; 
            overflow-y: auto; 
        }
        .modal-header { 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            margin-bottom: 16px; 
        }
        .modal-title { font-size: 1.2rem; font-weight: 500; }
        .close-btn { 
            background: none; 
            border: none; 
            font-size: 1.5rem; 
            cursor: pointer; 
            color: #70757a; 
        }
        .error-item { 
            padding: 12px; 
            border-bottom: 1px solid #e0e0e0; 
        }
        .error-item:last-child { border-bottom: none; }
        .error-url { font-weight: 500; margin-bottom: 4px; }
        .error-reason { font-size: 0.9rem; color: #db4437; }
        
        /* Loading indicator */
        .loading { display: inline-block; width: 20px; height: 20px; border: 2px solid #f3f3f3; border-top: 2px solid #1a73e8; border-radius: 50%; animation: spin 1s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
        
        .discovering { color: #1a73e8; }
        
        .no-results { text-align: center; padding: 40px; color: #70757a; }
        
        .refresh-note { 
            font-size: 0.85rem; 
            color: #70757a; 
            text-align: right; 
            margin-top: -15px; 
            margin-bottom: 10px; 
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="header-content">
            <a href="/" class="logo">🔍 Mini Search - Správcovská konzole</a>
            <div class="status-indicator">
                <span class="status-dot"></span>
                <span>Systém je aktivní</span>
            </div>
        </div>
    </div>

    <div class="container">
        <!-- Add Site Form -->
        <div class="card">
            <h2>Přidat nový web</h2>
            <form action="/add" method="post" class="form-group">
                <input type="text" name="url" placeholder="https://example.com" required>
                <input type="number" name="max_pages" value="500" min="1" max="10000" placeholder="Max stránek">
                <button type="submit" class="btn">Přidat a objevit</button>
            </form>
            {% if discovering_site %}
                <p class="discovering">⏳ Objevují se URL pro: <strong>{{ discovering_site }}</strong> <span class="loading"></span></p>
            {% endif %}
        </div>

        <!-- Sites Table -->
        <div class="card">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                <h2>Seznam webů</h2>
                <div class="refresh-note">
                    {% if has_active_crawls %}
                        ⏳ Auto-refresh každých 5 sekund (probíhá crawl)
                    {% else %}
                        ✅ Žádné aktivní crawly
                    {% endif %}
                </div>
            </div>
            
            {% if sites %}
            <table>
                <thead>
                    <tr>
                        <th>Kanonická URL</th>
                        <th>Alias</th>
                        <th>Max stránek</th>
                        <th>Naposledy crawlováno</th>
                        <th>Status</th>
                        <th>Pokrok</th>
                        <th>Akce</th>
                    </tr>
                </thead>
                <tbody>
                    {% for site in sites %}
                    <tr>
                        <td><strong>{{ site.canonical_url }}</strong></td>
                        <td class="aliases-list">
                            {% if site.aliases %}
                                {% for alias in site.aliases %}
                                    <span>{{ alias }}</span>
                                {% endfor %}
                            {% else %}
                                —
                            {% endif %}
                        </td>
                        <td>{{ site.max_pages }}</td>
                        <td>{{ site.last_crawled }}</td>
                        <td>
                            {% if site.status == 'active' %}
                                {% if site.error_count > 0 %}
                                    <span class="status-badge status-warning" onclick="showErrors({{ site.id }})" style="cursor: pointer;">⚠️ {{ site.error_count }} chyb</span>
                                {% else %}
                                    <span class="status-badge status-ok">✅ OK</span>
                                {% endif %}
                            {% elif site.status == 'blocked' %}
                                <span class="status-badge status-blocked">🚫 Blokováno</span>
                            {% else %}
                                <span class="status-badge status-error">❌ Chyba</span>
                            {% endif %}
                        </td>
                        <td>
                            <div class="progress-bar">
                                <div class="progress-fill" style="width: {{ site.progress_percent }}%"></div>
                            </div>
                            <small>{{ site.progress_text }}</small>
                        </td>
                        <td class="action-buttons">
                            <a href="/recrawl/{{ site.id }}" class="btn btn-secondary btn-sm">Re-crawl</a>
                            <a href="/delete/{{ site.id }}" class="btn btn-danger btn-sm" onclick="return confirm('Opravdu chcete smazat tento web a všechna jeho data?')">Smazat</a>
                        </td>
                    </tr>
                    
                    <!-- Sitemaps & Feeds Section -->
                    <tr>
                        <td colspan="7">
                            <div class="sitemaps-section">
                                <strong style="margin-bottom: 12px; display: block;">Sitemapy & Feedy:</strong>
                                {% if site.sitemaps_feeds %}
                                    {% for sitemap in site.sitemaps_feeds %}
                                        <div class="sitemap-item">
                                            <span class="type-badge type-{{ sitemap.type }}">{{ sitemap.type|upper }}</span>
                                            <a href="{{ sitemap.url }}" target="_blank" class="sitemap-link">{{ sitemap.url }}</a>
                                            <span class="sitemap-date">Naposledy: {{ sitemap.last_checked }}</span>
                                        </div>
                                    {% endfor %}
                                {% else %}
                                    <p style="color: #70757a; font-size: 0.9rem;">Žádné sitemapy ani feedy nenalezeny</p>
                                {% endif %}
                            </div>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
                <div class="no-results">
                    <p>Žádné weby nebyly přidány.</p>
                    <p style="margin-top: 10px;">Přidejte první web pomocí formuláře výše.</p>
                </div>
            {% endif %}
        </div>

        <!-- Links to other interfaces -->
        <div class="card" style="text-align: center;">
            <a href="http://localhost:8095" target="_blank" class="btn" style="background: #34a853; margin-right: 12px;">🔍 Otevřít vyhledávání (port 8095)</a>
            <span style="color: #70757a;">nebo</span>
            <a href="/" class="btn btn-secondary" style="margin-left: 12px;">Obnovit konzoli</a>
        </div>
    </div>

    <!-- Error Modal -->
    <div id="errorModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h3 class="modal-title">Chyby crawlu</h3>
                <button class="close-btn" onclick="closeModal()">&times;</button>
            </div>
            <div id="errorContent"></div>
        </div>
    </div>

    <script>
        // Show error modal
        function showErrors(siteId) {
            fetch('/errors/' + siteId)
                .then(response => response.json())
                .then(data => {
                    const content = document.getElementById('errorContent');
                    if (data.errors && data.errors.length > 0) {
                        content.innerHTML = data.errors.map(error => `
                            <div class="error-item">
                                <div class="error-url">${error.url}</div>
                                <div class="error-reason">${error.reason} (Počet pokusů: ${error.retry_count})</div>
                            </div>
                        `).join('');
                        document.getElementById('errorModal').style.display = 'block';
                    } else {
                        content.innerHTML = '<p>Žádné chyby nenalezeny.</p>';
                        document.getElementById('errorModal').style.display = 'block';
                    }
                });
        }

        // Close modal
        function closeModal() {
            document.getElementById('errorModal').style.display = 'none';
        }

        // Close modal when clicking outside
        window.onclick = function(event) {
            const modal = document.getElementById('errorModal');
            if (event.target === modal) {
                modal.style.display = 'none';
            }
        }

        // Auto-refresh if there are active crawls
        function checkActiveCrawls() {
            fetch('/has_active_crawls')
                .then(response => response.json())
                .then(data => {
                    if (data.active) {
                        setTimeout(() => location.reload(), 5000);
                    }
                });
        }

        // Check every 30 seconds
        setInterval(checkActiveCrawls, 30000);
    </script>
</body>
</html>
'''


@app.route('/')
def index():
    """Main admin console page"""
    conn = crawler_engine.get_db()
    cursor = conn.cursor()
    
    # Get all sites
    cursor.execute("""
        SELECT id, canonical_url, aliases, status, error_count, last_crawled, max_pages 
        FROM sites 
        ORDER BY last_crawled DESC, id DESC
    """)
    raw_sites = cursor.fetchall()
    
    sites = []
    for s in raw_sites:
        site_id, canonical_url, aliases_str, status, error_count, last_crawled, max_pages = s
        aliases = json.loads(aliases_str) if aliases_str else []
        
        # Get stats
        total, done, errors = get_site_stats(site_id)
        
        # Calculate progress
        if total > 0:
            progress_percent = min(100, (done / total) * 100)
            progress_text = f"{done}/{total}"
        else:
            progress_percent = 0
            progress_text = "0/0"
        
        # Format last crawled date
        if last_crawled > 0:
            last_crawled_str = datetime.fromtimestamp(last_crawled).strftime('%d.%m.%Y %H:%M')
        else:
            last_crawled_str = "Čeká na crawl"
        
        # Get sitemaps and feeds
        sitemaps_feeds = get_sitemaps_feeds(site_id)
        sitemaps_list = []
        for sm_id, sm_url, sm_type, sm_last_checked in sitemaps_feeds:
            sitemaps_list.append({
                'id': sm_id,
                'url': sm_url,
                'type': sm_type,
                'last_checked': datetime.fromtimestamp(sm_last_checked).strftime('%d.%m.%Y %H:%M') if sm_last_checked > 0 else "Nikdy"
            })
        
        sites.append({
            'id': site_id,
            'canonical_url': canonical_url,
            'aliases': aliases,
            'status': status,
            'error_count': error_count,
            'last_crawled': last_crawled_str,
            'max_pages': max_pages,
            'progress_percent': progress_percent,
            'progress_text': progress_text,
            'sitemaps_feeds': sitemaps_list
        })
    
    conn.close()
    
    # Check for discovering site in session
    discovering_site = request.args.get('discovering', None)
    
    return render_template_string(
        HTML_CONSOLE,
        sites=sites,
        has_active_crawls=has_active_crawls(),
        discovering_site=discovering_site
    )


@app.route('/add', methods=['POST'])
def add_site():
    """Add a new site and trigger Phase 1 discovery"""
    url = request.form['url'].strip()
    max_pages = int(request.form.get('max_pages', 500))
    
    # Add site and trigger discovery
    site_id = crawler_engine.add_site(url, max_pages)
    crawler_engine.phase_1_discovery(url, max_pages)
    
    return redirect(f'/?discovering={url}')


@app.route('/recrawl/<int:site_id>')
def recrawl_site(site_id):
    """Re-crawl a site"""
    crawler_engine.recrawl_site(site_id)
    return redirect('/')


@app.route('/delete/<int:site_id>')
def delete_site(site_id):
    """Delete a site and all its data"""
    crawler_engine.delete_site(site_id)
    return redirect('/')


@app.route('/errors/<int:site_id>')
def get_errors(site_id):
    """Get error details for a site"""
    errors = get_error_details(site_id)
    error_list = []
    for url, error_reason, retry_count in errors:
        error_list.append({
            'url': url,
            'reason': error_reason or 'Neznámá chyba',
            'retry_count': retry_count
        })
    
    return jsonify({'errors': error_list})


@app.route('/has_active_crawls')
def check_active_crawls():
    """Check if there are active crawls"""
    return jsonify({'active': has_active_crawls()})


# Initialize scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=auto_recrawl, trigger="interval", hours=12)
scheduler.start()


if __name__ == '__main__':
    # Make sure database is initialized
    crawler_engine.init_db()
    
    # Start workers
    worker_threads = crawler_engine.start_workers()
    
    print("=" * 60)
    print("Mini Search - Správcovská konzole")
    print("=" * 60)
    print(f"Spouštím na http://0.0.0.0:5000")
    print("Ctrl+C pro ukončení")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=5000, threaded=True)
