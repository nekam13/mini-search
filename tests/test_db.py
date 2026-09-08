#!/usr/bin/env python3
"""Testy pro databazi - 7 testu pro 7 souboru"""

import sqlite3
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_combined import DB_SCHEMA, DB_INDEXES, _init_schema


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
    assert row[10] == 1234567890
    
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
