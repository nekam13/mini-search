#!/usr/bin/env python3
"""Deterministic tests for metadata extraction, retry/backoff and low-memory mode.

Network behaviour is exercised against a throwaway local HTTP server (threaded
``http.server``), so nothing here depends on the live internet.
"""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name} {detail}")


ARTICLE_HTML = """<!doctype html>
<html lang="cs"><head>
<title>Starý název</title>
<link rel="canonical" href="/clanek/praha?utm_source=news">
<meta property="og:title" content="Praha – město">
<meta property="og:description" content="Praha je hlavní město Česka.">
<meta property="og:image" content="/img/praha.jpg">
<meta property="og:type" content="article">
<meta property="og:site_name" content="Příklad">
<meta property="article:published_time" content="2024-05-06T07:08:09Z">
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {"@type": ["WebPage"], "name": "Praha"},
    {"@type": "NewsArticle",
     "headline": "Praha",
     "author": {"@type": "Person", "name": "Jan Novák"},
     "datePublished": "2024-05-06T07:08:09Z",
     "breadcrumb": {"@type": "BreadcrumbList",
       "itemListElement": [
         {"@type": "ListItem", "name": "Domov", "position": 1},
         {"@type": "ListItem", "name": "Články", "position": 2}
       ]}
    }
  ]
}
</script>
<script type="application/ld+json">
   { this is not valid json  </script>
</head><body>
<article>
  <h1>Praha</h1>
  <p>Praha je hlavní město Česka a leží na řece Vltavě.</p>
  <p>Praha je hlavní město Česka a leží na řece Vltavě.</p>
  <audio src="/audio/praha.mp3"></audio>
  <img src="/img/1.jpg" alt="obrázek">
</article>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    hits = 0
    flaky_remaining = 0

    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        Handler.hits += 1
        if self.path.startswith('/flaky'):
            if Handler.flaky_remaining > 0:
                Handler.flaky_remaining -= 1
                self._send(503, b"busy")
            else:
                self._send(200, b"ok")
        elif self.path.startswith('/ratelimit'):
            self._send(429, b"slow down", {'Retry-After': '1'})
        elif self.path.startswith('/article'):
            self._send(200, ARTICLE_HTML.encode('utf-8'),
                       {'Content-Type': 'text/html; charset=utf-8'})
        elif self.path.startswith('/encoded'):
            # windows-1250 Czech page: a naive utf-8 decode would mangle it.
            body = '<html><head><title>Káva</title></head><body><p>Žluťoučký</p></body></html>'.encode('windows-1250')
            self._send(200, body, {'Content-Type': 'text/html; charset=windows-1250'})
        elif self.path.startswith('/robots.txt'):
            self._send(200, b"User-agent: *\nDisallow: /private\nCrawl-delay: 1\n")
        else:
            self._send(404, b"nope")


def main():
    tmpdir = tempfile.mkdtemp()
    os.chdir(tmpdir)

    import app_combined as m

    m.DB_PATH = os.path.join(tmpdir, "console.db")
    m._db_conn = None
    m.close_db()
    m.get_db()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        site_id = m.add_site(base, 100)
        check("local test site created", site_id is not None, str(site_id))

        # --- metadata extraction -------------------------------------------
        page = m.extract_page_content(f"{base}/article", site_id)
        check("page extraction succeeds", page is not None)
        check("canonical strips tracking parameter",
              page['url'] == f"{base}/clanek/praha", page['url'])
        check("og:title preferred", page['og_title'] == "Praha – město", page['og_title'])
        check("og:description extracted",
              "hlavní město" in page['og_description'], page['og_description'])
        check("og:image absolutised",
              page['og_image'] == f"{base}/img/praha.jpg", page['og_image'])
        check("schema type from JSON-LD @type array",
              page['schema_type'] == 'Newsarticle', page['schema_type'])
        details = page['schema_details']
        meta = details.get('_meta', {})
        check("author from nested Person JSON-LD",
              meta.get('author') == "Jan Novák", str(meta.get('author')))
        check("breadcrumbs from BreadcrumbList",
              meta.get('breadcrumbs') == ["Domov", "Články"], str(meta.get('breadcrumbs')))
        check("published timestamp parsed",
              page['published_timestamp'] == 1714979289, str(page['published_timestamp']))
        check("invalid JSON-LD tolerated",
              page['og_title'] == "Praha – město")
        check("audio detected", page['has_audio'] == 1 and page['audio_url'].endswith('.mp3'))
        check("images absolutised",
              any(i['url'] == f"{base}/img/1.jpg" for i in page['images']),
              str(page['images'][:2]))
        # Duplicate paragraph must appear only once.
        check("duplicate paragraph de-duplicated",
              page['body_text'].count("Praha je hlavní město Česka a leží") == 1,
              page['body_text'][:200])

        # --- charset handling ----------------------------------------------
        w1250 = m.extract_page_content(f"{base}/encoded", site_id)
        check("windows-1250 decoded correctly",
              w1250 is not None and "Žluťoučký" in w1250['body_text'],
              str(w1250 and w1250['title']))

        # --- robots.txt + retry/backoff ------------------------------------
        rp = m._get_robots(base)
        check("robots.txt disallow honoured",
              not rp.can_fetch(m.USER_AGENT, f"{base}/private/x"))
        check("robots.txt allows public path",
              rp.can_fetch(m.USER_AGENT, f"{base}/article"))
        check("crawl-delay parsed", (rp.crawl_delay(m.USER_AGENT) or 0) == 1)

        Handler.flaky_remaining = 2
        Handler.hits = 0
        resp = m._http_get(f"{base}/flaky", timeout=5, max_retries=3, purpose='test')
        check("retry recovers after transient 503s", resp.status_code == 200,
              str(resp.status_code))
        check("retries actually happened", Handler.hits == 3, str(Handler.hits))

        Handler.hits = 0
        resp = m._http_get(f"{base}/ratelimit", timeout=5, max_retries=1, purpose='test')
        check("429 surfaced to caller for scheduling", resp.status_code == 429)

        # _set_queue_retry_or_error schedules backoff and eventually errors.
        queue_id = m.execute_db(
            """INSERT INTO crawl_queue (site_id, url, url_hash, status, priority,
               retry_count, scheduled_at, error_reason)
               VALUES (?, ?, ?, 'pending', 5, 0, 0, '')""",
            (site_id, f"{base}/x", m.url_hash(f"{base}/x")), commit=True
        ).lastrowid
        m._set_queue_retry_or_error(queue_id, "HTTP 503", retryable=True)
        row = m.execute_db_fetchone(
            "SELECT retry_count, status, scheduled_at FROM crawl_queue WHERE id=?",
            (queue_id,))
        check("retry increments retry_count", row[0] == 1, str(row))
        check("retry stays pending", row[1] == 'pending', str(row))
        check("retry scheduled in the future", row[2] > int(time.time()), str(row))

        m.MAX_RETRIES = 1
        m._set_queue_retry_or_error(queue_id, "HTTP 503", retryable=True)
        row = m.execute_db_fetchone("SELECT status FROM crawl_queue WHERE id=?", (queue_id,))
        check("retries cap out into error state", row[0] == 'error', str(row))

    finally:
        server.shutdown()
        server.server_close()

    # --- low-memory mode ---------------------------------------------------
    # Simulate the Termux default by disabling embeddings and rebuilding the
    # module-level state.
    m.ENABLE_EMBEDDINGS = False
    m._hnsw_index = None
    check("embeddings disabled reports False", m.embeddings_enabled() is False)
    check("vector index not built when disabled", m.get_hnsw_index() is None)
    check("embedding skipped when disabled",
          m._embedding_for({'title': 'x', 'og_description': '', 'body_text': ''}) is None)
    check("vector_search empty when disabled", m.vector_search("praha") == [])
    check("hybrid_search still works without embeddings",
          isinstance(m.hybrid_search("praha"), list))

    m.ENABLE_EMBEDDINGS = True
    check("embeddings re-enabled for the default profile", m.embeddings_enabled() is True)

    print()
    if failures:
        print(f"{len(failures)} TEST(S) FAILED: {failures}")
        raise SystemExit(1)
    print("ALL CRAWL METADATA TESTS PASSED")


if __name__ == "__main__":
    main()
