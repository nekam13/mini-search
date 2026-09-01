#!/usr/bin/env python3
"""
Mini Search - Crawler Engine v3.0
Optimized for Ubuntu 26.04 ARM64 + Termux environment
Lightweight version with fallback vector search backends

Core crawling and indexing functionality with:
- Two-phase crawling (Discovery + Indexing)
- Multiple vector search backends (ChromaDB or SQLite+hnswlib)
- Sentence-transformers embeddings
- Ethical crawling with robots.txt respect
- Parallel workers for concurrent processing
"""

import sys
import os
import sqlite3
import requests
import hashlib
import time
import json
import threading
import signal
import atexit
import pickle
import numpy as np
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser
from bs4 import BeautifulSoup
import feedparser
import extruct

# Configuration
BOT_NAME = "MiniSearchBot/1.0"
BOT_URL = "http://localhost/bot"
HEADERS = {"User-Agent": f"{BOT_NAME} (+{BOT_URL})"}
MIN_DELAY = 1.0  # Minimum delay between requests in seconds
MAX_RETRIES = 3  # Maximum retry attempts
REQUEST_TIMEOUT = 30  # Request timeout in seconds
CHROMA_DB_PATH = "chroma_db"
VECTOR_DB_PATH = "vector_db"
MODEL_PATH = "models/paraphrase-multilingual-MiniLM-L12-v2"

# Global state
shutdown_flag = False
rate_limit_lock = threading.Lock()
last_request_time = 0.0

# Global references for cleanup
embedding_model = None
vector_db = None
VECTOR_BACKEND = "sqlite"  # Default to lightweight backend
CHROMA_AVAILABLE = False
HNSWLIB_AVAILABLE = False


class VectorSearchBackend:
    """Abstract base class for vector search backends"""
    
    def __init__(self):
        self.index = None
        self.metadata = {}
        self.ids = []
    
    def initialize(self):
        raise NotImplementedError
    
    def upsert(self, ids, embeddings, metadatas):
        raise NotImplementedError
    
    def query(self, query_embeddings, n_results=10, where=None):
        raise NotImplementedError
    
    def delete(self, ids):
        raise NotImplementedError
    
    def persist(self):
        raise NotImplementedError
    
    def get_by_id(self, id):
        raise NotImplementedError


class SQLiteHNSWBackend(VectorSearchBackend):
    """Lightweight vector search using SQLite + hnswlib"""
    
    def __init__(self, db_path="vector_db"):
        super().__init__()
        self.db_path = db_path
        self.index = None
        self.dim = 384  # Dimension for paraphrase-multilingual-MiniLM-L12-v2
        self.conn = None
        self._initialize_db()
        self._load_index()
    
    def _initialize_db(self):
        """Initialize SQLite database for metadata"""
        os.makedirs(self.db_path, exist_ok=True)
        self.conn = sqlite3.connect(os.path.join(self.db_path, "vectors.db"))
        cursor = self.conn.cursor()
        
        # Create tables
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vectors (
                id TEXT PRIMARY KEY,
                embedding BLOB NOT NULL,
                metadata TEXT NOT NULL
            )
        ''')
        
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_vectors_id ON vectors(id)')
        
        self.conn.commit()
    
    def _load_index(self):
        """Load or create hnswlib index"""
        try:
            import hnswlib
            index_path = os.path.join(self.db_path, "index.bin")
            
            if os.path.exists(index_path):
                # Load existing index
                self.index = hnswlib.Index(space='cosine', dim=self.dim)
                self.index.load_index(index_path, max_elements=100000)
                print("✅ hnswlib index načten")
            else:
                # Create new index
                self.index = hnswlib.Index(space='cosine', dim=self.dim)
                self.index.init_index(max_elements=100000, ef_construction=200, M=16)
                print("✅ Nový hnswlib index vytvořen")
            
            self._load_existing_vectors()
            HNSWLIB_AVAILABLE = True
            
        except ImportError:
            print("⚠️  hnswlib není dostupný, používám pouze SQLite")
            HNSWLIB_AVAILABLE = False
            self._load_existing_vectors()
    
    def _load_existing_vectors(self):
        """Load existing vectors from SQLite"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT id, embedding, metadata FROM vectors")
        
        for row in cursor.fetchall():
            id, embedding_blob, metadata_str = row
            self.ids.append(id)
            self.metadata[id] = json.loads(metadata_str)
            
            # Store embedding for similarity search
            embedding = pickle.loads(embedding_blob)
            if self.index is not None:
                try:
                    self.index.add_items(embedding, [len(self.ids) - 1])
                except:
                    pass  # Index not ready yet
        
        print(f"✅ Načteno {len(self.ids)} existujících vektorů")
    
    def initialize(self):
        """Initialize the backend"""
        print("✅ SQLite+hnswlib backend inicializován")
        return True
    
    def upsert(self, ids, embeddings, metadatas):
        """Upsert vectors into the index"""
        cursor = self.conn.cursor()
        
        for i, (id, embedding, metadata) in enumerate(zip(ids, embeddings, metadatas)):
            # Store in SQLite
            embedding_blob = pickle.dumps(embedding)
            metadata_str = json.dumps(metadata)
            
            cursor.execute(
                "INSERT OR REPLACE INTO vectors (id, embedding, metadata) VALUES (?, ?, ?)",
                (id, embedding_blob, metadata_str)
            )
            
            # Update in-memory structures
            if id not in self.ids:
                self.ids.append(id)
            self.metadata[id] = metadata
            
            # Add to hnswlib index if available
            if self.index is not None and HNSWLIB_AVAILABLE:
                try:
                    idx = self.ids.index(id) if id in self.ids else len(self.ids) - 1
                    self.index.add_items(np.array([embedding]), [idx])
                except Exception as e:
                    print(f"⚠️  Chyba při přidávání do hnswlib indexu: {e}")
        
        self.conn.commit()
        self.persist()
    
    def query(self, query_embeddings, n_results=10, where=None):
        """Query the index"""
        results = {
            'ids': [[]],
            'distances': [[]],
            'metadatas': [[]]
        }
        
        if not self.ids:
            return results
        
        # Convert query to numpy array
        query_array = np.array(query_embeddings)
        
        if self.index is not None and HNSWLIB_AVAILABLE and len(self.ids) > 0:
            # Use hnswlib for fast similarity search
            labels, distances = self.index.knn_query(query_array, k=min(n_results, len(self.ids)))
            
            for i, (label, distance) in enumerate(zip(labels[0], distances[0])):
                if label < len(self.ids):
                    id = self.ids[label]
                    results['ids'][0].append(id)
                    results['distances'][0].append(float(distance))
                    results['metadatas'][0].append(self.metadata.get(id, {}))
        else:
            # Fallback to brute-force search with SQLite
            cursor = self.conn.cursor()
            cursor.execute("SELECT id, embedding, metadata FROM vectors")
            
            all_data = cursor.fetchall()
            
            # Calculate cosine similarity for each vector
            similarities = []
            for row in all_data:
                id, embedding_blob, metadata_str = row
                try:
                    embedding = pickle.loads(embedding_blob)
                    
                    # Ensure embedding is numpy array
                    if not isinstance(embedding, np.ndarray):
                        embedding = np.array(embedding)
                    
                    # Ensure query is numpy array
                    query_arr = np.array(query_array[0]) if not isinstance(query_array[0], np.ndarray) else query_array[0]
                    
                    # Cosine similarity
                    dot_product = np.dot(query_arr, embedding)
                    norm_a = np.linalg.norm(query_arr)
                    norm_b = np.linalg.norm(embedding)
                    similarity = dot_product / (norm_a * norm_b) if norm_a > 0 and norm_b > 0 else 0
                    
                    # Convert similarity to distance (1 - similarity)
                    distance = 1.0 - similarity
                    
                    similarities.append((id, distance, json.loads(metadata_str) if isinstance(metadata_str, str) else metadata_str))
                except Exception as e:
                    print(f"⚠️  Chyba při zpracování vektoru {id}: {e}")
                    continue
            
            # Sort by distance (ascending)
            similarities.sort(key=lambda x: x[1])
            
            # Get top n_results
            for id, distance, metadata in similarities[:n_results]:
                results['ids'][0].append(id)
                results['distances'][0].append(distance)
                results['metadatas'][0].append(metadata)
        
        return results
    
    def delete(self, ids):
        """Delete vectors from the index"""
        cursor = self.conn.cursor()
        
        for id in ids:
            if id in self.ids:
                self.ids.remove(id)
            if id in self.metadata:
                del self.metadata[id]
            
            cursor.execute("DELETE FROM vectors WHERE id = ?", (id,))
        
        self.conn.commit()
        
        # Rebuild hnswlib index
        if self.index is not None and HNSWLIB_AVAILABLE:
            self.index = None
            self._load_index()
    
    def persist(self):
        """Persist the index to disk"""
        if self.index is not None and HNSWLIB_AVAILABLE:
            try:
                index_path = os.path.join(self.db_path, "index.bin")
                self.index.save_index(index_path)
            except Exception as e:
                print(f"⚠️  Chyba při ukládání indexu: {e}")
        
        if self.conn:
            self.conn.commit()
    
    def get_by_id(self, id):
        """Get vector by ID"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT embedding, metadata FROM vectors WHERE id = ?", (id,))
        row = cursor.fetchone()
        
        if row:
            embedding = pickle.loads(row[0])
            metadata = json.loads(row[1])
            return {'embedding': embedding, 'metadata': metadata}
        return None


class ChromaDBBackend(VectorSearchBackend):
    """ChromaDB vector search backend"""
    
    def __init__(self, db_path="chroma_db"):
        super().__init__()
        self.db_path = db_path
        self.client = None
        self.collection = None
    
    def initialize(self):
        """Initialize ChromaDB backend"""
        try:
            import chromadb
            from chromadb.config import Settings
            
            os.makedirs(self.db_path, exist_ok=True)
            
            self.client = chromadb.Client(
                Settings(
                    chroma_db_impl="duckdb+parquet",
                    persist_directory=self.db_path
                )
            )
            
            self.collection = self.client.get_or_create_collection(name="pages")
            CHROMA_AVAILABLE = True
            print("✅ ChromaDB backend inicializován")
            return True
            
        except Exception as e:
            print(f"⚠️  ChromaDB se nepodařilo inicializovat: {e}")
            print("💡  Budu používat SQLite+hnswlib backend")
            CHROMA_AVAILABLE = False
            return False
    
    def upsert(self, ids, embeddings, metadatas):
        """Upsert vectors into ChromaDB"""
        if self.collection:
            self.collection.upsert(
                ids=ids,
                documents=["" for _ in ids],  # Dummy documents
                embeddings=embeddings,
                metadatas=metadatas
            )
            self.client.persist()
    
    def query(self, query_embeddings, n_results=10, where=None):
        """Query ChromaDB"""
        if self.collection:
            return self.collection.query(
                query_embeddings=query_embeddings,
                n_results=n_results,
                where=where,
                include=["metadatas", "distances"]
            )
        return {'ids': [[]], 'distances': [[]], 'metadatas': [[]]}
    
    def delete(self, ids):
        """Delete vectors from ChromaDB"""
        if self.collection:
            self.collection.delete(ids=ids)
            self.client.persist()
    
    def persist(self):
        """Persist ChromaDB to disk"""
        if self.client:
            self.client.persist()
    
    def get_by_id(self, id):
        """Get vector by ID from ChromaDB"""
        if self.collection:
            result = self.collection.get(ids=[id], include=["metadatas", "embeddings"])
            if result.get('ids') and id in result['ids'][0]:
                idx = result['ids'][0].index(id)
                return {
                    'embedding': result['embeddings'][0][idx] if result.get('embeddings') else None,
                    'metadata': result['metadatas'][0][idx] if result.get('metadatas') else {}
                }
        return None


def init_vector_backend():
    """Initialize the appropriate vector search backend"""
    global vector_db, VECTOR_BACKEND, embedding_model
    
    # Try to load embedding model first
    try:
        from sentence_transformers import SentenceTransformer
        os.makedirs("models", exist_ok=True)
        embedding_model = SentenceTransformer(MODEL_PATH, cache_folder="models")
        print("✅ Embedding model načten")
    except Exception as e:
        print(f"⚠️  Embedding model se nepodařilo načíst: {e}")
        print("💡  Nainstalujte: pip install torch --index-url https://download.pytorch.org/whl/cpu")
        print("   Pak: pip install sentence-transformers==2.2.2")
        return False
    
    # Try ChromaDB first
    chroma_backend = ChromaDBBackend(CHROMA_DB_PATH)
    if chroma_backend.initialize():
        vector_db = chroma_backend
        VECTOR_BACKEND = "chromadb"
        print("✅ Používám ChromaDB backend")
        return True
    
    # Fallback to SQLite+hnswlib
    print("⚠️  ChromaDB není dostupný, používám SQLite+hnswlib backend")
    sqlite_backend = SQLiteHNSWBackend(VECTOR_DB_PATH)
    sqlite_backend.initialize()
    vector_db = sqlite_backend
    VECTOR_BACKEND = "sqlite"
    print("✅ Používám SQLite+hnswlib backend")
    return True


def cleanup():
    """Cleanup resources on shutdown"""
    global vector_db, shutdown_flag
    shutdown_flag = True
    
    print("\n🔄 Ukončování crawler engine...")
    
    if vector_db:
        try:
            vector_db.persist()
            print("✅ Vector database uložena")
        except Exception as e:
            print(f"⚠️  Chyba při ukládání vector database: {e}")
    
    print("✅ Crawler engine ukončen")


# Register cleanup on exit
atexit.register(cleanup)


def handle_shutdown(signum, frame):
    """Handle shutdown signals"""
    cleanup()
    sys.exit(0)


# Register signal handlers
signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


def get_db():
    """Get SQLite database connection with WAL mode for better concurrency"""
    conn = sqlite3.connect("console.db", timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize database tables"""
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
    
    # Create indexes for performance
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sites_last_crawled ON sites(last_crawled)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_sites_created_at ON sites(created_at)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_queue_site_id ON crawl_queue(site_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_queue_status ON crawl_queue(status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_queue_created_at ON crawl_queue(created_at)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_feeds_site_id ON sitemaps_feeds(site_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_feeds_type ON sitemaps_feeds(type)')
    
    conn.commit()
    conn.close()


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
    
    # Normalize scheme and netloc
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    
    # Remove www. prefix
    if netloc.startswith("www."):
        netloc = netloc[4:]
    
    # Normalize path
    path = parsed.path
    if path and path != '/':
        # Remove trailing slash
        path = path.rstrip('/')
    
    # Reconstruct URL
    normalized = urlunparse((scheme, netloc, path, parsed.params, parsed.query, ''))
    
    return normalized


def generate_doc_id(url):
    """Generate MD5 hash for document ID"""
    return hashlib.md5(url.encode('utf-8')).hexdigest()


def respect_rate_limit():
    """Enforce minimum delay between requests"""
    global last_request_time
    if shutdown_flag:
        return
    
    with rate_limit_lock:
        elapsed = time.time() - last_request_time
        if elapsed < MIN_DELAY:
            time.sleep(MIN_DELAY - elapsed)
        last_request_time = time.time()


def get_robots_parser(base_url):
    """Get and parse robots.txt for a domain"""
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rfp = RobotFileParser()
    rfp.set_url(robots_url)
    
    try:
        respect_rate_limit()
        rfp.read()
    except Exception as e:
        print(f"⚠️  Nelze načíst robots.txt pro {base_url}: {e}")
    
    return rfp


def extract_date(soup, feed_date=None):
    """Extract published date from page or feed"""
    if feed_date:
        try:
            if isinstance(feed_date, tuple):
                return int(time.mktime(feed_date))
            return int(time.mktime(feed_date.timetuple()))
        except Exception:
            pass
    
    # Try various meta tags
    meta_tags = [
        "article:published_time", "article:published",
        "og:published_time", "og:updated_time",
        "date", "dc.date", "dcterms.date",
        "published", "pubdate", "timestamp"
    ]
    
    for meta_name in meta_tags:
        meta = soup.find("meta", property=meta_name) or soup.find("meta", attrs={"name": meta_name})
        if meta and meta.get("content"):
            try:
                content = meta["content"].strip()
                # Try ISO format
                try:
                    dt = datetime.fromisoformat(content.replace("Z", "+00:00"))
                    return int(dt.timestamp())
                except:
                    pass
                # Try timestamp
                try:
                    return int(float(content))
                except:
                    pass
            except Exception:
                pass
    
    return int(time.time())


def extract_audio(soup, html, url):
    """Extract audio URL from page"""
    # Check <audio> tags
    for audio in soup.find_all("audio"):
        if audio.get("src"):
            return urljoin(url, audio["src"])
        for source in audio.find_all("source"):
            if source.get("src"):
                return urljoin(url, source["src"])
    
    # Check links to audio files
    audio_extensions = [".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac"]
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        if any(href.endswith(ext) for ext in audio_extensions):
            return urljoin(url, a["href"])
    
    return ""


def extract_image(soup, url):
    """Extract image URL from page"""
    # Check OpenGraph image
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        return urljoin(url, og_image["content"])
    
    # Check Twitter card image
    twitter_image = soup.find("meta", attrs={"name": "twitter:image"})
    if twitter_image and twitter_image.get("content"):
        return urljoin(url, twitter_image["content"])
    
    # Fall back to first non-logo image
    for img in soup.find_all("img", src=True):
        img_src = img["src"].lower()
        if not any(x in img_src for x in ["logo", "icon", "svg", "1x1", "avatar", "banner", "pixel"]):
            return urljoin(url, img["src"])
    
    return ""


def extract_metadata(soup, html, url):
    """Extract Schema.org metadata using extruct"""
    meta_info = {
        "og": {},
        "schema_types": [],
        "schema_details": {}
    }
    
    # Extract OpenGraph metadata
    for meta in soup.find_all("meta"):
        prop = meta.get("property", "") or meta.get("name", "")
        if prop.startswith("og:"):
            meta_info["og"][prop[3:]] = meta.get("content", "").strip()
        elif prop.startswith("twitter:"):
            meta_info["og"][prop[8:]] = meta.get("content", "").strip()
    
    # Extract Schema.org using extruct
    try:
        extracted = extruct.extract(html, base_url=url, syntaxes=['json-ld', 'microdata', 'rdfa'])
        
        for syntax in ['json-ld', 'microdata', 'rdfa']:
            items = extracted.get(syntax, [])
            if not items:
                continue
            
            for item in items:
                if not isinstance(item, dict):
                    continue
                
                stype = item.get("@type", "WebPage")
                if isinstance(stype, list):
                    stype = stype[0] if stype else "WebPage"
                
                if stype not in meta_info["schema_types"]:
                    meta_info["schema_types"].append(stype)
                
                # Handle important Schema.org types
                if stype in ["PodcastEpisode", "AudioObject", "Article", "BlogPosting", "NewsArticle"]:
                    meta_info["schema_type_override"] = stype
                
                # Extract rating
                if "aggregateRating" in item:
                    rating = item["aggregateRating"]
                    if isinstance(rating, dict):
                        meta_info["schema_details"]["rating"] = rating.get("ratingValue")
                        meta_info["schema_details"]["reviewCount"] = rating.get("reviewCount")
                
                # Extract price
                if "offers" in item:
                    offers = item["offers"]
                    if isinstance(offers, dict):
                        meta_info["schema_details"]["price"] = offers.get("price")
                        meta_info["schema_details"]["currency"] = offers.get("priceCurrency", "CZK")
                    elif isinstance(offers, list) and offers:
                        first_offer = offers[0]
                        if isinstance(first_offer, dict):
                            meta_info["schema_details"]["price"] = first_offer.get("price")
                            meta_info["schema_details"]["currency"] = first_offer.get("priceCurrency", "CZK")
                
                # Extract author
                if "author" in item:
                    author = item["author"]
                    if isinstance(author, dict):
                        meta_info["schema_details"]["author"] = author.get("name", "")
                    elif isinstance(author, list) and author:
                        if isinstance(author[0], dict):
                            meta_info["schema_details"]["author"] = author[0].get("name", "")
                        else:
                            meta_info["schema_details"]["author"] = ", ".join(str(a) for a in author)
                    else:
                        meta_info["schema_details"]["author"] = str(author)
                
                # Extract title from Schema.org
                if "headline" in item and not meta_info["og"].get("title"):
                    meta_info["og"]["title"] = item.get("headline", "")
                
                # Extract description
                if "description" in item and not meta_info["og"].get("description"):
                    meta_info["og"]["description"] = item.get("description", "")
    
    except Exception as e:
        print(f"⚠️  Chyba při extrakci Schema.org: {e}")
    
    return meta_info


def add_site(site_url, max_pages=500):
    """Add a new site with domain deduplication"""
    site_url = normalize_domain(site_url)
    if not site_url:
        return None
    
    parsed = urlparse(site_url)
    canonical_domain = parsed.netloc
    
    conn = get_db()
    cursor = conn.cursor()
    
    # Check if domain already exists
    cursor.execute(
        "SELECT id, canonical_url, aliases FROM sites WHERE canonical_url = ? OR aliases LIKE ?",
        (canonical_domain, f"%{canonical_domain}%")
    )
    existing = cursor.fetchone()
    
    if existing:
        site_id, existing_canonical, aliases_str = existing
        aliases = json.loads(aliases_str) if aliases_str else []
        
        # Add new URL as alias if not already present
        if site_url not in aliases and site_url != existing_canonical:
            aliases.append(site_url)
            cursor.execute(
                "UPDATE sites SET aliases = ? WHERE id = ?",
                (json.dumps(aliases), site_id)
            )
            conn.commit()
        
        conn.close()
        return site_id
    
    # Insert new site - store the domain (netloc) as canonical_url for consistency
    cursor.execute(
        "INSERT INTO sites (canonical_url, aliases, status, max_pages) VALUES (?, ?, 'active', ?)",
        (canonical_domain, json.dumps([site_url]), max_pages)
    )
    site_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return site_id


def discover_sitemaps_and_feeds(site_url, site_id):
    """Phase 1: Discovery - Find sitemaps and feeds without downloading content"""
    parsed = urlparse(site_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    
    rfp = get_robots_parser(site_url)
    
    # Try to get sitemap URLs from robots.txt
    sitemap_urls = []
    try:
        sitemap_urls = rfp.sitemaps()
    except:
        pass
    
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
    
    discovered_urls = []
    discovered_feeds = []
    
    # Check sitemaps from robots.txt
    for sitemap_url in sitemap_urls:
        if not sitemap_url:
            continue
        full_url = urljoin(base_url, sitemap_url)
        try:
            respect_rate_limit()
            response = requests.head(full_url, headers=HEADERS, timeout=10, allow_redirects=True)
            if response.status_code == 200:
                discovered_urls.append((full_url, 'from_robots'))
        except Exception as e:
            print(f"⚠️  Chyba při kontrole {full_url}: {e}")
    
    # Check common URLs
    for path in common_urls:
        full_url = urljoin(base_url, path)
        try:
            respect_rate_limit()
            response = requests.head(full_url, headers=HEADERS, timeout=10, allow_redirects=True)
            if response.status_code == 200:
                discovered_urls.append((full_url, 'common'))
        except Exception as e:
            print(f"⚠️  Chyba při kontrole {full_url}: {e}")
    
    # Process discovered URLs to identify type and extract links
    conn = get_db()
    cursor = conn.cursor()
    
    for url, source in discovered_urls:
        if shutdown_flag:
            break
            
        try:
            respect_rate_limit()
            response = requests.get(url, headers=HEADERS, timeout=15)
            content_type = response.headers.get('Content-Type', '').lower()
            
            # Determine type
            url_lower = url.lower()
            if any(ext in url_lower for ext in ['.xml', '.rss', '.atom']):
                if 'sitemap' in url_lower or 'sitemap' in content_type:
                    feed_type = 'sitemap'
                elif 'rss' in url_lower or 'application/rss+xml' in content_type:
                    feed_type = 'rss'
                elif 'atom' in url_lower or 'application/atom+xml' in content_type:
                    feed_type = 'atom'
                else:
                    # Try to parse as feed
                    try:
                        feed = feedparser.parse(response.text)
                        if feed.entries:
                            feed_type = 'rss' if 'rss' in url_lower else 'atom'
                        else:
                            feed_type = 'sitemap'
                    except:
                        feed_type = 'sitemap'
            else:
                continue
            
            # Save to sitemaps_feeds table
            cursor.execute(
                "INSERT OR IGNORE INTO sitemaps_feeds (site_id, url, type, last_checked) VALUES (?, ?, ?, ?)",
                (site_id, url, feed_type, int(time.time()))
            )
            
            # Extract URLs from sitemap or feed
            if feed_type in ['rss', 'atom']:
                try:
                    feed_data = feedparser.parse(response.text)
                    for entry in feed_data.entries:
                        if hasattr(entry, 'link'):
                            discovered_feeds.append({
                                'url': normalize_url(entry.link),
                                'date': entry.get('published_parsed', None)
                            })
                except Exception as e:
                    print(f"⚠️  Chyba při zpracování feedu {url}: {e}")
            else:  # sitemap
                try:
                    soup = BeautifulSoup(response.text, 'xml')
                    urls = [url.text for url in soup.find_all('loc') if url.text]
                    for sitemap_url in urls:
                        discovered_feeds.append({
                            'url': normalize_url(sitemap_url),
                            'date': None
                        })
                except Exception as e:
                    print(f"⚠️  Chyba při zpracování sitemapy {url}: {e}")
                    
        except Exception as e:
            print(f"⚠️  Chyba při zpracování {url}: {e}")
            continue
    
    conn.commit()
    conn.close()
    
    return discovered_feeds


def crawl_homepage_for_links(site_url, site_id, max_pages):
    """Fallback: crawl homepage to discover links"""
    try:
        respect_rate_limit()
        response = requests.get(site_url, headers=HEADERS, timeout=15)
        
        if response.status_code != 200:
            return []
        
        content_type = response.headers.get('Content-Type', '').lower()
        if 'text/html' not in content_type:
            return []
        
        soup = BeautifulSoup(response.text, 'html.parser')
        discovered_urls = []
        
        for a in soup.find_all('a', href=True):
            link = urljoin(site_url, a['href'])
            normalized = normalize_url(link)
            parsed_link = urlparse(link)
            parsed_site = urlparse(site_url)
            
            # Only follow links from the same domain
            if parsed_link.netloc == parsed_site.netloc:
                if normalized not in discovered_urls:
                    discovered_urls.append(normalized)
        
        return discovered_urls[:max_pages]
        
    except Exception as e:
        print(f"⚠️  Chyba při crawlování domovské stránky {site_url}: {e}")
        return []


def phase_1_discovery(site_url, max_pages=500):
    """Phase 1: Discovery - Build full URL queue"""
    site_url = normalize_domain(site_url)
    if not site_url:
        return None, 0
    
    site_id = add_site(site_url, max_pages)
    if not site_id:
        return None, 0
    
    conn = get_db()
    cursor = conn.cursor()
    
    # Clear existing queue for this site (for re-crawl)
    cursor.execute("DELETE FROM crawl_queue WHERE site_id = ?", (site_id,))
    
    # Get existing URLs for this site
    cursor.execute("SELECT url FROM crawl_queue WHERE site_id = ?", (site_id,))
    existing_urls = {row[0] for row in cursor.fetchall()}
    
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
            cursor.execute(
                "INSERT OR IGNORE INTO crawl_queue (site_id, url, status) VALUES (?, ?, 'pending')",
                (site_id, url)
            )
        except sqlite3.IntegrityError:
            pass
    
    conn.commit()
    conn.close()
    
    # Update site last_crawled time
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE sites SET last_crawled = ? WHERE id = ?", (int(time.time()), site_id))
    conn.commit()
    conn.close()
    
    return site_id, len(urls_to_add)


def process_url(url, site_id, max_pages):
    """Process a single URL: extract content, generate embeddings, store in vector DB"""
    if shutdown_flag:
        return None, "Shutdown in progress"
    
    try:
        # Check if URL is valid
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return None, "Invalid URL"
        
        # Check robots.txt
        rfp = get_robots_parser(url)
        if not rfp.can_fetch(BOT_NAME, url):
            crawl_delay = rfp.crawl_delay(BOT_NAME)
            if crawl_delay:
                time.sleep(max(crawl_delay, MIN_DELAY))
            else:
                return None, "Blocked by robots.txt"
        
        respect_rate_limit()
        
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        
        # Handle rate limiting (HTTP 429)
        if response.status_code == 429:
            retry_after = int(response.headers.get('Retry-After', 5))
            time.sleep(retry_after)
            return None, f"Rate limited, retry after {retry_after}s"
        
        # Handle forbidden (HTTP 403)
        if response.status_code == 403:
            # Mark entire domain as blocked
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("UPDATE sites SET status = 'blocked' WHERE id = ?", (site_id,))
            conn.commit()
            conn.close()
            return None, "HTTP 403 Forbidden - Domain blocked"
        
        if response.status_code != 200:
            return None, f"HTTP {response.status_code}"
        
        content_type = response.headers.get('Content-Type', '').lower()
        if 'text/html' not in content_type:
            return None, f"Non-HTML content: {content_type}"
        
        response.encoding = response.apparent_encoding or 'utf-8'
        html = response.text
        soup = BeautifulSoup(html, 'html.parser')
        
        # Extract metadata
        meta_info = extract_metadata(soup, html, url)
        pub_timestamp = extract_date(soup)
        audio_url = extract_audio(soup, html, url)
        image_url = extract_image(soup, url)
        
        # Clean HTML
        for el in soup(['script', 'style', 'nav', 'footer', 'iframe', 'noscript', 'head']):
            el.decompose()
        
        # Extract title
        title = meta_info["og"].get("title", "")
        if not title and soup.title:
            title = soup.title.string.strip() if soup.title.string else url
        
        # Extract body text
        body_parts = []
        for tag in ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'article', 'section', 'main']:
            for el in soup.find_all(tag):
                text = el.get_text().strip()
                if text and len(text) > 10:
                    body_parts.append(text)
        body_text = ' '.join(body_parts)[:3500]
        
        # Determine schema type
        schema_type = meta_info.get("schema_type_override") or (meta_info["schema_types"][0] if meta_info["schema_types"] else "WebPage")
        if audio_url and schema_type == "WebPage":
            schema_type = "PodcastEpisode"
        
        # Generate document ID
        doc_id = generate_doc_id(url)
        
        # Generate embedding
        if embedding_model:
            try:
                embedding = embedding_model.encode(f"{title} {body_text}").tolist()[0]
            except Exception as e:
                print(f"⚠️  Chyba při generování embeddingu: {e}")
                embedding = None
        else:
            embedding = None
        
        # Prepare document for vector DB
        if vector_db and embedding:
            # Store in vector database with metadata
            metadata = {
                "url": url,
                "title": title,
                "text": body_text,
                "image": image_url,
                "audio_url": audio_url,
                "has_audio": str(bool(audio_url)).lower(),
                "schema_type": schema_type,
                "schema_details": json.dumps(meta_info["schema_details"]),
                "published_timestamp": pub_timestamp,
                "published_date": datetime.fromtimestamp(pub_timestamp).strftime('%d.%m.%Y %H:%M'),
                "site_id": str(site_id)
            }
            
            try:
                vector_db.upsert(
                    ids=[doc_id],
                    embeddings=[embedding],
                    metadatas=[metadata]
                )
            except Exception as e:
                print(f"⚠️  Chyba při ukládání do vector DB: {e}")
        
        return {
            "id": doc_id,
            "url": url,
            "title": title,
            "text": body_text,
            "image": image_url,
            "audio_url": audio_url,
            "has_audio": bool(audio_url),
            "schema_type": schema_type,
            "schema_details": meta_info["schema_details"],
            "published_timestamp": pub_timestamp,
            "published_date": datetime.fromtimestamp(pub_timestamp).strftime('%d.%m.%Y %H:%M'),
            "site_id": site_id
        }, None
        
    except requests.exceptions.Timeout:
        return None, "Request timeout"
    except requests.exceptions.ConnectionError:
        return None, "Connection error"
    except requests.exceptions.RequestException as e:
        return None, f"Request error: {str(e)}"
    except Exception as e:
        return None, f"Error: {str(e)}"


def worker_a():
    """Worker A: Process pending URLs from crawl queue"""
    worker_name = "worker_a"
    print(f"✅ Worker {worker_name} spuštěn")
    
    while not shutdown_flag:
        try:
            conn = get_db()
            cursor = conn.cursor()
            
            # Lock a batch of pending URLs
            cursor.execute(
                "SELECT id, site_id, url FROM crawl_queue WHERE status = 'pending' AND retry_count < ? LIMIT 5 FOR UPDATE SKIP LOCKED",
                (MAX_RETRIES,)
            )
            batch = cursor.fetchall()
            
            if not batch:
                conn.close()
                time.sleep(5)
                continue
            
            # Mark as locked
            for row_id, site_id, url in batch:
                cursor.execute(
                    "UPDATE crawl_queue SET status = 'locked', locked_by = ? WHERE id = ?",
                    (worker_name, row_id)
                )
            
            conn.commit()
            
            # Get site info
            site_info = {}
            for row_id, site_id, url in batch:
                cursor.execute("SELECT max_pages FROM sites WHERE id = ?", (site_id,))
                result = cursor.fetchone()
                if result:
                    site_info[site_id] = result[0]
            
            # Process each URL
            for row_id, site_id, url in batch:
                if shutdown_flag:
                    break
                    
                max_pages = site_info.get(site_id, 500)
                result, error = process_url(url, site_id, max_pages)
                
                if result:
                    cursor.execute(
                        "UPDATE crawl_queue SET status = 'done' WHERE id = ?",
                        (row_id,)
                    )
                else:
                    # Get current retry count
                    cursor.execute("SELECT retry_count FROM crawl_queue WHERE id = ?", (row_id,))
                    retry_row = cursor.fetchone()
                    retry_count = retry_row[0] if retry_row else 0
                    
                    if retry_count >= MAX_RETRIES - 1:
                        cursor.execute(
                            "UPDATE crawl_queue SET status = 'error', error_reason = ? WHERE id = ?",
                            (error, row_id)
                        )
                        # Mark domain as error if too many errors
                        cursor.execute(
                            "UPDATE sites SET error_count = error_count + 1 WHERE id = ?",
                            (site_id,)
                        )
                    else:
                        cursor.execute(
                            "UPDATE crawl_queue SET status = 'pending', retry_count = retry_count + 1, error_reason = ? WHERE id = ?",
                            (error, row_id)
                        )
                
                conn.commit()
            
            conn.close()
            
        except sqlite3.OperationalError as e:
            print(f"⚠️  Worker {worker_name} - SQLite chyba: {e}")
            time.sleep(5)
        except Exception as e:
            print(f"⚠️  Worker {worker_name} chyba: {e}")
            time.sleep(10)
    
    print(f"✅ Worker {worker_name} ukončen")


def worker_b():
    """Worker B: Process pending URLs from crawl queue"""
    worker_name = "worker_b"
    print(f"✅ Worker {worker_name} spuštěn")
    
    while not shutdown_flag:
        try:
            conn = get_db()
            cursor = conn.cursor()
            
            # Lock a batch of pending URLs
            cursor.execute(
                "SELECT id, site_id, url FROM crawl_queue WHERE status = 'pending' AND retry_count < ? LIMIT 5 FOR UPDATE SKIP LOCKED",
                (MAX_RETRIES,)
            )
            batch = cursor.fetchall()
            
            if not batch:
                conn.close()
                time.sleep(5)
                continue
            
            # Mark as locked
            for row_id, site_id, url in batch:
                cursor.execute(
                    "UPDATE crawl_queue SET status = 'locked', locked_by = ? WHERE id = ?",
                    (worker_name, row_id)
                )
            
            conn.commit()
            
            # Get site info
            site_info = {}
            for row_id, site_id, url in batch:
                cursor.execute("SELECT max_pages FROM sites WHERE id = ?", (site_id,))
                result = cursor.fetchone()
                if result:
                    site_info[site_id] = result[0]
            
            # Process each URL
            for row_id, site_id, url in batch:
                if shutdown_flag:
                    break
                    
                max_pages = site_info.get(site_id, 500)
                result, error = process_url(url, site_id, max_pages)
                
                if result:
                    cursor.execute(
                        "UPDATE crawl_queue SET status = 'done' WHERE id = ?",
                        (row_id,)
                    )
                else:
                    # Get current retry count
                    cursor.execute("SELECT retry_count FROM crawl_queue WHERE id = ?", (row_id,))
                    retry_row = cursor.fetchone()
                    retry_count = retry_row[0] if retry_row else 0
                    
                    if retry_count >= MAX_RETRIES - 1:
                        cursor.execute(
                            "UPDATE crawl_queue SET status = 'error', error_reason = ? WHERE id = ?",
                            (error, row_id)
                        )
                        # Mark domain as error if too many errors
                        cursor.execute(
                            "UPDATE sites SET error_count = error_count + 1 WHERE id = ?",
                            (site_id,)
                        )
                    else:
                        cursor.execute(
                            "UPDATE crawl_queue SET status = 'pending', retry_count = retry_count + 1, error_reason = ? WHERE id = ?",
                            (error, row_id)
                        )
                
                conn.commit()
            
            conn.close()
            
        except sqlite3.OperationalError as e:
            print(f"⚠️  Worker {worker_name} - SQLite chyba: {e}")
            time.sleep(5)
        except Exception as e:
            print(f"⚠️  Worker {worker_name} chyba: {e}")
            time.sleep(10)
    
    print(f"✅ Worker {worker_name} ukončen")


def start_workers():
    """Start background worker threads"""
    global worker_threads
    
    # Create and start worker threads
    thread_a = threading.Thread(target=worker_a, name="worker_a", daemon=True)
    thread_b = threading.Thread(target=worker_b, name="worker_b", daemon=True)
    
    thread_a.start()
    thread_b.start()
    
    worker_threads = [thread_a, thread_b]
    print("✅ Workers spuštěny (worker_a, worker_b)")
    return worker_threads


def crawl_site(site_url, max_pages=500):
    """Main crawl function - triggers Phase 1 discovery"""
    init_db()
    site_url = normalize_domain(site_url)
    
    if not site_url:
        print("⚠️  Neplatná URL")
        return None
    
    # Run Phase 1 discovery
    site_id, urls_added = phase_1_discovery(site_url, max_pages)
    
    if site_id:
        print(f"[Bot] Objeveno {urls_added} URL pro {site_url}")
    else:
        print(f"[Bot] Nelze přidat {site_url}")
    
    return site_id


def delete_site(site_id):
    """Delete a site and all associated data"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Get site info
    cursor.execute("SELECT canonical_url FROM sites WHERE id = ?", (site_id,))
    site = cursor.fetchone()
    
    if not site:
        conn.close()
        return False
    
    # Delete from vector DB
    if vector_db:
        try:
            # Get all documents for this site
            all_ids = []
            for id in vector_db.ids:
                metadata = vector_db.metadata.get(id, {})
                if metadata.get('site_id') == str(site_id):
                    all_ids.append(id)
            
            if all_ids:
                vector_db.delete(all_ids)
                print(f"✅ Smazáno {len(all_ids)} dokumentů z vector DB")
        except Exception as e:
            print(f"⚠️  Chyba při mazání z vector DB: {e}")
    
    # Delete from SQLite
    cursor.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    cursor.execute("DELETE FROM crawl_queue WHERE site_id = ?", (site_id,))
    cursor.execute("DELETE FROM sitemaps_feeds WHERE site_id = ?", (site_id,))
    
    conn.commit()
    conn.close()
    
    print(f"✅ Web s ID {site_id} a všechna jeho data smazána")
    return True


def recrawl_site(site_id):
    """Re-crawl a site by resetting its queue"""
    conn = get_db()
    cursor = conn.cursor()
    
    # Get site info
    cursor.execute("SELECT canonical_url, max_pages FROM sites WHERE id = ?", (site_id,))
    site = cursor.fetchone()
    
    if not site:
        conn.close()
        return False
    
    site_url, max_pages = site
    
    # Reset site status
    cursor.execute("UPDATE sites SET status = 'active', error_count = 0 WHERE id = ?", (site_id,))
    
    # Delete existing queue and sitemaps/feeds
    cursor.execute("DELETE FROM crawl_queue WHERE site_id = ?", (site_id,))
    cursor.execute("DELETE FROM sitemaps_feeds WHERE site_id = ?", (site_id,))
    
    # Delete existing vector DB documents for this site
    if vector_db:
        try:
            all_ids = []
            for id in vector_db.ids:
                metadata = vector_db.metadata.get(id, {})
                if metadata.get('site_id') == str(site_id):
                    all_ids.append(id)
            
            if all_ids:
                vector_db.delete(all_ids)
                print(f"✅ Smazáno {len(all_ids)} dokumentů z vector DB")
        except Exception as e:
            print(f"⚠️  Chyba při mazání z vector DB: {e}")
    
    conn.commit()
    conn.close()
    
    # Re-run Phase 1 discovery
    site_id_new, urls_added = phase_1_discovery(site_url, max_pages)
    
    if site_id_new:
        print(f"✅ Re-crawl zahájen: {urls_added} nových URL")
    else:
        print("⚠️  Re-crawl se nezdařil")
    
    return True


def get_site_info(site_id):
    """Get information about a site"""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute("SELECT * FROM sites WHERE id = ?", (site_id,))
    site = cursor.fetchone()
    
    conn.close()
    
    if site:
        return dict(site)
    return None


def get_crawl_stats(site_id=None):
    """Get crawl statistics"""
    conn = get_db()
    cursor = conn.cursor()
    
    if site_id:
        cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE site_id = ?", (site_id,))
        total = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE site_id = ? AND status = 'done'", (site_id,))
        done = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE site_id = ? AND status = 'error'", (site_id,))
        errors = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE site_id = ? AND status IN ('pending', 'locked')", (site_id,))
        pending = cursor.fetchone()[0]
    else:
        cursor.execute("SELECT COUNT(*) FROM crawl_queue")
        total = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE status = 'done'")
        done = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE status = 'error'")
        errors = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE status IN ('pending', 'locked')")
        pending = cursor.fetchone()[0]
    
    conn.close()
    
    return {
        'total': total,
        'done': done,
        'errors': errors,
        'pending': pending
    }


if __name__ == "__main__":
    # Initialize vector backend
    init_vector_backend()
    
    # Initialize database
    init_db()
    
    # Start workers
    start_workers()
    
    print("=" * 70)
    print("Mini Search - Crawler Engine v3.0")
    print(f"Vector backend: {VECTOR_BACKEND}")
    print("=" * 70)
    print("Pro spuštění crawlu: crawl_site(url, max_pages)")
    print("Pro ukončení: Ctrl+C")
    print("=" * 70)
    
    if len(sys.argv) > 1:
        crawl_site(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 500)
    else:
        # Keep running
        try:
            while not shutdown_flag:
                time.sleep(1)
        except KeyboardInterrupt:
            cleanup()
