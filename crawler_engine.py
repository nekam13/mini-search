#!/usr/bin/env python3
"""
Mini Search - Crawler Engine v4.0
Kompletne prepracovany pro SQLite WAL mode a Termux/Ubuntu ARM64
"""

import sqlite3
import json
import time
import signal
import sys
import os
import threading
from urllib.parse import urlparse, urlunparse

# ============================================================================
# KONFIGURACE
# ============================================================================

DB_PATH = "console.db"
MAX_RETRIES = 3
REQUEST_TIMEOUT = 30
SHUTDOWN_FLAG = False

# ============================================================================
# DATABASE HELPERS
# ============================================================================

_db_lock = threading.Lock()
_db_conn = None


def get_db():
    """Get or create global SQLite connection with WAL mode"""
    global _db_conn
    
    with _db_lock:
        if _db_conn is None:
            # Ensure data directory exists
            os.makedirs("data", exist_ok=True)
            
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
            
            # Initialize tables
            init_db(_db_conn)
        
        return _db_conn


def init_db(conn=None):
    """Initialize database tables"""
    if conn is None:
        conn = get_db()
    
    cursor = conn.cursor()
    
    # Sites table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_url TEXT UNIQUE,
            aliases TEXT DEFAULT '[]',
            status TEXT DEFAULT 'active',
            error_count INTEGER DEFAULT 0,
            last_crawled INTEGER DEFAULT 0,
            max_pages INTEGER DEFAULT 500,
            created_at INTEGER DEFAULT 0
        )
    ''')
    
    # Crawl queue table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS crawl_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER,
            url TEXT,
            status TEXT DEFAULT 'pending',
            locked_by TEXT DEFAULT '',
            error_reason TEXT DEFAULT '',
            retry_count INTEGER DEFAULT 0,
            created_at INTEGER DEFAULT 0,
            FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
        )
    ''')
    
    # Sitemaps and feeds table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sitemaps_feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER,
            url TEXT,
            type TEXT DEFAULT 'sitemap',
            last_checked INTEGER DEFAULT 0,
            FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
        )
    ''')
    
    # Pages table for indexed content
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER,
            url TEXT,
            title TEXT DEFAULT '',
            content TEXT DEFAULT '',
            metadata TEXT DEFAULT '{}',
            indexed_at INTEGER DEFAULT 0,
            FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
        )
    ''')
    
    # Create indexes
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_queue(status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_queue_site ON crawl_queue(site_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_pages_site ON pages(site_id)')
    
    conn.commit()


def close_db():
    """Close database connection"""
    global _db_conn
    with _db_lock:
        if _db_conn is not None:
            _db_conn.close()
            _db_conn = None


def execute_db(query, params=(), commit=False):
    """Execute a database query with proper locking and error handling"""
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute(query, params)
        if commit:
            conn.commit()
        return cursor
    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            time.sleep(0.5)
            try:
                cursor.execute(query, params)
                if commit:
                    conn.commit()
                return cursor
            except Exception as e2:
                print(f"⚠️  DB Lock retry failed: {e2}")
                raise
        raise


def execute_db_fetchone(query, params=()):
    """Execute and fetch one result"""
    cursor = execute_db(query, params)
    return cursor.fetchone()


def execute_db_fetchall(query, params=()):
    """Execute and fetch all results"""
    cursor = execute_db(query, params)
    return cursor.fetchall()


# ============================================================================
# SHUTDOWN HANDLING
# ============================================================================


def handle_shutdown(signum, frame):
    """Handle shutdown signals"""
    global SHUTDOWN_FLAG
    SHUTDOWN_FLAG = True
    print(f"\n⚠️  Signal {signum} received, shutting down...")
    close_db()
    sys.exit(0)


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


# ============================================================================
# URL HELPERS
# ============================================================================


def normalize_domain(url):
    """Normalize domain: strip www., lowercase, strip trailing slash"""
    if not url:
        return ""
    
    parsed = urlparse(url)
    
    # Handle URLs without scheme
    if not parsed.scheme:
        url = f"https://{url}"
        parsed = urlparse(url)
    
    netloc = parsed.netloc.lower()
    
    # Remove www. prefix
    if netloc.startswith("www."):
        netloc = netloc[4:]
    
    # Reconstruct URL with normalized domain
    normalized = urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, ''))
    
    # Remove trailing slash from path if present
    if normalized.endswith('/') and len(parsed.path) > 1:
        normalized = normalized[:-1]
    
    return normalized


def normalize_url(url):
    """Normalize URL for comparison"""
    if not url:
        return ""
    
    parsed = urlparse(url)
    
    # Handle URLs without scheme
    if not parsed.scheme:
        url = f"https://{url}"
        parsed = urlparse(url)
    
    # Normalize domain
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    
    # Reconstruct
    normalized = urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, ''))
    
    # Remove trailing slash
    if normalized.endswith('/') and len(parsed.path) > 1:
        normalized = normalized[:-1]
    
    # Remove fragment
    normalized = normalized.split('#')[0]
    
    return normalized


# ============================================================================
# SITE MANAGEMENT
# ============================================================================


def add_site(site_url, max_pages=500):
    """Add a new site with domain deduplication"""
    site_url = normalize_domain(site_url)
    if not site_url:
        return None
    
    parsed = urlparse(site_url)
    canonical_domain = parsed.netloc
    
    # Check if domain already exists
    result = execute_db_fetchone(
        "SELECT id, canonical_url, aliases FROM sites WHERE canonical_url = ? OR aliases LIKE ?",
        (canonical_domain, f"%{canonical_domain}%")
    )
    
    if result:
        site_id, existing_canonical, aliases_str = result
        aliases = json.loads(aliases_str) if aliases_str else []
        
        # Add new URL as alias if not already present
        if site_url not in aliases and site_url != existing_canonical:
            aliases.append(site_url)
            execute_db(
                "UPDATE sites SET aliases = ? WHERE id = ?",
                (json.dumps(aliases), site_id),
                commit=True
            )
        
        return site_id
    
    # Insert new site - store the domain (netloc) as canonical_url
    execute_db(
        "INSERT INTO sites (canonical_url, aliases, status, max_pages) VALUES (?, ?, 'active', ?)",
        (canonical_domain, json.dumps([site_url]), max_pages),
        commit=True
    )
    
    return execute_db_fetchone("SELECT last_insert_rowid()")[0]


def get_site_by_id(site_id):
    """Get site by ID"""
    result = execute_db_fetchone("SELECT * FROM sites WHERE id = ?", (site_id,))
    return dict(result) if result else None


def get_all_sites():
    """Get all sites"""
    results = execute_db_fetchall("SELECT * FROM sites ORDER BY created_at DESC")
    return [dict(row) for row in results]


def update_site_status(site_id, status):
    """Update site status"""
    execute_db(
        "UPDATE sites SET status = ? WHERE id = ?",
        (status, site_id),
        commit=True
    )


def recrawl_site(site_id):
    """Re-crawl a site by resetting its queue"""
    site = get_site_by_id(site_id)
    if not site:
        return False
    
    site_url = site['canonical_url']
    max_pages = site['max_pages']
    
    # Reset site status
    execute_db(
        "UPDATE sites SET status = 'active', error_count = 0 WHERE id = ?",
        (site_id,),
        commit=True
    )
    
    # Delete existing queue and sitemaps/feeds
    execute_db("DELETE FROM crawl_queue WHERE site_id = ?", (site_id,), commit=True)
    execute_db("DELETE FROM sitemaps_feeds WHERE site_id = ?", (site_id,), commit=True)
    
    # Re-run Phase 1 discovery
    site_id_new, urls_added = phase_1_discovery(site_url, max_pages)
    
    if site_id_new:
        print(f"✅ Re-crawl zahájen: {urls_added} nových URL")
    else:
        print("⚠️  Re-crawl se nezdařil")
    
    return site_id_new is not None


# ============================================================================
# CRAWLING FUNCTIONS
# ============================================================================


def phase_1_discovery(site_url, max_pages=500):
    """Phase 1: Discovery - Build full URL queue"""
    site_url = normalize_domain(site_url)
    if not site_url:
        return None, 0
    
    site_id = add_site(site_url, max_pages)
    if not site_id:
        return None, 0
    
    # Clear existing queue for this site (for re-crawl)
    execute_db("DELETE FROM crawl_queue WHERE site_id = ?", (site_id,), commit=True)
    
    # Get existing URLs for this site
    results = execute_db_fetchall("SELECT url FROM crawl_queue WHERE site_id = ?", (site_id,))
    existing_urls = {row[0] for row in results}
    
    # Discover sitemaps and feeds
    discovered_items = discover_sitemaps_and_feeds(site_url, site_id)
    
    # Add discovered URLs to queue
    urls_to_add = []
    for item in discovered_items:
        url = item['url']
        if url and url not in existing_urls:
            urls_to_add.append(url)
            existing_urls.add(url)
    
    # If no URLs found from sitemaps/feeds, crawl homepage
    if not urls_to_add:
        urls_to_add = crawl_homepage_for_links(site_url, site_id, max_pages)
    
    # Add URLs to crawl queue
    for url in urls_to_add:
        if not url:
            continue
        try:
            execute_db(
                "INSERT OR IGNORE INTO crawl_queue (site_id, url, status) VALUES (?, ?, 'pending')",
                (site_id, url),
                commit=True
            )
        except sqlite3.IntegrityError:
            pass
    
    # Update site last_crawled time
    execute_db(
        "UPDATE sites SET last_crawled = strftime('%s', 'now') WHERE id = ?",
        (site_id,),
        commit=True
    )
    
    return site_id, len(urls_to_add)


def discover_sitemaps_and_feeds(site_url, site_id):
    """Phase 1: Discovery - Find sitemaps and feeds without downloading content"""
    parsed = urlparse(site_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    
    discovered = []
    
    # Common sitemap and feed URLs to check
    common_urls = [
        "/sitemap.xml",
        "/sitemap_index.xml",
        "/sitemap.xml.gz",
        "/sitemap_index.xml.gz",
        "/feed",
        "/rss",
        "/atom.xml",
        "/feed.xml",
        "/rss.xml",
        "/feed/rss",
        "/feed/atom",
        "/rss2.0.xml",
        "/rdf.xml"
    ]
    
    for path in common_urls:
        url = base_url + path
        discovered.append({'url': url, 'type': 'sitemap' if 'sitemap' in path else 'feed'})
    
    # Store discovered items in database
    for item in discovered:
        try:
            execute_db(
                "INSERT OR IGNORE INTO sitemaps_feeds (site_id, url, type) VALUES (?, ?, ?)",
                (site_id, item['url'], item['type']),
                commit=True
            )
        except sqlite3.IntegrityError:
            pass
    
    return discovered


def crawl_homepage_for_links(site_url, site_id, max_pages):
    """Crawl homepage to find links"""
    # Simple implementation - just return the homepage for now
    return [site_url]


# ============================================================================
# WORKER FUNCTIONS
# ============================================================================


def worker_a():
    """Worker A: Process pending URLs from crawl queue"""
    worker_name = "worker_a"
    print(f"✅ Worker {worker_name} spuštěn")
    
    while not SHUTDOWN_FLAG:
        try:
            # Lock a batch of pending URLs
            results = execute_db_fetchall(
                "SELECT id, site_id, url FROM crawl_queue WHERE status = 'pending' AND retry_count < ? LIMIT 5",
                (MAX_RETRIES,)
            )
            
            if not results:
                time.sleep(5)
                continue
            
            batch = results
            
            # Mark as locked
            for row_id, site_id, url in batch:
                execute_db(
                    "UPDATE crawl_queue SET status = 'locked', locked_by = ? WHERE id = ?",
                    (worker_name, row_id),
                    commit=True
                )
            
            # Process each URL
            for row_id, site_id, url in batch:
                if SHUTDOWN_FLAG:
                    break
                
                # Simple processing - just mark as completed for now
                execute_db(
                    "UPDATE crawl_queue SET status = 'completed' WHERE id = ?",
                    (row_id,),
                    commit=True
                )
                
                time.sleep(0.1)  # Rate limiting
            
        except Exception as e:
            print(f"⚠️  Worker {worker_name} error: {e}")
            time.sleep(5)


def worker_b():
    """Worker B: Process pending URLs from crawl queue"""
    worker_name = "worker_b"
    print(f"✅ Worker {worker_name} spuštěn")
    
    while not SHUTDOWN_FLAG:
        try:
            # Lock a batch of pending URLs
            results = execute_db_fetchall(
                "SELECT id, site_id, url FROM crawl_queue WHERE status = 'pending' AND retry_count < ? LIMIT 5",
                (MAX_RETRIES,)
            )
            
            if not results:
                time.sleep(5)
                continue
            
            batch = results
            
            # Mark as locked
            for row_id, site_id, url in batch:
                execute_db(
                    "UPDATE crawl_queue SET status = 'locked', locked_by = ? WHERE id = ?",
                    (worker_name, row_id),
                    commit=True
                )
            
            # Process each URL
            for row_id, site_id, url in batch:
                if SHUTDOWN_FLAG:
                    break
                
                # Simple processing - just mark as completed for now
                execute_db(
                    "UPDATE crawl_queue SET status = 'completed' WHERE id = ?",
                    (row_id,),
                    commit=True
                )
                
                time.sleep(0.1)  # Rate limiting
            
        except Exception as e:
            print(f"⚠️  Worker {worker_name} error: {e}")
            time.sleep(5)


def start_workers():
    """Start background worker threads"""
    thread_a = threading.Thread(target=worker_a, daemon=True)
    thread_b = threading.Thread(target=worker_b, daemon=True)
    
    thread_a.start()
    thread_b.start()
    
    print("✅ Workers spuštěny (worker_a, worker_b)")
    
    return thread_a, thread_b


# ============================================================================
# VECTOR SEARCH BACKEND (Simplified)
# ============================================================================


class DummyVectorBackend:
    """Dummy vector backend for when sentence-transformers is not available"""
    
    def __init__(self):
        self.index = None
        self.metadata = {}
        self.ids = []
    
    def initialize(self):
        pass
    
    def upsert(self, ids, embeddings, metadatas):
        for i, id in enumerate(ids):
            if id not in self.ids:
                self.ids.append(id)
            self.metadata[id] = metadatas[i]
    
    def query(self, query_embeddings, n_results=10, where=None):
        return [[], []]
    
    def delete(self, ids):
        for id in ids:
            if id in self.ids:
                self.ids.remove(id)
            if id in self.metadata:
                del self.metadata[id]
    
    def persist(self):
        pass
    
    def get_by_id(self, id):
        return self.metadata.get(id, {})


# Try to initialize proper vector backend
try:
    from sentence_transformers import SentenceTransformer
    
    # Try ChromaDB
    try:
        import chromadb
        from chromadb.config import Settings
        
        os.makedirs('chroma_db', exist_ok=True)
        vector_db = chromadb.Client(
            Settings(
                chroma_db_impl='duckdb+parquet',
                persist_directory='chroma_db'
            )
        )
        vector_db.get_or_create_collection(name='pages')
        print("✅ Vector backend: ChromaDB")
        VECTOR_BACKEND_TYPE = "chromadb"
    except Exception:
        # Fallback to dummy
        vector_db = DummyVectorBackend()
        print("⚠️  Vector backend: dummy (ChromaDB not available)")
        VECTOR_BACKEND_TYPE = "dummy"
        
except ImportError:
    vector_db = DummyVectorBackend()
    print("⚠️  Embedding model se nepodařilo načíst: No module named 'sentence_transformers'")
    print("💡  Nainstalujte: pip install torch --index-url https://download.pytorch.org/whl/cpu")
    print("   Pak: pip install sentence-transformers==2.2.2")
    VECTOR_BACKEND_TYPE = "dummy"


# ============================================================================
# MAIN
# ============================================================================


if __name__ == "__main__":
    print("=" * 70)
    print("Mini Search - Crawler Engine v4.0")
    print("=" * 70)
    print()
    
    # Initialize database
    init_db()
    print("✅ Databáze inicializována")
    
    # Start workers
    start_workers()
    
    # Keep main thread alive
    try:
        while not SHUTDOWN_FLAG:
            time.sleep(1)
    except KeyboardInterrupt:
        handle_shutdown(None, None)
