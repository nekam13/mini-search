#!/usr/bin/env python3
"""Deterministic tests for the streaming Czech-Wikipedia importer.

No live Wikipedia access: a small bz2 multistream-style dump is written to a
temp directory and imported through the real ``run_wiki_import`` pipeline.
Covers wikitext stripping, redirect/namespace filtering, deduplication,
idempotency, max_pages and pause/resume.
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


class _MockWikiAPI(BaseHTTPRequestHandler):
    """Minimal stand-in for the MediaWiki action API used by the importer."""

    PAGES = {
        "Praha": "Praha je hlavní město Česka.",
        "Brno": "Brno je druhé největší město v Česku.",
        "Ostrava": "Ostrava leží na severu Moravy.",
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
            if title in self.PAGES:
                pages[str(index)] = {
                    "title": title,
                    "extract": self.PAGES[title],
                    "fullurl": f"https://cs.wikipedia.org/wiki/{title}",
                }
        self._json({"query": {"pages": pages}})


def _start_mock_wiki_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockWikiAPI)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def check(name, condition, detail=""):
    if condition:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name} {detail}")


def make_dump(path, pages):
    body = "".join(pages)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/">'
           f'{body}</mediawiki>')
    with bz2.open(path, "wb") as handle:
        handle.write(xml.encode("utf-8"))


def page(title, text="", ns="0", redirect=None, timestamp=None):
    redirect_attr = f'<redirect title="{redirect}" />' if redirect else ''
    ts = f'<timestamp>{timestamp}</timestamp>' if timestamp else ''
    if redirect:
        return f'<page><title>{title}</title><ns>{ns}</ns>{redirect_attr}</page>'
    return (f'<page><title>{title}</title><ns>{ns}</ns><revision>{ts}'
            f'<text>{text}</text></revision></page>')


def main():
    tmpdir = tempfile.mkdtemp()
    os.chdir(tmpdir)

    import app_combined as m

    m.DB_PATH = os.path.join(tmpdir, "console.db")
    m._db_conn = None
    m.close_db()
    m.get_db()

    # --- language / source normalization (pure, no network) -----------------
    check("wiki:cs shorthand resolves to cs wiki",
          m.normalize_wiki_source("wiki:cs")[0] == "https://cs.wikipedia.org")
    check("bare language code resolves to cs wiki",
          m.normalize_wiki_source("cs")[0] == "https://cs.wikipedia.org")
    check("cs.wikipedia.org resolves to cs wiki",
          m.normalize_wiki_source("https://cs.wikipedia.org/wiki/Praha")[0]
          == "https://cs.wikipedia.org")
    check("bare URL detects wiki source type",
          m.detect_source_type("https://cs.wikipedia.org") == "wiki")
    check("default importer is api (no local storage)",
          m.normalize_wiki_source("cs")[1] == "api")
    check("file:// dump implies dump importer",
          m.normalize_wiki_source("file:///tmp/cswiki-x.xml.bz2")[1] == "dump")
    check("file:// dump infers language from filename",
          m.lang_from_wiki_url("/tmp/cswiki-latest-pages-articles.xml.bz2") == "cs")

    # --- dump fixture -------------------------------------------------------
    dump_path = os.path.join(
        tmpdir, "cswiki-latest-pages-articles-multistream.xml.bz2")
    make_dump(dump_path, [
        page("Praha", "Praha je hlavní město [[Česko|Česka]]. Má {{infobox}} "
                       "mnoho památek a <ref>zdroj</ref>.",
             timestamp="2023-01-02T03:04:05Z"),
        page("Pražský hrad", redirect="Praha"),
        page("Kategorie:Města", "Seznam měst", ns="14"),
        page("Brno", "Brno je druhé největší město v [[Česko|Česku]].",
             timestamp="2024-05-06T07:08:09Z"),
    ])

    source_id, err = m.add_source(0, "file://" + dump_path, "wiki")
    check("wiki source created", source_id is not None, str(err))
    source = m.get_source_by_id(source_id)
    check("wiki source uses dump importer", source["importer"] == "dump",
          str(source.get("importer")))
    check("wiki source attaches to the cs wikipedia site",
          source["url"].startswith("file://"))
    site = m.get_site_by_id(source["site_id"])
    check("wiki site canonical url", site["canonical_url"] == "cs.wikipedia.org",
          str(site["canonical_url"]))

    imported = m.run_wiki_import(source, max_pages=50)
    check("two encyclopaedic articles imported", imported == 2, str(imported))

    rows = m.execute_db_fetchall(
        "SELECT url, title, body_text, schema_type, published_timestamp "
        "FROM pages ORDER BY title")
    by_title = {r["title"]: dict(r) for r in rows}
    check("Praha indexed with wiki URL",
          by_title["Praha"]["url"] == "https://cs.wikipedia.org/wiki/Praha",
          by_title["Praha"]["url"])
    check("category page skipped", "Kategorie:Města" not in by_title,
          str(list(by_title)))
    check("redirect itself is not a page", "Pražský hrad" not in by_title)
    aliases = json.loads(m.get_site_by_id(site["id"])["aliases"] or "[]")
    check("redirect becomes a site alias",
          any(a.endswith("/Praha") for a in aliases), str(aliases))

    praha = by_title.get("Praha")
    check("wikitext templates stripped",
          "{{infobox}}" not in praha["body_text"], praha["body_text"])
    check("wikitext link label kept", "Česka" in praha["body_text"],
          praha["body_text"])
    check("ref tag stripped", "<ref>" not in praha["body_text"],
          praha["body_text"])
    check("title prepended for relevance",
          praha["body_text"].startswith("Praha."), praha["body_text"])
    check("published date parsed",
          praha["published_timestamp"] == 1672628645, str(praha["published_timestamp"]))
    check("wiki pages typed as Article", praha["schema_type"] == "Article",
          praha["schema_type"])

    # --- deduplication / idempotency ---------------------------------------
    check("re-import is idempotent",
          m.run_wiki_import(m.get_source_by_id(source_id), max_pages=50) == 0)
    check("still exactly two pages",
          m.execute_db_fetchone("SELECT COUNT(*) FROM pages")[0] == 2)
    check("url_hash unique across pages",
          m.execute_db_fetchone("SELECT COUNT(DISTINCT url_hash) FROM pages")[0] == 2)

    # --- search integration / Czech relevance ------------------------------
    hits = m.hybrid_search("Praha", 5)
    check("hybrid search finds wiki article",
          any(h["title"] == "Praha" for h in hits), str([h["title"] for h in hits]))
    # FTS folds diacritics, so a query without háčky still matches.
    hits_ascii = m.fts_search("hlavni", 5)
    check("diacritic-insensitive FTS query matches",
          any(h["title"] == "Praha" for h in hits_ascii),
          str([h["title"] for h in hits_ascii]))
    hits_ascii2 = m.fts_search("ceska", 5)
    check("folded query matches stored text too", len(hits_ascii2) >= 1,
          str([h["title"] for h in hits_ascii2]))

    # --- max_pages + pause/resume ------------------------------------------
    resume_dump = os.path.join(
        tmpdir, "cswiki-latest-pages-articles-multistream-resume.xml.bz2")
    make_dump(resume_dump, [
        page(f"Článek {i:02d}", f"Obsah článku číslo {i} o historii.")
        for i in range(10)
    ])
    # Point a fresh wiki source at the bigger dump via a new site.
    sid2, err2 = m.add_source(0, "file://" + resume_dump, "wiki")
    src2 = m.get_source_by_id(sid2)
    check("second wiki source created", sid2 is not None, str(err2))

    first = m.run_wiki_import(src2, max_pages=3)
    check("max_pages caps the import", first == 3, str(first))
    state = json.loads(m.get_source_by_id(sid2)["import_state"] or "{}")
    check("resume marker persisted", bool(state.get("next_title")), str(state))
    check("import counter persisted", state.get("imported") == 3, str(state))

    second = m.run_wiki_import(m.get_source_by_id(sid2), max_pages=3)
    check("resume continues where it stopped", second == 3, str(second))
    titles = [r["title"] for r in m.execute_db_fetchall(
        "SELECT title FROM pages WHERE url LIKE '%wikipedia.org%' ORDER BY id")]
    check("no duplicate pages after resume",
          len(titles) == len(set(titles)), str(titles))

    # --- importer respects a paused source ---------------------------------
    m.update_source(sid2, status="paused")
    before = m.execute_db_fetchone("SELECT COUNT(*) FROM pages")[0]
    m.index_source(sid2, queue_budget=50)
    after = m.execute_db_fetchone("SELECT COUNT(*) FROM pages")[0]
    check("paused wiki source imports nothing", before == after,
          f"{before} -> {after}")

    # --- API importer against a local mock MediaWiki server ----------------
    # Use a fresh database: the dump import above already claimed some titles on
    # the same cs.wikipedia.org site, and dedup would hide the API results.
    m.close_db()
    m.DB_PATH = os.path.join(tmpdir, "console-api.db")
    m._db_conn = None
    m.get_db()

    api_server = _start_mock_wiki_api()
    port = api_server.server_address[1]
    original_base = m.WIKI_API_BASE
    m.WIKI_API_BASE = f"http://127.0.0.1:{port}"
    try:
        sid3, err3 = m.add_source(0, "wiki:cs", "wiki", notes="api")
        src3 = m.get_source_by_id(sid3)
        check("api wiki source created", sid3 is not None, str(err3))
        check("api importer recorded", src3["importer"] == "api", str(src3["importer"]))
        count = m.run_wiki_import(src3, max_pages=10)
        check("api importer stores articles", count == 3, str(count))
        titles = [r["title"] for r in m.execute_db_fetchall(
            "SELECT title FROM pages ORDER BY title")]
        check("api importer keeps Czech titles",
              titles == ["Brno", "Ostrava", "Praha"], str(titles))
        entry = m.execute_db_fetchone(
            "SELECT body_text, og_description FROM pages WHERE title = ?", ("Praha",))
        check("api extract body indexed",
              entry and "hlavní město" in entry["body_text"], str(entry))
        check("api re-import idempotent",
              m.run_wiki_import(m.get_source_by_id(sid3), max_pages=10) == 0)
    finally:
        m.WIKI_API_BASE = original_base
        api_server.shutdown()
        api_server.server_close()

    print()
    if failures:
        print(f"{len(failures)} TEST(S) FAILED: {failures}")
        raise SystemExit(1)
    print("ALL WIKI IMPORT TESTS PASSED")


if __name__ == "__main__":
    main()
