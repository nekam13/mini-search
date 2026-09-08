#!/usr/bin/env python3
"""Testy pro databazi - v7.0"""

import sqlite3
import os

# Pouzijeme primo SQL prikazy pro testy
DB_SCHEMA = {
    'sites': "CREATE TABLE IF NOT EXISTS sites (id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_url TEXT UNIQUE, aliases TEXT DEFAULT '[]', status TEXT DEFAULT 'active', error_count INTEGER DEFAULT 0, last_crawled INTEGER DEFAULT 0, max_pages INTEGER DEFAULT 500, crawl_delay REAL DEFAULT 1.0, created_at INTEGER DEFAULT (strftime('%s','now')))",
    'crawl_queue': "CREATE TABLE IF NOT EXISTS crawl_queue (id INTEGER PRIMARY KEY AUTOINCREMENT, site_id INTEGER, url TEXT, status TEXT DEFAULT 'pending', locked_by TEXT DEFAULT '', error_reason TEXT DEFAULT '', retry_count INTEGER DEFAULT 0, priority INTEGER DEFAULT 5, scheduled_at INTEGER DEFAULT 0, created_at INTEGER DEFAULT (strftime('%s','now')), FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE)",
    'sitemaps_feeds': "CREATE TABLE IF NOT EXISTS sitemaps_feeds (id INTEGER PRIMARY KEY AUTOINCREMENT, site_id INTEGER, url TEXT, type TEXT, last_checked INTEGER DEFAULT 0, recursion_depth INTEGER DEFAULT 0, FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE)",
    'pages': "CREATE TABLE IF NOT EXISTS pages (id INTEGER PRIMARY KEY AUTOINCREMENT, site_id INTEGER, url TEXT UNIQUE, url_hash TEXT, title TEXT DEFAULT '', og_title TEXT DEFAULT '', og_description TEXT DEFAULT '', og_image TEXT DEFAULT '', favicon_url TEXT DEFAULT '', body_text TEXT DEFAULT '', images TEXT DEFAULT '[]', schema_type TEXT DEFAULT '', schema_details TEXT DEFAULT '{}', audio_url TEXT DEFAULT '', has_audio INTEGER DEFAULT 0, published_timestamp INTEGER DEFAULT 0, embedding BLOB, indexed_at INTEGER DEFAULT 0, seo_score REAL DEFAULT 0.0, FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE)",
    'pages_fts': "CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(page_id, title, body_text, og_title, og_description, url, schema_type)",
    'update_status': "CREATE TABLE IF NOT EXISTS update_status (id INTEGER PRIMARY KEY AUTOINCREMENT, last_check INTEGER DEFAULT 0, current_commit TEXT DEFAULT '', latest_commit TEXT DEFAULT '', update_available INTEGER DEFAULT 0, last_update_time INTEGER DEFAULT 0, last_update_result TEXT DEFAULT '')"
}

DB_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(status)",
    "CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_queue(status)",
    "CREATE INDEX IF NOT EXISTS idx_queue_priority ON crawl_queue(priority)",
    "CREATE INDEX IF NOT EXISTS idx_queue_site ON crawl_queue(site_id)",
    "CREATE INDEX IF NOT EXISTS idx_queue_scheduled ON crawl_queue(scheduled_at)",
    "CREATE INDEX IF NOT EXISTS idx_pages_site ON pages(site_id)",
    "CREATE INDEX IF NOT EXISTS idx_pages_url_hash ON pages(url_hash)",
    "CREATE INDEX IF NOT EXISTS idx_pages_title ON pages(og_title)",
    "CREATE INDEX IF NOT EXISTS idx_pages_indexed ON pages(indexed_at)"
]


def _init_schema(conn):
    cursor = conn.cursor()
    for schema in DB_SCHEMA.values():
        cursor.execute(schema)
    for idx in DB_INDEXES:
        try:
            cursor.execute(idx)
        except sqlite3.OperationalError:
            pass
    conn.commit()


def test_db_schema():
    """Test 1: Overeni, ze schema se vytvori bez chyby"""
    conn = sqlite3.connect(":memory:")
    _init_schema(conn)
    
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    
    assert 'sites' in tables
    assert 'crawl_queue' in tables
    assert 'sitemaps_feeds' in tables
    assert 'pages' in tables
    assert 'pages_fts' in tables
    assert 'update_status' in tables
    
    conn.close()
    print("Test 1: DB Schema (v7.0) - PASSED")


def test_db_indexes():
    """Test 2: Overeni, ze indexy existuji"""
    conn = sqlite3.connect(":memory:")
    _init_schema(conn)
    
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indexes = [row[0] for row in cursor.fetchall()]
    
    assert 'idx_sites_status' in indexes
    assert 'idx_queue_status' in indexes
    assert 'idx_pages_url_hash' in indexes
    assert 'idx_pages_indexed' in indexes
    
    conn.close()
    print("Test 2: DB Indexes (v7.0) - PASSED")


def test_fts5_table():
    """Test 3: FTS5 tabulka existuje a funguje"""
    conn = sqlite3.connect(":memory:")
    _init_schema(conn)
    
    cursor = conn.cursor()
    # Test FTS5
    cursor.execute("INSERT INTO pages_fts(page_id, title, body_text) VALUES (1, 'Test Title', 'Test body text')")
    conn.commit()
    
    # Search
    cursor.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'Test'")
    result = cursor.fetchone()
    assert result is not None
    assert result[0] == 1
    
    conn.close()
    print("Test 3: FTS5 Table - PASSED")


def test_update_status_table():
    """Test 4: Update status tabulka"""
    conn = sqlite3.connect(":memory:")
    _init_schema(conn)
    
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO update_status (id, last_check, current_commit, latest_commit, update_available)
        VALUES (1, 1234567890, 'abc123', 'def456', 1)
    """)
    conn.commit()
    
    cursor.execute("SELECT * FROM update_status WHERE id=1")
    row = cursor.fetchone()
    assert row is not None
    assert row[1] == 1234567890
    assert row[2] == 'abc123'
    assert row[3] == 'def456'
    assert row[4] == 1
    
    conn.close()
    print("Test 4: Update Status Table - PASSED")


def test_new_columns():
    """Test 5: Nove sloupce v tabulkach"""
    conn = sqlite3.connect(":memory:")
    _init_schema(conn)
    
    cursor = conn.cursor()
    
    # Test crawl_delay in sites
    cursor.execute("INSERT INTO sites (canonical_url, crawl_delay) VALUES ('test.com', 2.5)")
    conn.commit()
    cursor.execute("SELECT crawl_delay FROM sites WHERE canonical_url='test.com'")
    assert cursor.fetchone()[0] == 2.5
    
    # Test scheduled_at in crawl_queue
    cursor.execute("INSERT INTO crawl_queue (site_id, url, scheduled_at) VALUES (1, 'http://test.com', 1234567890)")
    conn.commit()
    cursor.execute("SELECT scheduled_at FROM crawl_queue WHERE url='http://test.com'")
    assert cursor.fetchone()[0] == 1234567890
    
    # Test seo_score in pages
    cursor.execute("INSERT INTO pages (site_id, url, url_hash, seo_score) VALUES (1, 'http://test.com', 'hash123', 85.5)")
    conn.commit()
    cursor.execute("SELECT seo_score FROM pages WHERE url='http://test.com'")
    assert cursor.fetchone()[0] == 85.5
    
    # Test recursion_depth in sitemaps_feeds
    cursor.execute("INSERT INTO sitemaps_feeds (site_id, url, type, recursion_depth) VALUES (1, 'http://test.com/sitemap.xml', 'sitemap', 2)")
    conn.commit()
    cursor.execute("SELECT recursion_depth FROM sitemaps_feeds WHERE url='http://test.com/sitemap.xml'")
    assert cursor.fetchone()[0] == 2
    
    conn.close()
    print("Test 5: New Columns (v7.0) - PASSED")


def test_foreign_keys_cascade():
    """Test 6: Foreign keys CASCADE"""
    conn = sqlite3.connect(":memory:")
    _init_schema(conn)
    
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    
    cursor.execute("INSERT INTO sites (canonical_url) VALUES ('https://example.com')")
    site_id = cursor.lastrowid
    
    cursor.execute("INSERT INTO crawl_queue (site_id, url) VALUES (?, ?)", (site_id, "https://example.com/page1"))
    conn.commit()
    
    cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE site_id = ?", (site_id,))
    count_before = cursor.fetchone()[0]
    assert count_before == 1
    
    cursor.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    conn.commit()
    
    cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE site_id = ?", (site_id,))
    count_after = cursor.fetchone()[0]
    assert count_after == 0
    
    conn.close()
    print("Test 6: Foreign Keys CASCADE - PASSED")


def test_fts5_czech():
    """Test 7: FTS5 s ceskymi znaky"""
    conn = sqlite3.connect(":memory:")
    _init_schema(conn)
    
    cursor = conn.cursor()
    # Vlozit text s diacritikou
    cursor.execute("INSERT INTO pages_fts(page_id, title, body_text) VALUES (1, ? , ?)", ('Český text', 'Toto je český text s diakritikou'))
    conn.commit()
    
    # Vyhledat bez diacritiky
    cursor.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'cesky'")
    result = cursor.fetchone()
    assert result is not None
    
    # Vyhledat s diacritikou
    cursor.execute("SELECT * FROM pages_fts WHERE pages_fts MATCH 'Český'")
    result = cursor.fetchone()
    assert result is not None
    
    conn.close()
    print("Test 7: FTS5 Czech Support - PASSED")


if __name__ == "__main__":
    test_db_schema()
    test_db_indexes()
    test_fts5_table()
    test_update_status_table()
    test_new_columns()
    test_foreign_keys_cascade()
    test_fts5_czech()
    print("\nVsech 7 testu prochazi!")
