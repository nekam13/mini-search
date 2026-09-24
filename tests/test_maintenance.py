#!/usr/bin/env python3
"""Deterministic tests for the nightly maintenance ("dreaming") mode.

No live network and no live Wikipedia: dead-link probing runs against a local
HTTP server and the wiki stage imports a tiny bz2 dump written to a temp dir.
Covers duplicate merging (url_hash + content), the resource-gated embedding
backfill, the resume-aware wiki stage, 404 detection/purge, the disk-gated
``PRAGMA optimize``/``VACUUM`` stage, the orchestrator and the CLI wiring.
"""
import bz2
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


def _seed(conn, site_id, url, title, body, indexed_at=1000, url_hash=None):
    """Insert a page directly, bypassing the network, for a stable fixture."""
    import app_combined as m
    conn.execute(
        """INSERT INTO pages (site_id, url, url_hash, title, og_title, og_description,
           body_text, schema_type, schema_details, images, indexed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (site_id, url, url_hash if url_hash is not None else m.url_hash(url),
         title, title, f"{title} popis", body, 'Article', '{}', '[]', indexed_at))


class _Handler(BaseHTTPRequestHandler):
    """Serves a live page, a gone page and a HEAD-refusing page."""

    def log_message(self, *args):
        pass

    def _send(self, status, body=b""):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_HEAD(self):
        if self.path.startswith("/gone"):
            self._send(404)
        elif self.path.startswith("/headless"):
            self._send(405)
        else:
            self._send(200)

    def do_GET(self):
        if self.path.startswith("/gone"):
            self._send(404)
        elif self.path.startswith("/headless"):
            self._send(200, b"<html><body>stale ale zivy clanek</body></html>")
        elif self.path.startswith("/robots.txt"):
            self._send(200, b"User-agent: *\n")
        else:
            self._send(200, b"<html><body>zivy clanek</body></html>")


def make_dump(path, pages):
    body = "".join(pages)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/">'
           f'{body}</mediawiki>')
    with bz2.open(path, "wb") as handle:
        handle.write(xml.encode("utf-8"))


def page(title, text="", ns="0"):
    return (f'<page><title>{title}</title><ns>{ns}</ns><revision>'
            f'<text>{text}</text></revision></page>')


def test_dedup():
    """Duplicate url_hash and identical content are both merged away."""
    import app_combined as m

    dup_body = ("Praha je hlavni mesto Ceska a lezi na rece Vltave. "
                "Mesto ma mnoho pamatek a je centrem kultury a politiky. "
                "Navstevnici obdivuji Karluv most a Prazsky hrad kazdy rok. ") * 3

    conn = m.get_db()
    site_id = m.add_site("https://example.com/", 50)
    # Two rows whose raw URL differs only by casing: unique on ``url`` (the DB
    # keeps that invariant) but identical after normalization, so they share a
    # url_hash and are a genuine duplicate pair.
    _seed(conn, site_id, "https://example.com/a", "A", "kratky unikatni obsah A", indexed_at=100)
    conn.execute(
        """INSERT INTO pages (site_id, url, url_hash, title, body_text, indexed_at)
           VALUES (?,?,?,?,?,?)""",
        (site_id, "https://EXAMPLE.com/a", m.url_hash("https://example.com/a"),
         "A duplikat", "jiny text", 200))
    # Two rows with different URLs but identical long content.
    _seed(conn, site_id, "https://example.com/copy-1", "Copy 1", dup_body, indexed_at=300)
    _seed(conn, site_id, "https://example.com/copy-2", "Copy 2", dup_body, indexed_at=400)
    # A short body must never be merged even if it repeats.
    _seed(conn, site_id, "https://example.com/short-1", "Short 1", "ahoj svete", indexed_at=500)
    _seed(conn, site_id, "https://example.com/short-2", "Short 2", "ahoj svete", indexed_at=600)
    conn.commit()

    before = m.execute_db_fetchone("SELECT COUNT(*) FROM pages")[0]
    removed = m.maintenance_dedup_pages()
    after = m.execute_db_fetchone("SELECT COUNT(*) FROM pages")[0]

    check("url duplicate removed", removed['url'] == 1, str(removed))
    check("content duplicate removed", removed['content'] == 1, str(removed))
    check("page count dropped by exactly 2", before - after == 2,
          f"{before} -> {after}")
    check("kept the newest url duplicate",
          m.execute_db_fetchone(
              "SELECT title FROM pages WHERE url_hash = ?",
              (m.url_hash("https://example.com/a"),))[0] == "A duplikat")
    check("kept the oldest content duplicate",
          m.execute_db_fetchone(
              "SELECT title FROM pages WHERE url LIKE ?", ("%copy-1",)) is not None)
    check("content duplicate gone",
          m.execute_db_fetchone(
              "SELECT 1 FROM pages WHERE url LIKE ?", ("%copy-2",)) is None)
    check("short identical bodies both kept",
          m.execute_db_fetchone(
              "SELECT COUNT(*) FROM pages WHERE url LIKE '%short-%'")[0] == 2)
    check("dedup is idempotent", m.maintenance_dedup_pages() == {'url': 0, 'content': 0})

    # FTS5 must not keep orphan rows after the cascade delete.
    fts = m.execute_db_fetchone("SELECT COUNT(*) FROM pages_fts")[0]
    pages = m.execute_db_fetchone("SELECT COUNT(*) FROM pages")[0]
    check("FTS stays in sync with pages", fts == pages, f"fts={fts} pages={pages}")


def test_embedding_backfill():
    """Vectors are computed only when allowed, and the batch limit is honoured."""
    import app_combined as m

    conn = m.get_db()
    site_id = m.add_site("https://embed.example.com/", 50)
    for i in range(5):
        _seed(conn, site_id, f"https://embed.example.com/p{i}", f"P{i}",
              f"unikatni obsah stranky cislo {i} o historii mesta")
    conn.commit()

    # With vectors disabled nothing is touched.
    original_pref = m.EMBEDDINGS_PREF
    m.EMBEDDINGS_PREF = '0'
    check("backfill is a no-op when embeddings are off",
          m.maintenance_backfill_embeddings() == 0)
    check("no embeddings written while off",
          m.execute_db_fetchone(
              "SELECT COUNT(*) FROM pages WHERE embedding IS NOT NULL")[0] == 0)

    # Enabled: a batch smaller than the corpus fills only that many.
    m.EMBEDDINGS_PREF = '1'
    done = m.maintenance_backfill_embeddings(batch=2)
    check("batch limit caps the backfill", done == 2, str(done))
    check("exactly two embeddings stored",
          m.execute_db_fetchone(
              "SELECT COUNT(*) FROM pages WHERE embedding IS NOT NULL")[0] == 2)

    # A second pass finishes the rest.
    done2 = m.maintenance_backfill_embeddings(batch=100)
    check("later pass finishes the rest", done2 == 3, str(done2))
    check("all embeddings present",
          m.execute_db_fetchone(
              "SELECT COUNT(*) FROM pages WHERE embedding IS NULL")[0] == 0)

    m.EMBEDDINGS_PREF = original_pref


def test_wiki_stage():
    """The maintenance wiki stage resumes from next_title and skips paused ones."""
    import app_combined as m

    tmpdir = tempfile.mkdtemp()
    dump_path = os.path.join(
        tmpdir, "cswiki-latest-pages-articles-multistream.xml.bz2")
    make_dump(dump_path, [
        page(f"Clanek {i:02d}", f"Obsah clanku cislo {i} o dejinach Ceska.")
        for i in range(10)
    ])
    source_id, err = m.add_source(0, "file://" + dump_path, "wiki")
    check("wiki source created", source_id is not None, str(err))

    # A small cap proves the stage is bounded and stores a resume marker.
    results = m.maintenance_import_wiki(max_pages=4)
    check("wiki stage reports per-source counts", results.get(source_id) == 4, str(results))
    state = json.loads(m.get_source_by_id(source_id)["import_state"] or "{}")
    check("wiki stage persisted the resume marker",
          bool(state.get("next_title")), str(state))

    # A second pass continues rather than restarting.
    results2 = m.maintenance_import_wiki(max_pages=3)
    check("wiki stage resumes", results2.get(source_id) == 3, str(results2))

    # A paused source is skipped entirely.
    m.update_source(source_id, status="paused")
    check("paused wiki source skipped", m.maintenance_import_wiki(max_pages=5) == {},
          "expected no import")
    m.update_source(source_id, status="active")


def test_dead_links():
    """404 pages are flagged, 200 pages stamped, and purge removes them."""
    import app_combined as m

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = m.get_db()
        site_id = m.add_site(base, 50)
        _seed(conn, site_id, f"{base}/alive", "Alive", "zivy clanek o meste " * 20, indexed_at=100)
        _seed(conn, site_id, f"{base}/gone/1", "Gone", "mrtvy clanek o meste " * 20, indexed_at=200)
        _seed(conn, site_id, f"{base}/headless/1", "Headless",
              "clanek ktery neumi HEAD " * 20, indexed_at=300)
        conn.commit()

        # Network probing is opt-in: a zero limit must not touch the network.
        check("link check disabled by limit", m.maintenance_check_dead_links(limit=0)
              == {'checked': 0, 'dead': 0, 'purged': 0})

        stats = m.maintenance_check_dead_links(limit=10, purge=False)
        check("all three pages checked", stats['checked'] == 3, str(stats))
        check("exactly one dead link found", stats['dead'] == 1, str(stats))
        check("nothing purged without purge flag", stats['purged'] == 0, str(stats))
        check("dead page flagged",
              m.execute_db_fetchone(
                  "SELECT link_status FROM pages WHERE url LIKE '%/gone/%'")[0] == 'dead')
        check("alive page stamped ok",
              m.execute_db_fetchone(
                  "SELECT link_status FROM pages WHERE url LIKE '%/alive'")[0] == 'ok')
        check("405 HEAD falls back to GET and stays alive",
              m.execute_db_fetchone(
                  "SELECT link_status FROM pages WHERE url LIKE '%/headless/%'")[0] == 'ok')

        # Already-checked pages are not re-probed the same night.
        check("second run skips freshly checked pages",
              m.maintenance_check_dead_links(limit=10)['checked'] == 0)

        # Purging requires an explicit opt-in and removes the dead row + its FTS.
        m.execute_db("UPDATE pages SET last_link_check = 0 WHERE link_status = 'dead'",
                     commit=True)
        purged = m.maintenance_check_dead_links(limit=10, purge=True)
        check("dead page purged when enabled", purged['purged'] == 1, str(purged))
        check("purged page gone from pages",
              m.execute_db_fetchone("SELECT 1 FROM pages WHERE url LIKE '%/gone/%'") is None)
        pages = m.execute_db_fetchone("SELECT COUNT(*) FROM pages")[0]
        fts = m.execute_db_fetchone("SELECT COUNT(*) FROM pages_fts")[0]
        check("FTS synced after purge", pages == fts, f"{pages} vs {fts}")
    finally:
        server.shutdown()
        server.server_close()


def test_optimize_and_orchestrator():
    """The DB stage is disk-gated and the orchestrator runs every stage once."""
    import app_combined as m

    # A comfortable free-space budget lets VACUUM run; the summary proves it.
    note = m.maintenance_optimize_db()
    check("optimize reports success", "optimize ok" in note, note)
    check("vacuum ran when disk allows", "VACUUM ok" in note, note)

    # A tiny budget must skip VACUUM rather than risk filling the disk.
    original = m.MAINTENANCE_VACUUM_MIN_FREE_MB
    m.MAINTENANCE_VACUUM_MIN_FREE_MB = 10 ** 9
    note2 = m.maintenance_optimize_db()
    check("vacuum skipped when disk is tight", "VACUUM skipped" in note2, note2)
    m.MAINTENANCE_VACUUM_MIN_FREE_MB = original

    # Kick off a full pass against the local fixture database.
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = m.get_db()
        site_id = m.add_site(base, 50)
        _seed(conn, site_id, f"{base}/a", "A", "obsah stranky A o meste " * 20, indexed_at=100)
        _seed(conn, site_id, f"{base}/gone/2", "Gone2", "obsah stranky B o meste " * 20,
              indexed_at=200)
        conn.commit()

        summary = m.run_maintenance(link_limit=10, wiki_pages=0)
        check("summary contains every stage",
              all(k in summary for k in ('dedup', 'embeddings', 'wiki', 'links', 'db')),
              str(sorted(summary)))
        check("summary records elapsed time", 'elapsed' in summary, str(summary))
        check("orchestrator ran the link stage",
              summary['links']['checked'] == 2, str(summary['links']))
        check("orchestrator found the dead link",
              summary['links']['dead'] == 1, str(summary['links']))
    finally:
        server.shutdown()
        server.server_close()


def test_logging_and_cli():
    """Progress is written to the maintenance log and the CLI wires the flags."""
    import app_combined as m
    import argparse

    tmpdir = tempfile.mkdtemp()
    original_log = m.MAINTENANCE_LOG_PATH
    m.MAINTENANCE_LOG_PATH = os.path.join(tmpdir, "logs", "maintenance.log")
    try:
        m.maintenance_log("test line")
        with open(m.MAINTENANCE_LOG_PATH, encoding="utf-8") as handle:
            content = handle.read()
        check("maintenance log written", "test line" in content, content)
    finally:
        m.MAINTENANCE_LOG_PATH = original_log

    # The CLI helper maps the skip flags onto the orchestrator switches.
    calls = {}
    original_run = m.run_maintenance

    def fake_run(**kwargs):
        calls.update(kwargs)
        return {'dedup': {'url': 0, 'content': 0}, 'elapsed': 0.0}

    m.run_maintenance = fake_run
    try:
        args = argparse.Namespace(
            no_dedup=True, no_embeddings=False, no_wiki=True,
            no_links=False, no_optimize=True, max_pages=7, link_check=3)
        rc = m._cli_maintenance(args)
        check("cli helper returns success", rc == 0, str(rc))
        check("cli honours --no-dedup", calls['dedup'] is False, str(calls))
        check("cli honours --no-wiki", calls['wiki'] is False, str(calls))
        check("cli honours --no-optimize", calls['optimize'] is False, str(calls))
        check("cli passes max-pages through", calls['wiki_pages'] == 7, str(calls))
        check("cli passes link-check through", calls['link_limit'] == 3, str(calls))
    finally:
        m.run_maintenance = original_run


def main():
    tmpdir = tempfile.mkdtemp()
    os.chdir(tmpdir)

    import app_combined as m

    m.DB_PATH = os.path.join(tmpdir, "console.db")
    m._db_conn = None
    m.close_db()
    m.get_db()

    test_dedup()
    m.close_db()
    m.DB_PATH = os.path.join(tmpdir, "embed.db")
    m._db_conn = None
    m.get_db()
    test_embedding_backfill()

    m.close_db()
    m.DB_PATH = os.path.join(tmpdir, "wiki.db")
    m._db_conn = None
    m.get_db()
    test_wiki_stage()

    m.close_db()
    m.DB_PATH = os.path.join(tmpdir, "links.db")
    m._db_conn = None
    m.get_db()
    test_dead_links()

    m.close_db()
    m.DB_PATH = os.path.join(tmpdir, "orch.db")
    m._db_conn = None
    m.get_db()
    test_optimize_and_orchestrator()

    test_logging_and_cli()

    print()
    if failures:
        print(f"{len(failures)} TEST(S) FAILED: {failures}")
        raise SystemExit(1)
    print("ALL MAINTENANCE TESTS PASSED")


if __name__ == "__main__":
    main()
