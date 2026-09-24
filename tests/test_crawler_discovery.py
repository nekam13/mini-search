#!/usr/bin/env python3
"""Deterministic tests for crawler link discovery and sitemap parsing.

Everything runs against a throwaway loopback HTTP server, so nothing here
depends on the live internet.
"""
import gzip
import os
import sys
import tempfile
import threading
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


SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{base}/child-sitemap.xml</loc></sitemap>
</sitemapindex>"""

CHILD_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>  {base}/sitemap-page-1  </loc></url>
  <url><loc>{base}/sitemap-page-2</loc></url>
  <url><loc>{base}/sitemap-page-1</loc></url>
</urlset>"""

BAD_SITEMAP = b"this is definitely not xml at all <<<>>>"

HOME = """<!doctype html>
<html><head><title>Home</title>
<link rel="canonical" href="/">
</head><body>
  <a href="/article/1">Article one</a>
  <a href="/article/2">Article two</a>
  <a href="article/3#fragment">Article three</a>
  <a href="mailto:a@b.cz">Mail</a>
  <a href="javascript:void(0)">JS</a>
  <a href="/img/logo.png">Logo</a>
  <a href="https://other.example.com/x">External</a>
  <a href="/article/1">Duplicate</a>
</body></html>"""

ARTICLE = """<html><head><title>A1</title></head><body>
  <article><h1>A1</h1><p>Prvni clanek o Praze.</p>
  <a href="/article/2">Next</a></article>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    base = ""

    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path == "/sitemap-index.xml":
            self._send(200, SITEMAP_INDEX.format(base=Handler.base),
                       {"Content-Type": "application/xml"})
        elif self.path == "/child-sitemap.xml":
            self._send(200, CHILD_SITEMAP.format(base=Handler.base),
                       {"Content-Type": "application/xml"})
        elif self.path == "/sitemap-gz.xml":
            payload = gzip.compress(CHILD_SITEMAP.format(base=Handler.base).encode())
            # Deliberately wrong Content-Type: the magic bytes must win.
            self._send(200, payload, {"Content-Type": "text/plain"})
        elif self.path == "/sitemap-bad.xml":
            self._send(200, BAD_SITEMAP, {"Content-Type": "application/xml"})
        elif self.path == "/robots.txt":
            body = f"User-agent: *\nAllow: /\nSitemap: {Handler.base}/sitemap-index.xml\n"
            self._send(200, body, {"Content-Type": "text/plain"})
        elif self.path == "/article/1":
            self._send(200, ARTICLE, {"Content-Type": "text/html; charset=utf-8"})
        else:
            self._send(200, HOME, {"Content-Type": "text/html; charset=utf-8"})


def main():
    tmpdir = tempfile.mkdtemp()
    os.chdir(tmpdir)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    Handler.base = base
    threading.Thread(target=server.serve_forever, daemon=True).start()

    import app_combined as m

    m.DB_PATH = os.path.join(tmpdir, "console.db")
    m._db_conn = None
    m.close_db()
    m.get_db()

    try:
        # --- link extraction (pure function) --------------------------------
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(HOME.format(base=base), "lxml")
        links = m.extract_links_from_soup(soup, f"{base}/")
        check("same-domain links extracted",
              f"{base}/article/1" in links and f"{base}/article/2" in links, str(links))
        check("relative link resolved", f"{base}/article/3" in links, str(links))
        check("fragment stripped from link",
              all("#" not in link for link in links), str(links))
        check("mailto dropped", not any("mailto" in link for link in links))
        check("javascript dropped", not any("javascript" in link for link in links))
        check("image asset dropped", not any(link.endswith(".png") for link in links), str(links))
        check("external domain dropped",
              not any("other.example.com" in link for link in links), str(links))
        check("duplicate link collapsed",
              links.count(f"{base}/article/1") == 1, str(links))

        # --- per-page link discovery across the crawl ------------------------
        site_id = m.add_site(f"{base}/", 50)
        home_queue = m.execute_db(
            """INSERT INTO crawl_queue (site_id, url, url_hash, status, priority,
               retry_count, scheduled_at, error_reason)
               VALUES (?, ?, ?, 'pending', 5, 0, 0, '')""",
            (site_id, f"{base}/", m.url_hash(f"{base}/")), commit=True
        ).lastrowid
        m.process_url(home_queue, site_id, f"{base}/")
        queued = [r[0] for r in m.execute_db_fetchall(
            "SELECT url FROM crawl_queue WHERE site_id=? AND status='pending'", (site_id,))]
        check("process_url follows links from the page",
              f"{base}/article/1" in queued, str(queued))
        check("link discovery drops non-page assets",
              all(not u.endswith(".png") for u in queued), str(queued))

        # max_pages budget is not exceeded by link discovery. A fresh site that
        # has indexed nothing starts with its full budget; the already-crawled
        # site has less.
        small_site = m.add_site("https://budget.example.com/", 7)
        budget = m._site_remaining_budget(small_site)
        check("budget starts at max_pages", budget == 7, str(budget))
        used = m._site_remaining_budget(site_id)
        check("budget shrinks after indexing/crawling", used < 50, str(used))

        # --- sitemap parsing -------------------------------------------------
        sm = m.parse_sitemap(f"{base}/sitemap-index.xml")
        check("sitemapindex recursed into child",
              any("sitemap-page-1" in u for u in sm), str(sm))
        check("child sitemap loc whitespace stripped",
              all(u == u.strip() for u in sm), str(sm))
        check("duplicate sitemap loc collapsed",
              len([u for u in sm if "sitemap-page-1" in u]) == 1, str(sm))

        gz = m.parse_sitemap(f"{base}/sitemap-gz.xml")
        check("gzip sitemap decompressed by magic bytes",
              len(gz) >= 2, str(gz))

        bad = m.parse_sitemap(f"{base}/sitemap-bad.xml")
        check("invalid sitemap tolerated", bad == [], str(bad))

        # --- discovery source dedup -----------------------------------------
        dedup_site = m.add_site(f"{base}/", 50)
        discovered = m.discover_sitemaps_and_feeds(f"{base}/", dedup_site, 50)
        types = sorted({d['type'] for d in discovered})
        check("discovery finds a sitemap", 'sitemap' in types, str(discovered))
        urls = [d['url'] for d in discovered]
        check("identical discovery bodies collapsed", len(urls) == len(set(urls)), str(urls))

        # --- embedding fallback does not produce zero vectors ---------------
        m.EMBEDDINGS_PREF = '1'
        emb = m._HashingEmbedding().encode("Praha je hlavni mesto")
        check("fallback embedding is not all zeros",
              float((emb != 0).sum()) > 0, str(emb[:5]))
        check("fallback embedding is normalised",
              abs(float((emb * emb).sum()) - 1.0) < 1e-4)
        # Diacritics fold, so a diacritic query and its plain form match.
        a = m._HashingEmbedding().encode("Praha")
        b = m._HashingEmbedding().encode("praha")
        check("fallback embedding folds diacritics/case",
              float((a * b).sum()) > 0.99, str(float((a * b).sum())))

        # --- lowmem scope: only vectors go away, crawling stays -------------
        # This is the regression the app was reporting: lowmem must not break
        # sitemap parsing or link discovery, it only drops the AI stack.
        m.EMBEDDINGS_PREF = '0'
        m.LOW_MEMORY_MODE = True
        m._hnsw_index = None
        check("lowmem disables embeddings", m.embeddings_enabled() is False)
        check("lowmem leaves the vector index unbuilt", m.get_hnsw_index() is None)
        lowmem_sm = m.parse_sitemap(f"{base}/sitemap-index.xml")
        check("lowmem still parses sitemaps",
              any("sitemap-page-1" in u for u in lowmem_sm), str(lowmem_sm))
        lowmem_links = m.extract_links_from_soup(
            BeautifulSoup(HOME.format(base=base), "lxml"), f"{base}/")
        check("lowmem still discovers links",
              f"{base}/article/1" in lowmem_links, str(lowmem_links))
        m.EMBEDDINGS_PREF = '1'
    finally:
        server.shutdown()
        server.server_close()

    print()
    if failures:
        print(f"{len(failures)} TEST(S) FAILED: {failures}")
        raise SystemExit(1)
    print("ALL CRAWLER DISCOVERY TESTS PASSED")


if __name__ == "__main__":
    main()
