"""Smoke test: index a real page served from a local HTTP server."""
import functools
import http.server
from http.client import RemoteDisconnected
import os
import socketserver
import sys
import tempfile
import threading
import time

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

# Retryable network failures should stay pending (up to MAX_RETRIES) with detail.
flaky_url = f"http://127.0.0.1:{port}/flaky"
m._queue_url(site_id, flaky_url)
flaky_row = m.execute_db_fetchone(
    "SELECT id FROM crawl_queue WHERE url = ?", (flaky_url,))
assert flaky_row, "flaky URL was not queued"

orig_http_get = m._http_get
try:
    def _raise_retryable(target_url, **kwargs):
        raise m.FetchError(
            target_url,
            "queue-fetch: RemoteDisconnected: Remote end closed connection without response",
            retryable=True,
            attempts=1,
            exc=RemoteDisconnected("Remote end closed connection without response"),
        )
    m._http_get = _raise_retryable
    m.process_url(flaky_row["id"], site_id, flaky_url)
finally:
    m._http_get = orig_http_get

flaky_status = m.execute_db_fetchone(
    "SELECT status, retry_count, error_reason, scheduled_at FROM crawl_queue WHERE id = ?",
    (flaky_row["id"],))
assert flaky_status["status"] == "pending"
assert flaky_status["retry_count"] == 1
assert "RemoteDisconnected" in flaky_status["error_reason"]
assert flaky_status["scheduled_at"] >= int(time.time())

# Non-retryable fetch errors should become permanent queue errors immediately.
bad_url = f"http://127.0.0.1:{port}/bad"
m._queue_url(site_id, bad_url)
bad_row = m.execute_db_fetchone(
    "SELECT id FROM crawl_queue WHERE url = ?", (bad_url,))
assert bad_row, "bad URL was not queued"

try:
    def _raise_non_retryable(target_url, **kwargs):
        raise m.FetchError(target_url, "Unsupported URL scheme: ftp", retryable=False, attempts=1)
    m._http_get = _raise_non_retryable
    m.process_url(bad_row["id"], site_id, bad_url)
finally:
    m._http_get = orig_http_get

bad_status = m.execute_db_fetchone(
    "SELECT status, retry_count, error_reason FROM crawl_queue WHERE id = ?",
    (bad_row["id"],))
assert bad_status["status"] == "error"
assert bad_status["retry_count"] == 0
assert "Unsupported URL scheme" in bad_status["error_reason"]

srv.shutdown()
print("\nLOCAL INDEXING SMOKE TEST PASSED")