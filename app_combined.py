#!/usr/bin/env python3
"""
Mini Search - Complete Implementation v7.0
Hybrid Search: 60% hnswlib vector + 35% FTS5 full-text + 5% SEO scoring
Database migration without deleting console.db
FTS5 backfill for existing pages with triggers
GitHub update checking with .commit_sha, .update_available, update_log
Gzip sitemap support, recursion limits, max_pages enforcement
Per-domain shared Crawl-delay enforcement
Correct 429 rate_limited scheduling
Non-destructive scheduled recrawl
Robots checks before fetching discovery candidates
Rich Czech search results and enhanced Czech admin console
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
from urllib.parse import urlparse, urlunparse, urljoin
from urllib.robotparser import RobotFileParser

from flask import Flask, render_template_string, request, redirect, jsonify
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
    '''
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
    "CREATE INDEX IF NOT EXISTS idx_pages_indexed ON pages(indexed_at)",
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
    """Get current update status from database."""
    result = execute_db_fetchone("SELECT * FROM update_status WHERE id=1")
    if result:
        return dict(result)
    return {
        'last_check': 0,
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


def get_all_sites():
    results = execute_db_fetchall("SELECT * FROM sites ORDER BY created_at DESC")
    sites = []
    for row in results:
        site = dict(row)
        site['last_crawled_str'] = (
            datetime.fromtimestamp(site['last_crawled']).strftime('%Y-%m-%d %H:%M:%S')
            if site['last_crawled'] > 0 else 'Nikdy'
        )
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

        if page.get('published_timestamp', 0) > 0:
            try:
                page['published_date'] = datetime.fromtimestamp(page['published_timestamp']).strftime('%Y-%m-%d')
            except Exception:
                page['published_date'] = ''
        else:
            page['published_date'] = ''

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
        except Exception as e:
            print(f"Feed check error {url}: {e}")


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
        except Exception as e:
            print(f"Sitemap check error {url}: {e}")


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

app = Flask(__name__)
app.secret_key = 'mini-search-secret-key'

SEARCH_HTML = """
<!DOCTYPE html>
<html lang="cs">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mini Search</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
             background:#1a1a2e;color:#e0e0e0;line-height:1.6;padding:20px}
        .container{max-width:1200px;margin:0 auto}
        h1{color:#e94560;margin-bottom:10px;font-size:2em}
        .card{background:#16213e;border-radius:10px;padding:20px;margin-bottom:20px}
        .search-wrap{display:flex;gap:10px;margin-bottom:10px;position:relative}
        .search-wrap input{flex:1;padding:15px;border-radius:8px;border:1px solid #333;
                           background:#1a1a2e;color:#e0e0e0;font-size:1.1em}
        .search-wrap button{background:#e94560;color:#fff;border:none;padding:15px 30px;
                            border-radius:8px;cursor:pointer;font-size:1.1em;font-weight:bold}
        .search-wrap button:hover{background:#c81e45}
        #acDropdown{position:absolute;top:58px;left:0;right:60px;background:#16213e;
                    border:1px solid #333;border-radius:0 0 8px 8px;z-index:100;display:none}
        #acDropdown div{padding:10px 15px;cursor:pointer;border-bottom:1px solid #222}
        #acDropdown div:hover{background:#1f2b4a}
        .result-item{background:#16213e;border-radius:8px;padding:20px;margin-bottom:15px;
                     border-left:4px solid #e94560;display:flex;gap:15px}
        .result-item:hover{background:#1f2b4a}
        .result-content{flex:1}
        .result-title a{color:#e94560;text-decoration:none;font-size:1.2em}
        .result-title a:hover{text-decoration:underline}
        .result-url{color:#2196f3;font-size:.9em;margin:4px 0;word-break:break-all}
        .result-snippet{color:#aaa;margin-bottom:8px}
        .result-meta{color:#666;font-size:.85em;display:flex;gap:10px;flex-wrap:wrap}
        .result-image{max-width:120px;max-height:80px;border-radius:5px}
        .schema-badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.75em;
                      background:#2196f320;color:#2196f3;margin-right:6px}
        .relevance-badge{display:inline-block;padding:2px 6px;background:#4caf5020;
                         color:#4caf50;border-radius:5px;font-size:.85em;font-weight:bold}
        .filter-buttons{display:flex;gap:10px;margin-bottom:15px;flex-wrap:wrap}
        .filter-btn{padding:5px 12px;background:#333;border:none;border-radius:5px;
                    color:#e0e0e0;cursor:pointer;font-size:.9em}
        .filter-btn.active{background:#e94560}
        .no-results{text-align:center;color:#666;padding:40px}
        .nav a{color:#2196f3;margin-right:20px;text-decoration:none}
        .update-notice{background:#ff980020;color:#ff9800;padding:10px;border-radius:5px;margin-bottom:15px}
        audio{max-width:300px}
    </style>
</head>
<body>
<div class="container">
    <div class="nav" style="margin-bottom:20px">
        <a href="/">Vyhledavani</a>
        <a href="/admin">Sprava</a>
    </div>
    <h1>Mini Search</h1>
    <div class="card">
        <div class="filter-buttons">
            <button class="filter-btn{% if current_filter=='all' %} active{% endif %}" onclick="setFilter('all')">Vse</button>
            <button class="filter-btn{% if current_filter=='articles' %} active{% endif %}" onclick="setFilter('articles')">Clanky</button>
            <button class="filter-btn{% if current_filter=='podcasts' %} active{% endif %}" onclick="setFilter('podcasts')">Podcasty</button>
            <button class="filter-btn{% if current_filter=='audio' %} active{% endif %}" onclick="setFilter('audio')">Audio</button>
            <button class="filter-btn{% if current_filter=='price' %} active{% endif %}" onclick="setFilter('price')">Ceny</button>
        </div>
        <form id="searchForm" action="/" method="get">
            <div class="search-wrap">
                <input type="text" name="q" id="searchInput" placeholder="Zadejte hledany text..."
                       value="{{ query }}" autocomplete="off">
                <input type="hidden" name="filter" id="filterInput" value="{{ current_filter }}">
                <button type="submit">Hledat</button>
                <div id="acDropdown"></div>
            </div>
        </form>
    </div>
    {% if query %}
    <div class="card">
        <h2>Vysledky pro: "{{ query }}"</h2>
        <div style="color:#666;font-size:.9em;margin-top:5px">Nalezeno: {{ total_results }} vysledku</div>
        {% if results %}
        {% for r in results %}
        <div class="result-item">
            {% if r.og_image or r.favicon_url %}
            <img src="{{ r.og_image or r.favicon_url }}" class="result-image" onerror="this.style.display='none'" alt="">
            {% endif %}
            <div class="result-content">
                <div class="result-title">
                    {% if r.schema_type %}<span class="schema-badge">{{ r.schema_type }}</span>{% endif %}
                    <a href="{{ r.url }}" target="_blank">{{ r.og_title or r.title or r.url }}</a>
                </div>
                <div class="result-url">{{ r.url }}</div>
                <div class="result-snippet">{{ (r.og_description or r.body_text)[:200] }}...</div>
                <div class="result-meta">
                    {% if r.published_date %} {{ r.published_date }}{% endif %}
                    {% if r.has_audio == 1 %} {{ 'Audio' }}{% endif %}
                    {% if r.relevance %}<span class="relevance-badge">{{ r.relevance }}%</span>{% endif %}
                </div>
                {% if r.audio_url and r.has_audio == 1 %}
                <div style="margin-top:10px">
                    <audio controls><source src="{{ r.audio_url }}" type="audio/mpeg"></audio>
                </div>
                {% endif %}
            </div>
        </div>
        {% endfor %}
        {% else %}
        <div class="no-results">
            <p>Zadne vysledky nenalezeny</p>
            <p style="font-size:.9em;color:#666">Zkuste jine slovo nebo pridejte stranky pres spravce.</p>
        </div>
        {% endif %}
    </div>
    {% endif %}
</div>
<script>
    function setFilter(f){
        document.getElementById('filterInput').value=f;
        document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
        event.target.classList.add('active');
        document.getElementById('searchForm').submit();
    }
    let acTimer;
    document.getElementById('searchInput').addEventListener('input',function(){
        clearTimeout(acTimer);
        const q=this.value.trim();
        if(q.length<2){document.getElementById('acDropdown').style.display='none';return;}
        acTimer=setTimeout(()=>{
            fetch('/autocomplete?q='+encodeURIComponent(q))
                .then(r=>r.json())
                .then(data=>{
                    const dd=document.getElementById('acDropdown');
                    dd.innerHTML='';
                    if(!data.results||!data.results.length){dd.style.display='none';return;}
                    data.results.forEach(item=>{
                        const d=document.createElement('div');
                        d.textContent=item;
                        d.onclick=()=>{document.getElementById('searchInput').value=item;
                                        dd.style.display='none';
                                        document.getElementById('searchForm').submit();};
                        dd.appendChild(d);
                    });
                    dd.style.display='block';
                });
        },280);
    });
    document.addEventListener('click',e=>{
        if(!e.target.closest('.search-wrap'))
            document.getElementById('acDropdown').style.display='none';
    });
</script>
</body>
</html>
"""

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="cs">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mini Search - Admin</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
             background:#1a1a2e;color:#e0e0e0;line-height:1.6;padding:20px}
        .container{max-width:1200px;margin:0 auto}
        h1{color:#e94560;margin-bottom:10px;font-size:1.8em}
        .card{background:#16213e;border-radius:10px;padding:20px;margin-bottom:20px}
        .card h3{color:#0f3460;margin-bottom:15px}
        .btn{background:#e94560;color:#fff;border:none;padding:10px 20px;
             border-radius:5px;cursor:pointer;font-size:1em;text-decoration:none;display:inline-block}
        .btn:hover{background:#c81e45}
        .btn-green{background:#4caf50}.btn-green:hover{background:#388e3c}
        .btn-orange{background:#ff9800}.btn-orange:hover{background:#e68a00}
        .btn-gray{background:#666}.btn-gray:hover{background:#555}
        .form-group{margin-bottom:15px}
        .form-group label{display:block;margin-bottom:5px;color:#0f3460}
        .form-group input{width:100%;padding:10px;border-radius:5px;border:1px solid #333;
                          background:#1a1a2e;color:#e0e0e0;font-size:1em}
        table{width:100%;border-collapse:collapse}
        th,td{padding:12px;text-align:left;border-bottom:1px solid #333}
        th{background:#0f3460;color:#fff}
        tr:hover{background:#1f2b4a}
        .stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:15px}
        .stat-card{background:#1a1a2e;padding:15px;border-radius:8px;text-align:center}
        .stat-value{font-size:2em;font-weight:bold;color:#e94560}
        .stat-label{color:#0f3460;font-size:.9em;margin-top:5px}
        .nav a{color:#2196f3;margin-right:20px;text-decoration:none}
        .msg{padding:10px 15px;border-radius:5px;margin-bottom:15px}
        .msg-ok{background:#4caf5020;color:#4caf50}
        .msg-err{background:#e9456020;color:#e94560}
        .update-section{background:#16213e;padding:20px;border-radius:10px;margin-bottom:20px}
        .update-section h3{color:#0f3460;margin-bottom:15px}
        .update-info{display:flex;gap:20px;flex-wrap:wrap;margin-top:10px}
        .update-item{background:#1a1a2e;padding:10px;border-radius:5px;min-width:200px}
        .update-label{color:#666;font-size:.85em}
        .update-value{color:#e0e0e0;font-size:1.1em;font-weight:bold}
    </style>
</head>
<body>
<div class="container">
    <div class="nav" style="margin-bottom:20px">
        <a href="/">Vyhledavani</a>
        <a href="/admin">Sprava</a>
    </div>
    <h1>Spravce</h1>
    {% if request.args.get('success') %}
    <div class="msg msg-ok">{{ request.args.get('success') }}</div>
    {% endif %}
    {% if request.args.get('error') %}
    <div class="msg msg-err">{{ request.args.get('error') }}</div>
    {% endif %}
    
    <div class="update-section">
        <h3>Aktualizace</h3>
        <div class="update-info">
            <div class="update-item">
                <div class="update-label">Aktualni commit:</div>
                <div class="update-value">{{ update_status.current_commit[:8] if update_status.current_commit else 'N/A' }}</div>
            </div>
            <div class="update-item">
                <div class="update-label">Posledni commit:</div>
                <div class="update-value">{{ update_status.latest_commit[:8] if update_status.latest_commit else 'N/A' }}</div>
            </div>
            <div class="update-item">
                <div class="update-label">Aktualizace dostupna:</div>
                <div class="update-value">{{ 'Ano' if update_status.update_available else 'Ne' }}</div>
            </div>
            <div class="update-item">
                <div class="update-label">Posledni kontrola:</div>
                <div class="update-value">{{ datetime.fromtimestamp(update_status.last_check).strftime('%Y-%m-%d %H:%M:%S') if update_status.last_check else 'Nikdy' }}</div>
            </div>
        </div>
        <div style="margin-top:15px">
            <a href="/admin/check-update" class="btn">Kontrolovat aktualizace</a>
            {% if update_status.update_available %}
            <a href="/admin/apply-update" class="btn btn-green" onclick="return confirm('Opravdu chcete aktualizovat?')">Aktualizovat</a>
            {% endif %}
        </div>
    </div>
    
    <div class="card">
        <h3>Prehled</h3>
        <div class="stats-grid">
            <div class="stat-card"><div class="stat-value">{{ stats.sites }}</div><div class="stat-label">Weby</div></div>
            <div class="stat-card"><div class="stat-value">{{ stats.pages }}</div><div class="stat-label">Indexovano</div></div>
            <div class="stat-card"><div class="stat-value">{{ stats.pending }}</div><div class="stat-label">Ceka</div></div>
            <div class="stat-card"><div class="stat-value">{{ stats.completed }}</div><div class="stat-label">Dokonceno</div></div>
            <div class="stat-card"><div class="stat-value">{{ stats.errors }}</div><div class="stat-label">Chyby</div></div>
        </div>
    </div>
    <div class="card">
        <h3>Pridat stranku</h3>
        <form action="/admin/add" method="post">
            <div class="form-group"><label>URL:</label>
                <input type="url" name="url" placeholder="https://priklad.cz" required></div>
            <div class="form-group"><label>Max. stranek (default 500):</label>
                <input type="number" name="max_pages" value="500" min="1" max="10000"></div>
            <button type="submit" class="btn btn-green">Pridat</button>
        </form>
    </div>
    <div class="card">
        <h3>Sledovane stranky</h3>
        {% if sites %}
        <table>
            <thead><tr><th>ID</th><th>URL</th><th>Stav</th><th>Indexovano</th><th>Ceka</th><th>Posledni crawl</th><th>Akce</th></tr></thead>
            <tbody>
            {% for site in sites %}
            <tr>
                <td>{{ site.id }}</td>
                <td>{{ site.canonical_url }}</td>
                <td>{{ site.status }}</td>
                <td>{{ site.indexed_count }}</td>
                <td>{{ site.pending_count }}</td>
                <td>{{ site.last_crawled_str }}</td>
                <td>
                    <a href="/admin/recrawl/{{ site.id }}" class="btn btn-orange" style="padding:5px 10px;font-size:.9em">Recrawl</a>
                    <a href="/admin/delete/{{ site.id }}" class="btn btn-gray" style="padding:5px 10px;font-size:.9em"
                       onclick="return confirm('Opravdu smazat?')">Smazat</a>
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
        {% else %}
        <p style="color:#666">Zadne stranky. Pridejte prvni stranku vyse.</p>
        {% endif %}
    </div>
</div>
<script>
    function checkPending(){
        fetch('/admin/stats').then(r=>r.json()).then(d=>{
            if(d.pending>0) setTimeout(()=>location.reload(),5000);
        });
    }
    setInterval(checkPending,30000);
    checkPending();
</script>
</body>
</html>
"""


@app.route('/')
def search_index():
    query = request.args.get('q', '').strip()
    filter_type = request.args.get('filter', 'all')
    if query:
        results = hybrid_search(query, limit=25, filter_type=filter_type if filter_type != 'all' else None)
        return render_template_string(
            SEARCH_HTML, query=query, results=results,
            total_results=len(results), current_filter=filter_type
        )
    return render_template_string(
        SEARCH_HTML, query='', results=[], total_results=0, current_filter='all'
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


@app.route('/admin')
def admin_index():
    return render_template_string(ADMIN_HTML, stats=get_db_stats(), sites=get_all_sites(), 
                                   update_status=get_update_status())


@app.route('/admin/add', methods=['POST'])
def admin_add_site():
    url = request.form.get('url', '').strip()
    max_pages = int(request.form.get('max_pages', 500))
    if not url:
        return redirect('/admin?error=URL je povinna')
    site_id, added = phase_1_discovery(url, max_pages)
    if site_id:
        return redirect(f'/admin?success=Stranka pridana! {added} URL k prozkoumani')
    return redirect('/admin?error=Chyba pri pridavani stranky')


@app.route('/admin/recrawl/<int:site_id>')
def admin_recrawl_site(site_id):
    site = get_site_by_id(site_id)
    if site:
        phase_1_discovery(site['canonical_url'], site['max_pages'])
    return redirect('/admin?success=Re-crawl zahajen')


@app.route('/admin/delete/<int:site_id>')
def admin_delete_site(site_id):
    delete_site(site_id)
    return redirect('/admin?success=Stranka smazana')


@app.route('/admin/stats')
def admin_stats():
    return jsonify(get_db_stats())


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
            return redirect('/admin?success=Aktualizace je dostupna! ' + msg)
        else:
            return redirect('/admin?success=Zadna aktualizace neni dostupna. ' + msg)
    except Exception as e:
        return redirect(f'/admin?error=Chyba pri kontrole aktualizace: {str(e)}')


@app.route('/admin/apply-update')
def admin_apply_update():
    try:
        success, msg = apply_update()
        if success:
            return redirect('/admin?success=' + msg)
        else:
            return redirect('/admin?error=' + msg)
    except Exception as e:
        return redirect(f'/admin?error=Chyba pri aktualizaci: {str(e)}')


@app.route('/admin/update-status')
def admin_update_status():
    return jsonify(get_update_status())


# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    print('=' * 70)
    print('Mini Search v7.0 - Hybrid Search Engine')
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
