#!/usr/bin/env python3
"""
Mini Search - Kombinovana aplikace v6.0
Kompletni implementace podle specifikace
- Single Flask app na portu 8070
- SQLite + hnswlib pro vektorove hledani
- Dva workery v background thread
- APScheduler pro pravidelne ulohy
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
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlunparse, urljoin
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

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

# Global instances
_db_lock = threading.Lock()
_db_conn = None
_hnsw_index = None
_model = None
_scheduler = None
_worker_threads = []

# ============================================================================
# DATABASE SCHEMA
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
            created_at INTEGER DEFAULT 0
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
            created_at INTEGER DEFAULT 0,
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

DB_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(status)",
    "CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_queue(status)",
    "CREATE INDEX IF NOT EXISTS idx_queue_priority ON crawl_queue(priority)",
    "CREATE INDEX IF NOT EXISTS idx_queue_site ON crawl_queue(site_id)",
    "CREATE INDEX IF NOT EXISTS idx_pages_site ON pages(site_id)",
    "CREATE INDEX IF NOT EXISTS idx_pages_url_hash ON pages(url_hash)"
]

# ============================================================================
# DATABASE FUNCTIONS
# ============================================================================


def get_db():
    """Get or create global SQLite connection with WAL mode"""
    global _db_conn
    
    with _db_lock:
        if _db_conn is None:
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
            
            init_db(_db_conn)
        
        return _db_conn


def init_db(conn=None):
    """Initialize database tables and indexes"""
    if conn is None:
        conn = get_db()
    
    cursor = conn.cursor()
    
    for table_name, schema in DB_SCHEMA.items():
        cursor.execute(schema)
    
    for index_sql in DB_INDEXES:
        cursor.execute(index_sql)
    
    conn.commit()


def close_db():
    """Close database connection"""
    global _db_conn
    with _db_lock:
        if _db_conn is not None:
            _db_conn.close()
            _db_conn = None


def execute_db(query, params=(), commit=False):
    """Execute a database query with proper locking"""
    conn = get_db()
    cursor = conn.cursor()
    
    try:
        with _db_lock:
            cursor.execute(query, params)
            if commit:
                conn.commit()
        return cursor
    except sqlite3.OperationalError as e:
        if "locked" in str(e):
            time.sleep(0.1)
            with _db_lock:
                cursor.execute(query, params)
                if commit:
                    conn.commit()
            return cursor
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
# URL HELPERS
# ============================================================================


def normalize_url(url):
    """Normalize URL: lowercase, add https://, strip www., strip trailing slash"""
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
    
    normalized = urlunparse((parsed.scheme, netloc, path, parsed.params, parsed.query, ''))
    
    return normalized


def get_domain(url):
    """Extract domain from URL"""
    parsed = urlparse(url)
    if not parsed.scheme:
        url = f"https://{url}"
        parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def url_hash(url):
    """Generate MD5 hash of normalized URL"""
    normalized = normalize_url(url)
    return hashlib.md5(normalized.encode('utf-8')).hexdigest()


# ============================================================================
# SITE MANAGEMENT
# ============================================================================


def add_site(site_url, max_pages=500):
    """Add a new site with domain deduplication"""
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
                (json.dumps(aliases), site_id),
                commit=True
            )
        
        return site_id
    
    execute_db(
        "INSERT INTO sites (canonical_url, aliases, status, max_pages) VALUES (?, ?, 'active', ?)",
        (domain, json.dumps([site_url]), max_pages),
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
    sites = []
    for row in results:
        site = dict(row)
        if site['last_crawled'] > 0:
            site['last_crawled_str'] = datetime.fromtimestamp(site['last_crawled']).strftime('%Y-%m-%d %H:%M:%S')
        else:
            site['last_crawled_str'] = 'Nikdy'
        
        # Count indexed pages
        cursor = execute_db("SELECT COUNT(*) FROM pages WHERE site_id = ?", (site['id'],))
        site['indexed_count'] = cursor.fetchone()[0]
        
        # Count pending
        cursor = execute_db("SELECT COUNT(*) FROM crawl_queue WHERE site_id = ? AND status = 'pending'", (site['id'],))
        site['pending_count'] = cursor.fetchone()[0]
        
        sites.append(site)
    return sites


def update_site_status(site_id, status):
    """Update site status"""
    execute_db(
        "UPDATE sites SET status = ? WHERE id = ?",
        (status, site_id),
        commit=True
    )


def delete_site(site_id):
    """Delete a site and all its data"""
    execute_db("DELETE FROM sites WHERE id = ?", (site_id,), commit=True)
    execute_db("DELETE FROM pages WHERE site_id = ?", (site_id,), commit=True)
    execute_db("DELETE FROM crawl_queue WHERE site_id = ?", (site_id,), commit=True)
    execute_db("DELETE FROM sitemaps_feeds WHERE site_id = ?", (site_id,), commit=True)
    
    # Rebuild hnsw index
    rebuild_hnsw_index()


def get_db_stats():
    """Get database statistics"""
    cursor = execute_db("SELECT COUNT(*) FROM sites")
    sites_count = cursor.fetchone()[0]
    
    cursor = execute_db("SELECT COUNT(*) FROM pages")
    pages_count = cursor.fetchone()[0]
    
    cursor = execute_db("SELECT COUNT(*) FROM crawl_queue WHERE status = 'pending'")
    pending_count = cursor.fetchone()[0]
    
    cursor = execute_db("SELECT COUNT(*) FROM crawl_queue WHERE status = 'error'")
    errors_count = cursor.fetchone()[0]
    
    return {
        'sites': sites_count,
        'pages': pages_count,
        'pending': pending_count,
        'errors': errors_count
    }


# ============================================================================
# VECTOR SEARCH (hnswlib)
# ============================================================================


def get_hnsw_index():
    """Get or create global hnswlib index"""
    global _hnsw_index
    
    if _hnsw_index is None:
        # Load model to get dimension
        model = get_model()
        dim = model.get_sentence_embedding_dimension()
        
        _hnsw_index = hnswlib.Index(space='cosine', dim=dim)
        _hnsw_index.init_index(max_elements=100000, ef_construction=200, M=16)
        _hnsw_index.set_ef(50)
        
        # Load existing embeddings
        rebuild_hnsw_index()
    
    return _hnsw_index


def rebuild_hnsw_index():
    """Rebuild hnswlib index from SQLite"""
    global _hnsw_index
    
    if _hnsw_index is None:
        _hnsw_index = get_hnsw_index()
    
    # Clear index
    _hnsw_index.reset_index()
    
    # Load all embeddings
    results = execute_db_fetchall("SELECT id, embedding FROM pages WHERE embedding IS NOT NULL")
    
    if results:
        ids = []
        embeddings = []
        
        for row in results:
            try:
                emb = np.frombuffer(row[1], dtype=np.float32)
                if emb.size > 0:
                    ids.append(row[0])
                    embeddings.append(emb)
            except Exception:
                pass
        
        if embeddings:
            _hnsw_index.add_items(np.array(embeddings), np.array(ids))


def get_model():
    """Get or load global SentenceTransformer model"""
    global _model
    
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')
        except Exception as e:
            print(f"⚠️  Embedding model se nepodařilo načíst: {e}")
            print("💡  Nainstalujte: pip install torch --index-url https://download.pytorch.org/whl/cpu")
            print("   Pak: pip install sentence-transformers==2.2.2")
            # Create dummy model
            class DummyModel:
                def get_sentence_embedding_dimension(self):
                    return 384
                def encode(self, text):
                    return np.zeros(384, dtype=np.float32)
            _model = DummyModel()
    
    return _model


def generate_embedding(text):
    """Generate embedding for text"""
    model = get_model()
    try:
        return model.encode(text[:1000])
    except Exception:
        return np.zeros(384, dtype=np.float32)


def vector_search(query, limit=25, filter_type=None):
    """Search vectors with hnswlib"""
    if _hnsw_index is None:
        get_hnsw_index()
    
    if _hnsw_index.ef > 0:
        query_embedding = generate_embedding(query)
        labels, distances = _hnsw_index.knn_query(query_embedding, k=limit)
        
        results = []
        for i, (label, distance) in enumerate(zip(labels[0], distances[0])):
            if label < 0:
                continue
            
            # Get page info
            page = execute_db_fetchone("SELECT * FROM pages WHERE id = ?", (label,))
            if page:
                page = dict(page)
                
                # Calculate relevance score
                relevance = (1 - distance) * 100
                
                # Bonuses
                if query.lower() in (page.get('title', '') + page.get('og_title', '')).lower():
                    relevance += 10
                
                if page.get('schema_type'):
                    relevance += 5
                
                if page.get('published_timestamp') and page['published_timestamp'] > int((datetime.now() - timedelta(days=30)).timestamp()):
                    relevance += 3
                
                page['relevance'] = round(relevance, 1)
                results.append(page)
        
        # Apply filter
        if filter_type == 'articles':
            results = [r for r in results if r.get('schema_type') in ['Article', 'BlogPosting', 'NewsArticle']]
        elif filter_type == 'podcasts':
            results = [r for r in results if r.get('schema_type') == 'PodcastEpisode']
        elif filter_type == 'audio':
            results = [r for r in results if r.get('has_audio') == 1]
        elif filter_type == 'price':
            results = [r for r in results if 'price' in json.loads(r.get('schema_details', '{}'))]
        
        return results
    
    return []


# ============================================================================
# CRAWLING FUNCTIONS
# ============================================================================


def fetch_robots_txt(base_url):
    """Fetch and parse robots.txt"""
    robots_url = urljoin(base_url, '/robots.txt')
    
    try:
        response = requests.get(robots_url, timeout=REQUEST_TIMEOUT, headers={'User-Agent': USER_AGENT})
        if response.status_code == 200:
            return response.text
    except Exception:
        pass
    return None


def parse_crawl_delay(robots_txt, user_agent=USER_AGENT):
    """Parse crawl-delay from robots.txt"""
    if not robots_txt:
        return MIN_DELAY
    
    delay = MIN_DELAY
    for line in robots_txt.split('\n'):
        line = line.strip()
        if line.lower().startswith('user-agent:'):
            current_agent = line.split(':', 1)[1].strip()
            if current_agent == '*' or current_agent == USER_AGENT:
                continue
        if line.lower().startswith('crawl-delay:'):
            try:
                delay = max(MIN_DELAY, float(line.split(':', 1)[1].strip()))
            except ValueError:
                pass
    return delay


def discover_sitemaps_and_feeds(site_url, site_id):
    """Phase 1: Discovery - Find sitemaps and feeds"""
    parsed = urlparse(site_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    
    # Try robots.txt first
    robots_txt = fetch_robots_txt(base_url)
    sitemap_urls = []
    
    if robots_txt:
        for line in robots_txt.split('\n'):
            line = line.strip().lower()
            if line.startswith('sitemap:'):
                sitemap_url = line.split(':', 1)[1].strip()
                sitemap_urls.append(urljoin(base_url, sitemap_url))
    
    # Common sitemap and feed URLs
    common_urls = [
        "/sitemap.xml",
        "/sitemap_index.xml", 
        "/sitemap.xml.gz",
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
        url = urljoin(base_url, path)
        if url not in sitemap_urls:
            sitemap_urls.append(url)
    
    discovered = []
    
    for url in sitemap_urls:
        try:
            response = requests.head(url, timeout=10, headers={'User-Agent': USER_AGENT})
            if response.status_code == 200:
                content_type = response.headers.get('Content-Type', '')
                if 'xml' in content_type:
                    discovered.append({'url': url, 'type': 'sitemap'})
                elif 'rss' in content_type or 'atom' in content_type or 'xml' in content_type:
                    discovered.append({'url': url, 'type': 'rss' if 'rss' in url else 'atom'})
        except Exception:
            pass
    
    # Save to database
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


def parse_sitemap(url):
    """Parse sitemap XML and extract URLs"""
    urls = []
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={'User-Agent': USER_AGENT})
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'lxml')
            
            # Check if it's a sitemap index
            if soup.find('sitemapindex'):
                for sitemap in soup.find_all('sitemap'):
                    loc = sitemap.find('loc')
                    if loc:
                        urls.extend(parse_sitemap(loc.text))
            else:
                for url_tag in soup.find_all('url'):
                    loc = url_tag.find('loc')
                    if loc:
                        urls.append(loc.text)
    except Exception:
        pass
    
    return urls


def parse_feed(url):
    """Parse feed and extract entry links"""
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


def crawl_homepage_for_links(site_url, site_id, max_pages):
    """Crawl homepage to find links"""
    urls = []
    domain = get_domain(site_url)
    
    try:
        response = requests.get(site_url, timeout=REQUEST_TIMEOUT, headers={'User-Agent': USER_AGENT})
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'lxml')
            
            for a in soup.find_all('a', href=True):
                href = a['href']
                full_url = urljoin(site_url, href)
                
                if get_domain(full_url) == domain:
                    normalized = normalize_url(full_url)
                    if normalized not in urls:
                        urls.append(normalized)
                
                if len(urls) >= max_pages:
                    break
    except Exception:
        pass
    
    return urls


def phase_1_discovery(site_url, max_pages=500):
    """Phase 1: Discovery - Build full URL queue"""
    site_url = normalize_url(site_url)
    if not site_url:
        return None, 0
    
    site_id = add_site(site_url, max_pages)
    if not site_id:
        return None, 0
    
    # Clear existing queue for this site
    execute_db("DELETE FROM crawl_queue WHERE site_id = ?", (site_id,), commit=True)
    
    # Discover sitemaps and feeds
    discovered_items = discover_sitemaps_and_feeds(site_url, site_id)
    
    # Parse sitemaps and feeds
    urls_to_add = []
    
    for item in discovered_items:
        if item['type'] == 'sitemap':
            sitemap_urls = parse_sitemap(item['url'])
            for url in sitemap_urls:
                normalized = normalize_url(url)
                if normalized and normalized not in urls_to_add:
                    urls_to_add.append((normalized, 3))  # priority 3 for sitemap
        elif item['type'] in ['rss', 'atom']:
            feed_urls = parse_feed(item['url'])
            for url in feed_urls:
                normalized = normalize_url(url)
                if normalized and normalized not in urls_to_add:
                    urls_to_add.append((normalized, 1))  # priority 1 for feed
    
    # If no URLs found from sitemaps/feeds, crawl homepage
    if not urls_to_add:
        homepage_urls = crawl_homepage_for_links(site_url, site_id, max_pages)
        for url in homepage_urls:
            urls_to_add.append((url, 5))  # priority 5 for homepage links
    
    # Add URLs to crawl queue
    for url, priority in urls_to_add:
        if not url:
            continue
        
        # Check if already exists
        existing = execute_db_fetchone("SELECT id FROM crawl_queue WHERE url = ?", (url,))
        if existing:
            continue
        
        try:
            execute_db(
                "INSERT INTO crawl_queue (site_id, url, status, priority) VALUES (?, ?, 'pending', ?)",
                (site_id, url, priority),
                commit=True
            )
        except sqlite3.IntegrityError:
            pass
    
    # Update site last_crawled
    execute_db(
        "UPDATE sites SET last_crawled = strftime('%s', 'now') WHERE id = ?",
        (site_id,),
        commit=True
    )
    
    return site_id, len(urls_to_add)


def extract_page_content(url, site_id):
    """Extract all content from a page"""
    page_data = {
        'url': url,
        'site_id': site_id,
        'url_hash': url_hash(url),
        'title': '',
        'og_title': '',
        'og_description': '',
        'og_image': '',
        'favicon_url': '',
        'body_text': '',
        'images': [],
        'schema_type': '',
        'schema_details': {},
        'audio_url': '',
        'has_audio': 0,
        'published_timestamp': 0
    }
    
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT, headers={'User-Agent': USER_AGENT})
        if response.status_code != 200:
            return None
        
        content = response.content
        soup = BeautifulSoup(content, 'lxml')
        
        # Extract title
        og_title = soup.find('meta', attrs={'property': 'og:title'})
        if og_title:
            page_data['og_title'] = og_title.get('content', '')
        
        title_tag = soup.find('title')
        if title_tag:
            page_data['title'] = title_tag.text.strip()
        
        if not page_data['og_title']:
            page_data['og_title'] = page_data['title']
        
        # Extract og:description
        og_desc = soup.find('meta', attrs={'property': 'og:description'})
        if og_desc:
            page_data['og_description'] = og_desc.get('content', '')
        
        # Extract og:image
        og_image = soup.find('meta', attrs={'property': 'og:image'})
        if og_image:
            page_data['og_image'] = og_image.get('content', '')
        
        # Extract favicon
        favicon = soup.find('link', rel='icon') or soup.find('link', rel='shortcut icon')
        if favicon and favicon.get('href'):
            page_data['favicon_url'] = urljoin(url, favicon['href'])
        else:
            page_data['favicon_url'] = urljoin(url, '/favicon.ico')
        
        # Extract body text
        body_parts = []
        for selector in ['article', 'main', 'p', 'h1', 'h2', 'h3']:
            elements = soup.select(selector)
            for el in elements:
                text = el.get_text().strip()
                if text:
                    body_parts.append(text)
        
        page_data['body_text'] = ' '.join(body_parts)[:3500]
        
        # Extract images
        for img in soup.find_all('img', src=True):
            img_url = urljoin(url, img['src'])
            page_data['images'].append({
                'url': img_url,
                'alt': img.get('alt', '')
            })
        
        # Extract audio
        audio = soup.find('audio', src=True)
        if audio:
            page_data['audio_url'] = urljoin(url, audio['src'])
            page_data['has_audio'] = 1
        else:
            for a in soup.find_all('a', href=True):
                href = a['href'].lower()
                if any(href.endswith(ext) for ext in ['.mp3', '.m4a', '.wav', '.ogg']):
                    page_data['audio_url'] = urljoin(url, a['href'])
                    page_data['has_audio'] = 1
                    break
        
        # Extract schema.org
        try:
            data = extruct.extract(content, uniform=True)
            if data:
                for schema in data.get('json-ld', []):
                    if isinstance(schema, dict) and '@type' in schema:
                        page_data['schema_type'] = schema['@type']
                        page_data['schema_details'] = schema
                        
                        # Extract published date
                        if 'datePublished' in schema:
                            try:
                                pub_date = schema['datePublished']
                                if isinstance(pub_date, str):
                                    page_data['published_timestamp'] = int(datetime.strptime(pub_date, '%Y-%m-%d').timestamp())
                            except Exception:
                                pass
                        break
        except Exception:
            pass
        
        # Generate embedding
        embed_text = (page_data['og_title'] or page_data['title']) + " " + page_data['og_description'] + " " + page_data['body_text']
        embedding = generate_embedding(embed_text)
        page_data['embedding'] = embedding.tobytes()
        
        return page_data
    
    except Exception as e:
        print(f"Error extracting {url}: {e}")
        return None


def process_url(queue_id, site_id, url):
    """Process a single URL from crawl queue"""
    try:
        page_data = extract_page_content(url, site_id)
        
        if page_data:
            # Check if already exists
            existing = execute_db_fetchone("SELECT id FROM pages WHERE url_hash = ?", (page_data['url_hash'],))
            
            if existing:
                # Update existing
                execute_db(
                    """UPDATE pages SET 
                       title = ?, og_title = ?, og_description = ?, og_image = ?, 
                       favicon_url = ?, body_text = ?, images = ?, 
                       schema_type = ?, schema_details = ?, audio_url = ?, has_audio = ?, 
                       published_timestamp = ?, embedding = ?, indexed_at = strftime('%s', 'now')
                       WHERE url_hash = ?""",
                    (page_data['title'], page_data['og_title'], page_data['og_description'],
                     page_data['og_image'], page_data['favicon_url'], page_data['body_text'],
                     json.dumps(page_data['images']), page_data['schema_type'],
                     json.dumps(page_data['schema_details']), page_data['audio_url'], page_data['has_audio'],
                     page_data['published_timestamp'], page_data['embedding'], page_data['url_hash']),
                    commit=True
                )
                page_id = existing[0]
            else:
                # Insert new
                execute_db(
                    """INSERT INTO pages 
                       (site_id, url, url_hash, title, og_title, og_description, og_image, 
                        favicon_url, body_text, images, schema_type, schema_details, 
                        audio_url, has_audio, published_timestamp, embedding, indexed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%s', 'now'))""",
                    (page_data['site_id'], page_data['url'], page_data['url_hash'],
                     page_data['title'], page_data['og_title'], page_data['og_description'],
                     page_data['og_image'], page_data['favicon_url'], page_data['body_text'],
                     json.dumps(page_data['images']), page_data['schema_type'],
                     json.dumps(page_data['schema_details']), page_data['audio_url'],
                     page_data['has_audio'], page_data['published_timestamp'], page_data['embedding']),
                    commit=True
                )
                page_id = execute_db_fetchone("SELECT last_insert_rowid()")[0]
            
            # Add to hnsw index
            if _hnsw_index is not None:
                embedding_array = np.frombuffer(page_data['embedding'], dtype=np.float32)
                _hnsw_index.add_items(embedding_array.reshape(1, -1), np.array([page_id]))
            
            # Mark as completed
            execute_db(
                "UPDATE crawl_queue SET status = 'completed' WHERE id = ?",
                (queue_id,),
                commit=True
            )
        else:
            # Mark as error
            execute_db(
                "UPDATE crawl_queue SET status = 'error', error_reason = 'Extraction failed' WHERE id = ?",
                (queue_id,),
                commit=True
            )
    
    except Exception as e:
        # Mark as error
        execute_db(
            "UPDATE crawl_queue SET status = 'error', error_reason = ? WHERE id = ?",
            (str(e), queue_id),
            commit=True
        )


# ============================================================================
# WORKER FUNCTIONS
# ============================================================================


def worker_a():
    """Worker A: Process pending URLs"""
    worker_name = "worker_a"
    print(f"✅ Worker {worker_name} spuštěn")
    
    while not SHUTDOWN_FLAG:
        try:
            # Get batch of pending URLs
            results = execute_db_fetchall(
                "SELECT id, site_id, url FROM crawl_queue WHERE status = 'pending' AND retry_count < ? ORDER BY priority ASC LIMIT 3",
                (MAX_RETRIES,)
            )
            
            if not results:
                time.sleep(5)
                continue
            
            # Lock batch
            for row_id, site_id, url in results:
                execute_db(
                    "UPDATE crawl_queue SET status = 'locked', locked_by = ? WHERE id = ?",
                    (worker_name, row_id),
                    commit=True
                )
            
            # Process each URL
            for row_id, site_id, url in results:
                if SHUTDOWN_FLAG:
                    break
                
                process_url(row_id, site_id, url)
                time.sleep(MIN_DELAY)
            
        except Exception as e:
            print(f"⚠️  Worker {worker_name} error: {e}")
            time.sleep(5)


def worker_b():
    """Worker B: Process pending URLs"""
    worker_name = "worker_b"
    print(f"✅ Worker {worker_name} spuštěn")
    
    while not SHUTDOWN_FLAG:
        try:
            # Get batch of pending URLs
            results = execute_db_fetchall(
                "SELECT id, site_id, url FROM crawl_queue WHERE status = 'pending' AND retry_count < ? ORDER BY priority ASC LIMIT 3",
                (MAX_RETRIES,)
            )
            
            if not results:
                time.sleep(5)
                continue
            
            # Lock batch
            for row_id, site_id, url in results:
                execute_db(
                    "UPDATE crawl_queue SET status = 'locked', locked_by = ? WHERE id = ?",
                    (worker_name, row_id),
                    commit=True
                )
            
            # Process each URL
            for row_id, site_id, url in results:
                if SHUTDOWN_FLAG:
                    break
                
                process_url(row_id, site_id, url)
                time.sleep(MIN_DELAY)
            
        except Exception as e:
            print(f"⚠️  Worker {worker_name} error: {e}")
            time.sleep(5)


def start_workers():
    """Start background worker threads"""
    global _worker_threads
    
    thread_a = threading.Thread(target=worker_a, daemon=True)
    thread_b = threading.Thread(target=worker_b, daemon=True)
    
    thread_a.start()
    thread_b.start()
    
    _worker_threads = [thread_a, thread_b]
    print("✅ Workers spuštěny (worker_a, worker_b)")


# ============================================================================
# SCHEDULER TASKS
# ============================================================================


def check_feeds():
    """Check RSS/Atom feeds for new entries"""
    if SHUTDOWN_FLAG:
        return
    
    feeds = execute_db_fetchall(
        "SELECT id, site_id, url, type FROM sitemaps_feeds WHERE type IN ('rss', 'atom')"
    )
    
    for feed_id, site_id, url, feed_type in feeds:
        try:
            new_urls = parse_feed(url)
            for new_url in new_urls:
                normalized = normalize_url(new_url)
                if normalized:
                    # Check if already in queue
                    existing = execute_db_fetchone(
                        "SELECT id FROM crawl_queue WHERE url = ?",
                        (normalized,)
                    )
                    if not existing:
                        execute_db(
                            "INSERT INTO crawl_queue (site_id, url, status, priority) VALUES (?, ?, 'pending', 1)",
                            (site_id, normalized),
                            commit=True
                        )
            
            # Update last_checked
            execute_db(
                "UPDATE sitemaps_feeds SET last_checked = strftime('%s', 'now') WHERE id = ?",
                (feed_id,),
                commit=True
            )
        except Exception as e:
            print(f"Error checking feed {url}: {e}")


def check_sitemaps():
    """Check sitemaps for new URLs"""
    if SHUTDOWN_FLAG:
        return
    
    sitemaps = execute_db_fetchall(
        "SELECT id, site_id, url FROM sitemaps_feeds WHERE type = 'sitemap'"
    )
    
    for sitemap_id, site_id, url in sitemaps:
        try:
            new_urls = parse_sitemap(url)
            for new_url in new_urls:
                normalized = normalize_url(new_url)
                if normalized:
                    existing = execute_db_fetchone(
                        "SELECT id FROM crawl_queue WHERE url = ?",
                        (normalized,)
                    )
                    if not existing:
                        execute_db(
                            "INSERT INTO crawl_queue (site_id, url, status, priority) VALUES (?, ?, 'pending', 3)",
                            (site_id, normalized),
                            commit=True
                        )
            
            execute_db(
                "UPDATE sitemaps_feeds SET last_checked = strftime('%s', 'now') WHERE id = ?",
                (sitemap_id,),
                commit=True
            )
        except Exception as e:
            print(f"Error checking sitemap {url}: {e}")


def recrawl_all_sites():
    """Re-run Phase 1 discovery for all active sites"""
    if SHUTDOWN_FLAG:
        return
    
    sites = execute_db_fetchall("SELECT id, canonical_url, max_pages FROM sites WHERE status = 'active'")
    
    for site_id, canonical_url, max_pages in sites:
        try:
            phase_1_discovery(canonical_url, max_pages)
        except Exception as e:
            print(f"Error re-crawling site {canonical_url}: {e}")


# ============================================================================
# SHUTDOWN HANDLING
# ============================================================================


def handle_shutdown(signum, frame):
    """Handle shutdown signals"""
    global SHUTDOWN_FLAG
    SHUTDOWN_FLAG = True
    print(f"\n⚠️  Signal {signum} received, shutting down...")
    close_db()
    if _scheduler:
        _scheduler.shutdown()
    sys.exit(0)


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


# ============================================================================
# FLASK APP
# ============================================================================


app = Flask(__name__)
app.secret_key = 'mini-search-secret-key'

# HTML Templates
SEARCH_HTML = """
<!DOCTYPE html>
<html lang="cs">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Mini Search - Vyhledávání</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
               background: #1a1a2e; color: #e0e0e0; line-height: 1.6; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { color: #0f3460; margin-bottom: 10px; font-size: 2em; }
        .card { background: #16213e; border-radius: 10px; padding: 20px; margin-bottom: 20px; }
        .search-box { display: flex; gap: 10px; margin-bottom: 20px; }
        .search-box input { flex: 1; padding: 15px; border-radius: 8px; border: 1px solid #333; 
                           background: #1a1a2e; color: #e0e0e0; font-size: 1.1em; }
        .search-box button { background: #e94560; color: white; border: none; padding: 15px 30px; 
                           border-radius: 8px; cursor: pointer; font-size: 1.1em; font-weight: bold; }
        .search-box button:hover { background: #c81e45; }
        .result-item { background: #16213e; border-radius: 8px; padding: 20px; margin-bottom: 15px;
                      border-left: 4px solid #e94560; display: flex; gap: 15px; }
        .result-item:hover { background: #1f2b4a; }
        .result-content { flex: 1; }
        .result-title { color: #e94560; font-size: 1.2em; margin-bottom: 5px; }
        .result-title a { color: #e94560; text-decoration: none; }
        .result-title a:hover { text-decoration: underline; }
        .result-url { color: #2196f3; font-size: 0.9em; margin-bottom: 8px; word-break: break-all; }
        .result-snippet { color: #aaa; line-height: 1.6; margin-bottom: 8px; }
        .result-meta { color: #666; font-size: 0.85em; display: flex; gap: 10px; flex-wrap: wrap; }
        .result-image { max-width: 120px; max-height: 80px; border-radius: 5px; }
        .schema-badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.75em; }
        .schema-Article { background: #2196f320; color: #2196f3; }
        .schema-BlogPosting { background: #4caf5020; color: #4caf50; }
        .schema-NewsArticle { background: #ff980020; color: #ff9800; }
        .schema-PodcastEpisode { background: #9c27b020; color: #9c27b0; }
        .no-results { text-align: center; color: #666; padding: 40px; }
        .stats { color: #666; font-size: 0.9em; margin-top: 10px; }
        .nav { margin-bottom: 20px; }
        .nav a { color: #2196f3; margin-right: 20px; text-decoration: none; }
        .nav a:hover { text-decoration: underline; }
        .filter-buttons { display: flex; gap: 10px; margin-bottom: 15px; flex-wrap: wrap; }
        .filter-btn { padding: 5px 12px; background: #333; border: none; border-radius: 5px; 
                     color: #e0e0e0; cursor: pointer; font-size: 0.9em; }
        .filter-btn.active { background: #e94560; }
        .relevance-badge { display: inline-block; padding: 2px 6px; background: #4caf5020; 
                           color: #4caf50; border-radius: 5px; font-size: 0.85em; font-weight: bold; }
        audio { max-width: 300px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="nav">
            <a href="/">🔍 Vyhledávání</a>
            <a href="/admin">🔧 Správa</a>
        </div>
        
        <h1>🔍 Mini Search</h1>
        
        <div class="card">
            <div class="filter-buttons">
                <button class="filter-btn active" onclick="setFilter('all')">Vše</button>
                <button class="filter-btn" onclick="setFilter('articles')">Články</button>
                <button class="filter-btn" onclick="setFilter('podcasts')">Podcasty</button>
                <button class="filter-btn" onclick="setFilter('audio')">Audio</button>
                <button class="filter-btn" onclick="setFilter('price')">Ceny</button>
            </div>
            
            <form id="searchForm" action="/" method="get">
                <div class="search-box">
                    <input type="text" name="q" id="searchInput" placeholder="Zadejte vyhledávaný text..." 
                           value="{{ query }}" autocomplete="off" required>
                    <input type="hidden" name="filter" id="filterInput" value="{{ current_filter }}">
                    <button type="submit">Vyhledat</button>
                </div>
            </form>
        </div>
        
        {% if query %}
        <div class="card">
            <h2>Výsledky pro: "{{ query }}"</h2>
            <div class="stats">Nalezeno: {{ total_results }} výsledků</div>
            
            {% if results %}
            <div id="resultsContainer">
                {% for result in results %}
                <div class="result-item">
                    {% if result.og_image or result.favicon_url %}
                    <img src="{{ result.og_image or result.favicon_url }}" class="result-image" 
                         onerror="this.style.display='none'" alt="">
                    {% endif %}
                    
                    <div class="result-content">
                        <div class="result-title">
                            {% if result.schema_type %}
                            <span class="schema-badge schema-{{ result.schema_type }}">{{ result.schema_type }}</span>
                            {% endif %}
                            <a href="{{ result.url }}" target="_blank">{{ result.og_title or result.title or result.url }}</a>
                        </div>
                        <div class="result-url">{{ result.url }}</div>
                        <div class="result-snippet">{{ (result.og_description or result.body_text)[:200] }}...</div>
                        <div class="result-meta">
                            {% if result.published_timestamp > 0 %}
                            <span>📅 {{ published_date }}</span>
                            {% endif %}
                            {% if result.has_audio == 1 %}
                            <span>🎵</span>
                            {% endif %}
                            {% if result.relevance %}
                            <span class="relevance-badge">{{ result.relevance }}% relevance</span>
                            {% endif %}
                        </div>
                        
                        {% if result.audio_url and result.has_audio == 1 %}
                        <div class="result-meta" style="margin-top: 10px;">
                            <audio controls>
                                <source src="{{ result.audio_url }}" type="audio/mpeg">
                            </audio>
                        </div>
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
            </div>
            {% else %}
            <div class="no-results">
                <p>🔍 Žádné výsledky nenalezeny</p>
                <p style="font-size: 0.9em; color: #666;">Zkuste jiné vyhledávací slovo nebo přidejte nové stránky přes správcovskou konzoli.</p>
            </div>
            {% endif %}
        </div>
        {% endif %}
    </div>
    
    <script>
        function setFilter(filter) {
            document.getElementById('filterInput').value = filter;
            document.querySelectorAll('.filter-btn').forEach(btn => {
                btn.classList.remove('active');
            });
            event.target.classList.add('active');
            document.getElementById('searchForm').submit();
        }
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
    <title>Mini Search - Správcovská konzole</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
               background: #1a1a2e; color: #e0e0e0; line-height: 1.6; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { color: #0f3460; margin-bottom: 10px; font-size: 1.8em; }
        .card { background: #16213e; border-radius: 10px; padding: 20px; margin-bottom: 20px; }
        .card h3 { color: #0f3460; margin-bottom: 15px; }
        .btn { background: #e94560; color: white; border: none; padding: 10px 20px; 
               border-radius: 5px; cursor: pointer; font-size: 1em; }
        .btn:hover { background: #c81e45; }
        .btn-success { background: #4caf50; }
        .btn-success:hover { background: #388e3c; }
        .btn-warning { background: #ff9800; }
        .btn-warning:hover { background: #e68a00; }
        .form-group { margin-bottom: 15px; }
        .form-group label { display: block; margin-bottom: 5px; color: #0f3460; }
        .form-group input { width: 100%; padding: 10px; border-radius: 5px; border: 1px solid #333; 
                           background: #1a1a2e; color: #e0e0e0; font-size: 1em; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #333; }
        th { background: #0f3460; color: white; }
        tr:hover { background: #1f2b4a; }
        .status-active { color: #4caf50; }
        .status-error { color: #e94560; }
        .status-pending { color: #ff9800; }
        .status-completed { color: #2196f3; }
        .status-locked { color: #9c27b0; }
        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; }
        .stat-card { background: #16213e; padding: 15px; border-radius: 8px; text-align: center; }
        .stat-value { font-size: 2em; font-weight: bold; color: #e94560; }
        .stat-label { color: #0f3460; font-size: 0.9em; margin-top: 5px; }
        .nav { margin-bottom: 20px; }
        .nav a { color: #2196f3; margin-right: 20px; text-decoration: none; }
        .nav a:hover { text-decoration: underline; }
        .progress-bar { background: #333; border-radius: 5px; height: 20px; overflow: hidden; }
        .progress-fill { background: #4caf50; height: 100%; border-radius: 5px; }
        .expandable { cursor: pointer; }
        .expandable-content { display: none; margin-top: 10px; padding: 10px; background: #1f2b4a; border-radius: 5px; }
        .expandable.active .expandable-content { display: block; }
    </style>
</head>
<body>
    <div class="container">
        <div class="nav">
            <a href="/">🔍 Vyhledávání</a>
            <a href="/admin">🔧 Správa</a>
        </div>
        
        <h1>🔧 Správcovská konzole</h1>
        
        <div class="card">
            <h3>📊 Přehled</h3>
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-value">{{ stats.sites }}</div>
                    <div class="stat-label">Webové stránky</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{{ stats.pages }}</div>
                    <div class="stat-label">Indexované stránky</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{{ stats.pending }}</div>
                    <div class="stat-label">Čekající URL</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{{ stats.completed }}</div>
                    <div class="stat-label">Dokončené URL</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">{{ stats.errors }}</div>
                    <div class="stat-label">Chyby</div>
                </div>
            </div>
        </div>
        
        <div class="card">
            <h3>➕ Přidat novou webovou stránku</h3>
            <form action="/admin/add" method="post">
                <div class="form-group">
                    <label>URL stránky:</label>
                    <input type="url" name="url" placeholder="https://priklad.cz" required>
                </div>
                <div class="form-group">
                    <label>Maximální počet stránek (default: 500):</label>
                    <input type="number" name="max_pages" placeholder="500" value="500" min="1" max="10000">
                </div>
                <button type="submit" class="btn btn-success">Přidat stránku</button>
            </form>
        </div>
        
        <div class="card">
            <h3>🌐 Seznam sledovaných stránek</h3>
            {% if sites %}
            <table>
                <thead>
                    <tr>
                        <th>ID</th>
                        <th>URL</th>
                        <th>Stav</th>
                        <th>Indexováno</th>
                        <th>Čeká</th>
                        <th>Poslední crawl</th>
                        <th>Akce</th>
                    </tr>
                </thead>
                <tbody>
                    {% for site in sites %}
                    <tr>
                        <td>{{ site.id }}</td>
                        <td>{{ site.canonical_url }}</td>
                        <td class="status-{{ site.status }}">{{ site.status }}</td>
                        <td>{{ site.indexed_count }}</td>
                        <td>{{ site.pending_count }}</td>
                        <td>{{ site.last_crawled_str }}</td>
                        <td>
                            <a href="/admin/recrawl/{{ site.id }}" class="btn btn-warning" style="padding: 5px 10px; font-size: 0.9em;">Recrawl</a>
                            <a href="/admin/delete/{{ site.id }}" class="btn" style="padding: 5px 10px; font-size: 0.9em; background: #666;" onclick="return confirm('Opravdu smazat?')">Smazat</a>
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            {% else %}
            <p style="color: #666;">Žádné stránky nepřidány. Přidejte první stránku výše.</p>
            {% endif %}
        </div>
    </div>
    
    <script>
        // Auto-refresh stats if there are pending items
        function checkPending() {
            fetch('/admin/stats')
                .then(response => response.json())
                .then(data => {
                    if (data.pending > 0) {
                        setTimeout(() => location.reload(), 5000);
                    }
                });
        }
        
        // Check every 30 seconds
        setInterval(checkPending, 30000);
        
        // Initial check
        checkPending();
    </script>
</body>
</html>
"""


# ============================================================================
# FLASK ROUTES
# ============================================================================


@app.route('/')
def search_index():
    """Main search page"""
    query = request.args.get('q', '').strip()
    filter_type = request.args.get('filter', 'all')
    
    if query:
        results = vector_search(query, limit=25, filter_type=filter_type)
        
        # Format dates
        for result in results:
            if result.get('published_timestamp') > 0:
                try:
                    result['published_date'] = datetime.fromtimestamp(result['published_timestamp']).strftime('%Y-%m-%d')
                except Exception:
                    result['published_date'] = ''
        
        return render_template_string(
            SEARCH_HTML,
            query=query,
            results=results,
            total_results=len(results),
            current_filter=filter_type
        )
    
    return render_template_string(
        SEARCH_HTML,
        query='',
        results=[],
        total_results=0,
        current_filter='all'
    )


@app.route('/admin')
def admin_index():
    """Admin dashboard"""
    stats = get_db_stats()
    sites = get_all_sites()
    
    return render_template_string(
        ADMIN_HTML,
        stats=stats,
        sites=sites
    )


@app.route('/admin/add', methods=['POST'])
def admin_add_site():
    """Add a new site"""
    url = request.form.get('url', '').strip()
    max_pages = int(request.form.get('max_pages', 500))
    
    if not url:
        return redirect('/admin?error=URL je povinná')
    
    site_id, urls_added = phase_1_discovery(url, max_pages)
    
    if site_id:
        return redirect(f'/admin?success=Stránka přidána! {urls_added} URL k prozkoumání')
    else:
        return redirect('/admin?error=Chyba při přidávání stránky')


@app.route('/admin/recrawl/<int:site_id>')
def admin_recrawl_site(site_id):
    """Recrawl a site"""
    site = get_site_by_id(site_id)
    if site:
        phase_1_discovery(site['canonical_url'], site['max_pages'])
    return redirect('/admin?success=Re-crawl zahájen')


@app.route('/admin/delete/<int:site_id>')
def admin_delete_site(site_id):
    """Delete a site"""
    delete_site(site_id)
    return redirect('/admin?success=Stránka smazána')


@app.route('/admin/stats')
def admin_stats():
    """API endpoint for statistics"""
    return jsonify(get_db_stats())


@app.route('/admin/errors/<int:site_id>')
def admin_errors(site_id):
    """Get failed URLs for a site"""
    errors = execute_db_fetchall(
        "SELECT url, error_reason FROM crawl_queue WHERE site_id = ? AND status = 'error'",
        (site_id,)
    )
    return jsonify([{'url': row[0], 'error': row[1]} for row in errors])


# ============================================================================
# MAIN
# ============================================================================


if __name__ == '__main__':
    print("=" * 70)
    print("Mini Search - Kombinovaná aplikace v6.0")
    print("=" * 70)
    print()
    
    # Initialize database
    init_db()
    print("✅ Databáze inicializována")
    
    # Initialize hnswlib index
    get_hnsw_index()
    print("✅ hnswlib index inicializován")
    
    # Load model
    get_model()
    print("✅ Model načten")
    
    # Start scheduler
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(check_feeds, IntervalTrigger(hours=1), id='check_feeds')
    _scheduler.add_job(check_sitemaps, IntervalTrigger(hours=24), id='check_sitemaps')
    _scheduler.add_job(recrawl_all_sites, IntervalTrigger(hours=12), id='recrawl_all')
    _scheduler.start()
    print("✅ Scheduler spuštěn")
    
    # Start workers
    start_workers()
    
    # Run Flask app
    print()
    print("Spouštím na http://0.0.0.0:8070")
    print("  / - Vyhledávání")
    print("  /admin - Správcovská konzole")
    print("Ctrl+C pro ukončení")
    print("=" * 70)
    print()
    
    app.run(host='0.0.0.0', port=8070, debug=False, threaded=True)
