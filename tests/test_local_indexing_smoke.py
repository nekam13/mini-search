"""Smoke test: index a real page served from a local HTTP server."""
import functools
import http.server
import os
import socketserver
import sys
import tempfile
import threading

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

tmpdir = tempfile.mkdtemp()
os.chdir(tmpdir)

HTML = b"""<html><head><title>Lokalni test</title>
<meta name="description" content="test"></head>
<body><h1>Lokalni test</h1><p>Obsah lokalni stranky v siti.</p></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/robots.txt":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"User-agent: *\nDisallow: /\n")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, *a):
        pass


srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

import app_combined as m  # noqa: E402

m.DB_PATH = os.path.join(tmpdir, "smoke.db")
m._db_conn = None
m.close_db()
m.get_db()

url = f"http://127.0.0.1:{port}/"
site_id = m.add_site(url, 10)
print(f"site_id={site_id} is_local={m.get_site_by_id(site_id)['is_local']}")

# Queue it the same way a real crawl would, then process one item synchronously.
m._queue_url(site_id, url)
row = m.execute_db_fetchone(
    "SELECT id, priority FROM crawl_queue WHERE url = ?", (url,))
assert row, "URL was not queued"
print(f"queued with priority {row['priority']} (expected {m.LOCAL_QUEUE_PRIORITY})")
assert row["priority"] == m.LOCAL_QUEUE_PRIORITY, "local URL not queued as urgent"

m.process_url(row["id"], site_id, url)

rows = m.execute_db_fetchall("SELECT url, title FROM pages WHERE site_id = ?", (site_id,))
print(f"indexed pages: {[dict(r) for r in rows]}")

assert rows, "no page indexed from the local server"
assert any(r["title"] == "Lokalni test" for r in rows), "title not extracted"
assert m.execute_db_fetchone(
    "SELECT status FROM crawl_queue WHERE id = ?", (row["id"],))["status"] == "completed"

res = m.hybrid_search("lokalni", 5)
print(f"search hits: {len(res)}")
assert res, "local page not returned by search"

srv.shutdown()
print("\nLOCAL INDEXING SMOKE TEST PASSED")