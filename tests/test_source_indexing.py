#!/usr/bin/env python3
"""Regression tests: adding a source must start indexing, and the admin
dashboard must render real per-site numbers.

Serves a tiny site over loopback HTTP so discovery needs no external network.
"""
import functools
import http.server
import os
import re
import socketserver
import sys
import tempfile
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/sitemap-page-1</loc></url>
  <url><loc>{base}/sitemap-page-2</loc></url>
</urlset>"""

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>T</title><link>{base}/</link><description>d</description>
<item><title>A</title><link>{base}/feed-page-1</link></item>
</channel></rss>"""

HOME = '<html><head><title>Home</title></head><body><a href="/home-page">p</a></body></html>'

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name} {detail}")


class _Handler(http.server.BaseHTTPRequestHandler):
    base = ""

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/sitemap.xml":
            body, ctype = SITEMAP.format(base=self.base).encode(), "application/xml"
        elif self.path == "/feed.xml":
            body, ctype = FEED.format(base=self.base).encode(), "application/rss+xml"
        elif self.path == "/robots.txt":
            body, ctype = b"User-agent: *\nAllow: /\n", "text/plain"
        else:
            body, ctype = HOME.encode(), "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server():
    handler = functools.partial(_Handler)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    _Handler.base = base
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, base


def wait_for(predicate, timeout=25):
    """Poll until predicate() is truthy (background indexing is async)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return False


def main():
    httpd, base = start_server()

    tmpdir = tempfile.mkdtemp()
    os.chdir(tmpdir)

    import app_combined as m

    m.DB_PATH = os.path.join(tmpdir, "console.db")
    m._db_conn = None
    m.close_db()
    m.get_db()

    client = m.app.test_client()

    # ---------------------------------------------------------------
    # Bug 1: adding a source through the admin API must queue its URLs
    # ---------------------------------------------------------------
    resp = client.post("/admin/api/sources", json={
        "url": f"{base}/sitemap.xml", "source_type": "sitemap", "priority": 5,
    })
    check("POST sitemap source returns 201", resp.status_code == 201,
          f"{resp.status_code} {resp.get_data(as_text=True)[:200]}")
    payload = resp.get_json() or {}
    check("creation message mentions indexing",
          "index" in (payload.get("message") or "").lower(), str(payload))

    check("sitemap URLs got queued without a manual recrawl",
          wait_for(lambda: m.execute_db_fetchone(
              "SELECT COUNT(*) FROM crawl_queue WHERE url LIKE ?",
              (f"{base}/sitemap-page-%",))[0] == 2),
          "nothing queued for the sitemap source")

    # A URL source must queue exactly that URL.
    resp = client.post("/admin/api/sources", json={
        "url": f"{base}/home-page", "source_type": "url", "priority": 8,
    })
    check("POST url source returns 201", resp.status_code == 201, str(resp.status_code))
    check("single URL got queued",
          wait_for(lambda: m.execute_db_fetchone(
              "SELECT COUNT(*) FROM crawl_queue WHERE url = ?",
              (f"{base}/home-page",))[0] == 1),
          "url source did not queue its URL")

    # A feed source must queue the entries it advertises.
    resp = client.post("/admin/api/sources", json={
        "url": f"{base}/feed.xml", "source_type": "rss", "priority": 5,
    })
    check("POST rss source returns 201", resp.status_code == 201, str(resp.status_code))
    check("feed entries got queued",
          wait_for(lambda: m.execute_db_fetchone(
              "SELECT COUNT(*) FROM crawl_queue WHERE url = ?",
              (f"{base}/feed-page-1",))[0] == 1),
          "rss source did not queue its entries")

    check("index_source stamps last_checked",
          m.execute_db_fetchone(
              "SELECT COUNT(*) FROM site_sources WHERE last_checked > 0")[0] >= 3,
          "last_checked never updated")

    # Adding a bare domain should still discover links from the homepage.
    resp = client.post("/admin/api/sources", json={
        "url": base, "source_type": "domain", "max_pages": 40,
    })
    check("POST domain source returns 201", resp.status_code == 201, str(resp.status_code))
    check("domain discovery queues homepage links",
          wait_for(lambda: m.execute_db_fetchone(
              "SELECT COUNT(*) FROM crawl_queue WHERE url = ?",
              (f"{base}/home-page",))[0] >= 1),
          "domain source queued nothing")

    # index_source must respect a paused source.
    paused_id, err = m.add_source(1, f"{base}/paused-page", "url")
    check("paused-source fixture created", paused_id is not None, str(err))
    m.update_source(paused_id, status="paused")
    before = m.execute_db_fetchone(
        "SELECT COUNT(*) FROM crawl_queue WHERE url = ?", (f"{base}/paused-page",))[0]
    m.index_source(paused_id)
    after = m.execute_db_fetchone(
        "SELECT COUNT(*) FROM crawl_queue WHERE url = ?", (f"{base}/paused-page",))[0]
    check("paused source is not indexed", before == after == 0, f"{before} -> {after}")

    # ---------------------------------------------------------------
    # Bug 2: the dashboard must show real numbers, not blanks
    # ---------------------------------------------------------------

    # Background discovery may still be queueing; wait for it to settle so the
    # dashboard numbers we assert on are stable.
    def queue_total():
        return m.execute_db_fetchone("SELECT COUNT(*) FROM crawl_queue")[0]

    last, stable_since = queue_total(), time.time()
    while time.time() - stable_since < 2.0:
        time.sleep(0.25)
        current = queue_total()
        if current != last:
            last, stable_since = current, time.time()

    # Pin the queue to a known state, then read everything in one go.
    m.execute_db("DELETE FROM crawl_queue", commit=True)
    for i in range(3):
        m._queue_url(1, f"{base}/pending-{i}", 5)

    sites = m.get_all_sites()
    check("get_all_sites returns sites", len(sites) >= 1, str(len(sites)))
    check("site rows expose indexed", all("indexed" in s for s in sites))
    check("site rows expose pending", all("pending" in s for s in sites))
    check("site rows expose errors", all("errors" in s for s in sites))
    check("site rows expose sources", all("sources" in s for s in sites))
    check("site rows expose aliases_list", all("aliases_list" in s for s in sites))
    check("get_all_sites reports the queued pending count",
          sum(s["pending"] for s in sites) == 3,
          str([s["pending"] for s in sites]))

    stats = m.get_db_stats()
    check("global stats expose sources", "sources" in stats, str(stats))

    body = client.get("/admin").get_data(as_text=True)
    check("dashboard returns 200", "Globální přehled" in body)

    def stat_value(key):
        match = re.search(r'data-stat="%s">(\d+)<' % re.escape(key), body)
        return int(match.group(1)) if match else None

    check("dashboard renders site count", stat_value("sites") == len(sites),
          f"{stat_value('sites')} vs {len(sites)}")
    check("dashboard renders pending count", stat_value("pending") == 3,
          str(stat_value("pending")))
    check("dashboard renders sources count", stat_value("sources") == stats["sources"],
          f"{stat_value('sources')} vs {stats['sources']}")
    check("dashboard does not render a blank pending value",
          'data-stat="pending"></div>' not in body)

    # The per-site card must show that site's own aggregated numbers.
    card_html = body.split('class="card site-card"', 1)[-1]
    check("dashboard renders a site card", 'class="card site-card"' in body)
    check("dashboard site card shows pending count",
          f'class="stat-value">{sites[0]["pending"]}<' in card_html,
          f"expected pending={sites[0]['pending']}")
    check("dashboard site card shows source count",
          f'class="stat-value">{sites[0]["sources"]}<' in card_html,
          f"expected sources={sites[0]['sources']}")
    check("dashboard site card shows indexed count",
          f'class="stat-value">{sites[0]["indexed"]}<' in card_html,
          f"expected indexed={sites[0]['indexed']}")
    check("dashboard site card links to detail", "/admin/sites/" in body)

    httpd.shutdown()

    print()
    if failures:
        print(f"{len(failures)} TEST(S) FAILED: {failures}")
        raise SystemExit(1)
    print("ALL SOURCE INDEXING + DASHBOARD TESTS PASSED")


if __name__ == "__main__":
    main()