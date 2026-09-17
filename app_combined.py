#!/usr/bin/env python3
"""
Mini Search - Complete Implementation v7.2
Hybrid Search: 60% hnswlib vector + 35% FTS5 full-text + 5% SEO scoring
Database migration without deleting console.db
FTS5 backfill for existing pages with triggers
GitHub update checking with .commit_sha, .update_available, update_log
Gzip sitemap support, recursion limits, max_pages enforcement
Per-domain shared Crawl-delay enforcement
Correct 429 rate_limited scheduling
Non-destructive scheduled recrawl
Robots checks before fetching discovery candidates
Clay design system shared by the public search page and the admin panel
"""

import sqlite3
import json
import time
import threading
import signal
import sys
import os
import hashlib
import re
import gzip
import io
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse, urljoin, urlencode
from urllib.robotparser import RobotFileParser

from flask import (Flask, render_template, request, redirect, jsonify)
from markupsafe import escape
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import requests
from bs4 import BeautifulSoup
import feedparser
import extruct
import numpy as np
import hnswlib


# ============================================================================
# GLOBAL CONFIG
# ============================================================================

DB_PATH = "console.db"
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30
MIN_DELAY = 1.0
USER_AGENT = "MiniSearchBot/1.0 (+http://localhost/bot)"
SHUTDOWN_FLAG = False
MAX_SITEMAP_RECURSION = 5
MAX_SITEMAP_URLS = 5000

_db_lock = threading.Lock()
_db_conn = None
_hnsw_index = None
_model = None
_scheduler = None
_worker_threads = []
_robots_cache = {}
_robots_lock = threading.Lock()
_domain_crawl_delay = {}
_domain_delay_lock = threading.Lock()

# Update tracking
COMMIT_SHA_PATH = ".commit_sha"
UPDATE_AVAILABLE_PATH = ".update_available"
UPDATE_LOG_PATH = "update_log"
REPO_OWNER = "nekam13"
REPO_NAME = "mini-search"
BRANCH = "beta-optimized"


# ============================================================================
# DATABASE SCHEMA - Extended with FTS5
# ============================================================================

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
            crawl_delay REAL DEFAULT 1.0,
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
            scheduled_at INTEGER DEFAULT 0,
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
            recursion_depth INTEGER DEFAULT 0,
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
            seo_score REAL DEFAULT 0.0,
            FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
        )
    ''',
    'pages_fts': '''
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
            page_id,
            title,
            body_text,
            og_title,
            og_description,
            url,
            schema_type
        )
    ''',
    'update_status': '''
        CREATE TABLE IF NOT EXISTS update_status (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            last_check INTEGER DEFAULT 0,
            current_commit TEXT DEFAULT '',
            latest_commit TEXT DEFAULT '',
            update_available INTEGER DEFAULT 0,
            last_update_time INTEGER DEFAULT 0,
            last_update_result TEXT DEFAULT ''
        )
    ''',
    'site_sources': '''
        CREATE TABLE IF NOT EXISTS site_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            source_type TEXT NOT NULL CHECK(source_type IN ('domain', 'url', 'sitemap', 'feed', 'rss', 'atom')),
            priority INTEGER DEFAULT 5,
            notes TEXT DEFAULT '',
            last_checked INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at INTEGER DEFAULT (strftime('%s','now')),
            FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
        )
    '''
}

VALID_SOURCE_TYPES = ('domain', 'url', 'sitemap', 'feed', 'rss', 'atom')
FEED_SOURCE_TYPES = ('feed', 'rss', 'atom')
VALID_SITE_STATUSES = ('active', 'blocked', 'paused')
MAX_PAGES_MIN = 1
MAX_PAGES_MAX = 10000
ERROR_LOG_PATH = "logs/errors.log"

DB_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(status)",
    "CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_queue(status)",
    "CREATE INDEX IF NOT EXISTS idx_queue_priority ON crawl_queue(priority)",
    "CREATE INDEX IF NOT EXISTS idx_queue_site ON crawl_queue(site_id)",
    "CREATE INDEX IF NOT EXISTS idx_queue_scheduled ON crawl_queue(scheduled_at)",
    "CREATE INDEX IF NOT EXISTS idx_pages_site ON pages(site_id)",
    "CREATE INDEX IF NOT EXISTS idx_pages_url_hash ON pages(url_hash)",
    "CREATE INDEX IF NOT EXISTS idx_pages_title ON pages(og_title)",
    "CREATE INDEX IF NOT EXISTS idx_pages_indexed ON pages(indexed_at)",
    "CREATE INDEX IF NOT EXISTS idx_sources_site ON site_sources(site_id)",
    "CREATE INDEX IF NOT EXISTS idx_sources_type ON site_sources(source_type)",
]

DB_TRIGGERS = [
    '''
    CREATE TRIGGER IF NOT EXISTS pages_after_insert AFTER INSERT ON pages
    BEGIN
        INSERT INTO pages_fts(page_id, title, body_text, og_title, og_description, url, schema_type)
        VALUES (NEW.id, NEW.title, NEW.body_text, NEW.og_title, NEW.og_description, NEW.url, NEW.schema_type);
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS pages_after_update AFTER UPDATE ON pages
    BEGIN
        UPDATE pages_fts SET 
            title = NEW.title,
            body_text = NEW.body_text,
            og_title = NEW.og_title,
            og_description = NEW.og_description,
            url = NEW.url,
            schema_type = NEW.schema_type
        WHERE page_id = NEW.id;
    END
    ''',
    '''
    CREATE TRIGGER IF NOT EXISTS pages_after_delete AFTER DELETE ON pages
    BEGIN
        DELETE FROM pages_fts WHERE page_id = OLD.id;
    END
    '''
]


# ============================================================================
# DATABASE
# ============================================================================

def get_db():
    """Get or create global SQLite connection (double-checked locking)."""
    global _db_conn
    if _db_conn is not None:
        return _db_conn
    with _db_lock:
        if _db_conn is None:
            _db_conn = sqlite3.connect(
                DB_PATH,
                timeout=60.0,
                check_same_thread=False
            )
            _db_conn.execute("PRAGMA journal_mode=WAL")
            _db_conn.execute("PRAGMA foreign_keys=ON")
            _db_conn.execute("PRAGMA busy_timeout=30000")
            _db_conn.execute("PRAGMA wal_autocheckpoint=1000")
            _db_conn.execute("PRAGMA synchronous=NORMAL")
            _db_conn.row_factory = sqlite3.Row
            _init_schema(_db_conn)
            _migrate_db(_db_conn)
            _backfill_fts(_db_conn)
            _migrate_sources(_db_conn)
    return _db_conn


def _init_schema(conn):
    """Initialize database schema - non-destructive."""
    cursor = conn.cursor()
    for schema in DB_SCHEMA.values():
        cursor.execute(schema)
    for idx in DB_INDEXES:
        try:
            cursor.execute(idx)
        except sqlite3.OperationalError:
            pass
    conn.commit()


def _migrate_db(conn):
    """Migrate existing database to new schema without deleting data."""
    cursor = conn.cursor()
    
    # Add crawl_delay column to sites if not exists
    try:
        cursor.execute("ALTER TABLE sites ADD COLUMN crawl_delay REAL DEFAULT 1.0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    
    # Add scheduled_at column to crawl_queue if not exists
    try:
        cursor.execute("ALTER TABLE crawl_queue ADD COLUMN scheduled_at INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    
    # Add seo_score column to pages if not exists
    try:
        cursor.execute("ALTER TABLE pages ADD COLUMN seo_score REAL DEFAULT 0.0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    
    # Add recursion_depth column to sitemaps_feeds if not exists
    try:
        cursor.execute("ALTER TABLE sitemaps_feeds ADD COLUMN recursion_depth INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    
    # Create pages_fts table if not exists
    try:
        cursor.execute(DB_SCHEMA['pages_fts'])
        conn.commit()
    except sqlite3.OperationalError:
        pass
    
    # Create update_status table if not exists
    try:
        cursor.execute(DB_SCHEMA['update_status'])
        conn.commit()
    except sqlite3.OperationalError:
        pass
    
    # Create site_sources table if not exists
    try:
        cursor.execute(DB_SCHEMA['site_sources'])
        conn.commit()
    except sqlite3.OperationalError:
        pass
    
    # Create triggers if not exists
    for trigger in DB_TRIGGERS:
        try:
            cursor.execute(trigger)
            conn.commit()
        except sqlite3.OperationalError:
            pass


def _backfill_fts(conn):
    """Backfill FTS5 table with existing pages data."""
    cursor = conn.cursor()
    
    # Check if pages_fts is empty
    cursor.execute("SELECT COUNT(*) FROM pages_fts")
    if cursor.fetchone()[0] == 0:
        # Copy data from pages to pages_fts
        cursor.execute("""
            INSERT INTO pages_fts(page_id, title, body_text, og_title, og_description, url, schema_type)
            SELECT id, title, body_text, og_title, og_description, url, schema_type FROM pages
        """)
        conn.commit()
        print("FTS5 table backfilled with existing pages data")


def _migrate_sources(conn):
    """Seed site_sources from existing sites and sitemaps_feeds (idempotent)."""
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO site_sources (site_id, url, source_type, priority, notes, last_checked, status)
            SELECT s.id, s.canonical_url, 'domain', 5, '', 0, 'active'
            FROM sites s
            WHERE NOT EXISTS (
                SELECT 1 FROM site_sources ss
                WHERE ss.site_id = s.id AND ss.source_type = 'domain'
            )
        """)
        cursor.execute("""
            INSERT INTO site_sources (site_id, url, source_type, priority, notes, last_checked, status)
            SELECT sf.site_id, sf.url,
                   CASE WHEN sf.type IN ('rss','atom') THEN sf.type ELSE 'sitemap' END,
                   CASE WHEN sf.type IN ('rss','atom') THEN 1 ELSE 3 END,
                   '', COALESCE(sf.last_checked, 0), 'active'
            FROM sitemaps_feeds sf
            WHERE NOT EXISTS (
                SELECT 1 FROM site_sources ss
                WHERE ss.site_id = sf.site_id AND ss.url = sf.url
            )
        """)
        conn.commit()
    except sqlite3.OperationalError as e:
        print(f"Source migration skipped: {e}")


def log_error(message, exc=None):
    """Append an error line to logs/errors.log (best effort)."""
    try:
        os.makedirs(os.path.dirname(ERROR_LOG_PATH) or '.', exist_ok=True)
        with open(ERROR_LOG_PATH, 'a', encoding='utf-8') as f:
            detail = f" | {type(exc).__name__}: {exc}" if exc is not None else ""
            f.write(f"{datetime.now().isoformat()} - {message}{detail}\n")
    except Exception:
        pass


def close_db():
    global _db_conn
    with _db_lock:
        if _db_conn is not None:
            _db_conn.close()
            _db_conn = None


def execute_db(query, params=(), commit=False):
    """Execute a query. Uses a single lock acquisition (no nested locking)."""
    conn = get_db()
    with _db_lock:
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            if commit:
                conn.commit()
            return cursor
        except sqlite3.OperationalError as e:
            if "locked" in str(e):
                time.sleep(0.1)
                cursor = conn.cursor()
                cursor.execute(query, params)
                if commit:
                    conn.commit()
                return cursor
            raise


def execute_db_fetchone(query, params=()):
    return execute_db(query, params).fetchone()


def execute_db_fetchall(query, params=()):
    return execute_db(query, params).fetchall()


def init_db():
    """Public function to initialize database (for setup.sh)."""
    get_db()
    print("Database initialized successfully")


# ============================================================================
# UPDATE SYSTEM
# ============================================================================

def _get_current_commit():
    """Get current commit SHA from file or git."""
    if os.path.exists(COMMIT_SHA_PATH):
        with open(COMMIT_SHA_PATH, 'r') as f:
            return f.read().strip()
    try:
        import subprocess
        result = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            sha = result.stdout.strip()
            with open(COMMIT_SHA_PATH, 'w') as f:
                f.write(sha)
            return sha
    except Exception:
        pass
    return "unknown"


def _get_latest_commit_from_github():
    """Get latest commit SHA from GitHub API."""
    try:
        url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/commits/{BRANCH}"
        resp = requests.get(url, timeout=10, headers={'User-Agent': USER_AGENT})
        if resp.status_code == 200:
            data = resp.json()
            return data.get('sha', '')
    except Exception as e:
        print(f"Error fetching latest commit: {e}")
    return ""


def check_for_update():
    """Check if there's a new version available on GitHub."""
    conn = get_db()
    cursor = conn.cursor()
    
    current_commit = _get_current_commit()
    latest_commit = _get_latest_commit_from_github()
    
    if not latest_commit:
        return False, "Cannot reach GitHub API"
    
    update_available = current_commit != latest_commit
    
    # Update database
    cursor.execute("""
        INSERT OR REPLACE INTO update_status 
        (id, last_check, current_commit, latest_commit, update_available, last_update_result)
        VALUES (1, strftime('%s','now'), ?, ?, ?, ?)
    """, (current_commit, latest_commit, 1 if update_available else 0, "OK"))
    conn.commit()
    
    # Update files
    with open(COMMIT_SHA_PATH, 'w') as f:
        f.write(latest_commit)
    
    with open(UPDATE_AVAILABLE_PATH, 'w') as f:
        f.write('1' if update_available else '0')
    
    # Log
    with open(UPDATE_LOG_PATH, 'a') as f:
        f.write(f"{datetime.now().isoformat()} - Check: current={current_commit}, latest={latest_commit}, available={update_available}\n")
    
    return update_available, f"Current: {current_commit[:8]}, Latest: {latest_commit[:8]}"


def get_update_status():
    result = execute_db_fetchone("SELECT * FROM update_status WHERE id=1")
    if result:
        status = dict(result)
        status['last_check_str'] = format_timestamp(status.get('last_check', 0))
        return status
    return {
        'last_check': 0,
        'last_check_str': 'Nikdy',
        'current_commit': _get_current_commit(),
        'latest_commit': '',
        'update_available': 0,
        'last_update_time': 0,
        'last_update_result': 'Not checked yet'
    }


def apply_update():
    """Apply update by pulling from GitHub."""
    import subprocess
    
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute("UPDATE update_status SET last_update_time=strftime('%s','now'), last_update_result='In progress' WHERE id=1")
        conn.commit()
        
        # Stop workers
        global SHUTDOWN_FLAG
        SHUTDOWN_FLAG = True
        time.sleep(2)
        
        # Git pull
        result = subprocess.run(
            ['git', 'pull', 'origin', BRANCH],
            capture_output=True, text=True, timeout=60, cwd=os.path.dirname(os.path.abspath(__file__))
        )
        
        if result.returncode == 0:
            # Restart application
            cursor.execute("UPDATE update_status SET last_update_result='Success - Restart required' WHERE id=1")
            conn.commit()
            
            with open(UPDATE_LOG_PATH, 'a') as f:
                f.write(f"{datetime.now().isoformat()} - Update: SUCCESS\n{result.stdout}\n")
            
            return True, "Update successful. Please restart the application."
        else:
            cursor.execute("UPDATE update_status SET last_update_result='Failed' WHERE id=1")
            conn.commit()
            
            with open(UPDATE_LOG_PATH, 'a') as f:
                f.write(f"{datetime.now().isoformat()} - Update: FAILED\n{result.stderr}\n")
            
            return False, f"Update failed: {result.stderr}"
            
    except Exception as e:
        cursor.execute("UPDATE update_status SET last_update_result='Error: ' || ? WHERE id=1", (str(e),))
        conn.commit()
        return False, f"Update error: {str(e)}"
    finally:
        SHUTDOWN_FLAG = False


# ============================================================================
# URL HELPERS
# ============================================================================

def normalize_url(url):
    if not url:
        return ""
    parsed = urlparse(url)
    if not parsed.scheme:
        url = f"https://{url}"
        parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path
    if path.endswith('/') and path != '/':
        path = path[:-1]
    return urlunparse((parsed.scheme, netloc, path, parsed.params, parsed.query, ''))


def get_domain(url):
    parsed = urlparse(url)
    if not parsed.scheme:
        url = f"https://{url}"
        parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def url_hash(url):
    return hashlib.md5(normalize_url(url).encode('utf-8')).hexdigest()


# ============================================================================
# ROBOTS.TXT
# ============================================================================

def _get_robots(base_url):
    """Return cached RobotFileParser for a domain."""
    domain = get_domain(base_url)
    with _robots_lock:
        if domain in _robots_cache:
            return _robots_cache[domain]
    robots_url = urljoin(base_url, '/robots.txt')
    rp = RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
    except Exception:
        pass
    with _robots_lock:
        _robots_cache[domain] = rp
    return rp


def is_allowed(url):
    """Return True if MiniSearchBot is allowed to fetch url."""
    try:
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        rp = _get_robots(base)
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


def get_crawl_delay(base_url):
    """Return crawl-delay for domain (minimum MIN_DELAY)."""
    domain = get_domain(base_url)
    with _domain_delay_lock:
        if domain in _domain_crawl_delay:
            return _domain_crawl_delay[domain]
    
    try:
        rp = _get_robots(base_url)
        delay = rp.crawl_delay(USER_AGENT) or rp.crawl_delay('*')
        if delay:
            delay = max(MIN_DELAY, float(delay))
        else:
            delay = MIN_DELAY
    except Exception:
        delay = MIN_DELAY
    
    with _domain_delay_lock:
        _domain_crawl_delay[domain] = delay
    return delay


def update_crawl_delay(site_id, delay):
    """Update crawl delay for a site."""
    execute_db(
        "UPDATE sites SET crawl_delay = ? WHERE id = ?",
        (delay, site_id), commit=True
    )


# ============================================================================
# SITE MANAGEMENT
# ============================================================================

def add_site(site_url, max_pages=500):
    site_url = normalize_url(site_url)
    if not site_url:
        return None
    domain = get_domain(site_url)
    result = execute_db_fetchone(
        "SELECT id, canonical_url, aliases FROM sites WHERE canonical_url = ? OR aliases LIKE ?",
        (domain, f"%{domain}%")
    )
    if result:
        site_id, existing_canonical, aliases_str = result
        aliases = json.loads(aliases_str) if aliases_str else []
        if site_url not in aliases and site_url != existing_canonical:
            aliases.append(site_url)
            execute_db(
                "UPDATE sites SET aliases = ? WHERE id = ?",
                (json.dumps(aliases), site_id), commit=True
            )
        return site_id
    
    crawl_delay = get_crawl_delay(site_url)
    execute_db(
        "INSERT INTO sites (canonical_url, aliases, status, max_pages, crawl_delay) VALUES (?, ?, 'active', ?, ?)",
        (domain, json.dumps([site_url]), max_pages, crawl_delay), commit=True
    )
    return execute_db_fetchone("SELECT last_insert_rowid()")[0]


def get_site_by_id(site_id):
    result = execute_db_fetchone("SELECT * FROM sites WHERE id = ?", (site_id,))
    return dict(result) if result else None


def format_timestamp(ts):
    if not ts or ts <= 0:
        return 'Nikdy'
    try:
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return 'Neznámý datum'


def get_all_sites():
    results = execute_db_fetchall("SELECT * FROM sites ORDER BY created_at DESC")
    sites = []
    for row in results:
        site = dict(row)
        site['last_crawled_str'] = format_timestamp(site['last_crawled'])
        site['indexed_count'] = execute_db_fetchone(
            "SELECT COUNT(*) FROM pages WHERE site_id = ?", (site['id'],)
        )[0]
        site['pending_count'] = execute_db_fetchone(
            "SELECT COUNT(*) FROM crawl_queue WHERE site_id = ? AND status = 'pending'", (site['id'],)
        )[0]
        sites.append(site)
    return sites


def delete_site(site_id):
    execute_db("DELETE FROM sites WHERE id = ?", (site_id,), commit=True)
    _rebuild_hnsw_index()


def get_db_stats():
    return {
        'sites': execute_db_fetchone("SELECT COUNT(*) FROM sites")[0],
        'pages': execute_db_fetchone("SELECT COUNT(*) FROM pages")[0],
        'pending': execute_db_fetchone("SELECT COUNT(*) FROM crawl_queue WHERE status='pending'")[0],
        'completed': execute_db_fetchone("SELECT COUNT(*) FROM crawl_queue WHERE status='completed'")[0],
        'errors': execute_db_fetchone("SELECT COUNT(*) FROM crawl_queue WHERE status='error'")[0],
    }

# ============================================================================
# SOURCE MANAGEMENT (domain / url / sitemap / feed)
# ============================================================================

def validate_url(value):
    """Return (ok, message). Accepts bare domains and full URLs."""
    if not value or not value.strip():
        return False, 'URL je povinná'
    candidate = value.strip()
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.\-]*://', candidate):
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme not in ('http', 'https'):
        return False, 'Povolena je pouze adresa http nebo https'
    host = parsed.hostname or ''
    if not host or '.' not in host or host.startswith('.') or host.endswith('.'):
        return False, 'Neplatný formát URL'
    if any(ch.isspace() for ch in host):
        return False, 'Neplatný formát URL'
    return True, ''


def validate_max_pages(value):
    """Return (ok, message, int_value)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return False, 'Max. stránek musí být číslo', None
    if number < MAX_PAGES_MIN or number > MAX_PAGES_MAX:
        return False, f'Max. stránek musí být {MAX_PAGES_MIN}–{MAX_PAGES_MAX}', None
    return True, '', number


def validate_priority(value):
    """Return (ok, message, int_value)."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return False, 'Priorita musí být číslo', None
    if number < 1 or number > 10:
        return False, 'Priorita musí být 1–10', None
    return True, '', number


def detect_source_type(url, fallback='url'):
    """Guess source_type from URL shape."""
    lower = (url or '').lower()
    if lower.endswith('.xml') or lower.endswith('.xml.gz') or 'sitemap' in lower:
        return 'sitemap'
    if (lower.endswith('.rss') or lower.endswith('.atom') or '/rss' in lower
            or '/feed' in lower or '/atom' in lower or 'feed' in lower):
        return 'rss'
    return fallback


def get_site_sources(site_id):
    rows = execute_db_fetchall(
        "SELECT * FROM site_sources WHERE site_id = ? ORDER BY source_type, priority, id",
        (site_id,)
    )
    sources = []
    for row in rows:
        source = dict(row)
        source['last_checked_str'] = format_timestamp(source.get('last_checked', 0))
        sources.append(source)
    return sources


def get_source_by_id(source_id):
    row = execute_db_fetchone("SELECT * FROM site_sources WHERE id = ?", (source_id,))
    return dict(row) if row else None


def add_source(site_id, url, source_type='url', priority=5, notes=''):
    """Add a source under a site. Returns (source_id, error_message)."""
    if source_type not in VALID_SOURCE_TYPES:
        return None, f'Nepodporovaný typ zdroje: {source_type}'

    ok, message = validate_url(url)
    if not ok:
        return None, message

    ok, message, priority = validate_priority(priority)
    if not ok:
        return None, message

    if source_type == 'domain':
        normalized = normalize_url(url)
        domain = get_domain(normalized)
        existing = execute_db_fetchone(
            "SELECT id FROM sites WHERE canonical_url = ?", (domain,)
        )
        if existing:
            site_id = existing[0]
        else:
            site_id = add_site(normalized, 500)
            if not site_id:
                return None, 'Doménu nebylo možné vytvořit'

    normalized = normalize_url(url)
    if source_type == 'domain':
        normalized = get_domain(normalized)

    duplicate = execute_db_fetchone(
        "SELECT id FROM site_sources WHERE site_id = ? AND url = ?",
        (site_id, normalized)
    )
    if duplicate:
        return None, 'Tento zdroj je již pod doménou zaregistrován'

    execute_db(
        """INSERT INTO site_sources (site_id, url, source_type, priority, notes, last_checked, status)
           VALUES (?, ?, ?, ?, ?, 0, 'active')""",
        (site_id, normalized, source_type, priority, notes or ''), commit=True
    )
    source_id = execute_db_fetchone("SELECT last_insert_rowid()")[0]

    if source_type in ('sitemap', 'feed', 'rss', 'atom'):
        _mirror_source_to_legacy(site_id, normalized, source_type)

    return source_id, None


def _mirror_source_to_legacy(site_id, url, source_type):
    """Keep sitemaps_feeds in sync so existing schedulers keep working."""
    legacy_type = source_type if source_type in ('rss', 'atom') else 'sitemap'
    try:
        existing = execute_db_fetchone(
            "SELECT id FROM sitemaps_feeds WHERE site_id = ? AND url = ?",
            (site_id, url)
        )
        if not existing:
            execute_db(
                "INSERT INTO sitemaps_feeds (site_id, url, type, last_checked) VALUES (?, ?, ?, 0)",
                (site_id, url, legacy_type), commit=True
            )
    except Exception as e:
        log_error(f"Could not mirror source {url} to sitemaps_feeds", e)


def update_source(source_id, **kwargs):
    """Update fields of a source. Returns (ok, message)."""
    source = get_source_by_id(source_id)
    if not source:
        return False, 'Zdroj nenalezen'

    updates = []
    params = []

    if kwargs.get('url') is not None:
        ok, message = validate_url(kwargs['url'])
        if not ok:
            return False, message
        new_url = normalize_url(kwargs['url'])
        if source['source_type'] == 'domain':
            new_url = get_domain(new_url)
        if new_url != source['url']:
            duplicate = execute_db_fetchone(
                "SELECT id FROM site_sources WHERE site_id = ? AND url = ? AND id != ?",
                (source['site_id'], new_url, source_id)
            )
            if duplicate:
                return False, 'Tento zdroj je již pod doménou zaregistrován'
        updates.append("url = ?")
        params.append(new_url)

    if kwargs.get('source_type') is not None:
        if kwargs['source_type'] not in VALID_SOURCE_TYPES:
            return False, f"Nepodporovaný typ zdroje: {kwargs['source_type']}"
        updates.append("source_type = ?")
        params.append(kwargs['source_type'])

    if kwargs.get('priority') is not None:
        ok, message, priority = validate_priority(kwargs['priority'])
        if not ok:
            return False, message
        updates.append("priority = ?")
        params.append(priority)

    if kwargs.get('notes') is not None:
        updates.append("notes = ?")
        params.append(str(kwargs['notes']))

    if kwargs.get('status') is not None:
        if kwargs['status'] not in VALID_SITE_STATUSES:
            return False, 'Neplatný stav zdroje'
        updates.append("status = ?")
        params.append(kwargs['status'])

    if kwargs.get('max_pages') is not None:
        ok, message, max_pages = validate_max_pages(kwargs['max_pages'])
        if not ok:
            return False, message
        execute_db(
            "UPDATE sites SET max_pages = ? WHERE id = ?",
            (max_pages, source['site_id']), commit=True
        )

    if not updates:
        return True, 'Nic ke změně'

    params.append(source_id)
    try:
        execute_db(
            f"UPDATE site_sources SET {', '.join(updates)} WHERE id = ?",
            tuple(params), commit=True
        )
    except sqlite3.IntegrityError as e:
        log_error(f"Update source {source_id} failed", e)
        return False, 'Aktualizace zdroje selhala'

    if kwargs.get('url') is not None:
        _sync_legacy_source(source, kwargs['url'])
    return True, 'Zdroj byl upraven'


def _sync_legacy_source(source, new_url):
    try:
        execute_db(
            "UPDATE sitemaps_feeds SET url = ? WHERE site_id = ? AND url = ?",
            (normalize_url(new_url), source['site_id'], source['url']), commit=True
        )
    except Exception as e:
        log_error(f"Could not sync legacy source {source['id']}", e)


def delete_source(source_id):
    """Delete a source. Domains delete the whole site (cascade)."""
    source = get_source_by_id(source_id)
    if not source:
        return False, 'Zdroj nenalezen'
    if source['source_type'] == 'domain':
        delete_site(source['site_id'])
        return True, 'Doména byla smazána'
    execute_db("DELETE FROM site_sources WHERE id = ?", (source_id,), commit=True)
    execute_db(
        "DELETE FROM sitemaps_feeds WHERE site_id = ? AND url = ?",
        (source['site_id'], source['url']), commit=True
    )
    return True, 'Zdroj byl smazán'


def get_source_stats(site_id):
    """Aggregate stats for a site (used by cards and detail view)."""
    indexed = execute_db_fetchone(
        "SELECT COUNT(*) FROM pages WHERE site_id = ?", (site_id,)
    )[0]
    pending = execute_db_fetchone(
        "SELECT COUNT(*) FROM crawl_queue WHERE site_id = ? AND status = 'pending'",
        (site_id,)
    )[0]
    errors = execute_db_fetchone(
        "SELECT COUNT(*) FROM crawl_queue WHERE site_id = ? AND status = 'error'",
        (site_id,)
    )[0]
    sources = execute_db_fetchone(
        "SELECT COUNT(*) FROM site_sources WHERE site_id = ?", (site_id,)
    )[0]
    return {
        'indexed': indexed,
        'pending': pending,
        'errors': errors,
        'sources': sources,
        'total': indexed + pending,
    }


def update_site(site_id, **kwargs):
    """Update editable site fields. Returns (ok, message)."""
    site = get_site_by_id(site_id)
    if not site:
        return False, 'Web nenalezen'

    updates = []
    params = []

    if kwargs.get('max_pages') is not None:
        ok, message, max_pages = validate_max_pages(kwargs['max_pages'])
        if not ok:
            return False, message
        updates.append("max_pages = ?")
        params.append(max_pages)

    if kwargs.get('status') is not None:
        status = kwargs['status']
        if status not in VALID_SITE_STATUSES:
            return False, 'Neplatný stav webu'
        updates.append("status = ?")
        params.append(status)

    if kwargs.get('aliases') is not None:
        aliases = kwargs['aliases']
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.split(',') if a.strip()]
        updates.append("aliases = ?")
        params.append(json.dumps(aliases))

    if not updates:
        return True, 'Nic ke změně'

    params.append(site_id)
    execute_db(f"UPDATE sites SET {', '.join(updates)} WHERE id = ?", tuple(params), commit=True)
    return True, 'Web byl upraven'


def pause_site(site_id):
    return update_site(site_id, status='paused')


def resume_site(site_id):
    return update_site(site_id, status='active')


def get_all_sources():
    """Flat list of every source enriched with its site (for API)."""
    rows = execute_db_fetchall("""
        SELECT ss.*, s.canonical_url AS site_domain, s.status AS site_status,
               s.max_pages AS max_pages
        FROM site_sources ss
        JOIN sites s ON ss.site_id = s.id
        ORDER BY s.created_at DESC, ss.source_type, ss.priority
    """)
    result = []
    for row in rows:
        item = dict(row)
        item['last_checked_str'] = format_timestamp(item.get('last_checked', 0))
        result.append(item)
    return result


def get_recent_pages(site_id, limit=10):
    rows = execute_db_fetchall(
        """SELECT id, url, og_title, title, schema_type, indexed_at, seo_score
           FROM pages WHERE site_id = ? ORDER BY indexed_at DESC LIMIT ?""",
        (site_id, limit)
    )
    pages = []
    for row in rows:
        page = dict(row)
        page['title'] = page['og_title'] or page['title'] or page['url']
        page['indexed_at_str'] = format_timestamp(page['indexed_at'])
        pages.append(page)
    return pages


def get_filtered_sites(search='', status='', source_type=''):
    """Return sites with stats, optionally filtered."""
    query = "SELECT * FROM sites WHERE 1=1"
    params = []
    if search:
        query += " AND (canonical_url LIKE ? OR aliases LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    if status:
        query += " AND status = ?"
        params.append(status)
    if source_type:
        query += " AND id IN (SELECT site_id FROM site_sources WHERE source_type = ?)"
        params.append(source_type)
    query += " ORDER BY created_at DESC"

    sites = []
    for row in execute_db_fetchall(query, tuple(params)):
        site = dict(row)
        site['last_crawled_str'] = format_timestamp(site['last_crawled'])
        stats = get_source_stats(site['id'])
        site.update(stats)
        site['indexed_count'] = stats['indexed']
        site['pending_count'] = stats['pending']
        try:
            site['aliases_list'] = json.loads(site.get('aliases') or '[]')
        except Exception:
            site['aliases_list'] = []
        sites.append(site)
    return sites


def recrawl_site(site_id):
    """Queue every domain/url/sitemap/feed source belonging to a site."""
    site = get_site_by_id(site_id)
    if not site:
        return False, 'Web nenalezen'
    try:
        site_id_result, added = phase_1_discovery(site['canonical_url'], site['max_pages'])
        return True, f"Re-crawl zahájen ({added} URL ve frontě)"
    except Exception as e:
        log_error(f"Recrawl failed for site {site_id}", e)
        return False, f"Re-crawl selhal: {e}"

# ============================================================================
# VECTOR SEARCH (hnswlib)
# ============================================================================

def _init_hnsw(dim=384):
    """Create a fresh hnswlib index with given dimension."""
    global _hnsw_index
    idx = hnswlib.Index(space='cosine', dim=dim)
    idx.init_index(max_elements=100000, ef_construction=200, M=16)
    idx.set_ef(50)
    _hnsw_index = idx
    return idx


def get_hnsw_index():
    """Return (or create+populate) global hnswlib index."""
    global _hnsw_index
    if _hnsw_index is None:
        dim = get_model().get_sentence_embedding_dimension()
        _init_hnsw(dim)
        _load_embeddings_into_index()
    return _hnsw_index


def _load_embeddings_into_index():
    """Load all stored embeddings from SQLite into _hnsw_index."""
    rows = execute_db_fetchall("SELECT id, embedding FROM pages WHERE embedding IS NOT NULL")
    if not rows:
        return
    ids, vecs = [], []
    for row in rows:
        try:
            emb = np.frombuffer(row[1], dtype=np.float32)
            if emb.size > 0:
                ids.append(row[0])
                vecs.append(emb)
        except Exception:
            pass
    if vecs:
        _hnsw_index.add_items(np.array(vecs), np.array(ids))


def _rebuild_hnsw_index():
    """Rebuild index from scratch (called after delete)."""
    global _hnsw_index
    if _hnsw_index is not None:
        dim = _hnsw_index.dim
        _init_hnsw(dim)
    else:
        dim = get_model().get_sentence_embedding_dimension()
        _init_hnsw(dim)
    _load_embeddings_into_index()


def get_model():
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
        except Exception as e:
            print(f"Model nelze nacist: {e}")
            class DummyModel:
                def get_sentence_embedding_dimension(self): return 384
                def encode(self, text): return np.zeros(384, dtype=np.float32)
            _model = DummyModel()
    return _model


def generate_embedding(text):
    try:
        return get_model().encode(text[:1000])
    except Exception:
        return np.zeros(384, dtype=np.float32)


def _jaccard(a, b):
    """Jaccard similarity on word sets of two strings."""
    sa = set(a.lower().split())
    sb = set(b.lower().split())
    if not sa and not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def calculate_seo_score(page_data):
    """Calculate SEO score for a page (0-100)."""
    score = 0.0
    
    # Title presence
    if page_data.get('og_title') or page_data.get('title'):
        score += 10
    
    # Description presence
    if page_data.get('og_description'):
        score += 10
    
    # Image presence
    if page_data.get('og_image'):
        score += 5
    
    # Schema markup
    if page_data.get('schema_type'):
        score += 15
    
    # Audio content
    if page_data.get('has_audio') == 1:
        score += 10
    
    # Published date
    if page_data.get('published_timestamp', 0) > 0:
        score += 5
    
    # Body text length
    body_len = len(page_data.get('body_text', ''))
    if body_len > 100:
        score += min(20, body_len / 50)
    
    # Recent content
    if page_data.get('published_timestamp', 0) > int((datetime.now() - timedelta(days=30)).timestamp()):
        score += 10
    
    return min(100.0, score)


def hybrid_search(query, limit=25, filter_type=None):
    """Hybrid search: 60% vector + 35% FTS5 + 5% SEO."""
    # Vector search (60%)
    vector_results = vector_search(query, limit=limit * 2, filter_type=None)
    
    # FTS5 search (35%)
    fts_results = fts_search(query, limit=limit * 2)
    
    # Combine results
    combined = {}
    
    # Add vector results
    for i, result in enumerate(vector_results):
        result_id = result['id']
        if result_id not in combined:
            combined[result_id] = {
                'page': result,
                'vector_score': (1 - (i / (len(vector_results) + 1))) * 60
            }
    
    # Add FTS5 results
    for i, result in enumerate(fts_results):
        result_id = result['id']
        if result_id not in combined:
            combined[result_id] = {
                'page': result,
                'vector_score': 0,
                'fts_score': (1 - (i / (len(fts_results) + 1))) * 35
            }
        else:
            combined[result_id]['fts_score'] = (1 - (i / (len(fts_results) + 1))) * 35
    
    # Calculate final scores
    final_results = []
    for result_id, data in combined.items():
        page = data['page']
        vector_score = data.get('vector_score', 0)
        fts_score = data.get('fts_score', 0)
        seo_score = page.get('seo_score', 0) * 0.05  # 5%
        
        final_score = vector_score + fts_score + seo_score
        page['relevance'] = round(final_score, 1)
        final_results.append(page)
    
    # Sort by final score
    final_results.sort(key=lambda x: x['relevance'], reverse=True)
    
    # Apply filter
    if filter_type == 'articles':
        final_results = [r for r in final_results if r.get('schema_type') in ('Article', 'BlogPosting', 'NewsArticle')]
    elif filter_type == 'podcasts':
        final_results = [r for r in final_results if r.get('schema_type') == 'PodcastEpisode']
    elif filter_type == 'audio':
        final_results = [r for r in final_results if r.get('has_audio') == 1]
    elif filter_type == 'price':
        final_results = [r for r in final_results if 'price' in json.loads(r.get('schema_details') or '{}')]
    
    # Deduplication
    seen_texts = []
    deduplicated = []
    for r in final_results:
        snippet = (r.get('body_text') or '')[:300]
        if not any(_jaccard(snippet, seen) > 0.9 for seen in seen_texts):
            seen_texts.append(snippet)
            deduplicated.append(r)
        if len(deduplicated) >= limit:
            break
    
    return deduplicated


def vector_search(query, limit=25, filter_type=None):
    """Search with hnswlib."""
    idx = get_hnsw_index()
    if idx.element_count == 0:
        return []

    query_embedding = generate_embedding(query)
    k = min(limit * 3, idx.element_count)
    labels, distances = idx.knn_query(query_embedding, k=k)

    results = []
    seen_texts = []

    for label, distance in zip(labels[0], distances[0]):
        if len(results) >= limit:
            break
        if label < 0:
            continue
        page = execute_db_fetchone("SELECT * FROM pages WHERE id = ?", (int(label),))
        if not page:
            continue
        page = dict(page)

        relevance = (1 - distance) * 100
        if query.lower() in (page.get('og_title', '') + page.get('title', '')).lower():
            relevance += 10
        if page.get('schema_type'):
            relevance += 5
        if page.get('published_timestamp', 0) > int((datetime.now() - timedelta(days=30)).timestamp()):
            relevance += 3

        page['relevance'] = round(relevance, 1)

        page['published_date'] = format_timestamp(page.get('published_timestamp', 0))

        snippet = (page.get('body_text') or '')[:300]
        if any(_jaccard(snippet, seen) > 0.9 for seen in seen_texts):
            continue
        seen_texts.append(snippet)

        results.append(page)

    if filter_type == 'articles':
        results = [r for r in results if r.get('schema_type') in ('Article', 'BlogPosting', 'NewsArticle')]
    elif filter_type == 'podcasts':
        results = [r for r in results if r.get('schema_type') == 'PodcastEpisode']
    elif filter_type == 'audio':
        results = [r for r in results if r.get('has_audio') == 1]
    elif filter_type == 'price':
        results = [r for r in results if 'price' in json.loads(r.get('schema_details') or '{}')]

    return results


def fts_search(query, limit=25):
    """Search using FTS5 full-text search."""
    results = []
    try:
        # Use FTS5 with unicode61 tokenizer for Czech support
        rows = execute_db_fetchall("""
            SELECT p.* FROM pages_fts fts
            JOIN pages p ON fts.page_id = p.id
            WHERE pages_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit * 2))
        
        for row in rows:
            page = dict(row)
            # Calculate relevance from FTS5 rank
            # For simplicity, use position in results as proxy
            results.append(page)
    except Exception as e:
        print(f"FTS5 search error: {e}")
    
    return results


# ============================================================================
# CRAWLING
# ============================================================================

def discover_sitemaps_and_feeds(site_url, site_id, max_pages):
    """Phase 1: detect sitemaps/feeds via GET with robots.txt check."""
    parsed = urlparse(site_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    # Check robots.txt first
    if not is_allowed(site_url):
        print(f"Robots.txt disallows crawling {site_url}")
        return []

    # Collect candidate URLs
    candidate_urls = []
    try:
        resp = requests.get(urljoin(base_url, '/robots.txt'), timeout=10,
                            headers={'User-Agent': USER_AGENT})
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                if line.lower().startswith('sitemap:'):
                    sitemap_url = line.split(':', 1)[1].strip()
                    candidate_urls.append(sitemap_url)
    except Exception:
        pass

    for path in [
        '/sitemap.xml', '/sitemap_index.xml', '/sitemap.xml.gz',
        '/feed', '/rss', '/atom.xml', '/feed.xml', '/rss.xml',
        '/feed/rss', '/feed/atom', '/rss2.0.xml', '/rdf.xml'
    ]:
        u = urljoin(base_url, path)
        if u not in candidate_urls:
            candidate_urls.append(u)

    discovered = []
    for url in candidate_urls:
        if len(discovered) >= max_pages:
            break
        try:
            # Check robots.txt for this URL
            if not is_allowed(url):
                continue
                
            resp = requests.get(url, timeout=10, headers={'User-Agent': USER_AGENT})
            if resp.status_code != 200:
                continue
            ct = resp.headers.get('Content-Type', '')
            body_start = resp.text[:500] if isinstance(resp.text, str) else resp.content[:500].decode('utf-8', errors='ignore')
            
            # Handle gzip content
            if ct == 'application/gzip' or url.endswith('.gz'):
                try:
                    body_start = gzip.decompress(resp.content[:1000]).decode('utf-8', errors='ignore')[:500]
                except Exception:
                    pass
            
            if '<urlset' in body_start or '<sitemapindex' in body_start or 'xml' in ct:
                item_type = 'sitemap'
            elif '<rss' in body_start or '<feed' in body_start or 'rss' in ct or 'atom' in ct:
                item_type = 'rss' if '<rss' in body_start else 'atom'
            else:
                continue
            discovered.append({'url': url, 'type': item_type})
        except Exception:
            pass

    for item in discovered:
        try:
            execute_db(
                "INSERT OR IGNORE INTO sitemaps_feeds (site_id, url, type) VALUES (?, ?, ?)",
                (site_id, item['url'], item['type']), commit=True
            )
        except Exception:
            pass
    return discovered


def parse_sitemap(url, max_depth=MAX_SITEMAP_RECURSION, current_depth=0):
    """Parse sitemap with gzip support and recursion limit."""
    if current_depth > max_depth:
        return []
    
    urls = []
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={'User-Agent': USER_AGENT})
        if resp.status_code != 200:
            return []
        
        content = resp.content
        ct = resp.headers.get('Content-Type', '')
        
        # Handle gzip
        if ct == 'application/gzip' or url.endswith('.gz'):
            try:
                content = gzip.decompress(content)
            except Exception:
                pass
        
        soup = BeautifulSoup(content, 'lxml')
        if soup.find('sitemapindex'):
            for s in soup.find_all('sitemap'):
                loc = s.find('loc')
                if loc:
                    urls.extend(parse_sitemap(loc.text, max_depth, current_depth + 1))
        else:
            for u in soup.find_all('url'):
                loc = u.find('loc')
                if loc:
                    urls.append(loc.text)
    except Exception as e:
        print(f"Error parsing sitemap {url}: {e}")
    
    return urls[:MAX_SITEMAP_URLS]


def parse_feed(url):
    urls = []
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries:
            if hasattr(entry, 'link'):
                urls.append(entry.link)
            elif hasattr(entry, 'links') and entry.links:
                urls.append(entry.links[0].get('href', ''))
    except Exception:
        pass
    return urls


def crawl_homepage_for_links(site_url, max_pages):
    urls = []
    domain = get_domain(site_url)
    try:
        if not is_allowed(site_url):
            return []
            
        resp = requests.get(site_url, timeout=REQUEST_TIMEOUT, headers={'User-Agent': USER_AGENT})
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, 'lxml')
            for a in soup.find_all('a', href=True):
                full_url = normalize_url(urljoin(site_url, a['href']))
                if get_domain(full_url) == domain and full_url not in urls:
                    urls.append(full_url)
                if len(urls) >= max_pages:
                    break
    except Exception:
        pass
    return urls


def _queue_url(site_id, url, priority):
    """Add URL to queue if allowed by robots.txt and not already queued/indexed."""
    if not url or not is_allowed(url):
        return
    
    # Check if already indexed
    url_h = url_hash(url)
    existing_page = execute_db_fetchone("SELECT id FROM pages WHERE url_hash=?", (url_h,))
    if existing_page:
        return
    
    # Check if already in queue
    existing = execute_db_fetchone("SELECT id FROM crawl_queue WHERE url = ?", (url,))
    if existing:
        return
    
    try:
        execute_db(
            "INSERT INTO crawl_queue (site_id, url, status, priority, scheduled_at) VALUES (?, ?, 'pending', ?, 0)",
            (site_id, url, priority), commit=True
        )
    except Exception:
        pass


def phase_1_discovery(site_url, max_pages=500):
    site_url = normalize_url(site_url)
    if not site_url:
        return None, 0
    
    # Check if site already exists
    domain = get_domain(site_url)
    existing = execute_db_fetchone("SELECT id FROM sites WHERE canonical_url = ?", (domain,))
    if existing:
        site_id = existing[0]
        execute_db("DELETE FROM crawl_queue WHERE site_id = ?", (site_id,), commit=True)
    else:
        site_id = add_site(site_url, max_pages)
        if not site_id:
            return None, 0
    
    # Update site max_pages
    execute_db("UPDATE sites SET max_pages = ? WHERE id = ?", (max_pages, site_id), commit=True)

    # Ensure a domain source row exists for this site
    if not execute_db_fetchone(
        "SELECT id FROM site_sources WHERE site_id = ? AND source_type = 'domain'", (site_id,)
    ):
        try:
            execute_db(
                """INSERT INTO site_sources (site_id, url, source_type, priority, notes, status)
                   VALUES (?, ?, 'domain', 5, '', 'active')""",
                (site_id, domain), commit=True
            )
        except Exception as e:
            log_error(f"Could not create domain source for site {site_id}", e)

    # Manually registered sources take priority over auto-discovery
    manual_sources = execute_db_fetchall(
        "SELECT id, url, source_type, priority FROM site_sources "
        "WHERE site_id = ? AND source_type != 'domain' AND status = 'active'",
        (site_id,)
    )
    for source_row in manual_sources:
        source_id, source_url, source_type, source_priority = source_row
        try:
            if source_type == 'url':
                _queue_url(site_id, normalize_url(source_url), source_priority or 5)
            elif source_type == 'sitemap':
                for u in parse_sitemap(source_url):
                    n = normalize_url(u)
                    if n:
                        _queue_url(site_id, n, 3)
            elif source_type in FEED_SOURCE_TYPES:
                for u in parse_feed(source_url):
                    n = normalize_url(u)
                    if n:
                        _queue_url(site_id, n, 1)
            execute_db(
                "UPDATE site_sources SET last_checked = strftime('%s','now') WHERE id = ?",
                (source_id,), commit=True
            )
        except Exception as e:
            log_error(f"Manual source {source_url} failed", e)

    discovered = discover_sitemaps_and_feeds(site_url, site_id, max_pages)
    urls_to_add = []

    for item in discovered:
        if item['type'] == 'sitemap':
            for u in parse_sitemap(item['url']):
                n = normalize_url(u)
                if n:
                    urls_to_add.append((n, 3))
        elif item['type'] in ('rss', 'atom'):
            for u in parse_feed(item['url']):
                n = normalize_url(u)
                if n:
                    urls_to_add.append((n, 1))

    if not urls_to_add:
        for u in crawl_homepage_for_links(site_url, max_pages):
            urls_to_add.append((u, 5))

    added = 0
    for url, priority in urls_to_add:
        if len(urls_to_add) > max_pages:
            break
        _queue_url(site_id, url, priority)
        added += 1

    execute_db(
        "UPDATE sites SET last_crawled = strftime('%s','now') WHERE id = ?",
        (site_id,), commit=True
    )
    return site_id, added


def extract_page_content(url, site_id):
    page_data = {
        'url': url, 'site_id': site_id, 'url_hash': url_hash(url),
        'title': '', 'og_title': '', 'og_description': '', 'og_image': '',
        'favicon_url': '', 'body_text': '', 'images': [],
        'schema_type': '', 'schema_details': {}, 'audio_url': '',
        'has_audio': 0, 'published_timestamp': 0, 'seo_score': 0.0
    }
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={'User-Agent': USER_AGENT})
        if resp.status_code != 200:
            return None
        content = resp.content
        soup = BeautifulSoup(content, 'lxml')

        def meta(prop):
            tag = soup.find('meta', attrs={'property': prop}) or soup.find('meta', attrs={'name': prop})
            return tag.get('content', '') if tag else ''

        page_data['og_title'] = meta('og:title')
        page_data['og_description'] = meta('og:description')
        page_data['og_image'] = meta('og:image')
        t = soup.find('title')
        page_data['title'] = t.text.strip() if t else ''
        if not page_data['og_title']:
            page_data['og_title'] = page_data['title']

        fav = soup.find('link', rel='icon') or soup.find('link', rel='shortcut icon')
        page_data['favicon_url'] = urljoin(url, fav['href']) if (fav and fav.get('href')) else urljoin(url, '/favicon.ico')

        body_parts = []
        for sel in ['article', 'main', 'p', 'h1', 'h2', 'h3']:
            for el in soup.select(sel):
                text = el.get_text().strip()
                if text:
                    body_parts.append(text)
        page_data['body_text'] = ' '.join(body_parts)[:3500]

        page_data['images'] = [
            {'url': urljoin(url, img['src']), 'alt': img.get('alt', '')}
            for img in soup.find_all('img', src=True)
        ]

        audio = soup.find('audio', src=True)
        if audio:
            page_data['audio_url'] = urljoin(url, audio['src'])
            page_data['has_audio'] = 1
        else:
            for a in soup.find_all('a', href=True):
                if any(a['href'].lower().endswith(ext) for ext in ('.mp3', '.m4a', '.wav', '.ogg')):
                    page_data['audio_url'] = urljoin(url, a['href'])
                    page_data['has_audio'] = 1
                    break

        try:
            data = extruct.extract(content, uniform=True)
            for schema in data.get('json-ld', []):
                if isinstance(schema, dict) and '@type' in schema:
                    page_data['schema_type'] = schema['@type']
                    page_data['schema_details'] = schema
                    dp = schema.get('datePublished', '')
                    if dp:
                        try:
                            page_data['published_timestamp'] = int(datetime.fromisoformat(dp[:10]).timestamp())
                        except Exception:
                            try:
                                page_data['published_timestamp'] = int(datetime.strptime(dp[:19], '%Y-%m-%dT%H:%M:%S').timestamp())
                            except Exception:
                                pass
                    break
        except Exception:
            pass

        # Calculate SEO score
        page_data['seo_score'] = calculate_seo_score(page_data)

        embed_text = (
            (page_data['og_title'] or page_data['title']) + ' ' +
            page_data['og_description'] + ' ' + page_data['body_text']
        )
        page_data['embedding'] = generate_embedding(embed_text).tobytes()
        return page_data
    except Exception as e:
        print(f"Error extracting {url}: {e}")
        return None


def process_url(queue_id, site_id, url):
    """Fetch and index one URL. Handles 429/403 at HTTP level."""
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={'User-Agent': USER_AGENT})

        if resp.status_code == 429:
            retry_after = int(resp.headers.get('Retry-After', 60))
            execute_db(
                "UPDATE crawl_queue SET status='pending', locked_by='', scheduled_at=? WHERE id=?",
                (int(time.time()) + retry_after, queue_id), commit=True
            )
            time.sleep(min(retry_after, 300))
            return

        if resp.status_code == 403:
            execute_db(
                "UPDATE sites SET status='blocked' WHERE id=?",
                (site_id,), commit=True
            )
            execute_db(
                "UPDATE crawl_queue SET status='error', error_reason='403 Forbidden' WHERE id=?",
                (queue_id,), commit=True
            )
            return

        if resp.status_code != 200:
            execute_db(
                "UPDATE crawl_queue SET retry_count=retry_count+1, status='pending', locked_by='' WHERE id=?",
                (queue_id,), commit=True
            )
            retry = execute_db_fetchone("SELECT retry_count FROM crawl_queue WHERE id=?", (queue_id,))
            if retry and retry[0] >= MAX_RETRIES:
                execute_db(
                    "UPDATE crawl_queue SET status='error', error_reason=? WHERE id=?",
                    (f"HTTP {resp.status_code}", queue_id), commit=True
                )
            return

        page_data = extract_page_content(url, site_id)
        if page_data is None:
            execute_db(
                "UPDATE crawl_queue SET retry_count=retry_count+1, status='pending', locked_by='' WHERE id=?",
                (queue_id,), commit=True
            )
            return

        # Check if already indexed (race condition)
        existing = execute_db_fetchone("SELECT id FROM pages WHERE url_hash=?", (page_data['url_hash'],))
        if existing:
            execute_db(
                """UPDATE pages SET title=?,og_title=?,og_description=?,og_image=?,
                   favicon_url=?,body_text=?,images=?,schema_type=?,schema_details=?,
                   audio_url=?,has_audio=?,published_timestamp=?,embedding=?,seo_score=?,
                   indexed_at=strftime('%s','now') WHERE url_hash=?""",
                (page_data['title'], page_data['og_title'], page_data['og_description'],
                 page_data['og_image'], page_data['favicon_url'], page_data['body_text'],
                 json.dumps(page_data['images']), page_data['schema_type'],
                 json.dumps(page_data['schema_details']), page_data['audio_url'],
                 page_data['has_audio'], page_data['published_timestamp'],
                 page_data['embedding'], page_data['seo_score'], page_data['url_hash']),
                commit=True
            )
            page_id = existing[0]
        else:
            execute_db(
                """INSERT INTO pages (site_id,url,url_hash,title,og_title,og_description,og_image,
                   favicon_url,body_text,images,schema_type,schema_details,audio_url,has_audio,
                   published_timestamp,embedding,seo_score,indexed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,strftime('%s','now'))""",
                (page_data['site_id'], page_data['url'], page_data['url_hash'],
                 page_data['title'], page_data['og_title'], page_data['og_description'],
                 page_data['og_image'], page_data['favicon_url'], page_data['body_text'],
                 json.dumps(page_data['images']), page_data['schema_type'],
                 json.dumps(page_data['schema_details']), page_data['audio_url'],
                 page_data['has_audio'], page_data['published_timestamp'], page_data['embedding'],
                 page_data['seo_score']),
                commit=True
            )
            page_id = execute_db_fetchone("SELECT last_insert_rowid()")[0]

        idx = get_hnsw_index()
        emb = np.frombuffer(page_data['embedding'], dtype=np.float32)
        idx.add_items(emb.reshape(1, -1), np.array([page_id]))

        execute_db(
            "UPDATE crawl_queue SET status='completed' WHERE id=?",
            (queue_id,), commit=True
        )

    except Exception as e:
        execute_db(
            "UPDATE crawl_queue SET status='error', error_reason=? WHERE id=?",
            (str(e), queue_id), commit=True
        )


# ============================================================================
# WORKERS
# ============================================================================

def _worker_loop(name):
    print(f"Worker {name} spusten")
    while not SHUTDOWN_FLAG:
        try:
            # Get domain crawl delay
            domain_delay = MIN_DELAY
            
            rows = execute_db_fetchall(
                """SELECT cq.id, cq.site_id, cq.url, s.crawl_delay 
                   FROM crawl_queue cq
                   JOIN sites s ON cq.site_id = s.id
                   WHERE cq.status='pending' AND cq.retry_count<? 
                   ORDER BY cq.priority ASC, cq.scheduled_at ASC LIMIT 3""",
                (MAX_RETRIES,)
            )
            if not rows:
                time.sleep(5)
                continue
            
            for row_id, site_id, url, site_delay in rows:
                domain_delay = max(MIN_DELAY, site_delay or MIN_DELAY)
                execute_db(
                    "UPDATE crawl_queue SET status='locked',locked_by=?, scheduled_at=0 WHERE id=?",
                    (name, row_id), commit=True
                )
            
            for row_id, site_id, url, site_delay in rows:
                if SHUTDOWN_FLAG:
                    break
                process_url(row_id, site_id, url)
                time.sleep(domain_delay)
                
        except Exception as e:
            print(f"Worker {name} error: {e}")
            time.sleep(5)


def start_workers():
    global _worker_threads
    for name in ('worker_a', 'worker_b'):
        t = threading.Thread(target=_worker_loop, args=(name,), daemon=True)
        t.start()
        _worker_threads.append(t)
    print("Workers spusteny (worker_a, worker_b)")


# ============================================================================
# SCHEDULER
# ============================================================================

def check_feeds():
    if SHUTDOWN_FLAG:
        return
    for row in execute_db_fetchall(
        "SELECT id,site_id,url FROM sitemaps_feeds WHERE type IN ('rss','atom')"
    ):
        feed_id, site_id, url = row
        try:
            for new_url in parse_feed(url):
                _queue_url(site_id, normalize_url(new_url), 1)
            execute_db(
                "UPDATE sitemaps_feeds SET last_checked=strftime('%s','now') WHERE id=?",
                (feed_id,), commit=True
            )
            execute_db(
                "UPDATE site_sources SET last_checked=strftime('%s','now') WHERE site_id=? AND url=?",
                (site_id, url), commit=True
            )
        except Exception as e:
            print(f"Feed check error {url}: {e}")
            log_error(f"Feed check error {url}", e)


def check_sitemaps():
    if SHUTDOWN_FLAG:
        return
    for row in execute_db_fetchall(
        "SELECT id,site_id,url FROM sitemaps_feeds WHERE type='sitemap'"
    ):
        sm_id, site_id, url = row
        try:
            for new_url in parse_sitemap(url):
                _queue_url(site_id, normalize_url(new_url), 3)
            execute_db(
                "UPDATE sitemaps_feeds SET last_checked=strftime('%s','now') WHERE id=?",
                (sm_id,), commit=True
            )
            execute_db(
                "UPDATE site_sources SET last_checked=strftime('%s','now') WHERE site_id=? AND url=?",
                (site_id, url), commit=True
            )
        except Exception as e:
            print(f"Sitemap check error {url}: {e}")
            log_error(f"Sitemap check error {url}", e)


def recrawl_all_sites():
    if SHUTDOWN_FLAG:
        return
    for row in execute_db_fetchall(
        "SELECT id,canonical_url,max_pages FROM sites WHERE status='active'"
    ):
        try:
            phase_1_discovery(row[1], row[2])
        except Exception as e:
            print(f"Recrawl error {row[1]}: {e}")


def check_for_updates_scheduled():
    if SHUTDOWN_FLAG:
        return
    try:
        available, msg = check_for_update()
        if available:
            print(f"Update available: {msg}")
    except Exception as e:
        print(f"Update check error: {e}")


# ============================================================================
# SHUTDOWN
# ============================================================================

def handle_shutdown(signum, frame):
    global SHUTDOWN_FLAG
    SHUTDOWN_FLAG = True
    print(f"Signal {signum}, shutting down...")
    close_db()
    if _scheduler:
        _scheduler.shutdown(wait=False)
    sys.exit(0)


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


# ============================================================================
# FLASK ROUTES
# ============================================================================

def _plural_cz(count, one, few, many):
    """Czech plural form: the last significant digit decides, except for teens."""
    count = abs(int(count))
    if count == 1:
        return one
    if 2 <= count <= 4:
        return few
    if count >= 5 and count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return few
    return many


app = Flask(__name__)
app.secret_key = 'mini-search-secret-key'
app.jinja_env.filters['plural_cz'] = _plural_cz

SEARCH_PAGE_SIZE = 25
# Hard ceiling on how many results a single query may pull from the index.
# Also bounds how deep pagination can go (page * SEARCH_PAGE_SIZE).
SEARCH_MAX_RESULTS = 500


def _result_domain(url):
    """Hostname for the breadcrumb line under a result title."""
    try:
        return (urlparse(url).hostname or '').replace('www.', '')
    except Exception:
        return ''


def _result_url_path(url):
    """Path portion shown after the domain in the breadcrumb line."""
    try:
        parsed = urlparse(url)
        path = parsed.path or '/'
        if parsed.query:
            path = f"{path}?{parsed.query}"
        return path
    except Exception:
        return url or ''


def _result_snippet(page, query, length=240):
    """Body text windowed around the first query match so the hit is visible."""
    text = (page.get('og_description') or page.get('body_text') or '').strip()
    if not text:
        return ''
    text = re.sub(r'\s+', ' ', text)

    lowered = text.lower()
    position = -1
    for term in (query or '').lower().split():
        position = lowered.find(term)
        if position != -1:
            break

    if position > length // 2:
        start = max(position - length // 3, 0)
        prefix = '… '
        text = text[start:start + length]
    else:
        prefix = ''
        text = text[:length]

    suffix = '…' if len(text) >= length else ''
    return f"{prefix}{text.strip()}{suffix}"


def _highlight_snippet(snippet, query):
    """Wrap query terms in <mark>. Escaping happens before the markup is added."""
    escaped = escape(snippet)
    terms = {t for t in (query or '').lower().split() if len(t) > 2}
    if not terms:
        return escaped

    pattern = '|'.join(re.escape(term) for term in sorted(terms, key=len, reverse=True))

    def replace(match):
        return f'<mark>{match.group(0)}</mark>'

    try:
        return re.sub(f'({pattern})', replace, escaped, flags=re.IGNORECASE)
    except re.error:
        return escaped


def prepare_results(results, query):
    """Attach display-only fields so the template stays free of logic."""
    prepared = []
    for page in results:
        item = dict(page)
        item['domain'] = _result_domain(item.get('url', ''))
        item['display_url_path'] = _result_url_path(item.get('url', ''))
        item['snippet_html'] = _highlight_snippet(_result_snippet(item, query), query)
        item['relevance_pct'] = int(round(float(item.get('relevance') or 0)))
        item['display_title'] = item.get('og_title') or item.get('title') or item.get('url', '')
        item['display_date'] = item.get('published_date') or ''
        item['thumb'] = item.get('og_image') or item.get('favicon_url') or ''
        prepared.append(item)
    return prepared


@app.route('/')
def search_index():
    query = request.args.get('q', '').strip()
    filter_type = request.args.get('filter', 'all')
    try:
        page = max(int(request.args.get('page', 1)), 1)
    except (TypeError, ValueError):
        page = 1

    results = []
    has_next = False
    if query:
        # hybrid_search has no offset, so fetch the window for the requested
        # page plus one peek row (to detect a next page) and slice it. The cap
        # bounds how deep pagination can reach into the index.
        fetch_limit = min(page * SEARCH_PAGE_SIZE + 1, SEARCH_MAX_RESULTS)
        window = hybrid_search(
            query, limit=fetch_limit,
            filter_type=filter_type if filter_type != 'all' else None
        )
        start = (page - 1) * SEARCH_PAGE_SIZE
        results = window[start:start + SEARCH_PAGE_SIZE]
        has_next = len(window) > start + SEARCH_PAGE_SIZE

    return render_template(
        'search.html',
        active_page='search',
        query=query,
        results=prepare_results(results, query),
        total_results=len(results),
        current_filter=filter_type,
        page=page,
        has_prev=page > 1,
        has_next=has_next,
    )


@app.route('/autocomplete')
def autocomplete():
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'results': []})
    like = f"%{q}%"
    rows = execute_db_fetchall(
        """SELECT og_title FROM pages WHERE og_title LIKE ? AND og_title != ''
           UNION
           SELECT title FROM pages WHERE title LIKE ? AND title != ''
           LIMIT 8""",
        (like, like)
    )
    return jsonify({'results': [row[0] for row in rows]})


# ============================================================================
# ADMIN UI (Clay panel, Google Search Console style)
# ============================================================================

ADMIN_PAGE_SIZE = 10


def _admin_message():
    """Read flash message from query string."""
    if request.args.get('success'):
        return request.args.get('success'), True
    if request.args.get('error'):
        return request.args.get('error'), False
    return '', True


def _page_url(page_number):
    args = request.args.to_dict()
    args['page'] = page_number
    return '/admin/sites?' + urlencode(args)


def _source_from_form(payload, require_site=True):
    """Extract and coerce a source payload. Returns (data, error)."""
    source_type = (payload.get('source_type') or '').strip()
    if source_type not in VALID_SOURCE_TYPES:
        return None, 'Neplatný typ zdroje'
    url = (payload.get('url') or '').strip()
    ok, message = validate_url(url)
    if not ok:
        return None, message
    ok, message, priority = validate_priority(payload.get('priority', 5))
    if not ok:
        return None, message
    data = {
        'url': url,
        'source_type': source_type,
        'priority': priority,
        'notes': (payload.get('notes') or '').strip(),
    }
    if require_site:
        site_id = payload.get('site_id')
        if not site_id:
            return None, 'Vyberte doménu'
        try:
            data['site_id'] = int(site_id)
        except (TypeError, ValueError):
            return None, 'Neplatná doména'
    elif payload.get('site_id'):
        try:
            data['site_id'] = int(payload['site_id'])
        except (TypeError, ValueError):
            return None, 'Neplatná doména'
    if payload.get('max_pages') not in (None, ''):
        ok, message, max_pages = validate_max_pages(payload['max_pages'])
        if not ok:
            return None, message
        data['max_pages'] = max_pages
    return data, None


@app.route('/admin')
def admin_index():
    message, ok = _admin_message()
    return render_template(
        'admin/dashboard.html',
        active_page='dashboard',
        stats=get_db_stats(),
        sites=get_all_sites()[:6],
        update_status=get_update_status(),
        message=message,
        message_ok=ok,
    )


@app.route('/admin/sites')
def admin_sites():
    search = request.args.get('q', '').strip()
    status = request.args.get('status', '').strip()
    source_type = request.args.get('source_type', '').strip()

    all_sites = get_filtered_sites(search=search, status=status, source_type=source_type)
    total = len(all_sites)

    try:
        page = max(1, int(request.args.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    pages = max(1, (total + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
    page = min(page, pages)
    start = (page - 1) * ADMIN_PAGE_SIZE
    page_sites = all_sites[start:start + ADMIN_PAGE_SIZE]

    message, ok = _admin_message()
    return render_template(
        'admin/sites.html',
        active_page='sites',
        sites=page_sites,
        total=total,
        page=page,
        pages=pages,
        query=search,
        status=status,
        source_type=source_type,
        build_page_url=_page_url,
        message=message,
        message_ok=ok,
    )


@app.route('/admin/sites/<int:site_id>')
def admin_site_detail(site_id):
    site = get_site_by_id(site_id)
    if not site:
        return redirect('/admin/sites?error=Web nenalezen')

    try:
        site['aliases_list'] = json.loads(site.get('aliases') or '[]')
    except Exception:
        site['aliases_list'] = []
    site['last_crawled_str'] = format_timestamp(site['last_crawled'])

    errors = [
        dict(row) for row in execute_db_fetchall(
            "SELECT url, error_reason, retry_count FROM crawl_queue "
            "WHERE site_id = ? AND status = 'error' ORDER BY id DESC LIMIT 50",
            (site_id,)
        )
    ]

    message, ok = _admin_message()
    return render_template(
        'admin/site_detail.html',
        active_page='sites',
        site=site,
        stats=get_source_stats(site_id),
        sources=get_site_sources(site_id),
        recent_pages=get_recent_pages(site_id, limit=15),
        errors=errors,
        message=message,
        message_ok=ok,
    )


@app.route('/admin/sites/<int:site_id>/edit', methods=['GET', 'POST'])
def admin_site_edit(site_id):
    site = get_site_by_id(site_id)
    if not site:
        return redirect('/admin/sites?error=Web nenalezen')

    if request.method == 'POST':
        ok, message = update_site(
            site_id,
            max_pages=request.form.get('max_pages'),
            status=request.form.get('status'),
            aliases=request.form.get('aliases', ''),
        )
        if ok:
            return redirect(f'/admin/sites/{site_id}?success={message}')
        return redirect(f'/admin/sites/{site_id}/edit?error={message}')

    try:
        site['aliases_list'] = json.loads(site.get('aliases') or '[]')
    except Exception:
        site['aliases_list'] = []

    message, ok = _admin_message()
    return render_template(
        'admin/edit_site.html',
        active_page='sites',
        site=site,
        max_pages_min=MAX_PAGES_MIN,
        max_pages_max=MAX_PAGES_MAX,
        message=message,
        message_ok=ok,
    )


@app.route('/admin/sites/<int:site_id>/recrawl')
def admin_site_recrawl(site_id):
    ok, message = recrawl_site(site_id)
    key = 'success' if ok else 'error'
    return redirect(f'/admin/sites/{site_id}?{key}={message}')


@app.route('/admin/sites/<int:site_id>/delete')
def admin_site_delete(site_id):
    delete_site(site_id)
    return redirect('/admin/sites?success=Web byl smazán')


@app.route('/admin/sources/new', methods=['GET'])
def admin_source_new():
    message, ok = _admin_message()
    preselect = request.args.get('site_id', type=int)
    return render_template(
        'admin/add_source.html',
        active_page='add',
        sites=get_all_sites(),
        preselect_site=preselect,
        redirect_to=f'/admin/sites/{preselect}' if preselect else '/admin/sites',
        message=message,
        message_ok=ok,
    )


@app.route('/admin/sites/<int:site_id>/sources/new', methods=['GET'])
def admin_site_source_new(site_id):
    message, ok = _admin_message()
    return render_template(
        'admin/add_source.html',
        active_page='sites',
        sites=get_all_sites(),
        preselect_site=site_id,
        redirect_to=f'/admin/sites/{site_id}',
        message=message,
        message_ok=ok,
    )


@app.route('/admin/sources/<int:source_id>/edit', methods=['GET'])
def admin_source_edit(source_id):
    source = get_source_by_id(source_id)
    if not source:
        return redirect('/admin/sites?error=Zdroj nenalezen')
    site = get_site_by_id(source['site_id']) or {}
    message, ok = _admin_message()
    return render_template(
        'admin/edit_source.html',
        active_page='sites',
        source=source,
        max_pages=site.get('max_pages', 500),
        message=message,
        message_ok=ok,
    )


@app.route('/admin/recrawl-all')
def admin_recrawl_all():
    count = 0
    for site in get_all_sites():
        if site['status'] == 'active':
            recrawl_site(site['id'])
            count += 1
    return redirect(f'/admin?success=Recrawl zahájen pro {count} webů')


@app.route('/admin/pause-all')
def admin_pause_all():
    execute_db("UPDATE sites SET status='paused' WHERE status='active'", commit=True)
    return redirect('/admin?success=Všechny aktivní weby byly pozastaveny')


@app.route('/admin/resume-all')
def admin_resume_all():
    execute_db("UPDATE sites SET status='active' WHERE status='paused'", commit=True)
    return redirect('/admin?success=Všechny pozastavené weby byly obnoveny')


@app.route('/admin/search')
def admin_search():
    query = request.args.get('q', '').strip()
    filter_type = request.args.get('filter', 'all')
    results = []
    if query:
        results = hybrid_search(query, limit=50,
                                filter_type=filter_type if filter_type != 'all' else None)
    message, ok = _admin_message()
    return render_template(
        'admin/search.html',
        active_page='index_search',
        query=query,
        results=results,
        current_filter=filter_type,
        message=message,
        message_ok=ok,
    )


# ---------------------------- JSON API ------------------------------------

@app.route('/admin/api/sources', methods=['GET'])
def api_sources_list():
    site_id = request.args.get('site_id', type=int)
    source_type = request.args.get('source_type', '').strip()
    if site_id:
        sources = get_site_sources(site_id)
    else:
        sources = get_all_sources()
    if source_type:
        sources = [s for s in sources if s.get('source_type') == source_type]
    return jsonify({'sources': sources, 'count': len(sources)})


@app.route('/admin/api/sources', methods=['POST'])
def api_sources_create():
    payload = request.get_json(silent=True) or request.form.to_dict()
    data, error = _source_from_form(payload, require_site=False)
    if error:
        return jsonify({'error': error}), 400

    site_id = data.pop('site_id', None)
    max_pages = data.pop('max_pages', None)

    if site_id is None:
        if data['source_type'] != 'domain':
            # Auto-create the parent domain from the supplied URL
            site_id = add_site(data['url'], 500)
            if not site_id:
                return jsonify({'error': 'Doménu nebylo možné vytvořit'}), 400
        else:
            site_id = 0

    source_id, error = add_source(site_id, **data)
    if error:
        return jsonify({'error': error}), 400

    if max_pages is not None:
        source = get_source_by_id(source_id)
        if source:
            update_site(source['site_id'], max_pages=max_pages)

    return jsonify({'id': source_id, 'message': 'Zdroj byl přidán'}), 201


@app.route('/admin/api/sources/<int:source_id>', methods=['GET'])
def api_source_get(source_id):
    source = get_source_by_id(source_id)
    if not source:
        return jsonify({'error': 'Zdroj nenalezen'}), 404
    return jsonify(source)


@app.route('/admin/api/sources/<int:source_id>', methods=['PUT', 'PATCH'])
def api_source_update(source_id):
    payload = request.get_json(silent=True) or request.form.to_dict()
    allowed = ('url', 'source_type', 'priority', 'notes', 'status', 'max_pages')
    kwargs = {k: payload[k] for k in allowed if k in payload and payload[k] != ''}
    ok, message = update_source(source_id, **kwargs)
    if not ok:
        return jsonify({'error': message}), 400
    return jsonify({'message': message, 'source': get_source_by_id(source_id)})


@app.route('/admin/api/sources/<int:source_id>', methods=['DELETE'])
def api_source_delete(source_id):
    ok, message = delete_source(source_id)
    if not ok:
        return jsonify({'error': message}), 404
    return jsonify({'message': message})


@app.route('/admin/api/sites', methods=['GET'])
def api_sites_list():
    search = request.args.get('q', '').strip()
    status = request.args.get('status', '').strip()
    source_type = request.args.get('source_type', '').strip()
    return jsonify({'sites': get_filtered_sites(search, status, source_type)})


@app.route('/admin/api/sites/<int:site_id>', methods=['GET'])
def api_site_get(site_id):
    site = get_site_by_id(site_id)
    if not site:
        return jsonify({'error': 'Web nenalezen'}), 404
    site['stats'] = get_source_stats(site_id)
    site['sources'] = get_site_sources(site_id)
    return jsonify(site)


@app.route('/admin/api/sites/<int:site_id>', methods=['PUT', 'PATCH'])
def api_site_update(site_id):
    payload = request.get_json(silent=True) or request.form.to_dict()
    allowed = ('max_pages', 'status', 'aliases')
    kwargs = {k: payload[k] for k in allowed if k in payload and payload[k] != ''}
    ok, message = update_site(site_id, **kwargs)
    if not ok:
        return jsonify({'error': message}), 400
    return jsonify({'message': message, 'site': get_site_by_id(site_id)})


@app.route('/admin/api/sites/<int:site_id>', methods=['DELETE'])
def api_site_delete(site_id):
    if not get_site_by_id(site_id):
        return jsonify({'error': 'Web nenalezen'}), 404
    delete_site(site_id)
    return jsonify({'message': 'Web byl smazán'})


@app.route('/admin/api/stats')
def api_stats():
    stats = get_db_stats()
    stats['sources'] = execute_db_fetchone("SELECT COUNT(*) FROM site_sources")[0]
    return jsonify(stats)


@app.route('/admin/api/sources/<int:source_id>/stats')
def api_source_stats(source_id):
    source = get_source_by_id(source_id)
    if not source:
        return jsonify({'error': 'Zdroj nenalezen'}), 404
    return jsonify(get_source_stats(source['site_id']))


@app.route('/admin/stats')
def admin_stats():
    return api_stats()


@app.route('/admin/errors/<int:site_id>')
def admin_errors(site_id):
    rows = execute_db_fetchall(
        "SELECT url,error_reason FROM crawl_queue WHERE site_id=? AND status='error'",
        (site_id,)
    )
    return jsonify([{'url': r[0], 'error': r[1]} for r in rows])


@app.route('/admin/check-update')
def admin_check_update():
    try:
        available, msg = check_for_update()
        if available:
            return redirect('/admin?success=Aktualizace je dostupná! ' + msg)
        return redirect('/admin?success=Žádná aktualizace není dostupná. ' + msg)
    except Exception as e:
        log_error('Update check failed', e)
        return redirect(f'/admin?error=Chyba při kontrole aktualizace: {str(e)}')


@app.route('/admin/apply-update')
def admin_apply_update():
    try:
        success, msg = apply_update()
        key = 'success' if success else 'error'
        return redirect(f'/admin?{key}={msg}')
    except Exception as e:
        log_error('Apply update failed', e)
        return redirect(f'/admin?error=Chyba při aktualizaci: {str(e)}')


@app.route('/admin/update-status')
def admin_update_status():
    return jsonify(get_update_status())



# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    print('=' * 70)
    print('Mini Search v7.2 - Hybrid Search Engine')
    print('=' * 70)

    get_db()
    print('Database initialized')

    get_hnsw_index()
    print('hnswlib index initialized')

    get_model()
    print('Model loaded')

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(check_feeds,      IntervalTrigger(hours=1),  id='check_feeds')
    _scheduler.add_job(check_sitemaps,   IntervalTrigger(hours=24), id='check_sitemaps')
    _scheduler.add_job(recrawl_all_sites, IntervalTrigger(hours=12), id='recrawl_all')
    _scheduler.add_job(check_for_updates_scheduled, IntervalTrigger(hours=6), id='check_updates')
    _scheduler.start()
    print('Scheduler started')

    start_workers()

    print()
    print('http://0.0.0.0:8070')
    print('Ctrl+C to stop')
    print('=' * 70)

    app.run(host='0.0.0.0', port=8070, debug=False, threaded=True)
