#!/usr/bin/env python3
"""Regresní testy pro běh backendu na Androidu (Chaquopy).

Chaquopy přibaluje SQLite bez modulu FTS5 a Android nemá
``/usr/share/zoneinfo``. Obojí dřív shodilo start serveru:

* ``OperationalError: no such module: fts5`` při vytváření ``pages_fts``,
* ``ZoneInfoNotFoundError: 'No time zone found with key Europe/Prague'``
  v APScheduleru (tzlocal).

Testy ověřují, že start, fulltextové hledání i restart fungují i bez FTS5 a
bez systémové timezone databáze, a že se diakritika skládá i v LIKE fallbacku.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_module(tmpdir):
    """Naimportuje app_combined s databází v soukromém temp adresáři."""
    os.environ["MINISEARCH_DB"] = os.path.join(tmpdir, "console.db")
    os.environ["MINISEARCH_PROFILE"] = "lowmem"
    os.environ["MINISEARCH_WORKERS"] = "1"
    for name in ("app_combined",):
        sys.modules.pop(name, None)
    import app_combined as m
    return m


def test_probe_detects_fts5_on_host():
    """Hostitelský SQLite FTS5 má -> _probe_fts5 musí vrátit True."""
    tmpdir = tempfile.mkdtemp()
    try:
        m = _fresh_module(tmpdir)
        conn = sqlite3.connect(":memory:")
        assert m._probe_fts5(conn) is True, "host SQLite má FTS5"
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("Test 1: _probe_fts5 detekuje FTS5 - PASSED")


def test_fallback_table_shape_matches_triggers():
    """Bez FTS5 se vytvoří mirror tabulka se stejnými sloupci a triggery žijí."""
    tmpdir = tempfile.mkdtemp()
    try:
        m = _fresh_module(tmpdir)
        m.FTS5_ENABLED = False
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.create_function("minisearch_fold", 1, m.fold_diacritics)
        conn.execute(m.DB_SCHEMA["pages"])
        conn.execute(m.DB_SCHEMA["sites"])
        m._ensure_fts_table(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pages_fts)")}
        assert cols == {"page_id", "title", "body_text", "og_title",
                        "og_description", "url", "schema_type"}, cols
        # Trigger z DB_TRIGGERS musí do mirror tabulky zapsat.
        for trigger in m.DB_TRIGGERS:
            conn.execute(trigger)
        conn.execute(
            "INSERT INTO pages (id, site_id, url, url_hash, title, body_text) "
            "VALUES (1, 1, 'http://x/1', 'h1', 'Český titulek', 'tělo')")
        row = conn.execute("SELECT title FROM pages_fts WHERE page_id = 1").fetchone()
        assert row["title"] == "Český titulek", row["title"]
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("Test 2: fallback tabulka + triggery - PASSED")


def test_like_fallback_folds_diacritics():
    """LIKE fallback najde 'cesky' i v 'český' a naopak."""
    tmpdir = tempfile.mkdtemp()
    try:
        m = _fresh_module(tmpdir)
        m.FTS5_ENABLED = False
        conn = sqlite3.connect(os.environ["MINISEARCH_DB"])
        conn.create_function("minisearch_fold", 1, m.fold_diacritics)
        conn.row_factory = sqlite3.Row
        conn.execute(m.DB_SCHEMA["pages"])
        conn.execute(m.DB_SCHEMA["sites"])
        m._ensure_fts_table(conn)
        conn.execute(
            "INSERT INTO pages (id, site_id, url, url_hash, title, body_text, "
            "indexed_at) VALUES (1, 1, 'http://x/1', 'h1', 'Český článek', "
            "'Toto je český text s diakritikou', 100)")
        conn.execute(
            "INSERT INTO pages_fts (page_id, title, body_text, og_title, "
            "og_description, url, schema_type) VALUES "
            "(1, 'Český článek', 'Toto je český text s diakritikou', '', '', "
            "'http://x/1', '')")
        conn.commit()
        conn.close()
        m._db_conn = None
        m.get_db()
        assert [r["title"] for r in m.fts_search("cesky", 5)] == ["Český článek"]
        assert [r["title"] for r in m.fts_search("Český", 5)] == ["Český článek"]
        assert [r["title"] for r in m.fts_search("diakritikou", 5)] == ["Český článek"]
        assert m.fts_search("neexistuje", 5) == []
        m.close_db()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("Test 3: LIKE fallback skládá diakritiku - PASSED")


def test_server_starts_and_restarts_without_fts5():
    """Start, readiness, HTTP a restart ve stejném procesu bez FTS5."""
    tmpdir = tempfile.mkdtemp()
    try:
        m = _fresh_module(tmpdir)
        m._probe_fts5 = lambda conn: False
        m.FTS5_ENABLED = None
        port = 8099
        assert m.start_server(host="127.0.0.1", port=port, block=False) is True
        assert m.server_status()["state"] == "RUNNING"
        assert m.FTS5_ENABLED is False, "fallback se musí aktivovat"
        for path in ("/", "/admin", "/health"):
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}", timeout=10) as resp:
                assert resp.status == 200, (path, resp.status)
        m.stop_server()
        assert m.server_status()["state"] == "STOPPED"
        # Nový start ve stejném procesu musí znovu naběhnout.
        assert m.start_server(host="127.0.0.1", port=port, block=False) is True
        assert m.server_status()["state"] == "RUNNING"
        m.stop_server()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("Test 4: start/stop/restart bez FTS5 - PASSED")


def test_scheduler_survives_missing_timezone():
    """Nedostupná timezone nesmí zabránit startu serveru ani scheduleru."""
    tmpdir = tempfile.mkdtemp()
    try:
        m = _fresh_module(tmpdir)
        port = 8098
        # Simuluj Android bez zoneinfo: BackgroundScheduler() vyhodí výjimku.
        import apscheduler.schedulers.background as bg
        original = bg.BackgroundScheduler

        class Boom:
            def __init__(self, *a, **k):
                if not k.get("timezone"):
                    raise Exception("No time zone found with key Europe/Prague")

        bg.BackgroundScheduler = Boom
        m.BackgroundScheduler = Boom
        try:
            assert m.start_server(host="127.0.0.1", port=port, block=False) is True
            assert m.server_status()["state"] == "RUNNING"
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=10) as resp:
                assert resp.status == 200
        finally:
            bg.BackgroundScheduler = original
            m.stop_server()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    print("Test 5: start bez timezone - PASSED")


if __name__ == "__main__":
    test_probe_detects_fts5_on_host()
    test_fallback_table_shape_matches_triggers()
    test_like_fallback_folds_diacritics()
    test_server_starts_and_restarts_without_fts5()
    test_scheduler_survives_missing_timezone()
    print("\nVsech 5 testu pro Android kompatibilitu prochazi!")
