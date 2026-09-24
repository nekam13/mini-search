#!/usr/bin/env python3
"""
Mini Search - Complete Implementation v7.4
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
Local-network (LAN) indexing: auto-detected private hosts skip robots.txt and
receive a 3x search boost, tunable per site in the admin panel
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
import socket
from http.client import RemoteDisconnected
from datetime import datetime, timedelta
from ipaddress import ip_address
from urllib.parse import urlparse, urlunparse, urljoin, urlencode, parse_qsl, quote
from urllib.robotparser import RobotFileParser

from flask import (Flask, render_template, request, redirect, jsonify)
from markupsafe import escape
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import requests
import requests.packages.urllib3.util.connection as urllib3_cn
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup
import feedparser
import extruct

# numpy/hnswlib are only needed for vector search. The mobile profile
# (requirements-mobile.txt) omits them, so import defensively and let the
# whole app run on FTS5 alone.
try:
    import numpy as np
except ImportError:  # pragma: no cover - depends on install profile
    np = None
try:
    import hnswlib
except ImportError:  # pragma: no cover - depends on install profile
    hnswlib = None


# Some local networks resolve a hostname to both IPv4 and IPv6, and a stalled
# IPv6 route makes requests hang until the timeout. Prefer IPv4 everywhere.
urllib3_cn.allowed_gai_family = lambda: socket.AF_INET


# ============================================================================
# GLOBAL CONFIG
# ============================================================================

def _env_int(name, default, minimum=None, maximum=None):
    """Read an int from the environment, clamped to a sane range.

    Invalid values silently fall back to ``default`` so a typo in the Termux
    shell cannot stop the app from booting.
    """
    raw = os.environ.get(name)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ('1', 'true', 'on', 'yes', 'ano')


DB_PATH = os.environ.get("MINISEARCH_DB", "console.db")
# ``MINISEARCH_PROFILE=lowmem`` flips every expensive default in one go, which is
# the recommended setting on Termux/Android where RAM and storage are tight.
LOW_MEMORY_MODE = _env_bool("MINISEARCH_LOWMEM") or \
    os.environ.get("MINISEARCH_PROFILE", "").strip().lower() in ('lowmem', 'low-memory', 'mobile')

MAX_RETRIES = _env_int("MINISEARCH_MAX_RETRIES", 3, 0, 10)
REQUEST_TIMEOUT = _env_int("MINISEARCH_REQUEST_TIMEOUT", 30, 1, 600)
DISCOVERY_TIMEOUT = _env_int("MINISEARCH_DISCOVERY_TIMEOUT", 10, 1, 600)
DISCOVERY_MAX_RETRIES = _env_int("MINISEARCH_DISCOVERY_MAX_RETRIES", 2, 0, 10)
HTTP_FETCH_RETRY_BASE_DELAY = 1.0
# Statuses that are worth retrying: rate limiting plus transient server/proxy
# problems. 429/503 are the ones Czech MediaWiki and shared hosts actually hit.
RETRYABLE_HTTP_STATUSES = frozenset((408, 425, 429, 500, 502, 503, 504))
# Cap for exponential HTTP backoff, kept modest so a slow phone recovers quickly.
HTTP_RETRY_MAX_DELAY = _env_int("MINISEARCH_RETRY_MAX_DELAY", 5, 1, 120)
MIN_DELAY = 1.0
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 MiniSearchBot/1.0")
SHUTDOWN_FLAG = False
MAX_SITEMAP_RECURSION = _env_int("MINISEARCH_SITEMAP_RECURSION", 5, 1, 20)
MAX_SITEMAP_URLS = _env_int("MINISEARCH_SITEMAP_MAX_URLS", 5000, 10, 500000)
RECRAWL_STALE_AFTER_SECONDS = 3 * 24 * 3600

# How many worker threads crawl the queue. One is plenty on a phone and keeps
# SQLite contention and RAM low; desktop installs can raise it.
WORKER_COUNT = _env_int("MINISEARCH_WORKERS", 1 if LOW_MEMORY_MODE else 2, 1, 16)
# Caps a single page's extracted body so one huge article cannot balloon the DB.
MAX_BODY_CHARS = _env_int("MINISEARCH_MAX_BODY_CHARS", 3500, 200, 200000)
# Embeddings are the biggest per-page cost (384 floats = 1.5 KB each) and the
# hnswlib index adds more RAM on top, so on a phone they should only run when
# there is headroom. ``MINISEARCH_EMBEDDINGS`` accepts:
#   1/0/true/false - hard on/off (the historical behaviour)
#   auto           - keep vector search unless RAM or battery is too low
# The default is ``auto`` in low-memory mode and always-on elsewhere.
EMBEDDINGS_PREF = os.environ.get(
    "MINISEARCH_EMBEDDINGS", "auto" if LOW_MEMORY_MODE else "1").strip().lower()
# Below this much free RAM (MB) automatic mode drops embeddings.
EMBED_MIN_FREE_MB = _env_int("MINISEARCH_MIN_FREE_MB", 300, 50, 100000)
# Below this battery percentage, and while unplugged, automatic mode drops them.
EMBED_MIN_BATTERY_PCT = _env_int("MINISEARCH_MIN_BATTERY_PCT", 15, 0, 100)
EMBED_MAX_CHARS = _env_int("MINISEARCH_EMBED_MAX_CHARS", 1000, 100, 8000)
# Above this many stored embeddings the hnswlib index is skipped in low-memory
# mode, because the in-RAM index (plus the vectors) can exceed a phone's budget.
HNSW_MAX_ELEMENTS = _env_int("MINISEARCH_HNSW_MAX_ELEMENTS", 100000, 100, 5000000)

# --- Czech Wikipedia importer -------------------------------------------------
# Default language for a bare wiki source and the API batch size. The default
# importer is ``api`` because it needs no local storage; ``dump`` streams a
# locally downloaded multistream dump instead (see run_wiki_import).
WIKI_DEFAULT_LANG = os.environ.get("MINISEARCH_WIKI_LANG", "cs")
WIKI_API_BATCH = _env_int("MINISEARCH_WIKI_BATCH", 50, 1, 50)
WIKI_IMPORT_RETRIES = _env_int("MINISEARCH_WIKI_RETRIES", 3, 0, 10)
# Optional API base for a Wikimedia mirror or a deterministic test server. When
# unset the standard ``https://<lang>.wikipedia.org`` host is used.
WIKI_API_BASE = os.environ.get("MINISEARCH_WIKI_API_BASE", "").rstrip("/")

# --- Nightly maintenance ("dreaming") -----------------------------------------
# A single background/CLI pass that consolidates the DB, backfills missing
# vectors, continues a slow wiki import and (optionally) checks for dead links.
# Every stage is batched and respects the global ``SHUTDOWN_FLAG``, so it is
# safe to interrupt. Logs go to ``logs/maintenance.log``.
MAINTENANCE_LOG_PATH = os.environ.get("MINISEARCH_MAINTENANCE_LOG", "logs/maintenance.log")
# Wiki import stage: how many articles one maintenance run may pull. Kept small
# so a night's run stays light on a phone; 0 disables the stage.
MAINTENANCE_WIKI_PAGES = _env_int("MINISEARCH_MAINT_WIKI_PAGES", 200, 0, 100000)
# Dead-link probe: at most this many oldest pages are checked per run, and only
# when explicitly enabled (it touches the network).
MAINTENANCE_LINK_CHECK_LIMIT = _env_int("MINISEARCH_MAINT_LINK_CHECK", 0, 0, 100000)
# Age (days) after which an indexed page becomes a candidate for a link probe.
MAINTENANCE_LINK_CHECK_DAYS = _env_int("MINISEARCH_MAINT_LINK_CHECK_DAYS", 30, 0, 3650)
# Pages returning 404/410 this many times are removed from the index.
MAINTENANCE_DEAD_LINK_PURGE = _env_bool("MINISEARCH_MAINT_PURGE_DEAD", False)
# A VACUUM rewrites the whole database and briefly needs roughly its size in
# free disk space, so it only runs when that much is genuinely available.
MAINTENANCE_VACUUM_MIN_FREE_MB = _env_int("MINISEARCH_MAINT_VACUUM_MIN_MB", 50, 0, 100000)
# Hard cap on embedding backfill work per run (keeps a night bounded).
MAINTENANCE_EMBED_BATCH = _env_int("MINISEARCH_MAINT_EMBED_BATCH", 200, 0, 100000)
# Upper bound for one dead-link HEAD/GET probe.
MAINTENANCE_LINK_TIMEOUT = _env_int("MINISEARCH_MAINT_LINK_TIMEOUT", 10, 1, 120)
# Content-duplicate scan is O(signatures) in memory but O(pages) in reads, so it
# is capped; url_hash dedup (the exact, cheap check) always runs in full.
MAINTENANCE_DEDUP_SCAN = _env_int("MINISEARCH_MAINT_DEDUP_SCAN", 20000, 0, 1000000)
# Pages shorter than this are never treated as content duplicates: short or
# boilerplate bodies collide too easily to be worth merging.
CONTENT_DUP_MIN_CHARS = _env_int("MINISEARCH_MAINT_DEDUP_MIN_CHARS", 200, 20, 100000)
# Register the nightly maintenance as a background job (off by default so a
# fresh install never surprises its owner with an overnight import/probe).
MAINTENANCE_NIGHTLY_ENABLED = _env_bool("MINISEARCH_NIGHTLY", False)
MAINTENANCE_NIGHTLY_HOUR = _env_int("MINISEARCH_NIGHTLY_HOUR", 3, 0, 23)

# Local (private-network) sites are trusted: we skip robots.txt for them and
# boost them in search results. The multiplier is stored per site so the admin
# can tune it.
LOCAL_SITE_PRIORITY_MULTIPLIER = 3.0
PUBLIC_SITE_PRIORITY_MULTIPLIER = 1.0
PRIORITY_MULTIPLIER_MIN = 1.0
PRIORITY_MULTIPLIER_MAX = 10.0
# The crawl worker selects by ``ORDER BY priority ASC``, so a *lower* number is
# crawled sooner. Local pages therefore get the most urgent priority.
LOCAL_QUEUE_PRIORITY = 1
DEFAULT_QUEUE_PRIORITY = 5

# Suffixes that only ever resolve inside a private network.
LOCAL_HOST_SUFFIXES = ('.local', '.localhost', '.internal', '.lan', '.home.arpa')


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
_http_local = threading.local()
_active_recrawls = set()
_recrawl_lock = threading.Lock()

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
            last_import_at INTEGER DEFAULT 0,
            import_state TEXT DEFAULT '{}',
            max_pages INTEGER DEFAULT 500,
            crawl_delay REAL DEFAULT 1.0,
            is_local INTEGER DEFAULT 0,
            search_priority_multiplier REAL DEFAULT 1.0,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        )
    ''',
    'crawl_queue': '''
        CREATE TABLE IF NOT EXISTS crawl_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER,
            url TEXT,
            url_hash TEXT DEFAULT '',
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
            last_link_check INTEGER DEFAULT 0,
            link_status TEXT DEFAULT '',
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
            source_type TEXT NOT NULL CHECK(source_type IN ('domain', 'url', 'sitemap', 'feed', 'rss', 'atom', 'wiki')),
            importer TEXT DEFAULT '',
            import_state TEXT DEFAULT '',
            priority INTEGER DEFAULT 5,
            notes TEXT DEFAULT '',
            last_checked INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            created_at INTEGER DEFAULT (strftime('%s','now')),
            FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
        )
    '''
}

# ``wiki`` is handled by the streaming Wikimedia dump importer rather than the
# generic crawler. It is deliberately appended so the existing CHECK constraint
# stays valid on upgraded databases (see ``_migrate_sources``).
VALID_SOURCE_TYPES = ('domain', 'url', 'sitemap', 'feed', 'rss', 'atom', 'wiki')
FEED_SOURCE_TYPES = ('feed', 'rss', 'atom')
WIKI_SOURCE_TYPE = 'wiki'
# Recognised ``importer`` values for a wiki source. ``dump`` streams a Wikimedia
# multistream dump; ``api`` walks the MediaWiki action API.
WIKI_IMPORTERS = ('dump', 'api')
VALID_SITE_STATUSES = ('active', 'blocked', 'paused')
MAX_PAGES_MIN = 1
MAX_PAGES_MAX = 10000
ERROR_LOG_PATH = "logs/errors.log"

DB_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(status)",
    "CREATE INDEX IF NOT EXISTS idx_sites_local ON sites(is_local)",
    "CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_queue(status)",
    "CREATE INDEX IF NOT EXISTS idx_queue_priority ON crawl_queue(priority)",
    "CREATE INDEX IF NOT EXISTS idx_queue_site ON crawl_queue(site_id)",
    "CREATE INDEX IF NOT EXISTS idx_queue_scheduled ON crawl_queue(scheduled_at)",
    "CREATE INDEX IF NOT EXISTS idx_queue_url_hash ON crawl_queue(url_hash)",
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
            run_self_migration(_db_conn)
    return _db_conn


def run_self_migration(conn=None):
    """Verify the schema upgrade and drop legacy leftovers once it is proven.

    Called automatically on every startup, so an existing install upgrades in
    place the same way it always has — no manual step. Nothing is deleted until
    the new shape is confirmed: the legacy ``site_sources_old`` table (if an
    interrupted rebuild left one behind) is only removed after the live table
    exists with all expected columns and has not lost rows relative to it.
    Returns a short human-readable summary string.
    """
    conn = conn or get_db()
    notes = []
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}

        # 1) Confirm the upgraded site_sources shape.
        expected = {'id', 'site_id', 'url', 'source_type', 'importer',
                    'import_state', 'priority', 'notes', 'last_checked',
                    'status', 'created_at'}
        cursor.execute("PRAGMA table_info(site_sources)")
        columns = {row[1] for row in cursor.fetchall()}
        missing = expected - columns
        if missing:
            notes.append(f"site_sources missing columns: {sorted(missing)}")
            return '; '.join(notes)  # do not delete anything on a bad shape

        # 2) Verify the CHECK constraint accepts the new 'wiki' type.
        cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='site_sources'")
        row = cursor.fetchone()
        if not row or "'wiki'" not in (row[0] or ''):
            notes.append("site_sources CHECK not upgraded")
            return '; '.join(notes)

        # 3) Only now, with the upgrade proven, drop the legacy backup table.
        if 'site_sources_old' in tables:
            cursor.execute("SELECT COUNT(*) FROM site_sources")
            new_count = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM site_sources_old")
            old_count = cursor.fetchone()[0]
            if new_count >= old_count:
                cursor.execute("DROP TABLE site_sources_old")
                conn.commit()
                notes.append(f"dropped legacy site_sources_old ({old_count} rows migrated)")
            else:
                notes.append(f"kept site_sources_old (new {new_count} < old {old_count})")
    except sqlite3.OperationalError as e:
        notes.append(f"self-migration skipped: {e}")
    return '; '.join(notes) if notes else 'ok'


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
    
    # Add is_local column to sites if not exists (marks private-network sites)
    try:
        cursor.execute("ALTER TABLE sites ADD COLUMN is_local INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Add search_priority_multiplier column to sites if not exists
    try:
        cursor.execute("ALTER TABLE sites ADD COLUMN search_priority_multiplier REAL DEFAULT 1.0")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Add Wiki-import bookkeeping columns to sites if not exists
    try:
        cursor.execute("ALTER TABLE sites ADD COLUMN last_import_at INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    try:
        cursor.execute("ALTER TABLE sites ADD COLUMN import_state TEXT DEFAULT '{}'")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Add scheduled_at column to crawl_queue if not exists
    try:
        cursor.execute("ALTER TABLE crawl_queue ADD COLUMN scheduled_at INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Add url_hash column to crawl_queue if not exists (robust deduplication)
    try:
        cursor.execute("ALTER TABLE crawl_queue ADD COLUMN url_hash TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    
    # Add seo_score column to pages if not exists
    try:
        cursor.execute("ALTER TABLE pages ADD COLUMN seo_score REAL DEFAULT 0.0")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Nightly maintenance bookkeeping on pages: when the link was last probed
    # and what the probe saw. Added in place so an upgrade never drops pages.
    for column, ddl in (
        ("last_link_check", "ALTER TABLE pages ADD COLUMN last_link_check INTEGER DEFAULT 0"),
        ("link_status", "ALTER TABLE pages ADD COLUMN link_status TEXT DEFAULT ''"),
    ):
        try:
            cursor.execute(ddl)
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

    # Add wiki-importer columns to site_sources (pre-7.5 databases)
    for column, ddl in (
        ("importer", "ALTER TABLE site_sources ADD COLUMN importer TEXT DEFAULT ''"),
        ("import_state", "ALTER TABLE site_sources ADD COLUMN import_state TEXT DEFAULT ''"),
    ):
        try:
            cursor.execute(ddl)
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # Older databases carry a CHECK constraint without 'wiki'. SQLite cannot
    # alter it, so rebuild the table only when the constraint still rejects it.
    try:
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='site_sources'")
        row = cursor.fetchone()
        ddl = (row[0] if row else '') or ''
        if "'wiki'" not in ddl:
            _rebuild_site_sources(cursor)
            conn.commit()
    except sqlite3.OperationalError as e:
        print(f"site_sources constraint migration skipped: {e}")

    # Create triggers if not exists
    for trigger in DB_TRIGGERS:
        try:
            cursor.execute(trigger)
            conn.commit()
        except sqlite3.OperationalError:
            pass


def _rebuild_site_sources(cursor):
    """Recreate site_sources with the extended CHECK constraint, preserving rows.

    Columns the old table did not have are filled with defaults rather than
    dropped, so upgrading an existing database never loses source rows or their
    importer progress.
    """
    cursor.execute("PRAGMA table_info(site_sources)")
    old_columns = {row[1] for row in cursor.fetchall()}

    def column(name, fallback="''"):
        return name if name in old_columns else f"{fallback} AS {name}"

    cursor.execute("ALTER TABLE site_sources RENAME TO site_sources_old")
    cursor.execute(DB_SCHEMA['site_sources'])
    cursor.execute(f"""
        INSERT INTO site_sources (id, site_id, url, source_type, importer, import_state,
                                  priority, notes, last_checked, status, created_at)
        SELECT id, site_id, url, source_type,
               {column('importer')},
               {column('import_state')},
               priority, notes,
               {column('last_checked', '0')},
               status, created_at
        FROM site_sources_old
    """)
    cursor.execute("DROP TABLE site_sources_old")
    # Indexes live on the dropped table, so recreate them.
    for index in DB_INDEXES:
        if 'site_sources' in index:
            try:
                cursor.execute(index)
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
        resp = _http_get(url, timeout=DISCOVERY_TIMEOUT, max_retries=DISCOVERY_MAX_RETRIES,
                         purpose='github-commit-check')
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

def extract_host(value):
    """Best-effort host extraction from a URL, ``host:port`` or bare hostname."""
    if not value:
        return ''
    candidate = value.strip()
    if '://' not in candidate:
        # urlparse("localhost:8000") reports scheme='localhost', so force a
        # scheme for anything that has no explicit one.
        candidate = f"http://{candidate}"
    try:
        host = urlparse(candidate).hostname or ''
    except ValueError:
        return ''
    return host.lower().rstrip('.')


def is_local_host(host):
    """True for loopback, link-local and RFC 1918 hosts, plus mDNS style names."""
    if not host:
        return False
    host = host.lower().rstrip('.')
    if host in ('localhost', 'localhost.localdomain', 'ip6-localhost', 'ip6-loopback'):
        return True
    if host.endswith(LOCAL_HOST_SUFFIXES):
        return True
    try:
        ip = ip_address(host)
    except ValueError:
        # A single-label name like "nas" can never be a public DNS name.
        return '.' not in host
    # is_private covers 10/8, 172.16/12, 192.168/16, 169.254/16 and their IPv6
    # counterparts; is_global additionally catches CGNAT (100.64/10) and other
    # ranges that cannot be reached from the public internet.
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or not ip.is_global)


def is_local_url(url):
    """Detect whether a URL points into the local network (ports are ignored)."""
    return is_local_host(extract_host(url))


def normalize_url(url):
    """Normalize a URL, keeping the port for local sites.

    A bare local address gets ``http://`` (private servers rarely serve TLS),
    a bare public hostname keeps ``https://``.
    """
    if not url:
        return ""
    url = url.strip()
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        # A bare "localhost:8000" parses with scheme='localhost', so re-parse
        # after forcing a scheme rather than trusting the first split.
        scheme = 'http' if is_local_url(url) else 'https'
        parsed = urlparse(f"{scheme}://{url}")
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parsed.path
    if path.endswith('/') and path != '/':
        path = path[:-1]
    return urlunparse((parsed.scheme, netloc, path, parsed.params, parsed.query, ''))


def get_domain(url):
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        scheme = 'http' if is_local_url(url) else 'https'
        parsed = urlparse(f"{scheme}://{url}")
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def url_hash(url):
    return hashlib.md5(normalize_url(url).encode('utf-8')).hexdigest()


def canonicalize_url(url, base_url=None):
    """Return a stable canonical form of a URL for deduplication.

    Builds on :func:`normalize_url` (lowercased host, stripped ``www.``, no
    fragment) and additionally drops tracking/utm parameters, sorts the query
    string and removes the default ``:80``/``:443`` port. Fragments are always
    stripped so ``#section`` anchors collapse onto the article URL.
    """
    if not url:
        return ''
    candidate = url
    if base_url:
        try:
            candidate = urljoin(base_url, url)
        except Exception:
            candidate = url
    candidate = (candidate or '').strip()
    if not candidate:
        return ''
    normalized = normalize_url(candidate)
    try:
        parsed = urlparse(normalized)
    except ValueError:
        return normalized

    # Drop obvious tracking parameters; keep the rest so real query pages differ.
    query_pairs = []
    if parsed.query:
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower().startswith('utm_') or key.lower() in (
                    'fbclid', 'gclid', 'yclid', 'ref', 'ref_src', 'spm', '_ga'):
                continue
            query_pairs.append((key, value))
        query_pairs.sort()
    query = urlencode(query_pairs)

    netloc = parsed.netloc
    # normalize_url lowercases the netloc; strip the scheme default port.
    if parsed.scheme == 'http' and netloc.endswith(':80'):
        netloc = netloc[:-3]
    elif parsed.scheme == 'https' and netloc.endswith(':443'):
        netloc = netloc[:-4]

    path = parsed.path or '/'
    if path != '/' and path.endswith('/'):
        path = path[:-1]
    return urlunparse((parsed.scheme, netloc, path, '', query, ''))


# Czech text uses diacritics that FTS5's unicode61 tokenizer keeps, so a query
# typed without them ("cesky") would miss "český". Folding happens only for
# lookup/snippets; stored text keeps its original diacritics for display.
# Built from a dict rather than two parallel strings so the mapping cannot drift.
_FOLD_MAP = {
    'á': 'a', 'ä': 'a', 'â': 'a', 'à': 'a', 'ã': 'a', 'å': 'a', 'ā': 'a',
    'č': 'c', 'ć': 'c', 'ç': 'c',
    'ď': 'd',
    'é': 'e', 'ě': 'e', 'è': 'e', 'ë': 'e', 'ê': 'e', 'ē': 'e',
    'í': 'i', 'ì': 'i', 'ï': 'i', 'î': 'i', 'ī': 'i',
    'ň': 'n', 'ń': 'n',
    'ó': 'o', 'ö': 'o', 'ô': 'o', 'ò': 'o', 'õ': 'o', 'ø': 'o', 'ō': 'o',
    'ř': 'r', 'ŕ': 'r',
    'š': 's', 'ś': 's', 'ş': 's',
    'ť': 't', 'ţ': 't',
    'ú': 'u', 'ů': 'u', 'ü': 'u', 'ù': 'u', 'û': 'u', 'ū': 'u',
    'ý': 'y', 'ÿ': 'y',
    'ž': 'z', 'ź': 'z', 'ż': 'z',
    'ľ': 'l', 'ĺ': 'l', 'ł': 'l',
    '·': '-',
}
_FOLD_TRANSLATION = str.maketrans(
    {**{k: v for k, v in _FOLD_MAP.items()},
     **{k.upper(): v for k, v in _FOLD_MAP.items()}}
)


def fold_diacritics(text):
    """Lowercase ``text`` and strip Czech/Latin diacritics for matching."""
    if not text:
        return ''
    return text.lower().translate(_FOLD_TRANSLATION)


def _has_diacritics(text):
    folded = fold_diacritics(text)
    return folded != (text or '').lower()


def _fold_tokenize(text):
    """Split text into folded alphanumeric tokens of length >= 1."""
    return [t for t in re.split(r'[^0-9a-z]+', fold_diacritics(text)) if t]


def page_string(page, key, default=''):
    """Read a text column from a page row without ever returning ``None``.

    sqlite3 hands back ``None`` for NULL columns, and NULLs propagate through
    string concatenation, so every read of an optional text field goes through
    here. Keeps the rest of the code free of ``or ''`` noise.
    """
    value = page.get(key) if hasattr(page, 'get') else None
    if value is None:
        return default
    return str(value)


def page_json(page, key, default):
    """Parse a JSON column off a page row, returning ``default`` on damage."""
    raw = page_string(page, key)
    if not raw:
        return default
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return default
    return parsed if parsed is not None else default


# Stopwords are only removed when building FTS queries; a query made entirely of
# stopwords is kept as-is so search never returns nothing for "je".
_CZ_STOPWORDS = {
    'a', 'i', 'o', 'u', 'v', 've', 'na', 'se', 'si', 'je', 'jsou', 'by', 'byl',
    'byla', 'bylo', 'ze', 'že', 'do', 'za', 'po', 'pro', 'od', 'k', 'ke', 's',
    'z', 'ze', 'to', 'ten', 'ta', 'ty', 'ale', 'nebo', 'jako', 'co', 'jak',
}


def build_fts_query(query):
    """Translate a user query into a safe FTS5 MATCH expression.

    Each whitespace-separated term becomes a quoted prefix token, so stray FTS
    operators can never raise a syntax error. When the query carries diacritics
    the folded form is OR-ed in, which makes "cesky" match "český" and vice
    versa while still ranking exact hits first (the original term comes first in
    the OR chain).
    """
    if not query:
        return ''
    terms = [t for t in re.split(r'\s+', query.strip()) if t]
    if not terms:
        return ''
    clauses = []
    for term in terms:
        cleaned = re.sub(r'["\'(){}\[\]^~*:+-]', ' ', term).strip()
        cleaned = cleaned.strip()
        if not cleaned:
            continue
        variants = [cleaned]
        folded = fold_diacritics(cleaned)
        if folded and folded != cleaned.lower():
            variants.append(folded)
        # Also add a tokenized folded variant for hyphenated words.
        for part in _fold_tokenize(cleaned):
            if part not in variants:
                variants.append(part)
        # Keep the original (possibly diacritic) form first for better ranking.
        seen = set()
        unique = []
        for variant in variants:
            if variant and variant not in seen:
                seen.add(variant)
                unique.append(variant)
        if len(unique) == 1:
            clauses.append(f'"{unique[0]}"*')
        else:
            clauses.append('(' + ' OR '.join(f'"{v}"*' for v in unique) + ')')
    if not clauses:
        return ''
    # Drop Czech stopwords, but never empty the whole expression.
    stripped = [c for c, t in zip(clauses, terms) if fold_diacritics(t) not in _CZ_STOPWORDS]
    if stripped:
        clauses = stripped
    return ' AND '.join(clauses)


# Schema.org ``@type`` fields are frequently arrays, and the useful object is
# often nested under ``@graph``. These helpers normalise both shapes.
def _schema_types(schema):
    """Return a lowercased list of @type values from a JSON-LD node."""
    if not isinstance(schema, dict):
        return []
    raw = schema.get('@type')
    if isinstance(raw, str):
        return [raw.lower()]
    if isinstance(raw, list):
        return [str(t).lower() for t in raw if isinstance(t, (str, int))]
    return []


def _iter_jsonld_nodes(node):
    """Yield every dict node from a JSON-LD document, walking ``@graph``."""
    if isinstance(node, list):
        for item in node:
            yield from _iter_jsonld_nodes(item)
    elif isinstance(node, dict):
        yield node
        graph = node.get('@graph')
        if graph is not None:
            yield from _iter_jsonld_nodes(graph)
        # Some sites nest the article under mainEntity / itemListElement.
        for key in ('mainEntity', 'mainEntityOfPage', 'itemListElement', 'hasPart'):
            if key in node:
                yield from _iter_jsonld_nodes(node[key])


def _jsonld_scripts(content):
    """Yield the raw text of every ``application/ld+json`` script block.

    Pages commonly embed several blocks and only one may be malformed. Parsing
    them individually means one bad block cannot discard the good ones, which
    extruct's whole-document parse would do.
    """
    try:
        soup = BeautifulSoup(content, 'lxml')
    except Exception:
        return
    for script in soup.find_all('script', attrs={'type': 'application/ld+json'}):
        text = script.string if script.string is not None else script.get_text()
        if text and text.strip():
            yield text


def _extract_jsonld(content, base_url=''):
    """Parse JSON-LD from raw HTML, tolerating invalid/nested documents.

    Returns ``(nodes, types)`` where ``nodes`` is a flat list of dicts and
    ``types`` is the set of lowercased ``@type`` values found. Never raises.
    """
    nodes = []
    types = set()
    try:
        data = extruct.extract(content, base_url=base_url or None, syntaxes=['json-ld'])
        for doc in data.get('json-ld', []):
            for node in _iter_jsonld_nodes(doc):
                nodes.append(node)
                types.update(_schema_types(node))
    except Exception:
        nodes = []

    if not nodes:
        # extruct refuses the whole document when any block is malformed; parse
        # each script on its own so the valid ones still contribute.
        for raw in _jsonld_scripts(content):
            try:
                doc = json.loads(raw)
            except (ValueError, TypeError):
                continue
            for node in _iter_jsonld_nodes(doc):
                nodes.append(node)
                types.update(_schema_types(node))
    return nodes, types


def _first_schema_value(nodes, types, wanted_types, keys):
    """Return the first non-empty string for ``keys`` in a node of ``wanted`` type."""
    wanted = {t.lower() for t in wanted_types}
    for node in nodes:
        node_types = set(_schema_types(node))
        if wanted and not (node_types & wanted):
            continue
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                # e.g. {"@type": "Person", "name": "..."}
                name = value.get('name') or value.get('@id')
                if isinstance(name, str) and name.strip():
                    return name.strip()
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, str) and first.strip():
                    return first.strip()
                if isinstance(first, dict):
                    name = first.get('name')
                    if isinstance(name, str) and name.strip():
                        return name.strip()
    return ''


def _parse_datetime(value):
    """Best-effort ISO-8601 / RFC timestamp parse, returning a unix int or 0."""
    if not value:
        return 0
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
    text = str(value).strip()
    if not text:
        return 0
    # Numeric strings may already be epoch seconds/millis.
    if re.fullmatch(r'\d{10,13}', text):
        number = int(text)
        return number // 1000 if number > 10 ** 12 else number
    candidates = [text, text[:19], text[:10]]
    candidates.append(text.replace('Z', '+00:00'))
    for candidate in candidates:
        try:
            return int(datetime.fromisoformat(candidate).timestamp())
        except Exception:
            continue
    for fmt in ('%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d',
                '%a, %d %b %Y %H:%M:%S %z', '%Y/%m/%d'):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except Exception:
            continue
    return 0


class FetchError(Exception):
    """HTTP fetch failed before receiving a usable response."""

    def __init__(self, url, message, retryable=False, attempts=1, exc=None):
        super().__init__(message)
        self.url = url
        self.message = message
        self.retryable = retryable
        self.attempts = attempts
        self.exc = exc


def _get_http_session():
    """Thread-local requests session with stable headers for flaky local servers."""
    session = getattr(_http_local, 'session', None)
    if session is None:
        session = requests.Session()
        session.headers.update({
            'User-Agent': USER_AGENT,
            'Connection': 'close',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        })
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=0)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        _http_local.session = session
    return session


def _exception_chain(exc):
    """Yield exception plus chained causes/contexts."""
    current = exc
    seen = set()
    while current and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _is_retryable_network_error(exc):
    """True for transient connection-level failures."""
    if isinstance(exc, (RemoteDisconnected, ConnectionResetError, TimeoutError,
                        requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    for item in _exception_chain(exc):
        if isinstance(item, (RemoteDisconnected, ConnectionResetError, TimeoutError,
                             requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
            return True
        text = str(item).lower()
        if ('remote end closed connection without response' in text
                or 'connection aborted' in text
                or 'connection reset' in text
                or 'timed out' in text):
            return True
    return False


def _format_network_error(exc):
    """Compact error for queue.error_reason and logs."""
    parts = []
    for item in _exception_chain(exc):
        parts.append(f"{type(item).__name__}: {item}")
    text = " | ".join(parts) if parts else f"{type(exc).__name__}: {exc}"
    return text[:500]


def _http_get(url, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES, purpose='fetch',
              method='GET', **kwargs):
    """Centralized HTTP request with finite retries for transient network errors."""
    parsed = urlparse(url or '')
    if parsed.scheme not in ('http', 'https'):
        raise FetchError(url, f"Unsupported URL scheme: {parsed.scheme or 'missing'}", retryable=False)

    attempts = max(1, int(max_retries or 1))
    session = _get_http_session()
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            resp = session.request(method, url, timeout=timeout, **kwargs)
        except Exception as exc:
            last_exc = exc
            retryable = _is_retryable_network_error(exc)
            if retryable and attempt < attempts:
                time.sleep(_retry_delay(attempt))
                continue
            msg = (f"{purpose}: {_format_network_error(exc)} "
                   f"(attempt {attempt}/{attempts})")
            raise FetchError(url, msg, retryable=retryable, attempts=attempt, exc=exc) from exc

        # Transient HTTP statuses are retried too, so callers do not each have to
        # reimplement backoff. 429 honours Retry-After (capped) when present.
        if resp.status_code in RETRYABLE_HTTP_STATUSES and attempt < attempts:
            delay = _retry_after_delay(resp) or _retry_delay(attempt)
            time.sleep(delay)
            continue
        return resp
    raise FetchError(url, f"{purpose}: unknown network error", retryable=True,
                     attempts=attempts, exc=last_exc)


def _retry_delay(attempt, base=None):
    """Exponential backoff for retry ``attempt`` (1-based), capped."""
    base = HTTP_FETCH_RETRY_BASE_DELAY if base is None else base
    return min(float(HTTP_RETRY_MAX_DELAY), base * (2 ** (attempt - 1)))


def _retry_after_delay(resp):
    """Parse a ``Retry-After`` header into a capped number of seconds, or 0."""
    raw = (resp.headers.get('Retry-After') or '').strip() if resp.headers else ''
    if not raw:
        return 0
    try:
        return min(float(HTTP_RETRY_MAX_DELAY), max(0.0, float(int(raw))))
    except (TypeError, ValueError):
        return 0


# ============================================================================
# ROBOTS.TXT
# ============================================================================

def _get_robots(base_url):
    """Return cached RobotFileParser for a domain.

    Fetched through :func:`_http_get` so it honours our timeout, retries and
    User-Agent instead of the stdlib's blocking ``urllib`` read. A missing or
    broken robots.txt yields an allow-all parser.
    """
    domain = get_domain(base_url)
    with _robots_lock:
        if domain in _robots_cache:
            return _robots_cache[domain]
    robots_url = urljoin(base_url, '/robots.txt')
    lines = []
    try:
        resp = _http_get(robots_url, timeout=DISCOVERY_TIMEOUT,
                         max_retries=DISCOVERY_MAX_RETRIES, purpose='robots')
        if resp.status_code == 200:
            text = resp.text or ''
            lines = text.splitlines()
        elif resp.status_code in (401, 403):
            # A protected robots.txt is treated as "no crawling allowed" for
            # non-local hosts, matching the standard convention.
            lines = ['User-agent: *', 'Disallow: /']
    except Exception:
        lines = []
    rp = RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.parse(lines)
    except Exception:
        pass
    with _robots_lock:
        _robots_cache[domain] = rp
    return rp


def is_allowed(url, site_id=None, is_local=None):
    """Return True if MiniSearchBot may fetch url.

    Local sites bypass robots.txt: they live on the user's own network, often
    ship no robots.txt at all, and when one exists it is typically misconfigured
    (a blanket ``Disallow: /`` from a dev server) which would block indexing.
    """
    if is_local is None and site_id:
        site = get_site_by_id(site_id)
        is_local = bool(site and site.get('is_local'))
    if is_local:
        return True
    if is_local_url(url):
        return True
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

    if is_local_url(base_url) or is_wikipedia_url(base_url):
        # No robots.txt to consult on a private LAN (and Wikipedia is served by
        # its own importer/API, not the crawler); stay a good citizen anyway.
        delay = MIN_DELAY
    else:
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

def add_site(site_url, max_pages=500, is_local=None):
    """Create (or extend) a site. Local sites get a search boost automatically.

    Returns the site id, or None when the URL is unusable.
    """
    site_url = normalize_url(site_url)
    if not site_url:
        return None

    if is_local is None:
        is_local = is_local_url(site_url)
    is_local = bool(is_local)
    multiplier = LOCAL_SITE_PRIORITY_MULTIPLIER if is_local else PUBLIC_SITE_PRIORITY_MULTIPLIER
    if is_local:
        print(f"Detected local site: {site_url}")

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
        # Never downgrade a local site: MAX() keeps the strongest flag/boost.
        execute_db(
            """UPDATE sites
               SET aliases = ?, is_local = MAX(is_local, ?),
                   search_priority_multiplier = MAX(search_priority_multiplier, ?)
               WHERE id = ?""",
            (json.dumps(aliases), int(is_local), multiplier, site_id), commit=True
        )
        return site_id

    crawl_delay = get_crawl_delay(site_url)
    execute_db(
        """INSERT INTO sites
           (canonical_url, aliases, status, max_pages, crawl_delay, is_local,
            search_priority_multiplier)
           VALUES (?, ?, 'active', ?, ?, ?, ?)""",
        (domain, json.dumps([site_url]), max_pages, crawl_delay,
         int(is_local), multiplier), commit=True
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


def _decorate_site(site):
    """Add derived display fields (stats, parsed aliases, local flag) to a site."""
    site['last_crawled_str'] = format_timestamp(site['last_crawled'])
    site.update(get_source_stats(site['id']))
    site['indexed_count'] = site['indexed']
    site['pending_count'] = site['pending']
    site['is_local'] = bool(site.get('is_local'))
    try:
        site['search_priority_multiplier'] = float(
            site.get('search_priority_multiplier') or PUBLIC_SITE_PRIORITY_MULTIPLIER)
    except (TypeError, ValueError):
        site['search_priority_multiplier'] = PUBLIC_SITE_PRIORITY_MULTIPLIER
    try:
        site['aliases_list'] = json.loads(site.get('aliases') or '[]')
    except Exception:
        site['aliases_list'] = []
    return site


def get_all_sites():
    # Local sites first so they are easy to find in the admin overview.
    results = execute_db_fetchall(
        "SELECT * FROM sites ORDER BY is_local DESC, created_at DESC")
    return [_decorate_site(dict(row)) for row in results]


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
        'sources': execute_db_fetchone("SELECT COUNT(*) FROM site_sources")[0],
    }

# ============================================================================
# SOURCE MANAGEMENT (domain / url / sitemap / feed)
# ============================================================================

def validate_url(value):
    """Return (ok, message). Accepts bare domains, local addresses and full URLs."""
    if not value or not value.strip():
        return False, 'URL je povinná'
    candidate = value.strip()
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.\-]*://', candidate):
        scheme = 'http' if is_local_url(candidate) else 'https'
        candidate = f"{scheme}://{candidate}"
    try:
        parsed = urlparse(candidate)
        host = parsed.hostname or ''
        port = parsed.port
    except ValueError:
        return False, 'Neplatný formát URL'
    if parsed.scheme not in ('http', 'https'):
        return False, 'Povolena je pouze adresa http nebo https'
    if port is not None and not (0 < port <= 65535):
        return False, 'Neplatné číslo portu'
    if not host or host.startswith('.') or host.endswith('.'):
        return False, 'Neplatný formát URL'
    if any(ch.isspace() for ch in host):
        return False, 'Neplatný formát URL'
    # Single-label hosts ("nas") and local suffixes (".local") are valid
    # inside a LAN even though they carry no dot / a non-public suffix.
    if '.' not in host and not is_local_host(host):
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


def validate_priority_multiplier(value):
    """Return (ok, message, float_value) for the search boost multiplier."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False, 'Násobek priority musí být číslo', None
    if number < PRIORITY_MULTIPLIER_MIN or number > PRIORITY_MULTIPLIER_MAX:
        return False, (f'Násobek priority musí být {PRIORITY_MULTIPLIER_MIN:g}–'
                       f'{PRIORITY_MULTIPLIER_MAX:g}'), None
    return True, '', round(number, 2)


def detect_source_type(url, fallback='url'):
    """Guess source_type from URL shape."""
    lower = (url or '').lower()
    if lower.endswith('.xml') or lower.endswith('.xml.gz') or 'sitemap' in lower:
        return 'sitemap'
    if (lower.endswith('.rss') or lower.endswith('.atom') or '/rss' in lower
            or '/feed' in lower or '/atom' in lower or 'feed' in lower):
        return 'rss'
    if 'wikipedia.org' in lower or 'wikimedia.org' in lower or lower.startswith('wiki:'):
        return WIKI_SOURCE_TYPE
    return fallback


def is_wikipedia_url(url):
    """True for Wikimedia/Wikipedia addresses that the dump importer can serve."""
    lower = (url or '').lower()
    return 'wikipedia.org' in lower or 'wikimedia.org' in lower


def normalize_wiki_source(url, importer=None):
    """Map a wiki source URL onto ``(site_url, importer, lang)``.

    Accepts a bare language code (``cs``), ``cs.wikipedia.org``, a full article
    URL, the ``wiki:cs`` shorthand, or a ``file:///path/dump.bz2`` local dump.
    ``importer`` is ``api`` (walk the MediaWiki action API, the default because
    it needs no local storage) or ``dump`` (stream a downloaded multistream
    dump, ideal offline on a device with the dump on disk).
    """
    value = (url or '').strip()
    lang = WIKI_DEFAULT_LANG
    mode = (importer or 'api').strip().lower()
    if mode not in WIKI_IMPORTERS:
        mode = 'api'

    # A local dump path implies the dump importer; the site it belongs to is
    # still the corresponding language wiki, inferred from the filename.
    if value.lower().startswith('file://'):
        return f"https://{lang_from_wiki_url(value)}.wikipedia.org", 'dump', lang

    if value.lower().startswith('wiki:'):
        lang = value.split(':', 1)[1].strip() or lang
        return f"https://{lang}.wikipedia.org", mode, lang

    # Bare language code, e.g. "cs" or "cs-cs".
    if re.fullmatch(r'[a-z]{2,3}(-[a-z]+)?', value.lower()):
        lang = value.lower()
        return f"https://{lang}.wikipedia.org", mode, lang

    host = extract_host(value)
    match = re.match(r'([a-z\-]+)\.(?:m\.)?wikipedia\.org$', host or '')
    if match:
        lang = match.group(1)
    elif host.endswith('wikipedia.org'):
        lang = WIKI_DEFAULT_LANG
    elif not host:
        return None, mode, lang
    return f"https://{lang}.wikipedia.org", mode, lang


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


def add_source(site_id, url, source_type='url', priority=5, notes='', is_local=None,
               importer=None):
    """Add a source under a site. Returns (source_id, error_message)."""
    if source_type not in VALID_SOURCE_TYPES:
        return None, f'Nepodporovaný typ zdroje: {source_type}'

    # A wiki source is a language/domain selector, not a crawlable URL, so it is
    # validated by the importer rather than ``validate_url``.
    if source_type == WIKI_SOURCE_TYPE:
        site_url, importer, _lang = normalize_wiki_source(url, importer)
        if not site_url:
            return None, 'Neplatný odkaz na Wikipedii'
        # Wiki content is public and served by the importer, never crawled, so
        # never mark it local (which would also apply the LAN search boost).
        is_local = False
    else:
        ok, message = validate_url(url)
        if not ok:
            return None, message

    ok, message, priority = validate_priority(priority)
    if not ok:
        return None, message

    if is_local and site_id:
        # Adding any local source to a domain promotes that domain, so the
        # boost and robots.txt bypass apply site-wide.
        parent = get_site_by_id(site_id)
        if parent and not parent.get('is_local'):
            update_site(site_id, is_local=True,
                        search_priority_multiplier=LOCAL_SITE_PRIORITY_MULTIPLIER)

    if source_type == 'domain':
        normalized = normalize_url(url)
        domain = get_domain(normalized)
        existing = execute_db_fetchone(
            "SELECT id FROM sites WHERE canonical_url = ?", (domain,)
        )
        if existing:
            site_id = existing[0]
            if is_local is not None and bool(is_local) and not get_site_by_id(site_id).get('is_local'):
                update_site(site_id, is_local=True,
                            search_priority_multiplier=LOCAL_SITE_PRIORITY_MULTIPLIER)
        else:
            site_id = add_site(normalized, 500, is_local=is_local)
            if not site_id:
                return None, 'Doménu nebylo možné vytvořit'

    if source_type == WIKI_SOURCE_TYPE:
        # Create/reuse the ``cs.wikipedia.org`` site. The stored source URL is
        # the language selector (or the ``file://`` dump path), so the importer
        # knows which dump/language to read.
        site_url, importer, lang = normalize_wiki_source(url, importer)
        domain = get_domain(site_url)
        existing_site = execute_db_fetchone(
            "SELECT id FROM sites WHERE canonical_url = ?", (domain,)
        )
        if existing_site:
            site_id = existing_site[0]
        else:
            site_id = add_site(site_url, 500, is_local=False)
            if not site_id:
                return None, 'Doménu nebylo možné vytvořit'
        normalized = url.strip() if url.strip().lower().startswith(
            WIKI_DUMP_SOURCE_PREFIX) else site_url
        duplicate = execute_db_fetchone(
            "SELECT id FROM site_sources WHERE site_id = ? AND source_type = ? "
            "AND url = ?",
            (site_id, WIKI_SOURCE_TYPE, normalized)
        )
        if duplicate:
            return None, 'Tento zdroj je již pod doménou zaregistrován'
        execute_db(
            """INSERT INTO site_sources (site_id, url, source_type, importer, import_state,
                                         priority, notes, last_checked, status)
               VALUES (?, ?, ?, ?, '', ?, ?, 0, 'active')""",
            (site_id, normalized, source_type, importer, priority, notes or ''),
            commit=True
        )
        source_id = execute_db_fetchone("SELECT last_insert_rowid()")[0]
        return source_id, None

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
        if source['source_type'] == WIKI_SOURCE_TYPE:
            new_url, _importer, _lang = normalize_wiki_source(
                kwargs['url'], kwargs.get('importer') or source.get('importer'))
            if not new_url:
                return False, 'Neplatný odkaz na Wikipedii'
        else:
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

    if kwargs.get('importer') is not None and source['source_type'] == WIKI_SOURCE_TYPE:
        mode = str(kwargs['importer']).strip().lower()
        if mode not in WIKI_IMPORTERS:
            return False, 'Nepodporovaný importér'
        updates.append("importer = ?")
        params.append(mode)

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

    if kwargs.get('is_local') is not None:
        value = kwargs['is_local']
        if isinstance(value, str):
            value = value.strip().lower() in ('1', 'true', 'on', 'yes', 'ano')
        value = bool(value)
        updates.append("is_local = ?")
        params.append(int(value))
        # Unmarking a site should also drop the boost it inherited, unless the
        # caller passes an explicit multiplier alongside.
        if not value and kwargs.get('search_priority_multiplier') is None:
            updates.append("search_priority_multiplier = ?")
            params.append(PUBLIC_SITE_PRIORITY_MULTIPLIER)

    if kwargs.get('search_priority_multiplier') is not None:
        ok, message, multiplier = validate_priority_multiplier(kwargs['search_priority_multiplier'])
        if not ok:
            return False, message
        updates.append("search_priority_multiplier = ?")
        params.append(multiplier)

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


def get_filtered_sites(search='', status='', source_type='', local_only=False):
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
    if local_only:
        query += " AND is_local = 1"
    query += " ORDER BY is_local DESC, created_at DESC"

    return [_decorate_site(dict(row)) for row in execute_db_fetchall(query, tuple(params))]


def _begin_site_recrawl(site_id):
    """Guard against overlapping recrawls for one site."""
    with _recrawl_lock:
        if site_id in _active_recrawls:
            return False
        _active_recrawls.add(site_id)
        return True


def _finish_site_recrawl(site_id):
    with _recrawl_lock:
        _active_recrawls.discard(site_id)


def recrawl_site(site_id):
    """Queue every domain/url/sitemap/feed source belonging to a site."""
    site = get_site_by_id(site_id)
    if not site:
        return False, 'Web nenalezen'
    if not _begin_site_recrawl(site_id):
        return False, 'Re-crawl tohoto webu právě běží'
    try:
        site_id_result, added = phase_1_discovery(
            site['canonical_url'],
            site['max_pages'],
            allow_indexed_refresh=True
        )
        return True, f"Re-crawl zahájen ({added} URL ve frontě)"
    except Exception as e:
        log_error(f"Recrawl failed for site {site_id}", e)
        return False, f"Re-crawl selhal: {e}"
    finally:
        _finish_site_recrawl(site_id)


def recrawl_async(site_id):
    """Run recrawl_site in the background (discovery hits the network)."""
    if not get_site_by_id(site_id):
        return False, 'Web nenalezen'
    if not _begin_site_recrawl(site_id):
        return False, 'Re-crawl tohoto webu právě běží'

    def _run():
        try:
            site = get_site_by_id(site_id)
            if not site:
                return
            phase_1_discovery(
                site['canonical_url'],
                site['max_pages'],
                allow_indexed_refresh=True
            )
        except Exception as e:
            log_error(f"Background recrawl of site {site_id} failed", e)
        finally:
            _finish_site_recrawl(site_id)

    threading.Thread(target=_run, daemon=True).start()
    return True, 'Re-crawl spuštěn na pozadí'

# ============================================================================
# VECTOR SEARCH (hnswlib)
# ============================================================================

def _init_hnsw(dim=384):
    """Create a fresh hnswlib index with given dimension."""
    global _hnsw_index
    idx = hnswlib.Index(space='cosine', dim=dim)
    idx.init_index(max_elements=HNSW_MAX_ELEMENTS, ef_construction=200, M=16)
    idx.set_ef(50)
    _hnsw_index = idx
    return idx


def _available_memory_mb():
    """Best-effort free RAM in MB, or ``None`` when it cannot be determined.

    Reads ``/proc/meminfo`` (Linux, incl. Termux) and falls back to
    ``os.sysconf``; returns ``None`` on platforms that expose neither, which
    callers treat as "don't know, don't block".
    """
    try:
        with open('/proc/meminfo') as handle:
            for line in handle:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        pass
    try:
        pages = os.sysconf('SC_AVPHYS_PAGES')
        size = os.sysconf('SC_PAGE_SIZE')
        if pages > 0 and size > 0:
            return pages * size / (1024.0 * 1024.0)
    except (ValueError, OSError, AttributeError):
        pass
    return None


def _battery_status():
    """Return ``(percent, charging)`` from sysfs, or ``(None, None)`` if unknown."""
    for base in ('/sys/class/power_supply/BAT0', '/sys/class/power_supply/battery'):
        try:
            with open(f"{base}/capacity") as handle:
                percent = int(handle.read().strip())
        except (OSError, ValueError):
            continue
        charging = True
        try:
            with open(f"{base}/status") as handle:
                charging = handle.read().strip().lower() in ('charging', 'full')
        except OSError:
            charging = True
        return percent, charging
    return None, None


def _embeddings_allowed_by_resources():
    """Automatic-mode gate: keep vectors only when RAM and battery allow."""
    free_mb = _available_memory_mb()
    if free_mb is not None and free_mb < EMBED_MIN_FREE_MB:
        return False
    percent, charging = _battery_status()
    if percent is not None and not charging and percent < EMBED_MIN_BATTERY_PCT:
        return False
    return True


def embeddings_enabled():
    """Whether vector search should run at all in this configuration.

    False when embeddings are explicitly disabled, or the numpy/hnswlib stack
    is missing (mobile profile), or automatic mode finds the device low on RAM
    or battery. In every one of those cases search falls back to FTS5.
    """
    if np is None or hnswlib is None:
        return False
    if EMBEDDINGS_PREF in ('0', 'false', 'off', 'no', 'ne'):
        return False
    if EMBEDDINGS_PREF in ('1', 'true', 'on', 'yes', 'ano'):
        return True
    return _embeddings_allowed_by_resources()


def get_hnsw_index():
    """Return (or create+populate) global hnswlib index, or ``None`` if disabled.

    In low-memory mode the index is never built: the per-page embeddings plus
    the hnswlib graph can easily exceed a phone's budget, and full-text search
    still works. Callers must handle ``None``.
    """
    global _hnsw_index
    if not embeddings_enabled():
        return None
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
    """Rebuild index from scratch (called after delete). No-op if disabled."""
    global _hnsw_index
    if not embeddings_enabled():
        _hnsw_index = None
        return
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
    if not embeddings_enabled():
        return None
    try:
        return get_model().encode(text[:EMBED_MAX_CHARS])
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
    """Hybrid search: 60% vector + 35% FTS5 + 5% SEO, times the site boost."""
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
        page = _attach_priority(data['page'])
        vector_score = data.get('vector_score', 0)
        fts_score = data.get('fts_score', 0)
        seo_score = page.get('seo_score', 0) * 0.05  # 5%

        multiplier = page['search_priority_multiplier']
        final_score = (vector_score + fts_score + seo_score) * multiplier
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


def _site_priority_multiplier(site_id):
    """Search boost for a site (3x by default for local sites, 1x otherwise)."""
    if not site_id:
        return PUBLIC_SITE_PRIORITY_MULTIPLIER
    site = get_site_by_id(site_id)
    if not site:
        return PUBLIC_SITE_PRIORITY_MULTIPLIER
    try:
        return float(site.get('search_priority_multiplier') or PUBLIC_SITE_PRIORITY_MULTIPLIER)
    except (TypeError, ValueError):
        return PUBLIC_SITE_PRIORITY_MULTIPLIER


def vector_search(query, limit=25, filter_type=None):
    """Search with hnswlib; local sites are boosted by their multiplier.

    Returns ``[]`` when embeddings are disabled (low-memory mode) so
    :func:`hybrid_search` silently falls back to full-text search.
    """
    idx = get_hnsw_index()
    if idx is None or idx.element_count == 0:
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

        multiplier = _site_priority_multiplier(page.get('site_id'))
        page['search_priority_multiplier'] = multiplier
        page['is_local'] = multiplier > PUBLIC_SITE_PRIORITY_MULTIPLIER

        relevance = (1 - distance) * 100 * multiplier
        if query.lower() in (page.get('og_title', '') + page.get('title', '')).lower():
            relevance += 10 * multiplier
        if page.get('schema_type'):
            relevance += 5 * multiplier
        if page.get('published_timestamp', 0) > int((datetime.now() - timedelta(days=30)).timestamp()):
            relevance += 3 * multiplier

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

    # The multiplier can push a later neighbour above an earlier one, so sort
    # again after scoring rather than trusting the approximate-index order.
    results.sort(key=lambda x: x['relevance'], reverse=True)
    return results[:limit]


def _attach_priority(page):
    """Make sure a page carries its site's search boost for hybrid scoring."""
    if 'search_priority_multiplier' not in page or page.get('search_priority_multiplier') is None:
        multiplier = _site_priority_multiplier(page.get('site_id'))
        page['search_priority_multiplier'] = multiplier
        page['is_local'] = multiplier > PUBLIC_SITE_PRIORITY_MULTIPLIER
    return page
def fts_search(query, limit=25):
    """Search using FTS5 full-text search with a diacritic-folded query."""
    results = []
    match_expr = build_fts_query(query)
    if not match_expr:
        return results
    try:
        rows = execute_db_fetchall("""
            SELECT p.* FROM pages_fts fts
            JOIN pages p ON fts.page_id = p.id
            WHERE pages_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (match_expr, limit * 2))
        for row in rows:
            results.append(dict(row))
    except Exception as e:
        # A malformed expression must never break the whole search.
        print(f"FTS5 search error: {e}")
    return results


# ============================================================================
# CRAWLING
# ============================================================================

def discover_sitemaps_and_feeds(site_url, site_id, max_pages):
    """Phase 1: detect sitemaps/feeds via GET with robots.txt check."""
    parsed = urlparse(site_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    # Check robots.txt first (skipped automatically for local sites)
    if not is_allowed(site_url, site_id=site_id):
        print(f"Robots.txt disallows crawling {site_url}")
        return []

    # Collect candidate URLs
    candidate_urls = []
    try:
        resp = _http_get(urljoin(base_url, '/robots.txt'),
                         timeout=DISCOVERY_TIMEOUT,
                         max_retries=DISCOVERY_MAX_RETRIES,
                         purpose='robots-discovery')
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                if line.lower().startswith('sitemap:'):
                    sitemap_url = line.split(':', 1)[1].strip()
                    candidate_urls.append(sitemap_url)
    except Exception as e:
        log_error(f"Could not probe robots.txt for {base_url}", e)

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
            # Check robots.txt for this URL (skipped for local sites)
            if not is_allowed(url, site_id=site_id):
                continue

            resp = _http_get(url,
                             timeout=DISCOVERY_TIMEOUT,
                             max_retries=DISCOVERY_MAX_RETRIES,
                             purpose='source-discovery')
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
        except Exception as e:
            log_error(f"Could not probe discovered source {url}", e)

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
        resp = _http_get(url, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES,
                         purpose='sitemap-fetch')
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
        resp = _http_get(url, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES,
                         purpose='feed-fetch')
        if resp.status_code != 200:
            return []
        feed = feedparser.parse(resp.content)
        for entry in feed.entries:
            if hasattr(entry, 'link'):
                urls.append(entry.link)
            elif hasattr(entry, 'links') and entry.links:
                urls.append(entry.links[0].get('href', ''))
    except Exception as e:
        log_error(f"Error parsing feed {url}", e)
    return urls


def crawl_homepage_for_links(site_url, max_pages):
    urls = []
    domain = get_domain(site_url)
    try:
        if not is_allowed(site_url):
            return []

        resp = _http_get(site_url, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES,
                         purpose='homepage-crawl')
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.content, 'lxml')
            for a in soup.find_all('a', href=True):
                full_url = normalize_url(urljoin(site_url, a['href']))
                if get_domain(full_url) == domain and full_url not in urls:
                    urls.append(full_url)
                if len(urls) >= max_pages:
                    break
    except Exception as e:
        log_error(f"Homepage crawl failed for {site_url}", e)
    return urls


def _queue_url(site_id, url, priority=None, allow_indexed_refresh=False,
               stale_after_seconds=None, revive_error=True):
    """Queue a URL for crawling unless it is disallowed or already known.

    Local sites ignore robots.txt and their URLs are queued with the most
    urgent priority so the user sees results quickly.
    """
    if not url:
        return False
    url = normalize_url(url)
    if not url:
        return False
    site = get_site_by_id(site_id) if site_id else None
    is_local = bool(site and site.get('is_local'))
    if not is_allowed(url, site_id=site_id, is_local=is_local):
        return False

    if priority is None:
        priority = LOCAL_QUEUE_PRIORITY if is_local else DEFAULT_QUEUE_PRIORITY
    elif is_local:
        priority = min(priority, LOCAL_QUEUE_PRIORITY)

    now = int(time.time())
    stale_cutoff = None
    if stale_after_seconds and stale_after_seconds > 0:
        stale_cutoff = now - int(stale_after_seconds)

    normalized_hash = url_hash(url)

    # If this URL already sits in the queue, avoid duplicate rows.
    existing_q = execute_db_fetchone(
        """SELECT id, status, retry_count, created_at
           FROM crawl_queue
           WHERE site_id = ? AND (url = ? OR url_hash = ?)
           ORDER BY id DESC LIMIT 1""",
        (site_id, url, normalized_hash)
    )
    if existing_q:
        row_id, status, retry_count, created_at = existing_q
        if status in ('pending', 'locked', 'completed'):
            return False
        if status == 'error' and revive_error:
            should_revive = (retry_count < MAX_RETRIES)
            if not should_revive and stale_cutoff is not None and (created_at or 0) <= stale_cutoff:
                should_revive = True
            if should_revive:
                new_retry = retry_count if retry_count < MAX_RETRIES else 0
                execute_db(
                    """UPDATE crawl_queue
                       SET status='pending', locked_by='', scheduled_at=0,
                           retry_count=?, error_reason='', priority=MIN(priority, ?),
                           url=?, url_hash=?
                       WHERE id=?""",
                    (new_retry, priority, url, normalized_hash, row_id), commit=True
                )
                return True
        return False

    # Check if already indexed
    existing_page = execute_db_fetchone(
        "SELECT id, indexed_at FROM pages WHERE url_hash=?",
        (normalized_hash,)
    )
    if existing_page:
        if not allow_indexed_refresh:
            return False
        indexed_at = existing_page[1] or 0
        if stale_cutoff is None:
            return False
        if indexed_at >= stale_cutoff:
            return False

    try:
        execute_db(
            """INSERT INTO crawl_queue (site_id, url, url_hash, status, priority, scheduled_at)
               VALUES (?, ?, ?, 'pending', ?, 0)""",
            (site_id, url, normalized_hash, priority), commit=True
        )
        return True
    except Exception:
        return False


def index_source(source_id, allow_indexed_refresh=False, stale_after_seconds=None,
                 queue_budget=None):
    """Queue every URL a single non-domain source contributes.

    Returns the number of URLs considered. Safe to call from a worker thread.
    """
    source = get_source_by_id(source_id)
    if not source:
        return 0
    if source['status'] != 'active':
        return 0

    site_id = source['site_id']
    url = source['url']
    source_type = source['source_type']
    priority = source['priority'] or 5
    queued = 0

    def _enqueue(candidate_url, candidate_priority):
        nonlocal queued
        if queue_budget is not None and queued >= queue_budget:
            return
        if _queue_url(
            site_id,
            candidate_url,
            candidate_priority,
            allow_indexed_refresh=allow_indexed_refresh,
            stale_after_seconds=stale_after_seconds
        ):
            queued += 1

    try:
        if source_type == WIKI_SOURCE_TYPE:
            # The Wikipedia importer streams articles straight into ``pages``;
            # it does not go through the crawl queue (there is no page to fetch).
            site = get_site_by_id(site_id) or {}
            budget = queue_budget
            if budget is None:
                budget = int(site.get('max_pages') or 500)
            return run_wiki_import(source, max_pages=budget,
                                   allow_indexed_refresh=allow_indexed_refresh)
        if source_type == 'url':
            _enqueue(normalize_url(url), priority)
        elif source_type == 'sitemap':
            for u in parse_sitemap(url):
                n = normalize_url(u)
                if n:
                    _enqueue(n, priority)
        elif source_type in FEED_SOURCE_TYPES:
            for u in parse_feed(url):
                n = normalize_url(u)
                if n:
                    _enqueue(n, priority)
        execute_db(
            "UPDATE site_sources SET last_checked = strftime('%s','now') WHERE id = ?",
            (source_id,), commit=True
        )
    except Exception as e:
        log_error(f"Indexing source {source_id} ({url}) failed", e)

    return queued


def index_source_async(source_id):
    """Run index_source off the request path so adding a source never blocks."""

    def _run():
        try:
            index_source(source_id)
        except Exception as e:
            log_error(f"Background indexing of source {source_id} failed", e)

    threading.Thread(target=_run, daemon=True).start()


def _revive_retryable_errors(site_id, stale_after_seconds=None):
    """Move retryable/old queue errors back to pending without touching locked work."""
    rows = execute_db_fetchall(
        """SELECT id, retry_count, created_at
           FROM crawl_queue
           WHERE site_id = ? AND status = 'error' AND locked_by = ''""",
        (site_id,)
    )
    revived = 0
    now = int(time.time())
    stale_cutoff = None
    if stale_after_seconds and stale_after_seconds > 0:
        stale_cutoff = now - int(stale_after_seconds)

    for row_id, retry_count, created_at in rows:
        should_revive = retry_count < MAX_RETRIES
        if not should_revive and stale_cutoff is not None and (created_at or 0) <= stale_cutoff:
            should_revive = True
        if not should_revive:
            continue
        new_retry = retry_count if retry_count < MAX_RETRIES else 0
        execute_db(
            """UPDATE crawl_queue
               SET status='pending', locked_by='', scheduled_at=0, retry_count=?,
                   error_reason=''
               WHERE id = ?""",
            (new_retry, row_id), commit=True
        )
        revived += 1
    return revived


def _queue_stale_pages(site_id, remaining, stale_after_seconds):
    """Queue already indexed pages only when they are stale."""
    if remaining <= 0 or stale_after_seconds <= 0:
        return 0
    rows = execute_db_fetchall(
        """SELECT url
           FROM pages
           WHERE site_id = ?
           ORDER BY indexed_at ASC
           LIMIT ?""",
        (site_id, remaining * 3)
    )
    added = 0
    for (candidate_url,) in rows:
        if added >= remaining:
            break
        if _queue_url(site_id, candidate_url, DEFAULT_QUEUE_PRIORITY,
                      allow_indexed_refresh=True,
                      stale_after_seconds=stale_after_seconds):
            added += 1
    return added


def site_has_wiki_source(site_id):
    """True when a site is driven by the Wikipedia importer, not the crawler."""
    row = execute_db_fetchone(
        "SELECT 1 FROM site_sources WHERE site_id = ? AND source_type = ? LIMIT 1",
        (site_id, WIKI_SOURCE_TYPE)
    )
    return row is not None


def phase_1_discovery(site_url, max_pages=500, allow_indexed_refresh=False):
    site_url = normalize_url(site_url)
    if not site_url:
        return None, 0

    # Check if site already exists
    domain = get_domain(site_url)
    existing = execute_db_fetchone("SELECT id FROM sites WHERE canonical_url = ?", (domain,))
    recrawl_mode = bool(existing or allow_indexed_refresh)
    stale_after_seconds = RECRAWL_STALE_AFTER_SECONDS if recrawl_mode else None
    if existing:
        site_id = existing[0]
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
        "SELECT id FROM site_sources "
        "WHERE site_id = ? AND source_type != 'domain' AND status = 'active'",
        (site_id,)
    )
    added = 0
    if recrawl_mode:
        added += _revive_retryable_errors(site_id, stale_after_seconds=stale_after_seconds)

    for (source_id,) in manual_sources:
        remaining = max(max_pages - added, 0)
        if remaining <= 0:
            break
        added += index_source(
            source_id,
            allow_indexed_refresh=recrawl_mode,
            stale_after_seconds=stale_after_seconds,
            queue_budget=remaining
        )

    # A Wikipedia site is served entirely by the dump/API importer. Running the
    # generic sitemap/homepage crawler against wikipedia.org would be pointless
    # and very expensive, so skip it and let the wiki sources above do the work.
    if site_has_wiki_source(site_id):
        execute_db(
            "UPDATE sites SET last_crawled = strftime('%s','now') WHERE id = ?",
            (site_id,), commit=True
        )
        return site_id, added

    discovered = discover_sitemaps_and_feeds(site_url, site_id, max_pages)
    urls_to_add = []
    seen_hashes = set()

    def _remember(url, priority):
        n = normalize_url(url)
        if not n:
            return
        h = url_hash(n)
        if h in seen_hashes:
            return
        seen_hashes.add(h)
        urls_to_add.append((n, priority))

    for item in discovered:
        if item['type'] == 'sitemap':
            for u in parse_sitemap(item['url']):
                _remember(u, 3)
        elif item['type'] in ('rss', 'atom'):
            for u in parse_feed(item['url']):
                _remember(u, 1)

    if not urls_to_add:
        for u in crawl_homepage_for_links(site_url, max_pages):
            _remember(u, 5)

    for url, priority in urls_to_add:
        if added >= max_pages:
            break
        if _queue_url(site_id, url, priority,
                      allow_indexed_refresh=recrawl_mode,
                      stale_after_seconds=stale_after_seconds):
            added += 1

    if recrawl_mode and added < max_pages:
        added += _queue_stale_pages(
            site_id,
            max_pages - added,
            stale_after_seconds=stale_after_seconds or 0
        )

    execute_db(
        "UPDATE sites SET last_crawled = strftime('%s','now') WHERE id = ?",
        (site_id,), commit=True
    )
    return site_id, added


# ============================================================================
# CZECH WIKIPEDIA IMPORTER (streaming, memory-bounded)
# ============================================================================
#
# Czech Wikipedia (and every other language) publishes the article wikitext as
# a ``pages-articles-multistream.xml.bz2`` dump plus an
# ``*-index.txt.bz2`` index. The importer offers two modes:
#
#   * ``api`` (default): walk the MediaWiki action API in batches. No local
#     storage, works immediately, ideal on a phone.
#   * ``dump``: point a source at a ``file:///.../cswiki-....xml.bz2`` dump and
#     stream it with ``xml.etree`` in pull mode. Nothing is loaded into RAM and
#     only one ``<page>`` is alive at a time. This is the offline-bulk mode.
#
# Bulk-importing via the dump is far cheaper than crawling the live site: one
# deterministic file, no per-article HTTP round-trips and no rate limits.

# A wiki source URL is either a plain language selector (``https://cs.wikipedia.org``)
# or a ``file://`` path pointing at a locally downloaded dump. Keeping the dump
# off the repository (see .gitignore) is what makes the mobile workflow viable.
WIKI_DUMP_SOURCE_PREFIX = 'file://'


def wiki_lang_and_importer(source):
    """Return ``(lang, importer, dump_path)`` for a wiki source row.

    ``dump_path`` is set only when the source points at a local dump file.
    """
    raw = (source.get('url') or '').strip()
    importer = (source.get('importer') or 'dump').strip().lower()
    if importer not in WIKI_IMPORTERS:
        importer = 'api'
    dump_path = ''
    if raw.startswith(WIKI_DUMP_SOURCE_PREFIX):
        dump_path = raw[len(WIKI_DUMP_SOURCE_PREFIX):]
    return lang_from_wiki_url(raw), importer, dump_path


def lang_from_wiki_url(url):
    """Extract the language code from a wiki URL, dump path or bare code."""
    host = extract_host(url)
    match = re.match(r'([a-z\-]+)\.(?:m\.)?wikipedia\.org$', host or '')
    if match:
        return match.group(1)
    value = (url or '').strip().lower()
    if re.fullmatch(r'[a-z]{2,3}(-[a-z]+)?', value):
        return value
    # A local dump filename looks like ``cswiki-latest-pages-articles...``.
    match = re.search(r'/([a-z\-]+)wiki-', value)
    if match:
        return match.group(1)
    return WIKI_DEFAULT_LANG


def run_wiki_import(source, max_pages=None, allow_indexed_refresh=False):
    """Import articles from a Wikipedia source into the local index.

    The import is streaming, idempotent (``store_page`` upserts by URL hash),
    respects ``max_pages`` and stores its progress on the source row so a
    pause/resume never restarts from scratch. Returns the number of articles
    newly or newly-refreshed indexed.
    """
    source_id = source['id']
    site_id = source['site_id']
    lang, importer, dump_path = wiki_lang_and_importer(source)

    try:
        state = json.loads(source.get('import_state') or '{}')
        if not isinstance(state, dict):
            state = {}
    except Exception:
        state = {}

    resume_title = state.get('next_title') or ''
    done = 0
    last_title = resume_title
    site = get_site_by_id(site_id) or {}
    if max_pages is None:
        max_pages = int(site.get('max_pages') or 500)
    max_pages = max(1, int(max_pages))

    base_imported = int(state.get('imported') or 0)

    def _progress(next_title):
        """Persist import progress so a pause/resume continues where it stopped."""
        state['next_title'] = next_title
        state['updated_at'] = int(time.time())
        # Absolute, not incremental: _progress is called repeatedly in one run.
        state['imported'] = base_imported + done
        execute_db(
            "UPDATE site_sources SET import_state = ?, last_checked = strftime('%s','now') "
            "WHERE id = ?",
            (json.dumps(state), source_id), commit=True
        )
        execute_db(
            "UPDATE sites SET last_import_at = strftime('%s','now'), import_state = ? WHERE id = ?",
            (json.dumps(state), site_id), commit=True
        )

    # Ask the iterator for one extra article: if it yields it we know the dump
    # has more to give and must record a resume point; if it stops short we have
    # seen everything and can start over next time.
    stop = False
    for article in _iter_wiki_articles(lang, resume_title, max_pages + 1, importer, dump_path):
        if SHUTDOWN_FLAG or done >= max_pages:
            stop = True
            break
        last_title = article['title']

        # Filtering: drop redirects and non-encyclopaedic namespaces. Redirects
        # are still recorded as aliases so a search for the redirect title finds
        # the target article.
        if article.get('redirect'):
            _record_wiki_alias(site_id, lang, article['redirect'], article['title'])
            continue
        if article.get('skip'):
            continue

        page = _wiki_article_to_page(article, site_id, lang)
        if not page:
            continue

        if not allow_indexed_refresh:
            existing = execute_db_fetchone(
                "SELECT 1 FROM pages WHERE url_hash = ?", (page['url_hash'],))
            if existing:
                continue

        try:
            store_page(page)
            done += 1
        except Exception as e:
            log_error(f"Could not store wiki article {article.get('title')}", e)
        if done and done % 50 == 0:
            _progress(article['title'])

    # Remember the last title seen so the next run continues (the boundary title
    # is re-read and skipped as a duplicate, which is harmless and safe).
    if stop and last_title:
        _progress(last_title)
    else:
        # A full pass over the dump: start again from the top next time.
        _progress('')
    return done


def _record_wiki_alias(site_id, lang, alias, target):
    """Store a redirect title as a site alias so it never becomes a dead end."""
    site = get_site_by_id(site_id)
    if not site:
        return
    try:
        aliases = json.loads(site.get('aliases') or '[]')
        if not isinstance(aliases, list):
            aliases = []
    except Exception:
        aliases = []
    alias_url = f"https://{lang}.wikipedia.org/wiki/{quote(alias.replace(' ', '_'))}"
    if alias_url not in aliases:
        aliases.append(alias_url)
        execute_db("UPDATE sites SET aliases = ? WHERE id = ?",
                   (json.dumps(aliases), site_id), commit=True)


def _wiki_article_to_page(article, site_id, lang):
    """Convert a parsed wiki article into the shared ``pages`` record shape."""
    title = article.get('title') or ''
    if not title:
        return None
    slug = quote(title.replace(' ', '_'))
    url = f"https://{lang}.wikipedia.org/wiki/{slug}"
    body = article.get('text') or ''

    # MediaWiki entity HTML embeds JSON-LD in a <script> tag, and the importer
    # sets ``article['schema_extra']``; feed both through the shared extractor
    # so wiki pages get identical OG/schema handling to crawled pages.
    html = article.get('html') or ''
    page = extract_page_content(
        url, site_id, html=html or f"<html><head><title>{title}</title></head>"
                                   f"<body>{body}</body></html>",
        body_text=body, canonical_hint=url
    )
    if not page:
        return None

    # The dump already gives us a clean title; prefer it over the <title> tag.
    page['title'] = title
    page['og_title'] = article.get('display_title') or title
    if article.get('description'):
        page['og_description'] = article['description']
    if article.get('timestamp'):
        page['published_timestamp'] = article['timestamp']
    page['schema_type'] = page.get('schema_type') or 'Article'
    page['seo_score'] = calculate_seo_score(page)
    # The title carries the strongest signal for wiki lookups, so prepend it.
    body_for_index = f"{title}. {body}"
    page['body_text'] = re.sub(r'\s+', ' ', body_for_index).strip()[:MAX_BODY_CHARS]
    page['embedding'] = _embedding_for(page)
    return page


# Non-encyclopaedic namespaces are skipped: talk pages, user pages, categories,
# templates, files, portals and Wikipedia-internal meta pages.
_WIKI_SKIP_NAMESPACES = (
    'talk:', 'user:', 'user talk:', 'wikipedia:', 'wikipedia talk:',
    'file:', 'file talk:', 'mediawiki:', 'mediawiki talk:', 'template:',
    'template talk:', 'help:', 'help talk:', 'category:', 'category talk:',
    'portal:', 'portal talk:', 'draft:', 'draft talk:', 'module:',
    'module talk:', 'special:', 'book:', 'education program:',
)
_WIKI_REDIRECT_RE = re.compile(r'^\s*#(?:REDIRECT|PŘESMĚRUJ|PŘESMĚROVAT)\s*:?\s*\[\[([^\]]+)\]\]',
                               re.IGNORECASE)


def _wiki_text_from_wikitext(wikitext):
    """Crude but dependency-free wikitext -> plain text conversion.

    The Czech Wikipedia dump stores wikitext; stripping the common markup is
    enough for full-text search and avoids adding a parser dependency. Template
    bodies, tables and external links are removed because they add noise.
    """
    text = wikitext or ''
    # Drop comments and templates (handling simple nesting).
    text = re.sub(r'<!--.*?-->', ' ', text, flags=re.DOTALL)
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r'\{\{[^{}]*\}\}', ' ', text, flags=re.DOTALL)
    # Drop tables and image/file links.
    text = re.sub(r'\{\|.*?\|\}', ' ', text, flags=re.DOTALL)
    text = re.sub(r'\[\[(?:File|Soubor|Image|Kategorie|Category):[^\]]*\]\]', ' ',
                  text, flags=re.IGNORECASE)
    # ``[[target|label]]`` -> label, ``[[target]]`` -> target.
    text = re.sub(r'\[\[(?:[^\]|]*\|)?([^\]]*)\]\]', r'\1', text)
    text = re.sub(r'\[(?:https?://\S+)\s+([^\]]*)\]', r'\1', text)
    text = re.sub(r'\[https?://\S+\]', ' ', text)
    text = re.sub(r"''+", '', text)
    # Headings, list markers, table cells, tags.
    text = re.sub(r'^[=]{2,}.*?[=]{2,}\s*$', ' ', text, flags=re.MULTILINE)
    text = re.sub(r'^[*#:;]+\s*', ' ', text, flags=re.MULTILINE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&nbsp;', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _iter_wiki_articles(lang, resume_title, max_pages, importer='api', dump_path=''):
    """Yield article dicts from a Wikipedia source.

    Two importers are supported:

    * ``api`` (default) walks the MediaWiki action API in batches. It needs a
      network connection but no local storage, so it is the sane default on a
      phone.
    * ``dump`` streams a locally downloaded ``pages-articles-multistream`` dump
      with ``xml.etree`` in pull mode: nothing is ever read into a string and
      only the current ``<page>`` element is held in memory. This is the
      preferred mode offline and is dramatically cheaper than crawling the live
      site one article at a time.

    Both are lazy, so the caller can stop after ``max_pages``.
    """
    if importer == 'dump' and dump_path:
        yield from _iter_wiki_articles_dump(dump_path, resume_title, max_pages)
        return
    yield from _iter_wiki_articles_api(lang, resume_title, max_pages)


def _xml_child(element, name):
    """Find a direct child by local name, ignoring the XML namespace.

    ``ElementTree.Element`` is falsy when it has no children, so ``find(a) or
    find(b)`` silently loses empty elements like ``<text>``. This helper uses
    explicit ``None`` checks instead.
    """
    if element is None:
        return None
    child = element.find('{*}' + name)
    if child is None:
        child = element.find(name)
    return child


def _wiki_page_from_element(page_el):
    """Build an article dict from a parsed ``<page>`` XML element."""
    def _text(name):
        child = _xml_child(page_el, name)
        return child.text.strip() if child is not None and child.text else ''

    title = _text('title')
    ns = _text('ns') or '0'
    redirect_el = _xml_child(page_el, 'redirect')
    revision = _xml_child(page_el, 'revision')

    raw_text = ''
    timestamp = 0
    if revision is not None:
        text_el = _xml_child(revision, 'text')
        if text_el is not None and text_el.text:
            raw_text = text_el.text
        ts_el = _xml_child(revision, 'timestamp')
        if ts_el is not None and ts_el.text:
            timestamp = _parse_datetime(ts_el.text)

    article = {
        'title': title,
        'ns': ns,
        'redirect': (redirect_el.get('title') if redirect_el is not None else '') or '',
        'timestamp': timestamp,
        'html': '',
    }
    if article['redirect']:
        return article

    if str(ns) not in ('', '0') or title.lower().startswith(_WIKI_SKIP_NAMESPACES):
        article['skip'] = True
        return article

    match = _WIKI_REDIRECT_RE.match(raw_text)
    if match:
        # ``#REDIRECT [[Target]]`` becomes an alias, not a page.
        article['redirect'] = match.group(1).split('|')[0].strip()
        return article

    article['skip'] = False
    article['text'] = _wiki_text_from_wikitext(raw_text)
    return article


def _iter_wiki_articles_dump(dump_path, resume_title, max_pages):
    """Stream ``<page>`` elements out of a local (b)z2 XML dump.

    Uses ``ElementTree.iterparse`` so the file is consumed incrementally and
    each element is dropped once handled. ``max_pages`` caps how many articles
    are even materialised, and ``resume_title`` lets a paused import continue
    where it stopped instead of rescanning from the beginning.
    """
    import bz2
    try:
        import xml.etree.ElementTree as ET
    except Exception:
        return

    try:
        if dump_path.endswith('.bz2'):
            handle = bz2.open(dump_path, 'rb')
        else:
            handle = open(dump_path, 'rb')
    except OSError as e:
        log_error(f"Could not open wiki dump {dump_path}", e)
        return

    yielded = 0
    try:
        # ``events=('end',)`` plus clearing each element keeps memory flat; the
        # multistream dump has a single <mediawiki> root, so iterparse is valid.
        for _event, element in ET.iterparse(handle, events=('end',)):
            if not element.tag.endswith('page'):
                continue
            article = _wiki_page_from_element(element)
            element.clear()
            if not article.get('title'):
                continue
            # Titles are ordered, so once we reach the resume marker everything
            # after it is a candidate; earlier titles were already imported.
            if resume_title and article['title'] < resume_title:
                continue
            yield article
            yielded += 1
            if yielded >= max_pages:
                return
    except Exception as e:
        log_error(f"Error streaming wiki dump {dump_path}", e)
    finally:
        try:
            handle.close()
        except Exception:
            pass


def _wiki_api_url(lang):
    """Action-API endpoint for a language, honouring a mirror override."""
    if WIKI_API_BASE:
        return f"{WIKI_API_BASE}/w/api.php"
    return f"https://{lang}.wikipedia.org/w/api.php"


def _iter_wiki_articles_api(lang, resume_title, max_pages):
    """Fallback importer that walks the MediaWiki action API."""
    api = _wiki_api_url(lang)
    params = {
        'action': 'query', 'format': 'json', 'list': 'allpages',
        'aplimit': str(WIKI_API_BATCH), 'apfilterredir': 'nonredirects',
        'apnamespace': '0',
    }
    if resume_title:
        params['apfrom'] = resume_title
    yielded = 0
    while yielded < max_pages:
        try:
            resp = _http_get(api, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES,
                             params=params, purpose='wiki-api')
            if resp.status_code != 200:
                return
            payload = resp.json()
        except Exception as e:
            log_error(f"Wiki API list failed for {lang}", e)
            return
        pages = (payload.get('query', {}) or {}).get('allpages', []) or []
        if not pages:
            return
        titles = [p.get('title') for p in pages if p.get('title')]
        for article in _fetch_wiki_api_batch(lang, titles):
            yield article
            yielded += 1
            if yielded >= max_pages:
                return
        cont = payload.get('continue', {}) or {}
        if not cont:
            return
        params.update(cont)


def _fetch_wiki_api_batch(lang, titles):
    """Fetch plaintext extracts for a batch of titles via the API."""
    if not titles:
        return
    api = _wiki_api_url(lang)
    params = {
        'action': 'query', 'format': 'json', 'prop': 'extracts|info',
        'explaintext': '1', 'exintro': '0', 'inprop': 'url',
        'redirects': '1', 'titles': '|'.join(titles),
    }
    try:
        resp = _http_get(api, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES,
                         params=params, purpose='wiki-api')
        if resp.status_code != 200:
            return
        payload = resp.json()
    except Exception as e:
        log_error(f"Wiki API extract failed for {lang}", e)
        return
    for page in ((payload.get('query', {}) or {}).get('pages', {}) or {}).values():
        if 'missing' in page:
            continue
        yield {
            'title': page.get('title') or '',
            'redirect': '',
            'timestamp': 0,
            'text': page.get('extract') or '',
            'html': '',
            'skip': False,
        }


def empty_page_data(url, site_id):
    """A page record with every field the store expects, blanked out."""
    return {
        'url': url, 'site_id': site_id,
        'url_hash': url_hash(canonicalize_url(url) or url),
        'title': '', 'og_title': '', 'og_description': '', 'og_image': '',
        'favicon_url': '', 'body_text': '', 'images': [],
        'schema_type': '', 'schema_details': {}, 'audio_url': '',
        'has_audio': 0, 'published_timestamp': 0, 'seo_score': 0.0,
        'embedding': None,
    }


def _decode_response(resp, content=None):
    """Return bytes plus the best-effort decoded text of an HTTP response."""
    raw = content if content is not None else resp.content
    charset = None
    try:
        charset = resp.encoding
    except Exception:
        charset = None
    if not charset:
        try:
            charset = resp.apparent_encoding
        except Exception:
            charset = None
    for candidate in (charset, 'utf-8', 'windows-1250'):
        if not candidate:
            continue
        try:
            return raw, raw.decode(candidate, errors='strict')
        except (LookupError, UnicodeDecodeError):
            continue
    return raw, raw.decode('utf-8', errors='replace')


def _body_text_from_soup(soup, limit):
    """Extract readable body text, preferring article/main over loose tags.

    Selecting ``article``/``main`` first prevents the same paragraph being
    counted twice (once via ``p`` and once via its container), which both
    bloats the DB and skews relevance.
    """
    parts = []
    seen = set()

    def add(text):
        text = re.sub(r'\s+', ' ', text or '').strip()
        if not text or text in seen:
            return
        seen.add(text)
        parts.append(text)

    containers = soup.select('article') or soup.select('main') or []
    if containers:
        for container in containers:
            for tag in container.find_all(['h1', 'h2', 'h3', 'p', 'li']):
                add(tag.get_text())
    else:
        # Fall back to the body but drop chrome that is never article content.
        body = soup.body or soup
        for tag in body.find_all(['script', 'style', 'nav', 'footer', 'header', 'aside', 'noscript']):
            tag.decompose()
        for tag in body.find_all(['h1', 'h2', 'h3', 'p', 'li']):
            add(tag.get_text())
    return ' '.join(parts)[:limit]


def extract_page_content(url, site_id, response=None, body_text=None,
                         html=None, canonical_hint=None):
    """Extract searchable metadata and body text from a fetched page.

    ``html``/``body_text``/``canonical_hint`` let callers that already parsed a
    response (the Wikimedia importer) reuse the same normalisation without a
    second network fetch.
    """
    page_data = empty_page_data(url, site_id)
    try:
        if html is None:
            resp = response
            if resp is None:
                resp = _http_get(url, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES,
                                 purpose='page-extract')
            if resp.status_code != 200:
                return None
            raw, text = _decode_response(resp)
        else:
            raw = html.encode('utf-8') if isinstance(html, str) else html
            text = html if isinstance(html, str) else raw.decode('utf-8', errors='replace')
        # Parse the decoded text, not the raw bytes: lxml ignores the HTTP
        # charset header and would mangle windows-1250 Czech pages if handed
        # bytes. Raw bytes are still kept for JSON-LD (extruct sniffs encoding).
        soup = BeautifulSoup(text, 'lxml')

        def meta(*props):
            for prop in props:
                tag = (soup.find('meta', attrs={'property': prop})
                       or soup.find('meta', attrs={'name': prop}))
                if tag and tag.get('content'):
                    return tag['content'].strip()
            return ''

        # Canonical URL: prefer the page's own <link rel=canonical>, then the
        # hint the caller supplied, then the URL we fetched.
        canonical = ''
        link = soup.find('link', rel=lambda v: v and 'canonical' in str(v).lower())
        if link and link.get('href'):
            canonical = canonicalize_url(link['href'], base_url=url)
        canonical = canonical or canonical_hint or url
        canonical = canonicalize_url(canonical, base_url=url) or url
        page_data['url'] = canonical
        page_data['url_hash'] = url_hash(canonical)

        page_data['og_title'] = meta('og:title', 'twitter:title')
        page_data['og_description'] = meta('og:description', 'twitter:description',
                                           'description')
        raw_image = meta('og:image', 'og:image:url', 'twitter:image')
        page_data['og_image'] = urljoin(canonical, raw_image) if raw_image else ''
        page_data['favicon_url'] = urljoin(
            canonical, (soup.find('link', rel='icon') or
                        soup.find('link', rel='shortcut icon') or {}).get('href', '')
        ) if (soup.find('link', rel='icon') or soup.find('link', rel='shortcut icon')) \
            else urljoin(canonical, '/favicon.ico')

        title_tag = soup.find('title')
        page_data['title'] = title_tag.text.strip() if title_tag else ''
        if not page_data['og_title']:
            page_data['og_title'] = page_data['title']

        if body_text is not None:
            page_data['body_text'] = re.sub(r'\s+', ' ', body_text).strip()[:MAX_BODY_CHARS]
        else:
            page_data['body_text'] = _body_text_from_soup(soup, MAX_BODY_CHARS)

        images = []
        for img in soup.find_all('img', src=True):
            try:
                src = urljoin(canonical, img['src'])
            except Exception:
                continue
            images.append({'url': src, 'alt': img.get('alt', '')})
        page_data['images'] = images

        audio = soup.find('audio', src=True)
        if audio:
            page_data['audio_url'] = urljoin(canonical, audio['src'])
            page_data['has_audio'] = 1
        else:
            for a in soup.find_all('a', href=True):
                if any(a['href'].lower().endswith(ext)
                       for ext in ('.mp3', '.m4a', '.wav', '.ogg')):
                    page_data['audio_url'] = urljoin(canonical, a['href'])
                    page_data['has_audio'] = 1
                    break

        # --- Schema.org JSON-LD (tolerates @graph, @type arrays, invalid JSON) ---
        nodes, types = _extract_jsonld(raw, base_url=canonical)
        article_types = ('article', 'blogposting', 'newsarticle', 'techarticle',
                         'scholarlyarticle', 'report', 'webpage', 'podcastepisode')
        chosen_type = ''
        for pref in ('article', 'blogposting', 'newsarticle', 'techarticle',
                     'scholarlyarticle', 'podcastepisode'):
            if pref in types:
                chosen_type = pref
                break
        if not chosen_type and types:
            chosen_type = sorted(types)[0]
        page_data['schema_type'] = chosen_type.title() if chosen_type else ''

        # Store the richest matching node as details, plus a reserved _meta bag.
        details = {}
        for node in nodes:
            node_types = set(_schema_types(node))
            if node_types & set(article_types) or not details:
                details = {k: v for k, v in node.items() if not k.startswith('@')}
                details['@type'] = node.get('@type')
                break

        author = _first_schema_value(nodes, types,
                                     ('article', 'blogposting', 'newsarticle', 'person'),
                                     ('author', 'creator'))
        date_published = _first_schema_value(
            nodes, types, article_types,
            ('datePublished', 'dateCreated', 'uploadDate'))
        breadcrumbs = []
        for node in nodes:
            node_types = set(_schema_types(node))
            # The list may be the node itself (BreadcrumbList) or nested under a
            # ``breadcrumb`` property of an Article/WebPage node.
            candidates = []
            if 'breadcrumblist' in node_types:
                candidates.append(node)
            nested = node.get('breadcrumb')
            if isinstance(nested, dict):
                candidates.append(nested)
            for candidate in candidates:
                for item in candidate.get('itemListElement', []) or []:
                    if not isinstance(item, dict):
                        continue
                    name = item.get('name')
                    if not name and isinstance(item.get('item'), dict):
                        name = item['item'].get('name')
                    if isinstance(name, str) and name.strip() \
                            and name.strip() not in breadcrumbs:
                        breadcrumbs.append(name.strip())
            if breadcrumbs:
                break

        meta_bag = {
            'canonical': canonical,
            'og_type': meta('og:type'),
            'og_site_name': meta('og:site_name'),
            'author': author or meta('author', 'article:author'),
            'breadcrumbs': breadcrumbs,
            'twitter_card': meta('twitter:card'),
            'html_lang': (soup.html.get('lang') if soup.html else '') or '',
        }
        published = (_parse_datetime(date_published)
                     or _parse_datetime(meta('article:published_time',
                                             'datePublished', 'date')))
        page_data['published_timestamp'] = published
        meta_bag['published'] = published
        if details:
            details['_meta'] = meta_bag
            page_data['schema_details'] = details
        else:
            page_data['schema_details'] = {'_meta': meta_bag}

        page_data['seo_score'] = calculate_seo_score(page_data)
        page_data['embedding'] = _embedding_for(page_data)
        return page_data
    except Exception as e:
        log_error(f"Error extracting {url}", e)
        print(f"Error extracting {url}: {e}")
        return None


def _embedding_for(page_data):
    """Bytes for the page embedding, or ``None`` when embeddings are disabled."""
    if not embeddings_enabled():
        return None
    embed_text = ((page_data.get('og_title') or page_data.get('title') or '') + ' ' +
                  (page_data.get('og_description') or '') + ' ' +
                  (page_data.get('body_text') or ''))
    vector = generate_embedding(embed_text)
    if vector is None:
        return None
    return np.asarray(vector, dtype=np.float32).tobytes()


def store_page(page_data):
    """Insert or refresh a page row and update the vector index.

    Shared by the crawler and the Wikipedia importer so both go through exactly
    the same FTS/vector pipeline. Returns the page id.
    """
    url = page_data['url']
    url_h = page_data['url_hash']
    embedding = page_data.get('embedding')
    page_id = None

    existing = execute_db_fetchone("SELECT id FROM pages WHERE url_hash=?", (url_h,))
    if existing:
        execute_db(
            """UPDATE pages SET site_id=?, url=?, title=?, og_title=?, og_description=?,
               og_image=?, favicon_url=?, body_text=?, images=?, schema_type=?
               , schema_details=?, audio_url=?, has_audio=?, published_timestamp=?,
               embedding=COALESCE(?, embedding), seo_score=?, indexed_at=strftime('%s','now')
               WHERE url_hash=?""",
            (page_data['site_id'], url, page_data['title'], page_data['og_title'],
             page_data['og_description'], page_data['og_image'], page_data['favicon_url'],
             page_data['body_text'], json.dumps(page_data['images']),
             page_data['schema_type'], json.dumps(page_data['schema_details']),
             page_data['audio_url'], page_data['has_audio'],
             page_data['published_timestamp'], embedding, page_data['seo_score'], url_h),
            commit=True
        )
        page_id = existing[0]
    else:
        execute_db(
            """INSERT INTO pages (site_id,url,url_hash,title,og_title,og_description,og_image,
               favicon_url,body_text,images,schema_type,schema_details,audio_url,has_audio,
               published_timestamp,embedding,seo_score,indexed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,strftime('%s','now'))""",
            (page_data['site_id'], url, url_h, page_data['title'], page_data['og_title'],
             page_data['og_description'], page_data['og_image'], page_data['favicon_url'],
             page_data['body_text'], json.dumps(page_data['images']),
             page_data['schema_type'], json.dumps(page_data['schema_details']),
             page_data['audio_url'], page_data['has_audio'],
             page_data['published_timestamp'], embedding, page_data['seo_score']),
            commit=True
        )
        page_id = execute_db_fetchone("SELECT last_insert_rowid()")[0]

    if embedding:
        try:
            idx = get_hnsw_index()
            if idx is not None:
                emb = np.frombuffer(embedding, dtype=np.float32)
                idx.add_items(emb.reshape(1, -1), np.array([page_id]))
        except Exception as e:
            # A full or unavailable index must not fail the whole page.
            log_error(f"Could not add page {page_id} to vector index", e)
    return page_id


def _set_queue_retry_or_error(queue_id, reason, retryable=True):
    """Retry pending items with backoff, then mark as permanent error."""
    reason = (reason or 'Unknown crawler error')[:500]
    if not retryable:
        execute_db(
            "UPDATE crawl_queue SET status='error', locked_by='', error_reason=? WHERE id=?",
            (reason, queue_id), commit=True
        )
        return

    row = execute_db_fetchone("SELECT retry_count FROM crawl_queue WHERE id=?", (queue_id,))
    current_retry = (row[0] if row else 0)
    next_retry = current_retry + 1
    delay = min(300, 5 * (2 ** max(0, next_retry - 1)))
    scheduled_at = int(time.time()) + delay
    execute_db(
        """UPDATE crawl_queue
           SET retry_count=?, status='pending', locked_by='', scheduled_at=?, error_reason=?
           WHERE id=?""",
        (next_retry, scheduled_at, reason, queue_id), commit=True
    )
    if next_retry >= MAX_RETRIES:
        execute_db(
            "UPDATE crawl_queue SET status='error', scheduled_at=0, error_reason=? WHERE id=?",
            (reason, queue_id), commit=True
        )


def process_url(queue_id, site_id, url):
    """Fetch and index one URL. Handles 429/403 at HTTP level."""
    try:
        resp = _http_get(url, timeout=REQUEST_TIMEOUT, max_retries=MAX_RETRIES,
                         purpose='queue-fetch')

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
            retryable_http = resp.status_code in RETRYABLE_HTTP_STATUSES
            _set_queue_retry_or_error(
                queue_id,
                f"HTTP {resp.status_code}",
                retryable=retryable_http
            )
            return

        page_data = extract_page_content(url, site_id, response=resp)
        if page_data is None:
            _set_queue_retry_or_error(queue_id, "Extraction returned no content", retryable=True)
            return

        store_page(page_data)

        execute_db(
            "UPDATE crawl_queue SET status='completed', locked_by='', scheduled_at=0, error_reason='' WHERE id=?",
            (queue_id,), commit=True
        )

    except FetchError as e:
        log_error(f"Fetch failed for queue item {queue_id} ({url})", e.exc or e)
        _set_queue_retry_or_error(queue_id, e.message, retryable=e.retryable)
    except Exception as e:
        log_error(f"Unexpected process_url error for {url}", e)
        _set_queue_retry_or_error(
            queue_id,
            _format_network_error(e),
            retryable=_is_retryable_network_error(e)
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
                     AND (cq.scheduled_at=0 OR cq.scheduled_at<=strftime('%s','now'))
                   ORDER BY cq.priority ASC, cq.scheduled_at ASC LIMIT 3""",
                (MAX_RETRIES,)
            )
            if not rows:
                time.sleep(5)
                continue
            
            locked_rows = []
            for row_id, site_id, url, site_delay in rows:
                domain_delay = max(MIN_DELAY, site_delay or MIN_DELAY)
                locked = execute_db(
                    """UPDATE crawl_queue
                       SET status='locked',locked_by=?, scheduled_at=0
                       WHERE id=? AND status='pending'""",
                    (name, row_id), commit=True
                )
                if locked.rowcount:
                    locked_rows.append((row_id, site_id, url, site_delay))
            
            for row_id, site_id, url, site_delay in locked_rows:
                if SHUTDOWN_FLAG:
                    break
                process_url(row_id, site_id, url)
                time.sleep(domain_delay)
                
        except Exception as e:
            print(f"Worker {name} error: {e}")
            time.sleep(5)


def _recover_interrupted_queue():
    """Return items left ``locked`` by a crash/shutdown to the pending state.

    A hard stop can strand rows in ``locked`` with no owner, so nothing would
    ever pick them up again. Called once at startup.
    """
    try:
        result = execute_db(
            """UPDATE crawl_queue SET status='pending', locked_by='', scheduled_at=0
               WHERE status='locked' AND locked_by != ''""",
            commit=True
        )
        if result.rowcount:
            print(f"Recovered {result.rowcount} interrupted queue item(s)")
    except Exception as e:
        log_error("Could not recover interrupted queue items", e)


def start_workers():
    global _worker_threads
    _recover_interrupted_queue()
    names = [f"worker_{chr(ord('a') + i)}" for i in range(WORKER_COUNT)]
    for name in names:
        t = threading.Thread(target=_worker_loop, args=(name,), daemon=True)
        t.start()
        _worker_threads.append(t)
    print(f"Workers spusteny ({WORKER_COUNT}x: {', '.join(names)})")


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
            recrawl_site(row[0])
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
# NIGHTLY MAINTENANCE ("dreaming mode")
# ============================================================================

_maintenance_lock = threading.Lock()
_maintenance_running = False


def maintenance_log(message):
    """Append one maintenance line to ``logs/maintenance.log`` (best effort).

    Mirrors :func:`log_error`: a broken log path must never abort the run.
    """
    line = f"{datetime.now().isoformat()} - {message}"
    print(f"[maintenance] {message}")
    try:
        os.makedirs(os.path.dirname(MAINTENANCE_LOG_PATH) or '.', exist_ok=True)
        with open(MAINTENANCE_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + "\n")
    except Exception:
        pass


def _available_disk_mb(path=None):
    """Free disk space in MB for ``path`` (or the DB directory), or ``None``."""
    target = path or os.path.dirname(os.path.abspath(DB_PATH)) or '.'
    try:
        stat = os.statvfs(target)
        return stat.f_bavail * stat.f_frsize / (1024.0 * 1024.0)
    except (OSError, AttributeError, ValueError):
        return None


def _content_signature(page):
    """Cheap order-independent fingerprint of a page body.

    Normalises to folded lower-case words and reduces them to a sorted set of
    the most frequent tokens. Two pages whose signature sets are equal are
    treated as the same content. Only bodies longer than
    :data:`CONTENT_DUP_MIN_CHARS` are considered, so boilerplate/near-empty
    pages do not collapse into one another.
    """
    body = (page['body_text'] or '').strip()
    if len(body) < CONTENT_DUP_MIN_CHARS:
        return None
    tokens = _fold_tokenize(body)
    if len(tokens) < 20:
        return None
    counts = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:40]
    return frozenset(token for token, _ in top)


def maintenance_dedup_pages(limit=None):
    """Merge duplicate pages (identical url_hash, then identical content).

    ``url_hash`` is an exact fingerprint of the canonical URL, so rows sharing
    it are true duplicates and the newest is kept. Content duplicates (same
    :func:`_content_signature`) keep the oldest row, because a stable URL is
    preferable to a newer alias. Removed rows cascade out of FTS5 via the
    existing delete trigger; their vectors are rebuilt by the caller.

    Returns ``{'url': n, 'content': m}`` with the number of rows removed.
    """
    removed = {'url': 0, 'content': 0}

    # 1) Exact URL duplicates. GROUP BY url_hash keeps this index-friendly.
    dup_groups = execute_db_fetchall(
        """SELECT url_hash, COUNT(*) AS n FROM pages
           WHERE url_hash IS NOT NULL AND url_hash != ''
           GROUP BY url_hash HAVING n > 1"""
    )
    for row in dup_groups:
        rows = execute_db_fetchall(
            "SELECT id FROM pages WHERE url_hash = ? ORDER BY indexed_at DESC, id DESC",
            (row['url_hash'],)
        )
        for extra in rows[1:]:
            execute_db("DELETE FROM pages WHERE id = ?", (extra['id'],), commit=True)
            removed['url'] += 1

    # 2) Content duplicates. Signatures are held in memory only as small
    #    token sets; the full rows are never loaded at once.
    if limit is None:
        limit = MAINTENANCE_DEDUP_SCAN
    seen = {}
    scanned = 0
    for page in execute_db_fetchall(
            "SELECT id, url_hash, body_text FROM pages ORDER BY indexed_at ASC, id ASC"):
        if SHUTDOWN_FLAG or scanned >= limit:
            break
        scanned += 1
        signature = _content_signature(page)
        if signature is None:
            continue
        first = seen.get(signature)
        if first is None:
            seen[signature] = page['id']
            continue
        execute_db("DELETE FROM pages WHERE id = ?", (page['id'],), commit=True)
        removed['content'] += 1

    return removed


def maintenance_optimize_db():
    """Run ``PRAGMA optimize`` and, when safe, a space-reclaiming ``VACUUM``.

    ``VACUUM`` rewrites the entire database and temporarily needs roughly its
    size in free space, so it is skipped unless a healthy margin is available.
    Returns a short human-readable summary.
    """
    try:
        execute_db("PRAGMA optimize", commit=True)
    except sqlite3.OperationalError as e:
        return f"optimize failed: {e}"

    note = "PRAGMA optimize ok"
    try:
        db_bytes = os.path.getsize(DB_PATH)
    except OSError:
        return note + "; size unknown, VACUUM skipped"

    free_mb = _available_disk_mb()
    needed_mb = max(MAINTENANCE_VACUUM_MIN_FREE_MB, (db_bytes / (1024.0 * 1024.0)) * 1.2)
    if free_mb is not None and free_mb < needed_mb:
        return note + f"; VACUUM skipped (free {free_mb:.0f} MB < {needed_mb:.0f} MB)"

    # VACUUM cannot run inside a transaction and fights concurrent writers, so
    # take the DB lock and use the raw connection.
    conn = get_db()
    with _db_lock:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.isolation_level = None
            conn.execute("VACUUM")
            conn.isolation_level = ""
        except sqlite3.OperationalError as e:
            return note + f"; VACUUM failed: {e}"
        finally:
            try:
                conn.isolation_level = ""
            except Exception:
                pass
    return note + "; VACUUM ok"


def maintenance_backfill_embeddings(batch=None):
    """Compute embeddings for pages indexed without them (low-memory catch-up).

    In low-memory mode pages are stored with ``embedding IS NULL``. This stage
    computes those vectors only when the device is actually allowed to use
    embeddings, and processes a bounded batch so one night cannot exhaust RAM
    or battery. Returns the number of pages embedded.
    """
    if not embeddings_enabled():
        return 0
    if batch is None:
        batch = MAINTENANCE_EMBED_BATCH
    if batch <= 0:
        return 0

    rows = execute_db_fetchall(
        """SELECT id, og_title, title, og_description, body_text
           FROM pages WHERE embedding IS NULL ORDER BY id ASC LIMIT ?""",
        (batch,)
    )
    done = 0
    for row in rows:
        if SHUTDOWN_FLAG:
            break
        page = {
            'og_title': row['og_title'], 'title': row['title'],
            'og_description': row['og_description'], 'body_text': row['body_text'],
        }
        # Stop as soon as resources tighten mid-run: the rest is picked up by a
        # later maintenance pass rather than risking a low-memory kill.
        if not embeddings_enabled():
            break
        embedding = _embedding_for(page)
        if embedding is None:
            continue
        execute_db("UPDATE pages SET embedding = ? WHERE id = ?",
                   (embedding, row['id']), commit=True)
        done += 1

    if done:
        _rebuild_hnsw_index()
    return done


def maintenance_import_wiki(max_pages=None):
    """Continue any configured wiki source for a bounded number of articles.

    Uses the paused/resume-aware :func:`run_wiki_import`, so it picks up from
    the stored ``next_title`` and stays idempotent. Paused sources are skipped.
    Returns a ``{source_id: imported}`` mapping.
    """
    if max_pages is None:
        max_pages = MAINTENANCE_WIKI_PAGES
    if max_pages <= 0:
        return {}

    results = {}
    sources = execute_db_fetchall(
        """SELECT * FROM site_sources
           WHERE source_type = ? AND status = 'active' ORDER BY priority ASC, id ASC""",
        (WIKI_SOURCE_TYPE,)
    )
    remaining = max_pages
    for row in sources:
        if SHUTDOWN_FLAG or remaining <= 0:
            break
        source = dict(row)
        try:
            imported = run_wiki_import(source, max_pages=remaining)
        except Exception as e:
            maintenance_log(f"wiki import failed for source {source.get('id')}: {e}")
            log_error(f"maintenance wiki import {source.get('id')}", e)
            continue
        results[source['id']] = imported
        remaining -= imported
    return results


def maintenance_check_dead_links(limit=None, purge=None):
    """Probe the oldest indexed pages and record dead (404/410) links.

    Only runs when a limit is configured, because it touches the network. A
    page found dead gets ``link_status='dead'``; when purge is enabled it is
    removed from the index. Alive pages are stamped ``link_status='ok'`` and a
    fresh ``last_link_check`` so they are not re-probed every night.

    Returns ``{'checked': n, 'dead': m, 'purged': k}``.
    """
    if limit is None:
        limit = MAINTENANCE_LINK_CHECK_LIMIT
    if purge is None:
        purge = MAINTENANCE_DEAD_LINK_PURGE
    stats = {'checked': 0, 'dead': 0, 'purged': 0}
    if limit <= 0:
        return stats

    cutoff = int(time.time()) - MAINTENANCE_LINK_CHECK_DAYS * 24 * 3600
    rows = execute_db_fetchall(
        """SELECT id, url FROM pages
           WHERE url LIKE 'http%' AND (last_link_check = 0 OR last_link_check < ?)
           ORDER BY last_link_check ASC, indexed_at ASC LIMIT ?""",
        (cutoff, limit)
    )
    for row in rows:
        if SHUTDOWN_FLAG:
            break
        stats['checked'] += 1
        dead = False
        try:
            resp = _http_get(row['url'], timeout=MAINTENANCE_LINK_TIMEOUT,
                             max_retries=0, purpose='maintenance-link-check',
                             method='HEAD')
            # Only a definitive "gone" counts. Other non-2xx statuses (405 for
            # HEAD, 403 from a WAF, 5xx hiccups) are inconclusive and must not
            # cost a page its place in the index.
            if resp.status_code in (404, 410):
                dead = True
            elif resp.status_code == 405:
                # Server refuses HEAD; retry with a real GET before judging.
                resp = _http_get(row['url'], timeout=MAINTENANCE_LINK_TIMEOUT,
                                 max_retries=0, purpose='maintenance-link-check')
                dead = resp.status_code in (404, 410)
        except FetchError as e:
            # A 404 surfaced as a non-retryable failure is equally conclusive.
            dead = not e.retryable and ('404' in e.message or '410' in e.message)
        except Exception as e:
            log_error(f"maintenance link check {row['url']}", e)

        if dead:
            stats['dead'] += 1
            execute_db(
                "UPDATE pages SET link_status='dead', last_link_check=strftime('%s','now') WHERE id=?",
                (row['id'],), commit=True)
            if purge:
                execute_db("DELETE FROM pages WHERE id = ?", (row['id'],), commit=True)
                stats['purged'] += 1
        else:
            execute_db(
                "UPDATE pages SET link_status='ok', last_link_check=strftime('%s','now') WHERE id=?",
                (row['id'],), commit=True)
        time.sleep(0.2)

    if stats['purged']:
        _rebuild_hnsw_index()
    return stats


def run_maintenance(dedup=True, backfill_embeddings=True, wiki=True,
                    check_links=True, optimize=True, wiki_pages=None,
                    link_limit=None):
    """Run one complete nightly maintenance pass.

    Stages are independent and individually guarded, so a failure in one still
    lets the others finish. Returns a dict of per-stage summaries. Safe to call
    from the CLI or a scheduler thread; a second concurrent run is refused.
    """
    global _maintenance_running
    with _maintenance_lock:
        if _maintenance_running:
            maintenance_log("already running, skipping")
            return {'skipped': True}
        _maintenance_running = True
    started = time.time()
    maintenance_log("start")
    summary = {}
    try:
        if dedup:
            if SHUTDOWN_FLAG:
                return summary
            try:
                summary['dedup'] = maintenance_dedup_pages()
                maintenance_log(f"dedup removed {summary['dedup']}")
            except Exception as e:
                summary['dedup'] = f"error: {e}"
                maintenance_log(f"dedup error: {e}")
                log_error("maintenance dedup", e)

        if backfill_embeddings:
            if SHUTDOWN_FLAG:
                return summary
            try:
                summary['embeddings'] = maintenance_backfill_embeddings()
                maintenance_log(f"embeddings backfilled: {summary['embeddings']}")
            except Exception as e:
                summary['embeddings'] = f"error: {e}"
                maintenance_log(f"embeddings error: {e}")
                log_error("maintenance embeddings", e)

        if wiki:
            if SHUTDOWN_FLAG:
                return summary
            try:
                summary['wiki'] = maintenance_import_wiki(max_pages=wiki_pages)
                maintenance_log(f"wiki import: {summary['wiki']}")
            except Exception as e:
                summary['wiki'] = f"error: {e}"
                maintenance_log(f"wiki error: {e}")
                log_error("maintenance wiki", e)

        if check_links:
            if SHUTDOWN_FLAG:
                return summary
            try:
                summary['links'] = maintenance_check_dead_links(limit=link_limit)
                maintenance_log(f"links: {summary['links']}")
            except Exception as e:
                summary['links'] = f"error: {e}"
                maintenance_log(f"links error: {e}")
                log_error("maintenance links", e)

        if optimize:
            try:
                summary['db'] = maintenance_optimize_db()
                maintenance_log(f"db: {summary['db']}")
            except Exception as e:
                summary['db'] = f"error: {e}"
                maintenance_log(f"db error: {e}")
                log_error("maintenance optimize", e)
    finally:
        _maintenance_running = False
        summary['elapsed'] = round(time.time() - started, 1)
        maintenance_log(f"done in {summary['elapsed']}s")
    return summary


def maintenance_scheduled():
    """Scheduler entry point: run maintenance unless the app is shutting down."""
    if SHUTDOWN_FLAG:
        return
    try:
        run_maintenance()
    except Exception as e:
        maintenance_log(f"scheduled run error: {e}")
        log_error("scheduled maintenance", e)


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
    """Body text windowed around the first query match so the hit is visible.

    Matching is diacritic-insensitive, so a query typed without háčky still
    finds the window in text that carries them.
    """
    text = (page_string(page, 'og_description') or page_string(page, 'body_text')).strip()
    if not text:
        return ''
    text = re.sub(r'\s+', ' ', text)

    lowered = fold_diacritics(text)
    position = -1
    for term in _fold_tokenize(query or ''):
        found = lowered.find(term)
        if found != -1:
            position = found
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
    """Wrap query terms in <mark>. Escaping happens before the markup is added.

    The match is performed on the diacritic-folded text so ``cesky`` highlights
    ``český``, but the *original* substring is what gets wrapped, so the escape
    order (escape first, then insert markup) is preserved exactly.
    """
    escaped = escape(snippet or '')
    terms = [t for t in _fold_tokenize(query or '') if len(t) > 2]
    if not terms:
        return escaped

    # Fold only to locate matches; slice the escaped string with the same
    # indices so markup never touches the raw input.
    folded = fold_diacritics(str(escaped))
    pattern = '|'.join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True))
    try:
        matches = list(re.finditer(pattern, folded, flags=re.IGNORECASE))
    except re.error:
        return escaped
    if not matches:
        return escaped

    out = []
    cursor = 0
    for match in matches:
        start, end = match.span()
        if start < cursor:
            continue
        out.append(str(escaped)[cursor:start])
        out.append(f'<mark>{str(escaped)[start:end]}</mark>')
        cursor = end
    out.append(str(escaped)[cursor:])
    return ''.join(out)


# --- Rich result cards ------------------------------------------------------
# prepare_results() classifies each hit and precomputes every display value, so
# the template only renders. Detection is best-effort: an untyped page always
# falls back to a plain article card.

_WIKI_HOST_RE = re.compile(
    r'(?:^|\.)(?:wikipedia|wikimedia|wiktionary|wikinews|wikisource|wikiquote|'
    r'wikibooks|wikiversity|wikivoyage|mediawiki)\.org$')

CARD_WIKI = 'wiki'
CARD_PRODUCT = 'product'
CARD_RECIPE = 'recipe'
CARD_ORGANIZATION = 'organization'
CARD_ARTICLE = 'article'
CARD_TYPES = (CARD_WIKI, CARD_RECIPE, CARD_PRODUCT, CARD_ORGANIZATION, CARD_ARTICLE)

_RECIPE_SCHEMA_TYPES = frozenset(('recipe',))
_PRODUCT_SCHEMA_TYPES = frozenset((
    'product', 'individualproduct', 'productmodel', 'productgroup', 'offer',
    'aggregateoffer', 'vehicle', 'book', 'movie', 'softwareapplication',
    'mobileapplication', 'videogame', 'course', 'event', 'apispecification',
))
_ORGANIZATION_SCHEMA_TYPES = frozenset((
    'organization', 'localbusiness', 'corporation', 'ngo',
    'educationalorganization', 'governmentorganization', 'medicalorganization',
    'sportsorganization', 'restaurant', 'store', 'professionalservice',
    'hotel', 'dentist', 'physician', 'pharmacy', 'bank', 'library', 'museum',
    'cafeorcoffeeshop', 'barorpub', 'grocery store', 'grocery', 'autodealer',
    'travelagency',
))
# Schema.org availability values -> (Czech label, in-stock flag).
_AVAILABILITY_LABELS = {
    'instock': ('Skladem', True),
    'limitedavailability': ('Omezená dostupnost', True),
    'onlineonly': ('Pouze online', True),
    'instoreonly': ('Pouze na prodejně', True),
    'preorder': ('Předprodej', True),
    'presale': ('Předprodej', True),
    'backorder': ('Na objednávku', False),
    'outofstock': ('Vyprodáno', False),
    'soldout': ('Vyprodáno', False),
    'discontinued': ('Ukončeno', False),
}
_CURRENCY_SYMBOLS = {'CZK': 'Kč', 'EUR': '€', 'USD': '$', 'GBP': '£',
                     'PLN': 'zł', 'HUF': 'Ft', 'CHF': 'CHF'}
_ISO_DURATION_RE = re.compile(
    r'^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?'
    r'(?:(?P<seconds>\d+)S)?)?$', re.IGNORECASE)


def _schema_scalar(value):
    """Best-effort plain string from a JSON-LD scalar or wrapper object."""
    if value is None or isinstance(value, bool):
        return ''
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ('name', 'value', '@id', 'url', 'text'):
            if key in value:
                found = _schema_scalar(value[key])
                if found:
                    return found
        return ''
    if isinstance(value, (list, tuple)):
        for entry in value:
            found = _schema_scalar(entry)
            if found:
                return found
    return ''


def _schema_number(value):
    """Parse a numeric value, tolerating Czech decimal commas and spaces."""
    text = _schema_scalar(value)
    if not text:
        return None
    cleaned = text.replace(' ', '').replace('\u00a0', '')
    if ',' in cleaned and '.' in cleaned:
        cleaned = cleaned.replace('.', '').replace(',', '.')
    else:
        cleaned = cleaned.replace(',', '.')
    try:
        return float(cleaned)
    except ValueError:
        return None


def _nested_scalar(node, path):
    """Follow ``path`` through dicts/lists and return the first scalar found."""
    current = node
    for key in path:
        if isinstance(current, (list, tuple)):
            current = next((e for e in current if isinstance(e, dict)), None)
        if not isinstance(current, dict):
            return ''
        current = current.get(key)
    return _schema_scalar(current)


def _format_price(amount, currency):
    """Czech-friendly price: thousands separated by spaces, comma decimals."""
    if amount is None:
        return ''
    currency = (currency or '').strip().upper()
    if abs(amount - round(amount)) < 0.005:
        text = f"{int(round(amount)):,}".replace(',', ' ')
    else:
        text = f"{amount:,.2f}".replace(',', ' ').replace('.', ',')
    symbol = _CURRENCY_SYMBOLS.get(currency)
    return f"{text} {symbol}" if symbol else f"{text} {currency}".strip()


def _format_duration(value):
    """Render an ISO-8601 duration (PT30M, PT1H20M) as Czech minutes/hours."""
    text = _schema_scalar(value)
    if not text:
        return ''
    match = _ISO_DURATION_RE.match(text)
    if match:
        minutes = (int(match.group('days') or 0) * 1440
                   + int(match.group('hours') or 0) * 60
                   + int(match.group('minutes') or 0)
                   + (1 if int(match.group('seconds') or 0) >= 30 else 0))
    else:
        number = _schema_number(text)
        minutes = int(round(number)) if number is not None else 0
    if minutes <= 0:
        return ''
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} h {rest} min"
    if hours:
        return f"{hours} h"
    return f"{rest} min"


def _safe_image_url(value):
    """Allow only absolute http(s) and root-relative image URLs.

    Keeps ``javascript:``/``data:`` payloads out of ``<img src>`` even though a
    stored page can carry arbitrary JSON-LD.
    """
    text = _schema_scalar(value)
    if not text:
        return ''
    lower = text.lower()
    if lower.startswith('http://') or lower.startswith('https://'):
        return text
    if text.startswith('/') and not text.startswith('//'):
        return text
    return ''


def _first_image_url(item):
    """First usable URL from the stored ``images`` list, or ``''``."""
    images = page_json(item, 'images', [])
    if isinstance(images, list):
        for entry in images:
            if isinstance(entry, dict):
                safe = _safe_image_url(entry.get('url'))
                if safe:
                    return safe
    return ''


def _card_type_for(item, schema_types):
    """Classify a result into a rich-card type (see ``CARD_TYPES``)."""
    try:
        host = (urlparse(page_string(item, 'url')).hostname or '').lower()
    except Exception:
        host = ''
    if _WIKI_HOST_RE.search(host):
        return CARD_WIKI
    if schema_types & _RECIPE_SCHEMA_TYPES:
        return CARD_RECIPE
    if schema_types & _PRODUCT_SCHEMA_TYPES:
        return CARD_PRODUCT
    if schema_types & _ORGANIZATION_SCHEMA_TYPES:
        return CARD_ORGANIZATION
    return CARD_ARTICLE


def _rich_card_fields(item):
    """Derive card type and structured display fields for a result.

    Every value is a plain pre-formatted string (already safe for autoescape),
    so the template renders without branching on raw JSON-LD.
    """
    details = page_json(item, 'schema_details', {})
    if not isinstance(details, dict):
        details = {}
    schema_types = set(_schema_types(details))
    declared = (page_string(item, 'schema_type') or '').lower()
    if declared:
        schema_types.add(declared)

    price_amount = None
    for path in (('offers', 'price'), ('offers', 'lowPrice'), ('offers', 'highPrice'),
                 ('price',), ('priceSpecification', 'price')):
        price_amount = _schema_number(_nested_scalar(details, path))
        if price_amount is not None:
            break

    card_type = _card_type_for(item, schema_types)
    if card_type == CARD_ARTICLE and price_amount is not None:
        card_type = CARD_PRODUCT

    fields = {'card_type': card_type}
    badge = page_string(item, 'schema_type')
    if card_type == CARD_WIKI:
        fields['card_badge'] = 'Wikipedie'
    else:
        fields['card_badge'] = badge or {
            CARD_PRODUCT: 'Produkt', CARD_RECIPE: 'Recept',
            CARD_ORGANIZATION: 'Organizace', CARD_ARTICLE: 'Článek',
        }.get(card_type, 'Článek')

    meta = details.get('_meta') if isinstance(details.get('_meta'), dict) else {}
    breadcrumbs = meta.get('breadcrumbs')
    if not isinstance(breadcrumbs, list):
        breadcrumbs = []
    fields['breadcrumbs'] = [str(b) for b in breadcrumbs if str(b).strip()][:6]
    fields['meta_author'] = _schema_scalar(meta.get('author'))

    rows = []

    # Rating is shared by products and recipes.
    rating_value = _schema_number(
        _nested_scalar(details, ('aggregateRating', 'ratingValue')))
    rating_count = _nested_scalar(
        details, ('aggregateRating', 'ratingCount'))
    rating_count = rating_count or _nested_scalar(
        details, ('aggregateRating', 'reviewCount'))
    if rating_value is not None and rating_value > 0:
        best = _schema_number(
            _nested_scalar(details, ('aggregateRating', 'bestRating'))) or 5.0
        best = best if best > 0 else 5.0
        fields['rating_value'] = f"{rating_value:.1f}".rstrip('0').rstrip('.')
        fields['rating_pct'] = max(0, min(100, int(round(rating_value / best * 100))))
        fields['rating_count'] = _schema_scalar(rating_count)

    if card_type == CARD_PRODUCT:
        currency = (_nested_scalar(details, ('offers', 'priceCurrency'))
                    or _nested_scalar(details, ('priceCurrency',)))
        fields['price_display'] = _format_price(price_amount, currency)
        availability = _nested_scalar(details, ('offers', 'availability'))
        if availability:
            key = availability.rstrip('/').rsplit('/', 1)[-1].lower()
            label, ok = _AVAILABILITY_LABELS.get(key, (availability, True))
            fields['availability'] = label
            fields['availability_ok'] = ok
        brand = _nested_scalar(details, ('brand',)) or _nested_scalar(details, ('brand', 'name'))
        sku = _nested_scalar(details, ('sku',)) or _nested_scalar(details, ('mpn',))
        if brand:
            rows.append(('Značka', brand))
        if sku:
            rows.append(('Kód', sku))

    elif card_type == CARD_RECIPE:
        time_display = _format_duration(_nested_scalar(details, ('totalTime',)))
        if not time_display:
            time_display = _format_duration(_nested_scalar(details, ('cookTime',)))
        if not time_display:
            time_display = _format_duration(_nested_scalar(details, ('prepTime',)))
        fields['time_display'] = time_display
        calories = _nested_scalar(details, ('nutrition', 'calories'))
        if calories:
            fields['calories'] = f"{calories} kcal" if calories.isdigit() else calories
        yield_value = _nested_scalar(details, ('recipeYield',))
        category = _nested_scalar(details, ('recipeCategory',))
        cuisine = _nested_scalar(details, ('recipeCuisine',))
        if yield_value:
            rows.append(('Porce', yield_value))
        if category:
            rows.append(('Kategorie', category))
        if cuisine:
            rows.append(('Kuchyně', cuisine))

    elif card_type == CARD_ORGANIZATION:
        address = _nested_scalar(details, ('address', 'streetAddress'))
        locality = _nested_scalar(details, ('address', 'addressLocality'))
        if not address:
            address = _nested_scalar(details, ('address',))
        fields['address'] = ', '.join(p for p in (address, locality) if p)
        fields['phone'] = _nested_scalar(details, ('telephone',))
        logo = (_safe_image_url(_nested_scalar(details, ('logo',)))
                or _safe_image_url(_nested_scalar(details, ('image',))))
        if logo:
            fields['logo'] = logo
        hours = _nested_scalar(details, ('openingHours',))
        if hours:
            rows.append(('Otevírací doba', hours))

    elif card_type == CARD_WIKI:
        lang = ''
        try:
            host = (urlparse(page_string(item, 'url')).hostname or '').lower()
            lang = host.split('.')[0] if host.endswith('.wikipedia.org') else ''
        except Exception:
            lang = ''
        if lang:
            rows.append(('Jazyk', f"Wikipedie ({lang})"))
        if fields['breadcrumbs']:
            rows.append(('Kategorie', ' › '.join(fields['breadcrumbs'])))
        rows.append(('Zdroj', 'Wikimedia'))

    if card_type != CARD_WIKI and fields['breadcrumbs']:
        rows.append(('Zařazení', ' › '.join(fields['breadcrumbs'])))
    if fields['meta_author'] and card_type != CARD_WIKI:
        rows.append(('Autor', fields['meta_author']))

    fields['rich_metadata'] = [{'label': label, 'value': value} for label, value in rows]
    return fields


def prepare_results(results, query):
    """Attach display-only fields so the template stays free of logic."""
    prepared = []
    for page in results:
        item = dict(page)
        item['domain'] = _result_domain(page_string(item, 'url'))
        item['display_url_path'] = _result_url_path(page_string(item, 'url'))
        item['snippet_html'] = _highlight_snippet(_result_snippet(item, query), query)
        item['relevance_pct'] = int(round(float(item.get('relevance') or 0)))
        item['display_title'] = (page_string(item, 'og_title') or page_string(item, 'title')
                                 or page_string(item, 'url'))
        item['display_date'] = page_string(item, 'published_date') or (
            format_timestamp(item.get('published_timestamp'))
            if item.get('published_timestamp') else '')

        item.update(_rich_card_fields(item))

        thumb = (_safe_image_url(page_string(item, 'og_image'))
                 or _safe_image_url(page_string(item, 'favicon_url')))
        if not thumb and item['card_type'] in (CARD_PRODUCT, CARD_RECIPE, CARD_ORGANIZATION):
            thumb = _first_image_url(item)
        item['thumb'] = item.get('logo') or thumb
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
    importer = (payload.get('importer') or '').strip().lower()
    if source_type == WIKI_SOURCE_TYPE:
        site_url, importer, _lang = normalize_wiki_source(url, importer or None)
        if not site_url:
            return None, 'Neplatný odkaz na Wikipedii'
        url = site_url
    else:
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
    if source_type == WIKI_SOURCE_TYPE:
        data['importer'] = importer or 'api'
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
    # ``is_local`` is tri-state: absent means "detect from the URL", while an
    # explicit checkbox value forces the flag.
    if payload.get('is_local') not in (None, ''):
        value = payload['is_local']
        if isinstance(value, str):
            value = value.strip().lower() in ('1', 'true', 'on', 'yes', 'ano')
        data['is_local'] = bool(value)
    return data, None


@app.route('/admin')
@app.route('/admin/')
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
    local_only = request.args.get('local') == '1'

    all_sites = get_filtered_sites(search=search, status=status,
                                   source_type=source_type, local_only=local_only)
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
        local_only=local_only,
        build_page_url=_page_url,
        message=message,
        message_ok=ok,
    )


@app.route('/admin/sites/<int:site_id>')
def admin_site_detail(site_id):
    site = get_site_by_id(site_id)
    if not site:
        return redirect('/admin/sites?error=Web nenalezen')

    _decorate_site(site)

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
            # A checkbox only appears in the payload when ticked, so read it
            # from the raw form to make unticking work too.
            is_local='is_local' in request.form,
            search_priority_multiplier=request.form.get('search_priority_multiplier'),
        )
        if ok:
            return redirect(f'/admin/sites/{site_id}?success={message}')
        return redirect(f'/admin/sites/{site_id}/edit?error={message}')

    _decorate_site(site)

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
    ok, message = recrawl_async(site_id)
    key = 'success' if ok else 'error'
    target = f'/admin/sites/{site_id}' if get_site_by_id(site_id) else '/admin/sites'
    return redirect(f'{target}?{key}={message}')


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

    is_local = data.pop('is_local', None)
    if site_id is None:
        if data['source_type'] != 'domain':
            # Auto-create the parent domain from the supplied URL
            site_id = add_site(data['url'], 500, is_local=is_local)
            if not site_id:
                return jsonify({'error': 'Doménu nebylo možné vytvořit'}), 400
        else:
            site_id = 0
    elif is_local:
        # An explicit "local" tick on an existing domain upgrades that domain.
        update_site(site_id, is_local=True,
                    search_priority_multiplier=LOCAL_SITE_PRIORITY_MULTIPLIER)

    source_id, error = add_source(site_id, is_local=is_local, **data)
    if error:
        return jsonify({'error': error}), 400

    source = get_source_by_id(source_id)
    if max_pages is not None and source:
        update_site(source['site_id'], max_pages=max_pages)

    # Actually index the new source instead of waiting for a manual recrawl.
    if source:
        index_source_async(source_id)
        if source['source_type'] == 'domain':
            # A bare domain has no sitemap/feed to walk yet, so discover the
            # links reachable from the homepage to give the queue something.
            recrawl_async(source['site_id'])

    return jsonify({'id': source_id, 'message': 'Zdroj byl přidán a zařazen k indexaci'}), 201


@app.route('/admin/api/sources/<int:source_id>', methods=['GET'])
def api_source_get(source_id):
    source = get_source_by_id(source_id)
    if not source:
        return jsonify({'error': 'Zdroj nenalezen'}), 404
    return jsonify(source)


@app.route('/admin/api/sources/<int:source_id>', methods=['PUT', 'PATCH'])
def api_source_update(source_id):
    payload = request.get_json(silent=True) or request.form.to_dict()
    allowed = ('url', 'source_type', 'priority', 'notes', 'status', 'max_pages', 'importer')
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
    allowed = ('max_pages', 'status', 'aliases', 'is_local', 'search_priority_multiplier')
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

def _cli_import_wiki(args):
    """Run a Wikipedia import from the command line, without starting the server.

    Lets a Termux user bulk-import a downloaded Czech dump (or hit the API)
    in a one-off process that exits when done, instead of driving it through
    the always-on Flask app.
    """
    get_db()
    source = None
    if args.source_id:
        source = get_source_by_id(args.source_id)
        if not source:
            print(f"Zdroj {args.source_id} nenalezen")
            return 1
    else:
        if not args.import_wiki:
            print("Zadejte --import-wiki <cs|file:///cesta/dump.bz2> nebo --source-id N")
            return 1
        source_id, error = add_source(
            0, args.import_wiki, WIKI_SOURCE_TYPE, importer=args.importer or None)
        if source_id is None:
            print(f"Zdroj nelze vytvořit: {error}")
            return 1
        source = get_source_by_id(source_id)

    total = 0
    while True:
        imported = run_wiki_import(source, max_pages=args.max_pages,
                                   allow_indexed_refresh=args.refresh)
        total += imported
        print(f"Importováno {imported} článků (celkem {total})")
        if imported == 0 or not args.all:
            break
        source = get_source_by_id(source['id'])
    print(f"Hotovo. Celkem importováno: {total}")
    return 0


def _cli_maintenance(args):
    """Run one nightly-maintenance pass from the command line and exit.

    Intended for a Termux ``cron``/``termux-job-scheduler`` entry so the phone
    can consolidate its index overnight while the server is asleep. Stages can
    be turned off individually; the log goes to ``logs/maintenance.log``.
    """
    get_db()
    summary = run_maintenance(
        dedup=not args.no_dedup,
        backfill_embeddings=not args.no_embeddings,
        wiki=not args.no_wiki,
        check_links=not args.no_links,
        optimize=not args.no_optimize,
        wiki_pages=args.max_pages,
        link_limit=args.link_check,
    )
    print('-' * 70)
    for key, value in summary.items():
        print(f"{key}: {value}")
    return 0


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Mini Search – hybridní vyhledávač')
    parser.add_argument('--import-wiki', metavar='CILE',
                        help='Importovat Wikipedii: jazykový kód "cs", '
                             'nebo "file:///cesta/cswiki-….xml.bz2" pro lokální dump')
    parser.add_argument('--source-id', type=int,
                        help='Pokračovat v importu existujícího wiki zdroje')
    parser.add_argument('--importer', choices=WIKI_IMPORTERS,
                        help='Použitý importér (api/dump)')
    parser.add_argument('--max-pages', type=int, default=None,
                        help='Kolik článků na jeden běh (výchozí dle domény)')
    parser.add_argument('--all', action='store_true',
                        help='Opakovat import, dokud dump nedá žádné nové články')
    parser.add_argument('--refresh', action='store_true',
                        help='Znovu indexovat již uložené články')

    # Nightly maintenance ("dreaming mode").
    parser.add_argument('--maintenance', '--nightly', dest='maintenance',
                        action='store_true',
                        help='Spustit noční údržbu a skončit (dedup, úklid DB, '
                             'dopočet vektorů, pokračování wiki importu, kontrola odkazů)')
    parser.add_argument('--no-dedup', action='store_true',
                        help='Přeskočit slučování duplicitních stránek')
    parser.add_argument('--no-embeddings', action='store_true',
                        help='Přeskočit dopočet vektorů')
    parser.add_argument('--no-wiki', action='store_true',
                        help='Přeskočit pokračování wiki importu')
    parser.add_argument('--no-links', action='store_true',
                        help='Přeskočit kontrolu neplatných odkazů')
    parser.add_argument('--no-optimize', action='store_true',
                        help='Přeskočit PRAGMA optimize / VACUUM')
    parser.add_argument('--link-check', type=int, default=None,
                        help='Kolik nejstarších stránek ověřit na 404 (0 vypne)')
    args = parser.parse_args()

    if args.maintenance:
        raise SystemExit(_cli_maintenance(args))

    if args.import_wiki or args.source_id:
        raise SystemExit(_cli_import_wiki(args))

    print('=' * 70)
    print('Mini Search v7.4 - Hybrid Search Engine')
    print('=' * 70)

    get_db()
    print('Database initialized')

    if embeddings_enabled():
        get_hnsw_index()
        print('hnswlib index initialized')

    # Loading the sentence-transformer model is the single largest RAM cost; in
    # low-memory mode we never touch it and search stays on FTS5.
    if embeddings_enabled():
        get_model()
        print('Model loaded')
    else:
        print('Embeddings disabled (low-memory mode) - FTS5 fallback active')

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(check_feeds,      IntervalTrigger(hours=1),  id='check_feeds')
    _scheduler.add_job(check_sitemaps,   IntervalTrigger(hours=24), id='check_sitemaps')
    _scheduler.add_job(recrawl_all_sites, IntervalTrigger(hours=12), id='recrawl_all')
    _scheduler.add_job(check_for_updates_scheduled, IntervalTrigger(hours=6), id='check_updates')
    if MAINTENANCE_NIGHTLY_ENABLED:
        # One nightly pass at the configured hour; a cron-style trigger keeps it
        # out of the user's way far better than an interval job.
        from apscheduler.triggers.cron import CronTrigger
        _scheduler.add_job(maintenance_scheduled,
                           CronTrigger(hour=MAINTENANCE_NIGHTLY_HOUR),
                           id='nightly_maintenance')
        print(f"Nightly maintenance scheduled at {MAINTENANCE_NIGHTLY_HOUR:02d}:00")
    _scheduler.start()
    print('Scheduler started')

    start_workers()

    print()
    print('http://0.0.0.0:8070')
    print('Ctrl+C to stop')
    print('=' * 70)

    app.run(host='0.0.0.0', port=8070, debug=False, threaded=True)
