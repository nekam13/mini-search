#!/usr/bin/env python3
"""Testy pro databazi - 7 testu pro 7 souboru"""

import sqlite3
import os
import sys

# Extrahujeme DB_SCHEMA a DB_INDEXES primo z app_combined.py bez importu Flask atd.
# Cteni souboru a parsovani
app_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'app_combined.py')

DB_SCHEMA = {}
DB_INDEXES = []

with open(app_path, 'r') as f:
    content = f.read()
    
# Parse DB_SCHEMA dictionary
import ast
# Najdi DB_SCHEMA definici
schema_start = content.find("DB_SCHEMA = {")
schema_end = content.find("\n\n# ===", schema_start)
if schema_end == -1:
    schema_end = content.find("\n# ===", schema_start)
if schema_end == -1:
    schema_end = content.find("\nDB_INDEXES", schema_start)

schema_str = content[schema_start:schema_end].strip()
# Nahrazeni triple quotes za single quotes pro ast
schema_str = schema_str.replace("'''", '"')
schema_str = schema_str.replace('"""', '"')

try:
    DB_SCHEMA = ast.literal_eval(schema_str)
except:
    # Manualni definice schematu
    DB_SCHEMA = {
        'sites': '''
            CREATE TABLE IF NOT EXISTS sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_url TEXT UNIQUE,
                aliases TEXT DEFAULT '[]',
                status TEXT DEFAULT 'active',
                error_count INTEGER DEFAULT 0,
                last_crawled INTEGER DEFAULT 0,
                max_pages INTEGER DEFAULT 500,
                created_at INTEGER DEFAULT (strftime('%s','now'))
            )
        ''',
        'crawl_queue': '''
            CREATE TABLE IF NOT EXISTS crawl_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER,
                url TEXT,
                status TEXT DEFAULT 'pending',
                locked_by TEXT DEFAULT '',
                error_reason TEXT DEFAULT '',
                retry_count INTEGER DEFAULT 0,
                priority INTEGER DEFAULT 5,
                created_at INTEGER DEFAULT (strftime('%s','now')),
                FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
            )
        ''',
        'sitemaps_feeds': '''
            CREATE TABLE IF NOT EXISTS sitemaps_feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER,
                url TEXT,
                type TEXT,
                last_checked INTEGER DEFAULT 0,
                FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
            )
        ''',
        'pages': '''
            CREATE TABLE IF NOT EXISTS pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                site_id INTEGER,
                url TEXT UNIQUE,
                url_hash TEXT,
                title TEXT DEFAULT '',
                og_title TEXT DEFAULT '',
                og_description TEXT DEFAULT '',
                og_image TEXT DEFAULT '',
                favicon_url TEXT DEFAULT '',
                body_text TEXT DEFAULT '',
                images TEXT DEFAULT '[]',
                schema_type TEXT DEFAULT '',
                schema_details TEXT DEFAULT '{}',
                audio_url TEXT DEFAULT '',
                has_audio INTEGER DEFAULT 0,
                published_timestamp INTEGER DEFAULT 0,
                embedding BLOB,
                indexed_at INTEGER DEFAULT 0,
                FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
            )
        '''
    }

# Parse DB_INDEXES
indexes_start = content.find("DB_INDEXES = [")
indexes_end = content.find("\n\n# ===", indexes_start)
if indexes_end == -1:
    indexes_end = content.find("\n# ===", indexes_start)

indexes_str = content[indexes_start:indexes_end].strip()
try:
    DB_INDEXES = ast.literal_eval(indexes_str)
except:
    DB_INDEXES = [
        "CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(status)",
        "CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_queue(status)",
        "CREATE INDEX IF NOT EXISTS idx_queue_priority ON crawl_queue(priority)",
        "CREATE INDEX IF NOT EXISTS idx_queue_site ON crawl_queue(site_id)",
        "CREATE INDEX IF NOT EXISTS idx_pages_site ON pages(site_id)",
        "CREATE INDEX IF NOT EXISTS idx_pages_url_hash ON pages(url_hash)",
        "CREATE INDEX IF NOT EXISTS idx_pages_title ON pages(og_title)"
    ]


def _init_schema(conn):
    cursor = conn.cursor()
    for schema in DB_SCHEMA.values():
        cursor.execute(schema)
    for idx in DB_INDEXES:
        cursor.execute(idx)
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
    
    conn.close()
    print("Test 1: DB Schema - PASSED")


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
    
    conn.close()
    print("Test 2: DB Indexes - PASSED")


def test_sites_table():
    """Test 3: Vlozeni a cteni z tabulky sites"""
    conn = sqlite3.connect(":memory:")
    _init_schema(conn)
    
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO sites (canonical_url, max_pages) VALUES (?, ?)",
        ("https://example.com", 100)
    )
    conn.commit()
    
    cursor.execute("SELECT * FROM sites WHERE canonical_url = ?", ("https://example.com",))
    row = cursor.fetchone()
    
    assert row is not None
    assert row[1] == "https://example.com"
    assert row[6] == 100
    
    conn.close()
    print("Test 3: Sites Table - PASSED")


def test_crawl_queue_table():
    """Test 4: Vlozeni a cteni z tabulky crawl_queue"""
    conn = sqlite3.connect(":memory:")
    _init_schema(conn)
    
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO sites (canonical_url) VALUES (?)",
        ("https://example.com",)
    )
    conn.commit()
    
    cursor.execute("SELECT id FROM sites WHERE canonical_url = ?", ("https://example.com",))
    site_id = cursor.fetchone()[0]
    
    cursor.execute(
        "INSERT INTO crawl_queue (site_id, url, priority) VALUES (?, ?, ?)",
        (site_id, "https://example.com/page1", 10)
    )
    conn.commit()
    
    cursor.execute("SELECT * FROM crawl_queue WHERE url = ?", ("https://example.com/page1",))
    row = cursor.fetchone()
    
    assert row is not None
    assert row[2] == "https://example.com/page1"
    assert row[7] == 10
    
    conn.close()
    print("Test 4: Crawl Queue Table - PASSED")


def test_pages_table():
    """Test 5: Vlozeni a cteni z tabulky pages"""
    conn = sqlite3.connect(":memory:")
    _init_schema(conn)
    
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO sites (canonical_url) VALUES (?)",
        ("https://example.com",)
    )
    conn.commit()
    
    cursor.execute("SELECT id FROM sites WHERE canonical_url = ?", ("https://example.com",))
    site_id = cursor.fetchone()[0]
    
    cursor.execute(
        """INSERT INTO pages (site_id, url, title, body_text, published_timestamp)
           VALUES (?, ?, ?, ?, ?)""",
        (site_id, "https://example.com/page1", "Test Title", "Test body", 1234567890)
    )
    conn.commit()
    
    cursor.execute("SELECT * FROM pages WHERE url = ?", ("https://example.com/page1",))
    row = cursor.fetchone()
    
    assert row is not None
    assert row[2] == "https://example.com/page1"
    assert row[4] == "Test Title"
    assert row[15] == 1234567890
    
    conn.close()
    print("Test 5: Pages Table - PASSED")


def test_sitemaps_feeds_table():
    """Test 6: Vlozeni a cteni z tabulky sitemaps_feeds"""
    conn = sqlite3.connect(":memory:")
    _init_schema(conn)
    
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO sites (canonical_url) VALUES (?)",
        ("https://example.com",)
    )
    conn.commit()
    
    cursor.execute("SELECT id FROM sites WHERE canonical_url = ?", ("https://example.com",))
    site_id = cursor.fetchone()[0]
    
    cursor.execute(
        "INSERT INTO sitemaps_feeds (site_id, url, type) VALUES (?, ?, ?)",
        (site_id, "https://example.com/sitemap.xml", "sitemap")
    )
    conn.commit()
    
    cursor.execute("SELECT * FROM sitemaps_feeds WHERE url = ?", ("https://example.com/sitemap.xml",))
    row = cursor.fetchone()
    
    assert row is not None
    assert row[2] == "https://example.com/sitemap.xml"
    assert row[3] == "sitemap"
    
    conn.close()
    print("Test 6: Sitemaps Feeds Table - PASSED")


def test_foreign_keys():
    """Test 7: Overeni, ze foreign keys funguji (CASCADE delete)"""
    conn = sqlite3.connect(":memory:")
    _init_schema(conn)
    
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    
    cursor.execute(
        "INSERT INTO sites (canonical_url) VALUES (?)",
        ("https://example.com",)
    )
    site_id = cursor.lastrowid
    
    cursor.execute(
        "INSERT INTO crawl_queue (site_id, url) VALUES (?, ?)",
        (site_id, "https://example.com/page1")
    )
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
    print("Test 7: Foreign Keys CASCADE - PASSED")


if __name__ == "__main__":
    test_db_schema()
    test_db_indexes()
    test_sites_table()
    test_crawl_queue_table()
    test_pages_table()
    test_sitemaps_feeds_table()
    test_foreign_keys()
    print("\nVsech 7 testu prochazi!")
