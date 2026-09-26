#!/usr/bin/env python3
"""Tests for the Czech search-quality pass: charset decoding, asset filtering,
search weights, recrawl config and the separate image results section.

All network behaviour is exercised against a throwaway loopback server, so
nothing here touches the live internet and every assertion is deterministic.
"""
import json
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


# --- pages served by the loopback server ------------------------------------
# UTF-8 body sent with a bare ``text/html`` header. requests would call this
# ISO-8859-1 and mojibake every háček; the decoder must not.
UTF8_NO_CHARSET = (
    '<html><head><title>RS-Kouzelné recepty</title></head>'
    '<body><p>Pečeme s láskou a Žluťoučký kůň.</p></body></html>'
).encode('utf-8')

# windows-1250 page that declares its charset only in a meta tag.
W1250_META = (
    '<html><head><meta http-equiv="Content-Type" content="text/html; charset=windows-1250">'
    '<title>Káva</title></head><body><p>Žluťoučký</p></body></html>'
).encode('windows-1250')

# UTF-8 page with the charset declared only via <meta charset>.
UTF8_META = (
    '<html><head><meta charset="utf-8"><title>Praha</title></head>'
    '<body><p>Hlavní město Česka.</p></body></html>'
).encode('utf-8')

SITEMAP_WITH_IMAGES = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">
  <url>
    <loc>{base}/clanek/1</loc>
    <image:image><image:loc>{base}/img/hero.jpg</image:loc></image:image>
  </url>
  <url>
    <loc>{base}/clanek/2</loc>
    <image:image><image:loc>{base}/img/druhy.png</image:loc></image:image>
  </url>
</urlset>"""

SITEMAP_PLAIN = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/clanek/1</loc></url>
  <url><loc>{base}/clanek/2</loc></url>
</urlset>"""


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
        if self.path == "/utf8-no-charset":
            self._send(200, UTF8_NO_CHARSET, {"Content-Type": "text/html"})
        elif self.path == "/w1250-meta":
            self._send(200, W1250_META, {"Content-Type": "text/html"})
        elif self.path == "/utf8-meta":
            self._send(200, UTF8_META, {"Content-Type": "text/html"})
        elif self.path == "/sitemap-images.xml":
            self._send(200, SITEMAP_WITH_IMAGES.format(base=Handler.base),
                       {"Content-Type": "application/xml"})
        elif self.path == "/sitemap-plain.xml":
            self._send(200, SITEMAP_PLAIN.format(base=Handler.base),
                       {"Content-Type": "application/xml"})
        elif self.path == "/feed.xml":
            self._send(200, FEED, {"Content-Type": "application/rss+xml"})
        else:
            self._send(404, b"nope")


FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test</title>
  <item><title>Clanek</title><link>{base}/clanek/1</link></item>
  <item><title>Obrazek</title><link>{base}/img/enclosure.jpg</link></item>
</channel></rss>"""


def _seed_pages(m, site_id):
    conn = m.get_db()
    rows = [
        # Two article pages carrying images, plus a Czech body for FTS.
        ("https://cz.example.com/praha", "Praha", "Praha – hlavní město Česka",
         "Praha je hlavní město Česka a leží na Vltavě.",
         "https://cz.example.com/img/praha.jpg",
         json.dumps([{"url": "https://cz.example.com/img/praha-2.jpg", "alt": "Praha"}]),
         90),
        ("https://cz.example.com/brno", "Brno", "Brno – město v Česku",
         "Brno je druhé největší město Česka.",
         "https://cz.example.com/img/brno.jpg", "[]", 70),
    ]
    for url, title, og_title, body, og_image, images, seo in rows:
        conn.execute(
            """INSERT INTO pages (site_id, url, url_hash, title, og_title, og_description,
               body_text, og_image, images, schema_type, seo_score, indexed_at)
               VALUES (?,?,?,?,?,?,?,?,?,'Article',?,strftime('%s','now'))""",
            (site_id, url, m.url_hash(url), title, og_title, body, body,
             og_image, images, seo))
    conn.commit()


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
        # ---- charset decoding (Czech mojibake regression) ------------------
        r = m._http_get(f"{base}/utf8-no-charset", timeout=5)
        _, text = m._decode_response(r)
        check("UTF-8 without charset header decoded as UTF-8",
              "RS-Kouzelné recepty" in text, text[20:60])
        check("UTF-8 háčky not mojibaked",
              "KouzelnÃ" not in text, text[20:60])

        r = m._http_get(f"{base}/utf8-meta", timeout=5)
        _, text = m._decode_response(r)
        check("meta charset=utf-8 honoured", "Hlavní město Česka" in text, text[:120])

        r = m._http_get(f"{base}/w1250-meta", timeout=5)
        _, text = m._decode_response(r)
        check("windows-1250 from meta tag decoded", "Žluťoučký" in text, text[:120])

        # A real extraction must carry the correct Czech title all the way.
        site_id = m.add_site(f"{base}/", 50)
        page = m.extract_page_content(f"{base}/utf8-no-charset", site_id)
        check("extracted title keeps Czech diacritics",
              page is not None and page['title'] == "RS-Kouzelné recepty",
              str(page and page['title']))

        # ---- sitemap image:loc must never become a page --------------------
        locs, _ = m._parse_sitemap_locs(SITEMAP_WITH_IMAGES.format(base=base).encode())
        check("sitemap image:loc excluded from locs",
              all('.jpg' not in u and '.png' not in u for u in locs), str(locs))
        check("sitemap page locs preserved",
              f"{base}/clanek/1" in locs and f"{base}/clanek/2" in locs, str(locs))

        parsed = m.parse_sitemap(f"{base}/sitemap-images.xml")
        check("parse_sitemap drops image assets",
              all(not u.endswith(('.jpg', '.png')) for u in parsed), str(parsed))
        check("parse_sitemap keeps real pages",
              len(parsed) == 2, str(parsed))

        plain = m.parse_sitemap(f"{base}/sitemap-plain.xml")
        check("plain sitemap unaffected", len(plain) == 2, str(plain))

        # ---- asset URLs never enter the crawl queue ------------------------
        check("_looks_like_asset_url flags jpg",
              m._looks_like_asset_url(f"{base}/img/x.jpg") is True)
        check("_looks_like_asset_url flags mp3",
              m._looks_like_asset_url(f"{base}/a/x.mp3") is True)
        check("_looks_like_asset_url ignores html",
              m._looks_like_asset_url(f"{base}/clanek/1") is False)
        check("_looks_like_asset_url ignores query-only path",
              m._looks_like_asset_url(f"{base}/img?id=5") is False)

        check("_queue_url rejects an image URL",
              m._queue_url(site_id, f"{base}/img/hero.jpg", 5) is False)
        queued = [row[0] for row in m.execute_db_fetchall(
            "SELECT url FROM crawl_queue WHERE site_id=?", (site_id,))]
        check("image URL absent from crawl_queue",
              all('.jpg' not in u for u in queued), str(queued))

        # A feed whose enclosure points at an image must not queue it either.
        for new_url in m.parse_feed(f"{base}/feed.xml"):
            m._queue_url(site_id, m.normalize_url(new_url), 5)
        queued = [row[0] for row in m.execute_db_fetchall(
            "SELECT url FROM crawl_queue WHERE site_id=?", (site_id,))]
        check("feed image enclosure not queued",
              all(not u.endswith('.jpg') for u in queued), str(queued))
        check("feed article still queued",
              any(u.endswith('/clanek/1') for u in queued), str(queued))

        # ---- hybrid search weights ----------------------------------------
        check("weights default to AI 50 / text 40 / SEO 10",
              (m.VECTOR_WEIGHT, m.FTS_WEIGHT, m.SEO_WEIGHT) == (50, 40, 10),
              f"{m.VECTOR_WEIGHT}/{m.FTS_WEIGHT}/{m.SEO_WEIGHT}")
        check("weights sum to 100",
              m.VECTOR_WEIGHT + m.FTS_WEIGHT + m.SEO_WEIGHT == 100)

        # SEO contributes exactly its share of the score. The vector and FTS
        # scorers are stubbed so the arithmetic is exact and independent of
        # whether embeddings happen to be available in this environment.
        _seed_pages(m, site_id)
        praha_row = m.execute_db_fetchone(
            "SELECT * FROM pages WHERE url LIKE '%/praha'")
        original_vector, original_fts = m.vector_search, m.fts_search
        try:
            m.vector_search = lambda q, limit=25, filter_type=None: [dict(praha_row)]
            m.fts_search = lambda q, limit=25: [dict(praha_row)]
            results = m.hybrid_search("Praha", limit=10)
        finally:
            m.vector_search, m.fts_search = original_vector, original_fts

        praha = next(r for r in results if r['url'].endswith('/praha'))
        multiplier = m._site_priority_multiplier(praha['site_id'])
        # A single hit ranks first (i=0), so the rank factor is 1 - 0/2 = 1:
        # vector -> 50, FTS -> 40, SEO 90 -> 90 * 0.10 = 9. Sum 99, times the
        # local 3x boost.
        expected = (m.VECTOR_WEIGHT + m.FTS_WEIGHT
                    + praha['seo_score'] * (m.SEO_WEIGHT / 100.0)) * multiplier
        check("SEO weight applies at exactly 10%",
              abs(praha['relevance'] - expected) < 0.05,
              f"{praha['relevance']} vs {expected}")
        check("hybrid_search finds the Czech page",
              any(r['url'].endswith('/praha') for r in results), str([r['url'] for r in results]))

        # Differential check: dropping SEO to 0 must remove exactly the SEO
        # share, which pins the weight without relying on relative ordering.
        try:
            m.SEO_WEIGHT = 0
            m.vector_search = lambda q, limit=25, filter_type=None: [dict(praha_row)]
            m.fts_search = lambda q, limit=25: [dict(praha_row)]
            without_seo = m.hybrid_search("Praha", limit=10)[0]['relevance']
        finally:
            m.SEO_WEIGHT = 10
            m.vector_search, m.fts_search = original_vector, original_fts
        delta = praha['relevance'] - without_seo
        check("removing SEO drops exactly its 10% share",
              abs(delta - praha['seo_score'] * 0.10 * multiplier) < 0.05,
              f"delta {delta}")

        # ---- recrawl interval configuration --------------------------------
        check("recrawl intervals exposed as config",
              all(hasattr(m, name) for name in (
                  'RECRAWL_SITEMAP_HOURS', 'RECRAWL_FEED_HOURS',
                  'RECRAWL_SITE_HOURS', 'RECRAWL_PAGE_STALE_HOURS')))
        check("stale seconds derived from hours config",
              m.RECRAWL_STALE_AFTER_SECONDS == m.RECRAWL_PAGE_STALE_HOURS * 3600,
              str(m.RECRAWL_STALE_AFTER_SECONDS))
        check("feed recrawl defaults sooner than full site",
              m.RECRAWL_FEED_HOURS < m.RECRAWL_SITE_HOURS,
              f"{m.RECRAWL_FEED_HOURS} vs {m.RECRAWL_SITE_HOURS}")

        # ---- image results section -----------------------------------------
        client = m.app.test_client()
        resp = client.get("/?q=Praha")
        body = resp.get_data(as_text=True)
        check("GET /?q= renders 200", resp.status_code == 200, str(resp.status_code))
        check("image section rendered", 'class="image-results"' in body)
        check("image grid rendered", 'class="image-grid"' in body)
        check("og:image appears in image section",
              "https://cz.example.com/img/praha.jpg" in body)
        check("second stored image appears",
              "https://cz.example.com/img/praha-2.jpg" in body)
        check("image section labelled in Czech", "Obrázky" in body)
        check("image section notes non-indexing",
              "neindexují jako samostatné stránky" in body)

        # Images must not leak into the article result list as their own hits.
        article_hits = [r['url'] for r in m.hybrid_search("Praha", limit=20)]
        check("no image URL is a search result",
              all(not u.endswith(('.jpg', '.png')) for u in article_hits), str(article_hits))

        # ---- _collect_result_images is display-only and deduplicated --------
        rows = m.execute_db_fetchall("SELECT * FROM pages")
        imgs = m._collect_result_images(rows)
        urls = [i['url'] for i in imgs]
        check("collector deduplicates image URLs", len(urls) == len(set(urls)), str(urls))
        check("collector carries source page url",
              all(i['source_url'] for i in imgs), str(imgs))
        check("collector carries a title",
              all(i['title'] for i in imgs), str(imgs))
        check("collector honours limit",
              len(m._collect_result_images(rows, limit=1)) == 1)
        check("collector disabled at limit 0",
              m._collect_result_images(rows, limit=0) == [])

        # XSS: a hostile og:image must not become a javascript: src.
        conn = m.get_db()
        conn.execute(
            """INSERT INTO pages (site_id, url, url_hash, title, og_title, body_text,
               og_image, images, seo_score, indexed_at)
               VALUES (?,?,?,?,?,?,?,?,?,strftime('%s','now'))""",
            (site_id, "https://cz.example.com/xss", "x", "XSS", "XSS",
             "Praha", "javascript:alert(1)", json.dumps([{"url": "data:text/html,x"}]), 10))
        conn.commit()
        rows = m.execute_db_fetchall("SELECT * FROM pages")
        imgs = m._collect_result_images(rows)
        check("javascript: image URL rejected",
              all(not i['url'].lower().startswith('javascript:') for i in imgs), str(imgs))
        check("data: image URL rejected",
              all(not i['url'].lower().startswith('data:') for i in imgs), str(imgs))

        resp = client.get("/?q=Praha")
        body = resp.get_data(as_text=True)
        check("no javascript: src in rendered page",
              'src="javascript:' not in body)
        check("no data: src in rendered page", 'src="data:' not in body)

    finally:
        server.shutdown()
        server.server_close()

    print()
    if failures:
        print(f"{len(failures)} TEST(S) FAILED: {failures}")
        raise SystemExit(1)
    print("ALL SEARCH QUALITY TESTS PASSED")


if __name__ == "__main__":
    main()
