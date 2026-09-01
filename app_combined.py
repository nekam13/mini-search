#!/usr/bin/env python3
"""
Mini Search - Kombinovaná aplikace v5.0
Vše v jednom procesu pro maximální spolehlivost
- Správcovská konzole na /admin (port 8070)
- Vyhledávání na / (port 8070)
- Crawler engine v background thread
"""

import sqlite3
import json
import time
import threading
import signal
import sys
import os
from datetime import datetime
from urllib.parse import urlparse, urlunparse
from flask import Flask, render_template_string, request, redirect, jsonify

# ============================================================================
# KONFIGURACE
# ============================================================================

DB_PATH = "console.db"
MAX_RETRIES = 3
SHUTDOWN_FLAG = False

# ============================================================================
# DATABASE HELPERS
# ============================================================================

_db_lock = threading.Lock()
_db_conn = None


def get_db():
    """Get or create global SQLite connection with WAL mode"""
    global _db_conn
    
    with _db_lock:
        if _db_conn is None:
            os.makedirs("data", exist_ok=True)
            
            _db_conn = sqlite3.connect(
                DB_PATH,
                timeout=60.0,
                check_same_thread=False
            )
            _db_conn.execute("PRAGMA journal_mode=WAL")
            _db_conn.execute("PRAGMA foreign_keys=ON")
            _db_conn.execute("PRAGMA busy_timeout=30000")
            _db_conn.execute("PRAGMA wal_autocheckpoint=1000")
            _db_conn.execute("PRAGMA synchronous=NORMAL")
            _db_conn.row_factory = sqlite3.Row
            
            init_db(_db_conn)
        
        return _db_conn


def init_db(conn=None):
    """Initialize database tables"""
    if conn is None:
        conn = get_db()
    
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_url TEXT UNIQUE,
            aliases TEXT DEFAULT '[]',
            status TEXT DEFAULT 'active',
            error_count INTEGER DEFAULT 0,
            last_crawled INTEGER DEFAULT 0,
            max_pages INTEGER DEFAULT 500,
            created_at INTEGER DEFAULT 0
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS crawl_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER,
            url TEXT,
            status TEXT DEFAULT 'pending',
            locked_by TEXT DEFAULT '',
            error_reason TEXT DEFAULT '',
            retry_count INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT 0,
            FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER,
            url TEXT,
            title TEXT DEFAULT '',
            content TEXT DEFAULT '',
            metadata TEXT DEFAULT '{}',
            indexed_at INTEGER DEFAULT 0,
            FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
        )
    ''')
    
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_queue(status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_queue_site ON crawl_queue(site_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_pages_site ON pages(site_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_pages_content ON pages(content)')
    
    conn.commit()


def close_db():
    """Close database connection"""
    global _db_conn
    with _db_lock:
        if _db_conn is not None:
            _db_conn.close()
            _db_conn = None


def execute_db(query, params=(), commit=False):
    """Execute a database query with proper locking"""
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute(query, params)
        if commit:
            conn.commit()
        return cursor
    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            time.sleep(0.1)
            try:
                cursor.execute(query, params)
                if commit:
                    conn.commit()
                return cursor
            except Exception:
                raise
        raise


# ============================================================================
# URL HELPERS
# ============================================================================


def normalize_domain(url):
    """Normalize domain: strip www., lowercase, strip trailing slash"""
    if not url:
        return ""
    
    parsed = urlparse(url)
    
    if not parsed.scheme:
        url = f"https://{url}"
        parsed = urlparse(url)
    
    netloc = parsed.netloc.lower()
    
    if netloc.startswith("www."):
        netloc = netloc[4:]
    
    normalized = urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, ''))
    
    if normalized.endswith('/') and len(parsed.path) > 1:
        normalized = normalized[:-1]
    
    return normalized


# ============================================================================
# SITE MANAGEMENT
# ============================================================================


def add_site(site_url, max_pages=500):
    """Add a new site with domain deduplication"""
    site_url = normalize_domain(site_url)
    if not site_url:
        return None
    
    parsed = urlparse(site_url)
    canonical_domain = parsed.netloc
    
    result = execute_db_fetchone(
        "SELECT id, canonical_url, aliases FROM sites WHERE canonical_url = ? OR aliases LIKE ?",
        (canonical_domain, f"%{canonical_domain}%")
    )
    
    if result:
        site_id, existing_canonical, aliases_str = result
        aliases = json.loads(aliases_str) if aliases_str else []
        
        if site_url not in aliases and site_url != existing_canonical:
            aliases.append(site_url)
            execute_db(
                "UPDATE sites SET aliases = ? WHERE id = ?",
                (json.dumps(aliases), site_id),
                commit=True
            )
        
        return site_id
    
    execute_db(
        "INSERT INTO sites (canonical_url, aliases, status, max_pages) VALUES (?, ?, 'active', ?)",
        (canonical_domain, json.dumps([site_url]), max_pages),
        commit=True
    )
    
    return execute_db_fetchone("SELECT last_insert_rowid()")[0]


def get_site_by_id(site_id):
    """Get site by ID"""
    result = execute_db_fetchone("SELECT * FROM sites WHERE id = ?", (site_id,))
    return dict(result) if result else None


def get_all_sites():
    """Get all sites"""
    results = execute_db_fetchall("SELECT * FROM sites ORDER BY created_at DESC")
    sites = []
    for row in results:
        site = dict(row)
        if site['last_crawled'] > 0:
            site['last_crawled_str'] = datetime.fromtimestamp(site['last_crawled']).strftime('%Y-%m-%d %H:%M:%S')
        else:
            site['last_crawled_str'] = 'Nikdy'
        sites.append(site)
    return sites


def update_site_status(site_id, status):
    """Update site status"""
    execute_db(
        "UPDATE sites SET status = ? WHERE id = ?",
        (status, site_id),
        commit=True
    )


def delete_site(site_id):
    """Delete a site"""
    execute_db("DELETE FROM sites WHERE id = ?", (site_id,), commit=True)


def recrawl_site(site_id):
    """Re-crawl a site by resetting its queue"""
    site = get_site_by_id(site_id)
    if not site:
        return False
    
    site_url = site['canonical_url']
    max_pages = site['max_pages']
    
    execute_db(
        "UPDATE sites SET status = 'active', error_count = 0 WHERE id = ?",
        (site_id,),
        commit=True
    )
    
    execute_db("DELETE FROM crawl_queue WHERE site_id = ?", (site_id,), commit=True)
    
    site_id_new, urls_added = phase_1_discovery(site_url, max_pages)
    
    return site_id_new is not None


# ============================================================================
# CRAWLING FUNCTIONS
# ============================================================================


def phase_1_discovery(site_url, max_pages=500):
    """Phase 1: Discovery - Build full URL queue"""
    site_url = normalize_domain(site_url)
    if not site_url:
        return None, 0
    
    site_id = add_site(site_url, max_pages)
    if not site_id:
        return None, 0
    
    execute_db("DELETE FROM crawl_queue WHERE site_id = ?", (site_id,), commit=True)
    
    discovered_items = discover_sitemaps_and_feeds(site_url, site_id)
    
    urls_to_add = []
    for item in discovered_items:
        url = item['url']
        if url:
            urls_to_add.append(url)
    
    if not urls_to_add:
        urls_to_add = [site_url]
    
    for url in urls_to_add:
        if not url:
            continue
        try:
            execute_db(
                "INSERT OR IGNORE INTO crawl_queue (site_id, url, status) VALUES (?, ?, 'pending')",
                (site_id, url),
                commit=True
            )
        except sqlite3.IntegrityError:
            pass
    
    execute_db(
        "UPDATE sites SET last_crawled = strftime('%s', 'now') WHERE id = ?",
        (site_id,),
        commit=True
    )
    
    return site_id, len(urls_to_add)


def discover_sitemaps_and_feeds(site_url, site_id):
    """Discover sitemaps and feeds"""
    parsed = urlparse(site_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    
    common_urls = [
        "/sitemap.xml", "/sitemap_index.xml", "/sitemap.xml.gz",
        "/feed", "/rss", "/atom.xml", "/feed.xml", "/rss.xml"
    ]
    
    discovered = []
    for path in common_urls:
        url = base_url + path
        discovered.append({'url': url, 'type': 'sitemap' if 'sitemap' in path else 'feed'})
    
    return discovered


# ============================================================================
# WORKER FUNCTION
# ============================================================================


def worker():
    """Background worker: Process pending URLs"""
    worker_name = "worker"
    print(f"✅ Worker {worker_name} spuštěn")
    
    while not SHUTDOWN_FLAG:
        try:
            results = execute_db_fetchall(
                "SELECT id, site_id, url FROM crawl_queue WHERE status = 'pending' AND retry_count < ? LIMIT 5",
                (MAX_RETRIES,)
            )
            
            if not results:
                time.sleep(5)
                continue
            
            batch = results
            
            for row_id, site_id, url in batch:
                execute_db(
                    "UPDATE crawl_queue SET status = 'locked', locked_by = ? WHERE id = ?",
                    (worker_name, row_id),
                    commit=True
                )
            
            for row_id, site_id, url in batch:
                if SHUTDOWN_FLAG:
                    break
                
                # Simulate crawling - store page in database
                try:
                    # Try to fetch the page
                    import urllib.request
                    from urllib.error import URLError, HTTPError
                    
                    try:
                        with urllib.request.urlopen(url, timeout=30) as response:
                            content = response.read().decode('utf-8', errors='ignore')
                            title = ""
                            if '<title>' in content:
                                title_start = content.find('<title>') + 7
                                title_end = content.find('</title>', title_start)
                                if title_end > title_start:
                                    title = content[title_start:title_end].strip()
                            
                            execute_db(
                                "INSERT INTO pages (site_id, url, title, content, indexed_at) VALUES (?, ?, ?, ?, strftime('%s', 'now'))",
                                (site_id, url, title, content),
                                commit=True
                            )
                            
                            execute_db(
                                "UPDATE crawl_queue SET status = 'completed' WHERE id = ?",
                                (row_id,),
                                commit=True
                            )
                    except (URLError, HTTPError, TimeoutError) as e:
                        retry_count = execute_db_fetchone("SELECT retry_count FROM crawl_queue WHERE id = ?", (row_id,))[0]
                        new_retry = retry_count + 1
                        
                        if new_retry >= MAX_RETRIES:
                            execute_db(
                                "UPDATE crawl_queue SET status = 'error', error_reason = ? WHERE id = ?",
                                (str(e), row_id),
                                commit=True
                            )
                        else:
                            execute_db(
                                "UPDATE crawl_queue SET status = 'pending', retry_count = ? WHERE id = ?",
                                (new_retry, row_id),
                                commit=True
                            )
                except Exception as e:
                    execute_db(
                        "UPDATE crawl_queue SET status = 'error', error_reason = ? WHERE id = ?",
                        (str(e), row_id),
                        commit=True
                    )
                
                time.sleep(0.5)
            
        except Exception as e:
            print(f"⚠️  Worker error: {e}")
            time.sleep(5)


def execute_db_fetchone(query, params=()):
    """Execute and fetch one result"""
    cursor = execute_db(query, params)
    return cursor.fetchone()


def execute_db_fetchall(query, params=()):
    """Execute and fetch all results"""
    cursor = execute_db(query, params)
    return cursor.fetchall()


def get_db_stats():
    """Get database statistics"""
    cursor = execute_db("SELECT COUNT(*) FROM sites")
    sites_count = cursor.fetchone()[0]
    
    cursor = execute_db("SELECT COUNT(*) FROM crawl_queue WHERE status = 'pending'")
    pending_count = cursor.fetchone()[0]
    
    cursor = execute_db("SELECT COUNT(*) FROM crawl_queue WHERE status = 'completed'")
    completed_count = cursor.fetchone()[0]
    
    cursor = execute_db("SELECT COUNT(*) FROM crawl_queue WHERE status = 'error'")
    errors_count = cursor.fetchone()[0]
    
    cursor = execute_db("SELECT COUNT(*) FROM pages")
    pages_count = cursor.fetchone()[0]
    
    return {
        'sites': sites_count,
        'pending': pending_count,
        'completed': completed_count,
        'errors': errors_count,
        'pages': pages_count
    }


def get_crawl_queue():
    """Get crawl queue items"""
    results = execute_db_fetchall("SELECT * FROM crawl_queue ORDER BY created_at DESC LIMIT 50")
    return [dict(row) for row in results]


# ============================================================================
# FLASK APP
# ============================================================================


app = Flask(__name__)
app.secret_key = 'mini-search-secret-key'

# HTML Templates
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="cs">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mini Search - Správcovská konzole</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
               background: #1a1a2e; color: #e0e0e0; line-height: 1.6; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { color: #0f3460; margin-bottom: 10px; font-size: 1.8em; }
        .card { background: #16213e; border-radius: 10px; padding: 20px; margin-bottom: 20px; }
        .card h3 { color: #0f3460; margin-bottom: 15px; }
        .btn { background: #e94560; color: white; border: none; padding: 10px 20px; 
               border-radius: 5px; cursor: pointer; font-size: 1em; }
        .btn:hover { background: #c81e45; }
        .btn-success { background: #4caf50; }
        .btn-success:hover { background: #388e3c; }
        .form-group { margin-bottom: 15px; }
        .form-group label { display: block; margin-bottom: 5px; color: #0f3460; }
        .form-group input { width: 100%; padding: 10px; border-radius: 5px; border: 1px solid #333; 
                           background: #1a1a2e; color: #e0e0e0; font-size: 1em; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #333; }
        th { background: #0f3460; color: white; }
        tr:hover { background: #1f2b4a; }
        .status-active { color: #4caf50; }
        .status-error { color: #e94560; }
        .status-pending { color: #ff9800; }
        .status-completed { color: #2196f3; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; }
        .stat-card { background: #16213e; padding: 15px; border-radius: 8px; text-align: center; }
        .stat-value { font-size: 2em; font-weight: bold; color: #e94560; }
        .stat-label { color: #0f3460; font-size: 0.9em; margin-top: 5px; }
        .nav { margin-bottom: 20px; }
        .nav a { color: #2196f3; margin-right: 20px; text-decoration: none; }
        .nav a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <div class="nav">
            <a href="/">🔍 Vyhledávání</a>
            <a href="/admin">🔧 Správa</a>
        </div>
        
        <h1>🔧 Správcovská konzole</h1>
        
        <div class="card">
            <h3>📊 Přehled</h3>
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-value">{{ stats.sites }}</div>
                    <div class="stat-label">Webové stránky</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{{ stats.pages }}</div>
                    <div class="stat-label">Indexované stránky</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{{ stats.pending }}</div>
                    <div class="stat-label">Čekající URL</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{{ stats.completed }}</div>
                    <div class="stat-label">Dokončené URL</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{{ stats.errors }}</div>
                    <div class="stat-label">Chyby</div>
                </div>
            </div>
        </div>
        
        <div class="card">
            <h3>➕ Přidat novou webovou stránku</h3>
            <form action="/admin/add" method="post">
                <div class="form-group">
                    <label>URL stránky:</label>
                    <input type="url" name="url" placeholder="https://priklad.cz" required>
                </div>
                <div class="form-group">
                    <label>Maximální počet stránek (default: 500):</label>
                    <input type="number" name="max_pages" placeholder="500" value="500" min="1" max="10000">
                </div>
                <button type="submit" class="btn btn-success">Přidat stránku</button>
            </form>
        </div>
        
        <div class="card">
            <h3>🌐 Seznam sledovaných stránek</h3>
            {% if sites %}
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>URL</th>
                        <th>Stav</th>
                        <th>Poslední crawl</th>
                        <th>Akce</th>
                    </tr>
                </thead>
                <tbody>
                    {% for site in sites %}
                    <tr>
                        <td>{{ site.id }}</td>
                        <td>{{ site.canonical_url }}</td>
                        <td class="status-{{ site.status }}">{{ site.status }}</td>
                        <td>{{ site.last_crawled_str }}</td>
                        <td>
                            <a href="/admin/recrawl/{{ site.id }}" class="btn" style="padding: 5px 10px; font-size: 0.9em; background: #ff9800;">Recrawl</a>
                            <a href="/admin/delete/{{ site.id }}" class="btn" style="padding: 5px 10px; font-size: 0.9em; background: #666;" onclick="return confirm('Opravdu smazat?')">Smazat</a>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <p style="color: #666;">Žádné stránky nepřidány. Přidejte první stránku výše.</p>
            {% endif %}
        </div>
        
        <div class="card">
            <h3>⏳ Fronta pro crawl</h3>
            {% if queue %}
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>URL</th>
                        <th>Stav</th>
                        <th>Počet pokusů</th>
                    </tr>
                </thead>
                <tbody>
                    {% for item in queue %}
                    <tr>
                        <td>{{ item.id }}</td>
                        <td>{{ item.url }}</td>
                        <td class="status-{{ item.status }}">{{ item.status }}</td>
                        <td>{{ item.retry_count }}</td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <p style="color: #666;">Fronta je prázdná.</p>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""

SEARCH_HTML = """
<!DOCTYPE html>
<html lang="cs">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mini Search - Vyhledávání</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
               background: #1a1a2e; color: #e0e0e0; line-height: 1.6; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { color: #0f3460; margin-bottom: 10px; font-size: 2em; }
        .card { background: #16213e; border-radius: 10px; padding: 20px; margin-bottom: 20px; }
        .search-box { display: flex; gap: 10px; margin-bottom: 20px; }
        .search-box input { flex: 1; padding: 15px; border-radius: 8px; border: 1px solid #333; 
                           background: #1a1a2e; color: #e0e0e0; font-size: 1.1em; }
        .search-box button { background: #e94560; color: white; border: none; padding: 15px 30px; 
                           border-radius: 8px; cursor: pointer; font-size: 1.1em; font-weight: bold; }
        .search-box button:hover { background: #c81e45; }
        .result-item { background: #16213e; border-radius: 8px; padding: 20px; margin-bottom: 15px;
                      border-left: 4px solid #e94560; }
        .result-item:hover { background: #1f2b4a; }
        .result-title { color: #e94560; font-size: 1.2em; margin-bottom: 10px; }
        .result-url { color: #2196f3; font-size: 0.9em; margin-bottom: 10px; word-break: break-all; }
        .result-snippet { color: #aaa; line-height: 1.6; }
        .result-meta { color: #666; font-size: 0.85em; margin-top: 10px; }
        .no-results { text-align: center; color: #666; padding: 40px; }
        .stats { color: #666; font-size: 0.9em; margin-top: 20px; }
        .nav { margin-bottom: 20px; }
        .nav a { color: #2196f3; margin-right: 20px; text-decoration: none; }
        .nav a:hover { text-decoration: underline; }
        .autocomplete-results { position: absolute; background: #16213e; border-radius: 8px; 
                               max-height: 300px; overflow-y: auto; z-index: 1000; 
                               width: calc(100% - 140px); box-shadow: 0 4px 20px rgba(0,0,0,0.3); }
        .autocomplete-item { padding: 12px 20px; cursor: pointer; }
        .autocomplete-item:hover { background: #1f2b4a; }
    </style>
</head>
<body>
    <div class="container">
        <div class="nav">
            <a href="/">🔍 Vyhledávání</a>
            <a href="/admin">🔧 Správa</a>
        </div>
        
        <h1>🔍 Mini Search</h1>
        
        <div class="card">
            <div style="position: relative;">
                <form id="searchForm" action="/" method="get">
                    <div class="search-box">
                        <input type="text" name="q" id="searchInput" placeholder="Zadejte vyhledávaný text..." 
                               value="{{ query }}" autocomplete="off" required>
                        <button type="submit">Vyhledat</button>
                    </div>
                </form>
                <div id="autocompleteResults" class="autocomplete-results" style="display: none;"></div>
            </div>
        </div>
        
        {% if query %}
        <div class="card">
            <h2>Výsledky pro: "{{ query }}"</h2>
            <div class="stats">Nalezeno: {{ total_results }} výsledků</div>
            
            {% if results %}
            <div id="resultsContainer">
                {% for result in results %}
                <div class="result-item">
                    <div class="result-title">{{ result.title or result.url }}</div>
                    <div class="result-url">{{ result.url }}</div>
                    <div class="result-snippet">{{ result.snippet or '...' }}</div>
                </div>
                {% endfor %}
            </div>
            {% else %}
            <div class="no-results">
                <p>🔍 Žádné výsledky nenalezeny</p>
                <p style="font-size: 0.9em; color: #666;">Zkuste jiné vyhledávací slovo nebo přidejte nové stránky přes správcovskou konzoli.</p>
            </div>
            {% endif %}
        </div>
        {% endif %}
    </div>
    
    <script>
        let autocompleteTimeout;
        
        document.getElementById('searchInput').addEventListener('input', function(e) {
            clearTimeout(autocompleteTimeout);
            const query = e.target.value.trim();
            if (query.length < 2) {
                document.getElementById('autocompleteResults').style.display = 'none';
                return;
            }
            
            autocompleteTimeout = setTimeout(() => {
                fetch('/autocomplete?q=' + encodeURIComponent(query))
                    .then(response => response.json())
                    .then(data => {
                        const resultsDiv = document.getElementById('autocompleteResults');
                        if (data.results && data.results.length > 0) {
                            resultsDiv.innerHTML = data.results.map(r => 
                                '<div class="autocomplete-item" onclick="selectSuggestion(\'' + r + '\')">' + r + '</div>'
                            ).join('');
                            resultsDiv.style.display = 'block';
                        } else {
                            resultsDiv.style.display = 'none';
                        }
                    })
                    .catch(() => {
                        document.getElementById('autocompleteResults').style.display = 'none';
                    });
            }, 300);
        });
        
        function selectSuggestion(text) {
            document.getElementById('searchInput').value = text;
            document.getElementById('autocompleteResults').style.display = 'none';
            document.getElementById('searchForm').submit();
        }
        
        document.addEventListener('click', function(e) {
            if (!e.target.closest('.search-box') && !e.target.closest('.autocomplete-results')) {
                document.getElementById('autocompleteResults').style.display = 'none';
            }
        });
    </script>
</body>
</html>
"""


# ============================================================================
# FLASK ROUTES
# ============================================================================


@app.route('/')
def search_index():
    """Main search page"""
    query = request.args.get('q', '').strip()
    
    if query:
        results = search(query)
        return render_template_string(
            SEARCH_HTML,
            query=query,
            results=results,
            total_results=len(results)
        )
    
    return render_template_string(SEARCH_HTML, query='', results=[], total_results=0)


@app.route('/autocomplete')
def autocomplete():
    """Autocomplete endpoint"""
    query = request.args.get('q', '').strip()
    
    if len(query) < 2:
        return jsonify({'results': []})
    
    try:
        search_term = f"%{query}%"
        results = execute_db_fetchall(
            "SELECT DISTINCT title FROM pages WHERE title LIKE ? LIMIT 10",
            (search_term,)
        )
        
        return jsonify({'results': [row[0] for row in results if row[0]]})
    except Exception as e:
        return jsonify({'results': [], 'error': str(e)})


@app.route('/admin')
def admin_index():
    """Admin dashboard"""
    stats = get_db_stats()
    sites = get_all_sites()
    queue = get_crawl_queue()
    
    return render_template_string(
        ADMIN_HTML,
        stats=stats,
        sites=sites,
        queue=queue
    )


@app.route('/admin/add', methods=['POST'])
def admin_add_site():
    """Add a new site"""
    url = request.form.get('url', '').strip()
    max_pages = int(request.form.get('max_pages', 500))
    
    if not url:
        return redirect('/admin?error=URL je povinná')
    
    site_id, urls_added = phase_1_discovery(url, max_pages)
    
    if site_id:
        return redirect(f'/admin?success=Stránka přidána! {urls_added} URL k prozkoumání')
    else:
        return redirect('/admin?error=Chyba při přidávání stránky')


@app.route('/admin/recrawl/<int:site_id>')
def admin_recrawl_site(site_id):
    """Recrawl a site"""
    recrawl_site(site_id)
    return redirect('/admin?success=Re-crawl zahájen')


@app.route('/admin/delete/<int:site_id>')
def admin_delete_site(site_id):
    """Delete a site"""
    delete_site(site_id)
    return redirect('/admin?success=Stránka smazána')


# ============================================================================
# SEARCH FUNCTION
# ============================================================================


def search(query, limit=50):
    """Search in indexed pages"""
    search_term = f"%{query}%"
    
    results = execute_db_fetchall(
        "SELECT id, site_id, url, title, content FROM pages WHERE title LIKE ? OR content LIKE ? LIMIT ?",
        (search_term, search_term, limit)
    )
    
    formatted = []
    for row in results:
        formatted.append({
            'id': row[0],
            'site_id': row[1],
            'url': row[2],
            'title': row[3],
            'content': row[4],
            'snippet': row[4][:200] + '...' if len(row[4]) > 200 else row[4]
        })
    
    return formatted


# ============================================================================
# SHUTDOWN HANDLING
# ============================================================================


def handle_shutdown(signum, frame):
    """Handle shutdown signals"""
    global SHUTDOWN_FLAG
    SHUTDOWN_FLAG = True
    print(f"\n⚠️  Signal {signum} received, shutting down...")
    close_db()
    sys.exit(0)


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


# ============================================================================
# MAIN
# ============================================================================


if __name__ == '__main__':
    print("=" * 70)
    print("Mini Search - Kombinovaná aplikace v5.0")
    print("=" * 70)
    print()
    print("Spouštím na http://0.0.0.0:8070")
    print("  / - Vyhledávání")
    print("  /admin - Správcovská konzole")
    print("Ctrl+C pro ukončení")
    print("=" * 70)
    print()
    
    # Initialize database
    init_db()
    print("✅ Databáze inicializována")
    
    # Start worker thread
    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()
    print("✅ Worker spuštěn")
    
    # Run Flask app
    app.run(host='0.0.0.0', port=8070, debug=False, threaded=True)
