import sqlite3
import time
from flask import Flask, render_template_string, request, redirect
from apscheduler.schedulers.background import BackgroundScheduler
import crawler_engine

app = Flask(__name__)
crawler_engine.init_db()

def auto_recrawl():
    conn = crawler_engine.get_db()
    cur = conn.cursor()
    cur.execute("SELECT url, max_pages FROM sites")
    sites = cur.fetchall()
    conn.close()
    
    for site_url, max_pages in sites:
        print(f"[Autopilot] Re-crawling webu: {site_url}")
        crawler_engine.crawl_site(site_url, max_pages)

scheduler = BackgroundScheduler()
scheduler.add_job(func=auto_recrawl, trigger="interval", hours=12)
scheduler.start()

HTML_CONSOLE = '''
<!DOCTYPE html>
<html lang="cs">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mini Search Console</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f8f9fa; margin: 0; padding: 20px; color: #202124; }
        .container { max-width: 900px; margin: 0 auto; }
        .card { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.12); margin-bottom: 20px; }
        h1, h2 { font-weight: 400; margin-top: 0; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { text-align: left; padding: 12px; border-bottom: 1px solid #e0e0e0; }
        th { background: #f1f3f4; }
        input[type="text"], input[type="number"] { padding: 8px; border: 1px solid #dadce0; border-radius: 4px; margin-right: 10px; }
        .btn { background: #1a73e8; color: white; border: none; padding: 9px 16px; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; }
        .btn:hover { background: #1557b0; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔍 Mini Search Console</h1>
        
        <div class="card">
            <h2>Přidat nový web / Sitemap / Feed</h2>
            <form action="/add" method="post" style="display:flex; gap:10px; flex-wrap:wrap;">
                <input type="text" name="url" placeholder="https://example.com/sitemap.xml" style="flex:2;" required>
                <input type="number" name="max_pages" value="500" style="width:100px;" required>
                <button type="submit" class="btn">Přidat k indexaci</button>
            </form>
        </div>

        <div class="card">
            <h2>Sledované domény & Sitemapy</h2>
            <table>
                <tr>
                    <th>URL Web / Sitemap</th>
                    <th>Max Stran</th>
                    <th>Naposledy prověřeno</th>
                    <th>Akce</th>
                </tr>
                {% for site in sites %}
                <tr>
                    <td><b>{{ site[1] }}</b></td>
                    <td>{{ site[2] }}</td>
                    <td>{{ site[4] }}</td>
                    <td><a href="/crawl/{{ site[0] }}" class="btn" style="padding:4px 8px; font-size:0.8rem;">Re-crawl</a></td>
                </tr>
                {% endfor %}
            </table>
        </div>

        <div class="card" style="text-align:center;">
            <a href="http://localhost:7700" target="_blank" class="btn" style="background:#34a853;">🌐 Otevřít vyhledávání (Meilisearch)</a>
        </div>
    </div>
</body>
</html>
'''

@app.route('/')
def index():
    conn = crawler_engine.get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, url, max_pages, interval_hours, last_crawled FROM sites")
    raw_sites = cur.fetchall()
    conn.close()
    
    sites = []
    for s in raw_sites:
        last = time.strftime('%Y-%m-%d %H:%M', time.localtime(s[4])) if s[4] > 0 else "Čeká na spuštění"
        sites.append((s[0], s[1], s[2], s[3], last))
        
    return render_template_string(HTML_CONSOLE, sites=sites)

@app.route('/add', methods=['POST'])
def add():
    url = request.form['url']
    max_pages = int(request.form['max_pages'])
    scheduler.add_job(func=crawler_engine.crawl_site, args=[url, max_pages])
    return redirect('/')

@app.route('/crawl/<int:site_id>')
def crawl_now(site_id):
    conn = crawler_engine.get_db()
    cur = conn.cursor()
    cur.execute("SELECT url, max_pages FROM sites WHERE id = ?", (site_id,))
    site = cur.fetchone()
    conn.close()
    if site:
        scheduler.add_job(func=crawler_engine.crawl_site, args=[site[0], site[1]])
    return redirect('/')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
