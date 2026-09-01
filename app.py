#!/usr/bin/env python3
"""
Mini Search - Správcovská konzole v4.0
Zjednodušená verze pro maximální spolehlivost
"""

import sqlite3
import time
import json
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, jsonify
import crawler_engine
import threading
import signal
import sys
import os

app = Flask(__name__)
app.secret_key = 'mini-search-secret-key'

# HTML Template pro správcovskou konzoli
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="cs">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mini Search - Správcovská konzole</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1a2e; color: #e0e0e0; line-height: 1.6; padding: 20px;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { color: #0f3460; margin-bottom: 20px; font-size: 1.8em; }
        h2 { color: #e94560; margin: 20px 0 10px; font-size: 1.3em; }
        .card { background: #16213e; border-radius: 10px; padding: 20px; margin-bottom: 20px; }
        .card h3 { color: #0f3460; margin-bottom: 15px; }
        .btn { 
            background: #e94560; color: white; border: none; padding: 10px 20px; 
            border-radius: 5px; cursor: pointer; font-size: 1em; 
            transition: background 0.3s;
        }
        .btn:hover { background: #c81e45; }
        .btn-success { background: #4caf50; }
        .btn-success:hover { background: #388e3c; }
        .btn-warning { background: #ff9800; }
        .btn-warning:hover { background: #e68a00; }
        .form-group { margin-bottom: 15px; }
        .form-group label { display: block; margin-bottom: 5px; color: #0f3460; }
        .form-group input, .form-group select { 
            width: 100%; padding: 10px; border-radius: 5px; border: 1px solid #333; 
            background: #1a1a2e; color: #e0e0e0; font-size: 1em;
        }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #333; }
        th { background: #0f3460; color: white; }
        tr:hover { background: #1f2b4a; }
        .status-active { color: #4caf50; }
        .status-error { color: #e94560; }
        .status-pending { color: #ff9800; }
        .status-completed { color: #2196f3; }
        .status-locked { color: #9c27b0; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; }
        .stat-card { background: #16213e; padding: 15px; border-radius: 8px; text-align: center; }
        .stat-value { font-size: 2em; font-weight: bold; color: #e94560; }
        .stat-label { color: #0f3460; font-size: 0.9em; margin-top: 5px; }
        .alert { padding: 15px; border-radius: 5px; margin-bottom: 20px; }
        .alert-warning { background: #ff980020; border-left: 4px solid #ff9800; color: #ff9800; }
        .alert-success { background: #4caf5020; border-left: 4px solid #4caf50; color: #4caf50; }
        .alert-info { background: #2196f320; border-left: 4px solid #2196f3; color: #2196f3; }
        .header-info { font-size: 0.9em; color: #666; margin-top: 5px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔧 Mini Search - Správcovská konzole</h1>
        <div class="header-info">
            Vector backend: {{ vector_backend }} | 
            Databáze: SQLite (WAL mode) | 
            Port: 8070
        </div>
        
        {% if message %}
        <div class="alert alert-{{ message_type }}">{{ message }}</div>
        {% endif %}
        
        <!-- Přehled -->
        <div class="card">
            <h3>📊 Přehled</h3>
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-value">{{ stats.sites }}</div>
                    <div class="stat-label">Webové stránky</div>
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
        
        <!-- Přidat novou stránku -->
        <div class="card">
            <h3>➕ Přidat novou webovou stránku</h3>
            <form action="/add" method="post">
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
        
        <!-- Seznam stránek -->
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
                            <a href="/recrawl/{{ site.id }}" class="btn btn-warning" style="padding: 5px 10px; font-size: 0.9em;">Recrawl</a>
                            <a href="/delete/{{ site.id }}" class="btn" style="padding: 5px 10px; font-size: 0.9em; background: #666;" onclick="return confirm('Opravdu smazat?')">Smazat</a>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <p style="color: #666;">Žádné stránky nepřidány. Přidejte první stránku výše.</p>
            {% endif %}
        </div>
        
        <!-- Fronta pro crawl -->
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
        
        <div class="card" style="text-align: center; color: #666;">
            <p>Mini Search v4.0 | Port 8070 | <a href="http://localhost:8095" style="color: #e94560;">Přejít na vyhledávání →</a></p>
        </div>
    </div>
</body>
</html>
"""


def get_db_stats():
    """Get database statistics"""
    conn = crawler_engine.get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM sites")
    sites_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE status = 'pending'")
    pending_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE status = 'completed'")
    completed_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE status = 'error'")
    errors_count = cursor.fetchone()[0]
    
    return {
        'sites': sites_count,
        'pending': pending_count,
        'completed': completed_count,
        'errors': errors_count
    }


def get_all_sites():
    """Get all sites with formatted dates"""
    sites = crawler_engine.get_all_sites()
    for site in sites:
        if site['last_crawled'] > 0:
            site['last_crawled_str'] = datetime.fromtimestamp(site['last_crawled']).strftime('%Y-%m-%d %H:%M:%S')
        else:
            site['last_crawled_str'] = 'Nikdy'
    return sites


def get_crawl_queue():
    """Get crawl queue items"""
    conn = crawler_engine.get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM crawl_queue ORDER BY created_at DESC LIMIT 50")
    return [dict(row) for row in cursor.fetchall()]


@app.route('/')
def index():
    """Main admin page"""
    try:
        stats = get_db_stats()
        sites = get_all_sites()
        queue = get_crawl_queue()
        
        return render_template_string(
            ADMIN_HTML,
            stats=stats,
            sites=sites,
            queue=queue,
            vector_backend=crawler_engine.VECTOR_BACKEND_TYPE,
            message=request.args.get('message'),
            message_type=request.args.get('type', 'info')
        )
    except Exception as e:
        return f"<h1>Chyba</h1><p>{str(e)}</p>", 500


@app.route('/add', methods=['POST'])
def add_site():
    """Add a new site"""
    try:
        url = request.form.get('url', '').strip()
        max_pages = int(request.form.get('max_pages', 500))
        
        if not url:
            return redirect('/?message=URL je povinná&type=warning')
        
        # Add site and start discovery
        site_id, urls_added = crawler_engine.phase_1_discovery(url, max_pages)
        
        if site_id:
            return redirect(f'/?message=Stránka přidána! {urls_added} URL k prozkoumání&type=success')
        else:
            return redirect('/?message=Chyba při přidávání stránky&type=error')
    except Exception as e:
        return redirect(f'/?message=Chyba: {str(e)}&type=error')


@app.route('/recrawl/<int:site_id>')
def recrawl_site(site_id):
    """Recrawl a site"""
    try:
        crawler_engine.recrawl_site(site_id)
        return redirect('/?message=Re-crawl zahájen&type=success')
    except Exception as e:
        return redirect(f'/?message=Chyba při re-crawlu: {str(e)}&type=error')


@app.route('/delete/<int:site_id>')
def delete_site(site_id):
    """Delete a site"""
    try:
        conn = crawler_engine.get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sites WHERE id = ?", (site_id,))
        conn.commit()
        return redirect('/?message=Stránka smazána&type=success')
    except Exception as e:
        return redirect(f'/?message=Chyba při mazání: {str(e)}&type=error')


@app.route('/api/stats')
def api_stats():
    """API endpoint for statistics"""
    try:
        stats = get_db_stats()
        return jsonify(stats)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/sites')
def api_sites():
    """API endpoint for sites"""
    try:
        sites = get_all_sites()
        return jsonify(sites)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    print("=" * 70)
    print("Mini Search - Správcovská konzole v4.0")
    print("=" * 70)
    print(f"Vector backend: {crawler_engine.VECTOR_BACKEND_TYPE}")
    print()
    print("Spouštím na http://0.0.0.0:8070")
    print("Ctrl+C pro ukončení")
    print("=" * 70)
    
    app.run(host='0.0.0.0', port=8070, debug=False, threaded=True)
