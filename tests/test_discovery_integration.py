#!/usr/bin/env python3
"""Integration test: manual sitemap/feed/url sources drive discovery and schedulers.

Serves a tiny site over loopback HTTP so no external network access is needed.
"""
import functools
import http.server
import os
import socketserver
import sys
import tempfile
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/from-sitemap-1</loc></url>
  <url><loc>{base}/from-sitemap-2</loc></url>
</urlset>"""

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>T</title><link>{base}/</link><description>d</description>
<item><title>A</title><link>{base}/from-feed-1</link></item>
<item><title>B</title><link>{base}/from-feed-2</link></item>
</channel></rss>"""

HOME = '<html><head><title>Home</title></head><body><a href="/page">p</a></body></html>'

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
            body = SITEMAP.format(base=self.base).encode()
            ctype = "application/xml"
        elif self.path == "/feed.xml":
            body = FEED.format(base=self.base).encode()
            ctype = "application/rss+xml"
        elif self.path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            ctype = "text/plain"
        else:
            body = HOME.encode()
            ctype = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    tmpdir = tempfile.mkdtemp()
    os.chdir(tmpdir)

    with socketserver.TCPServer(("127.0.0.1", 0), _Handler) as httpd:
        port = httpd.server_address[1]
        base = f"http://127.0.0.1:{port}"
        _Handler.base = base
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        import app_combined as m

        m.DB_PATH = os.path.join(tmpdir, "console.db")
        m._db_conn = None
        m.close_db()
        m.get_db()

        site_id = m.add_site(f"{base}/", 50)
        sm_id, err = m.add_source(site_id, f"{base}/sitemap.xml", "sitemap", priority=8)
        check("register sitemap source", sm_id is not None and err is None, str(err))
        rss_id, err = m.add_source(site_id, f"{base}/feed.xml", "rss", priority=6)
        check("register rss source", rss_id is not None and err is None, str(err))
        url_id, err = m.add_source(site_id, f"{base}/manual-page", "url", priority=9)
        check("register url source", url_id is not None and err is None, str(err))

        legacy = m.execute_db_fetchall(
            "SELECT url, type FROM sitemaps_feeds WHERE site_id = ?", (site_id,))
        check("legacy mirror for schedulers", len(legacy) == 2, str(legacy))

        m.execute_db("DELETE FROM crawl_queue", commit=True)
        m.phase_1_discovery(f"{base}/", 50)
        urls = [r[0] for r in m.execute_db_fetchall(
            "SELECT url FROM crawl_queue WHERE site_id = ?", (site_id,))]
        check("discovery queued sitemap urls", any('from-sitemap' in u for u in urls), str(urls))
        check("discovery queued feed urls", any('from-feed' in u for u in urls), str(urls))
        check("discovery queued manual url", any('manual-page' in u for u in urls), str(urls))

        checked = m.execute_db_fetchall(
            "SELECT last_checked FROM site_sources WHERE id IN (?, ?)", (sm_id, rss_id))
        check("last_checked updated", all(r[0] > 0 for r in checked), str(checked))

        m.execute_db("DELETE FROM crawl_queue", commit=True)
        m.check_sitemaps()
        m.check_feeds()
        urls = [r[0] for r in m.execute_db_fetchall(
            "SELECT url FROM crawl_queue WHERE site_id = ?", (site_id,))]
        check("scheduler queued sitemap urls", any('from-sitemap' in u for u in urls), str(urls))
        check("scheduler queued feed urls", any('from-feed' in u for u in urls), str(urls))

        # Deleting a non-domain source must not remove the site
        ok, msg = m.delete_source(sm_id)
        check("delete sitemap source", ok, msg)
        check("site survived source delete", m.get_site_by_id(site_id) is not None)
        legacy_after = m.execute_db_fetchall(
            "SELECT url FROM sitemaps_feeds WHERE site_id = ?", (site_id,))
        check("legacy mirror cleaned up",
              all('sitemap.xml' not in r[0] for r in legacy_after), str(legacy_after))

        httpd.shutdown()

    print()
    if failures:
        print(f"{len(failures)} TEST(S) FAILED: {failures}")
        raise SystemExit(1)
    print("ALL DISCOVERY INTEGRATION TESTS PASSED")


if __name__ == "__main__":
    main()
