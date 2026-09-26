#!/usr/bin/env python3
"""Deterministic tests for the admin settings panel, the images filter and the
wiki-image plumbing.

No live internet: the settings tests run against the real Flask app with a
throwaway SQLite file, and the wiki tests use a loopback MediaWiki mock and a
bz2 dump written to a temp directory.
"""
import bz2
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name} {detail}")


class _MockWikiAPI(BaseHTTPRequestHandler):
    """MediaWiki action API mock that also answers ``pageimages``."""

    PAGES = {
        "Praha": ("Praha je hlavní město Česka.",
                  "https://upload.wikimedia.org/praha-lead.jpg"),
        "Brno": ("Brno je druhé největší město v Česku.", ""),
        "Ostrava": ("Ostrava leží na severu Moravy.",
                    "https://upload.wikimedia.org/ostrava-lead.jpg"),
    }

    def log_message(self, *args):
        pass

    def _json(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        if query.get("list") == ["allpages"]:
            self._json({"query": {"allpages": [
                {"title": title} for title in sorted(self.PAGES)]}})
            return
        titles = (query.get("titles", [""])[0]).split("|")
        pages = {}
        for index, title in enumerate(titles):
            if title not in self.PAGES:
                continue
            extract, image = self.PAGES[title]
            entry = {
                "title": title,
                "extract": extract,
                "fullurl": f"https://cs.wikipedia.org/wiki/{title}",
            }
            if image and query.get("piprop") == ["original"]:
                entry["original"] = {"source": image}
            pages[str(index)] = entry
        self._json({"query": {"pages": pages}})


def _start_mock_wiki_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockWikiAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def make_dump(path, pages):
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/">'
           + "".join(pages) + '</mediawiki>')
    with bz2.open(path, "wb") as handle:
        handle.write(xml.encode("utf-8"))


def page(title, text="", ns="0"):
    return (f'<page><title>{title}</title><ns>{ns}</ns>'
            f'<revision><text>{text}</text></revision></page>')


def test_settings(m, tmpdir):
    """Settings persist, apply to the live globals and validate input."""
    import app_combined as app

    check("settings table exists", bool(m.execute_db_fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='app_settings'")))

    ok, errors = m.save_settings({'search.vector_weight': '55'})
    check("save_settings accepts a valid value", ok, str(errors))
    check("setting applied to the live global", m.VECTOR_WEIGHT == 55,
          str(m.VECTOR_WEIGHT))
    stored = m.execute_db_fetchone(
        "SELECT value FROM app_settings WHERE key='search.vector_weight'")
    check("setting persisted", stored and stored["value"] == "55", str(stored))

    # Out-of-range input is clamped, not rejected outright.
    ok, _ = m.save_settings({'search.vector_weight': '9999'})
    check("out-of-range int clamped to max", ok and m.VECTOR_WEIGHT == 100,
          str(m.VECTOR_WEIGHT))

    # A bool round-trips through the DB as 0/1.
    ok, _ = m.save_settings({'maintenance.nightly': True})
    check("bool setting saved", ok and m.MAINTENANCE_NIGHTLY_ENABLED is True,
          str(m.MAINTENANCE_NIGHTLY_ENABLED))

    # An unknown key is reported, not silently ignored.
    ok, errors = m.save_settings({'nonexistent.key': '1'})
    check("unknown setting rejected", not ok and errors, str(errors))

    # An invalid choice is rejected.
    ok, errors = m.save_settings({'embeddings.pref': 'maybe'})
    check("invalid choice rejected", not ok, str(errors))

    # The UI view is grouped and display-ready.
    groups = m.settings_for_ui()
    keys = {item['key'] for group in groups for item in group['items']}
    check("ui exposes the search weight", 'search.vector_weight' in keys)
    check("ui exposes the wiki language", 'wiki.lang' in keys)
    check("ui carries help text and env name",
          all(item['help'] and 'env' in item for group in groups
              for item in group['items']))

    # Reset drops every override and restores the env-seeded defaults.
    m.reset_settings()
    check("reset clears stored rows",
          m.execute_db_fetchone("SELECT COUNT(*) FROM app_settings")[0] == 0)
    check("reset restores default global",
          m.VECTOR_WEIGHT == m.SETTINGS_SPEC['search.vector_weight']['default'],
          str(m.VECTOR_WEIGHT))

    # A saved value survives a fresh connection and is re-applied on startup.
    m.save_settings({'search.fts_weight': '42'})
    m.close_db()
    m.DB_PATH = os.path.join(tmpdir, "console-restart.db")
    m._db_conn = None
    m.get_db()
    check("stored setting re-applied on startup", m.FTS_WEIGHT == 42,
          str(m.FTS_WEIGHT))
    m.reset_settings()


def test_settings_routes(m, tmpdir):
    """The admin settings page and JSON API work end to end."""
    import app_combined as app

    app.DB_PATH = os.path.join(tmpdir, "console-web.db")
    app._db_conn = None
    app.close_db()
    app.get_db()
    client = app.app.test_client()

    resp = client.get("/admin/settings")
    body = resp.get_data(as_text=True)
    check("settings page returns 200", resp.status_code == 200, str(resp.status_code))
    check("settings page renders groups", "Vyhledávání" in body and "Wikipedie" in body)
    check("settings page has the save button", "Uložit nastavení" in body)
    check("nav shows the settings link", "/admin/settings" in body)

    resp = client.post("/admin/settings",
                       data={'setting__search.vector_weight': '33'},
                       follow_redirects=False)
    check("form POST redirects", resp.status_code in (302, 303), str(resp.status_code))
    check("form value applied", app.VECTOR_WEIGHT == 33, str(app.VECTOR_WEIGHT))

    resp = client.get("/admin/api/settings")
    payload = resp.get_json()
    check("api returns groups", resp.status_code == 200 and payload.get("groups"))
    flat = {i['key']: i for g in payload['groups'] for i in g['items']}
    check("api reflects the saved value", flat['search.vector_weight']['value'] == 33,
          str(flat.get('search.vector_weight')))

    resp = client.post("/admin/api/settings", json={"settings": {"search.seo_weight": 7}})
    check("api POST saves", resp.status_code == 200 and app.SEO_WEIGHT == 7,
          str(app.SEO_WEIGHT))

    resp = client.post("/admin/api/settings", json={"settings": {"bogus": 1}})
    check("api POST rejects unknown key", resp.status_code == 400, str(resp.status_code))

    resp = client.post("/admin/api/settings", json={"action": "reset"})
    check("api reset works", resp.status_code == 200
          and app.execute_db_fetchone("SELECT COUNT(*) FROM app_settings")[0] == 0)
    app.reset_settings()


def test_wiki_images(m, tmpdir):
    """Wiki importers store images for the separate grid, never as pages."""
    import app_combined as app

    app.DB_PATH = os.path.join(tmpdir, "console-wiki.db")
    app._db_conn = None
    app.close_db()
    app.get_db()

    # --- dump importer: [[File:...]] becomes an image URL -------------------
    dump = os.path.join(tmpdir, "cswiki-latest-pages-articles-multistream.xml.bz2")
    make_dump(dump, [
        page("Praha", "Praha je město. [[Soubor:Prague_Castle.jpg|thumb|Hrad]] "
                      "a [[File:Old_Town.jpg|náhled]] a [[Soubor:Wiki.png]]."),
        page("Brno", "Brno bez obrázku."),
    ])
    sid, err = app.add_source(0, "file://" + dump, "wiki")
    check("wiki source created", sid is not None, str(err))
    app.run_wiki_import(app.get_source_by_id(sid), max_pages=10)

    row = app.execute_db_fetchone(
        "SELECT og_image, images FROM pages WHERE title = ?", ("Praha",))
    images = json.loads(row["images"] or "[]")
    urls = [i["url"] for i in images]
    check("dump importer stores images", len(urls) >= 2, str(urls))
    check("image URL built from the file name",
          all("Special:FilePath" in u for u in urls), str(urls))
    check("placeholder Wiki.png skipped",
          all("Wiki.png" not in u for u in urls), str(urls))
    check("og_image filled from the first image",
          row["og_image"] == urls[0], str(row["og_image"]))

    # Images are metadata only: no page row is created per image.
    page_count = app.execute_db_fetchone("SELECT COUNT(*) FROM pages")[0]
    check("images are not separate pages", page_count == 2, str(page_count))

    # --- image cap keeps storage flat --------------------------------------
    many = os.path.join(tmpdir, "cswiki-many.xml.bz2")
    files = " ".join(f"[[Soubor:File{i}.jpg]]" for i in range(10))
    make_dump(many, [page("Hodně", "Text. " + files)])
    sid2, _ = app.add_source(0, "file://" + many, "wiki")
    app.run_wiki_import(app.get_source_by_id(sid2), max_pages=10)
    row2 = app.execute_db_fetchone(
        "SELECT images FROM pages WHERE title = ?", ("Hodně",))
    check("images per article capped",
          len(json.loads(row2["images"])) == app.WIKI_MAX_IMAGES_PER_ARTICLE,
          str(row2["images"]))

    # --- API importer: pageimages becomes an image URL ---------------------
    app.close_db()
    app.DB_PATH = os.path.join(tmpdir, "console-wiki-api.db")
    app._db_conn = None
    app.get_db()

    server = _start_mock_wiki_api()
    port = server.server_address[1]
    original = app.WIKI_API_BASE
    app.WIKI_API_BASE = f"http://127.0.0.1:{port}"
    try:
        sid3, _ = app.add_source(0, "wiki:cs", "wiki")
        app.run_wiki_import(app.get_source_by_id(sid3), max_pages=10)
        row3 = app.execute_db_fetchone(
            "SELECT og_image, images FROM pages WHERE title = ?", ("Praha",))
        check("api importer stores the lead image",
              row3["og_image"] == "https://upload.wikimedia.org/praha-lead.jpg",
              str(row3["og_image"]))
        row4 = app.execute_db_fetchone(
            "SELECT images FROM pages WHERE title = ?", ("Brno",))
        check("article without an image stores none",
              not json.loads(row4["images"] or "[]"), str(row4["images"]))
    finally:
        app.WIKI_API_BASE = original
        server.shutdown()
        server.server_close()


def test_images_filter(m, tmpdir):
    """The Obrázky filter returns images as the primary result."""
    import app_combined as app

    app.DB_PATH = os.path.join(tmpdir, "console-filter.db")
    app._db_conn = None
    app.close_db()
    conn = app.get_db()
    conn.execute("INSERT INTO sites (canonical_url, status) VALUES (?,?)",
                 ("cz.example.com", "active"))
    site_id = conn.execute("SELECT id FROM sites").fetchone()["id"]
    conn.execute(
        """INSERT INTO pages (site_id, url, url_hash, title, og_title, body_text,
           og_image, images, seo_score, indexed_at)
           VALUES (?,?,?,?,?,?,?,?,?,strftime('%s','now'))""",
        (site_id, "https://cz.example.com/praha", "h1", "Praha", "Praha",
         "Praha je hlavní město Česka.", "https://cz.example.com/praha.jpg",
         json.dumps([{"url": "https://cz.example.com/hrad.jpg"}]), 10))
    conn.commit()

    client = app.app.test_client()
    resp = client.get("/?q=Praha&filter=images")
    body = resp.get_data(as_text=True)
    check("images filter returns 200", resp.status_code == 200, str(resp.status_code))
    check("images filter shows the image section", 'class="image-results"' in body)
    check("images filter heading is image-specific", "Obrázky pro" in body)
    check("images filter renders both images",
          "praha.jpg" in body and "hrad.jpg" in body)
    check("images filter hides article cards", 'id="resultList"' not in body)

    # The default view keeps the article list and the image section together.
    resp2 = client.get("/?q=Praha")
    body2 = resp2.get_data(as_text=True)
    check("default view keeps article list", 'id="resultList"' in body2)
    check("default view still shows images", 'class="image-results"' in body2)
    check("images filter button rendered", 'data-filter="images"' in body2)

    # A hostile og:image must not become a javascript: src even in this mode.
    conn.execute(
        """INSERT INTO pages (site_id, url, url_hash, title, og_title, body_text,
           og_image, images, seo_score, indexed_at)
           VALUES (?,?,?,?,?,?,?,?,?,strftime('%s','now'))""",
        (site_id, "https://cz.example.com/xss", "h2", "XSS", "XSS", "Praha",
         "javascript:alert(1)", json.dumps([{"url": "data:text/html,x"}]), 10))
    conn.commit()
    body3 = client.get("/?q=Praha&filter=images").get_data(as_text=True)
    check("no javascript: image src", 'src="javascript:' not in body3)
    check("no data: image src", 'src="data:' not in body3)


def main():
    tmpdir = tempfile.mkdtemp()
    os.chdir(tmpdir)

    import app_combined as m

    m.DB_PATH = os.path.join(tmpdir, "console.db")
    m._db_conn = None
    m.close_db()
    m.get_db()

    test_settings(m, tmpdir)
    test_settings_routes(m, tmpdir)
    test_wiki_images(m, tmpdir)
    test_images_filter(m, tmpdir)

    print()
    if failures:
        print(f"{len(failures)} TEST(S) FAILED: {failures}")
        raise SystemExit(1)
    print("ALL SETTINGS/IMAGES TESTS PASSED")


if __name__ == "__main__":
    main()
