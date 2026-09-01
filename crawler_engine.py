import sys
import os
import sqlite3
import requests
import hashlib
import time
import json
import threading
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
CHROMA_DB_PATH = "chroma_db"
MODEL_PATH = "models/paraphrase-multilingual-MiniLM-L12-v2"

# Global lock for rate limiting
rate_limit_lock = threading.Lock()
last_request_time = 0.0

# Robots.txt cache
robots_cache = {}
robots_cache_lock = threading.Lock()

# Initialize ChromaDB client
try:
    import chromadb
    from sentence_transformers import SentenceTransformer
    
    # Load embedding model
    os.makedirs("models", exist_ok=True)
    embedding_model = SentenceTransformer(MODEL_PATH, cache_folder="models")
    
    # Initialize ChromaDB with PersistentClient
    os.makedirs(CHROMA_DB_PATH, exist_ok=True)
    chroma_client = chromadb.PersistentClient(path=CHROMA_DB_PATH)
    
    class MiniSearchEmbedding(chromadb.EmbeddingFunction):
        def __call__(self, input):
            return embedding_model.encode(input).tolist()
    
    # Create or get pages collection
    pages_collection = chroma_client.get_or_create_collection(
        name="pages",
        embedding_function=MiniSearchEmbedding()
    )
    
    CHROMA_AVAILABLE = True
except Exception as e:
    print(f"Warning: ChromaDB not available: {e}")
    CHROMA_AVAILABLE = False


def get_db():
    """Get SQLite database connection"""
    conn = sqlite3.connect("console.db", timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
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
            max_pages INTEGER DEFAULT 500
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
            UNIQUE(site_id, url),
            FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
        )
    ''')
    
    # Add new columns to crawl_queue if they don't exist
    try:
        cursor.execute("ALTER TABLE crawl_queue ADD COLUMN content_hash TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass  # Column already exists
    
    try:
        cursor.execute("ALTER TABLE crawl_queue ADD COLUMN last_checked INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    
    try:
        cursor.execute("ALTER TABLE crawl_queue ADD COLUMN priority REAL DEFAULT 0.5")
    except sqlite3.OperationalError:
        pass
    
    try:
        cursor.execute("ALTER TABLE crawl_queue ADD COLUMN hit_count INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    
    try:
        cursor.execute("ALTER TABLE crawl_queue ADD COLUMN archived_at INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    
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
    
    # Add lastmod column to sitemaps_feeds if it doesn't exist
    try:
        cursor.execute("ALTER TABLE sitemaps_feeds ADD COLUMN lastmod TEXT DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    
    conn.commit()
    conn.close()


def normalize_domain(url):
    """Normalize domain: strip www., lowercase, strip trailing slash"""
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


def generate_doc_id(url):
    """Generate MD5 hash for document ID"""
    return hashlib.md5(url.encode('utf-8')).hexdigest()


def respect_rate_limit():
    """Enforce minimum delay between requests"""
    global last_request_time
    with rate_limit_lock:
        elapsed = time.time() - last_request_time
        if elapsed < MIN_DELAY:
            time.sleep(MIN_DELAY - elapsed)
        last_request_time = time.time()


def get_robots_parser(base_url):
    """Get and parse robots.txt for a domain"""
    parsed = urlparse(base_url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    with robots_cache_lock:
        if domain in robots_cache:
            return robots_cache[domain]
    robots_url = f"{domain}/robots.txt"
    rfp = RobotFileParser()
    rfp.set_url(robots_url)
    try:
        respect_rate_limit()
        rfp.read()
    except Exception:
        pass
    with robots_cache_lock:
        robots_cache[domain] = rfp
    return rfp


def extract_date(soup, feed_date=None):
    """Extract published date from page or feed"""
    if feed_date:
        try:
            return int(time.mktime(feed_date))
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
    
    except Exception as e:
        print(f"Error extracting Schema.org: {e}")
    
    return meta_info


def add_site(site_url, max_pages=500):
    """Add a new site with domain deduplication"""
    site_url = normalize_domain(site_url)
    parsed = urlparse(site_url)
    canonical_domain = parsed.netloc
    
    conn = get_db()
    cursor = conn.cursor()
    
    # Check if domain already exists
    cursor.execute("SELECT id, canonical_url, aliases FROM sites WHERE canonical_url LIKE ? OR aliases LIKE ?",
                   (f"%{canonical_domain}%", f"%{canonical_domain}%"))
    existing = cursor.fetchone()
    
    if existing:
        site_id, canonical_url, aliases_str = existing
        aliases = json.loads(aliases_str) if aliases_str else []
        
        # Add new URL as alias if not already present
        if site_url not in aliases and site_url != canonical_url:
            aliases.append(site_url)
            cursor.execute("UPDATE sites SET aliases = ? WHERE id = ?", 
                          (json.dumps(aliases), site_id))
            conn.commit()
        
        conn.close()
        return site_id
    
    # Insert new site
    cursor.execute(
        "INSERT INTO sites (canonical_url, aliases, status, max_pages) VALUES (?, ?, 'active', ?)",
        (site_url, json.dumps([]), max_pages)
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
        robots_url = f"{urlparse(site_url).scheme}://{urlparse(site_url).netloc}/robots.txt"
        resp = requests.get(robots_url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                line = line.strip()
                if line.lower().startswith("sitemap:"):
                    sitemap_url = line.split(":", 1)[1].strip()
                    if sitemap_url:
                        sitemap_urls.append(sitemap_url)
    except Exception:
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
        "/feed/atom"
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
                discovered_urls.append(full_url)
        except:
            pass
    
    # Check common URLs
    for path in common_urls:
        full_url = urljoin(base_url, path)
        try:
            respect_rate_limit()
            response = requests.head(full_url, headers=HEADERS, timeout=10, allow_redirects=True)
            if response.status_code == 200:
                discovered_urls.append(full_url)
        except:
            pass
    
    # Process discovered URLs to identify type and extract links
    conn = get_db()
    cursor = conn.cursor()
    
    for url in discovered_urls:
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
                "INSERT INTO sitemaps_feeds (site_id, url, type, last_checked) VALUES (?, ?, ?, ?)",
                (site_id, url, feed_type, int(time.time()))
            )
            
            # Extract URLs from sitemap or feed
            if feed_type in ['rss', 'atom']:
                try:
                    feed_data = feedparser.parse(response.text)
                    for entry in feed_data.entries:
                        if hasattr(entry, 'link'):
                            discovered_feeds.append({
                                'url': normalize_domain(entry.link),
                                'date': entry.get('published_parsed', None),
                                'priority': 1.0,
                                'lastmod': ''
                            })
                except:
                    pass
            else:  # sitemap
                try:
                    soup = BeautifulSoup(response.text, 'xml')
                    for url_tag in soup.find_all('url'):
                        loc = url_tag.find('loc')
                        priority_tag = url_tag.find('priority')
                        lastmod_tag = url_tag.find('lastmod')
                        if loc and loc.text:
                            discovered_feeds.append({
                                'url': normalize_domain(loc.text.strip()),
                                'date': None,
                                'priority': float(priority_tag.text.strip()) if priority_tag else 0.5,
                                'lastmod': lastmod_tag.text.strip() if lastmod_tag else ''
                            })
                except:
                    pass
                    
        except Exception as e:
            print(f"Error processing {url}: {e}")
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
        
        soup = BeautifulSoup(response.text, 'html.parser')
        discovered_urls = []
        
        for a in soup.find_all('a', href=True):
            link = urljoin(site_url, a['href'])
            normalized = normalize_domain(link)
            parsed_link = urlparse(link)
            parsed_site = urlparse(site_url)
            
            # Only follow links from the same domain
            if parsed_link.netloc == parsed_site.netloc:
                if normalized not in discovered_urls:
                    discovered_urls.append(normalized)
        
        return discovered_urls[:max_pages]
        
    except Exception as e:
        print(f"Error crawling homepage {site_url}: {e}")
        return []


def phase_1_discovery(site_url, max_pages=500):
    """Phase 1: Discovery - Build full URL queue"""
    site_url = normalize_domain(site_url)
    site_id = add_site(site_url, max_pages)
    
    conn = get_db()
    cursor = conn.cursor()
    
    # Get existing URLs for this site
    cursor.execute("SELECT url FROM crawl_queue WHERE site_id = ?", (site_id,))
    existing_urls = {row[0] for row in cursor.fetchall()}
    
    # Discover sitemaps and feeds
    discovered_items = discover_sitemaps_and_feeds(site_url, site_id)
    
    # Add discovered URLs to queue
    urls_to_add = []
    for item in discovered_items:
        url = item['url']
        if url not in existing_urls:
            urls_to_add.append((url, item.get('priority', 0.5)))
            existing_urls.add(url)
    
    # If no URLs found from sitemaps/feeds, crawl homepage
    if not urls_to_add:
        homepage_urls = crawl_homepage_for_links(site_url, site_id, max_pages)
        urls_to_add = [(u, 0.5) for u in homepage_urls]
    
    # Add URLs to crawl queue with priority
    for url, priority in urls_to_add:
        try:
            cursor.execute(
                "INSERT INTO crawl_queue (site_id, url, status, priority) VALUES (?, ?, 'pending', ?)",
                (site_id, url, priority)
            )
        except sqlite3.IntegrityError:
            pass  # URL already exists
    
    conn.commit()
    conn.close()
    
    return site_id, len(urls_to_add)


def process_url(url, site_id, max_pages):
    """Process a single URL: extract content, generate embeddings, store in ChromaDB"""
    try:
        respect_rate_limit()
        
        # Check robots.txt
        rfp = get_robots_parser(url)
        if not rfp.can_fetch(BOT_NAME, url):
            crawl_delay = rfp.crawl_delay(BOT_NAME)
            if crawl_delay:
                time.sleep(max(crawl_delay, MIN_DELAY))
            else:
                return None, "Blocked by robots.txt"
        
        response = requests.get(url, headers=HEADERS, timeout=15)
        
        # Handle rate limiting (HTTP 429)
        if response.status_code == 429:
            retry_after = int(response.headers.get('Retry-After', 5))
            time.sleep(retry_after)
            return None, f"Rate limited, retry after {retry_after}s"
        
        # Handle forbidden (HTTP 403)
        if response.status_code == 403:
            return None, "HTTP 403 Forbidden"
        
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
        
        # Clean HTML
        for el in soup(['script', 'style', 'nav', 'footer', 'iframe', 'noscript']):
            el.decompose()
        
        # Extract title
        title = meta_info["og"].get("title", "")
        if not title and soup.title:
            title = soup.title.string.strip() if soup.title.string else url
        
        # Extract body text
        body_parts = []
        for tag in ['p', 'h1', 'h2', 'h3', 'article']:
            for el in soup.find_all(tag):
                text = el.get_text().strip()
                if text:
                    body_parts.append(text)
        body_text = ' '.join(body_parts)[:3500]
        
        # Part 3: Compute content hash and check if unchanged
        new_hash = hashlib.md5(body_text.encode('utf-8')).hexdigest()
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT content_hash FROM crawl_queue WHERE url = ?", (url,))
        row = cursor.fetchone()
        
        if row and row[0] == new_hash:
            # Content unchanged — update last_checked only, skip re-embedding
            cursor.execute("UPDATE crawl_queue SET status='done', last_checked=? WHERE url=?", 
                          (int(time.time()), url))
            conn.commit()
            conn.close()
            return {"skipped": True, "url": url}, None
        
        # Extract image
        image_url = meta_info["og"].get("image", "")
        if not image_url:
            for img in soup.find_all("img", src=True):
                img_src = img["src"].lower()
                if not any(x in img_src for x in ["logo", "icon", "svg", "1x1", "avatar", "banner"]):
                    image_url = urljoin(url, img["src"])
                    break
        
        # Determine schema type
        schema_type = meta_info.get("schema_type_override") or (meta_info["schema_types"][0] if meta_info["schema_types"] else "WebPage")
        if audio_url and schema_type == "WebPage":
            schema_type = "PodcastEpisode"
        
        # Generate document ID
        doc_id = generate_doc_id(url)
        
        # Part 6: Check active index limit and evict if needed
        cursor.execute("SELECT max_pages FROM sites WHERE id=?", (site_id,))
        max_row = cursor.fetchone()
        max_pages_limit = max_row[0] if max_row else 500
        
        cursor.execute("SELECT COUNT(*) FROM crawl_queue WHERE site_id=? AND status='done'", (site_id,))
        active_count = cursor.fetchone()[0]
        
        if active_count >= max_pages_limit:
            # Archive the lowest scoring page
            cursor.execute("""
                SELECT id FROM crawl_queue
                WHERE site_id=? AND status='done'
                ORDER BY priority ASC, last_checked ASC
                LIMIT 1
            """, (site_id,))
            evict_row = cursor.fetchone()
            if evict_row:
                cursor.execute(
                    "UPDATE crawl_queue SET status='archived', archived_at=? WHERE id=?",
                    (int(time.time()), evict_row[0])
                )
        
        conn.commit()
        conn.close()
        
        # Prepare document for ChromaDB
        if CHROMA_AVAILABLE and body_text.strip():
            # Embed title + body text
            text_to_embed = f"{title} {body_text}"
            
            # Store in ChromaDB with metadata
            metadata = {
                "url": url,
                "title": title,
                "image": image_url,
                "audio_url": audio_url,
                "has_audio": bool(audio_url),
                "schema_type": schema_type,
                "schema_details": json.dumps(meta_info["schema_details"]),
                "published_timestamp": pub_timestamp,
                "published_date": datetime.fromtimestamp(pub_timestamp).strftime('%d.%m.%Y %H:%M'),
                "site_id": str(site_id)
            }
            
            try:
                pages_collection.upsert(
                    ids=[doc_id],
                    documents=[text_to_embed],
                    metadatas=[metadata]
                )
            except Exception as e:
                print(f"Error storing in ChromaDB: {e}")
        
        # Save new hash and last_checked
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("UPDATE crawl_queue SET content_hash=?, last_checked=? WHERE url=?",
                      (new_hash, int(time.time()), url))
        conn.commit()
        conn.close()
        
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
        
    except Exception as e:
        return None, str(e)


def worker_a():
    """Worker A: Process pending URLs from crawl queue"""
    while True:
        try:
            conn = get_db()
            cursor = conn.cursor()
            
            # Lock a batch of pending URLs
            worker_name = threading.current_thread().name
            cursor.execute(
                "UPDATE crawl_queue SET status='locked', locked_by=? WHERE id IN "
                "(SELECT id FROM crawl_queue WHERE status='pending' AND retry_count < 3 LIMIT 5)",
                (worker_name,)
            )
            conn.commit()
            cursor.execute(
                "SELECT id, site_id, url FROM crawl_queue WHERE status='locked' AND locked_by=?",
                (worker_name,)
            )
            batch = cursor.fetchall()
            
            if not batch:
                conn.close()
                time.sleep(5)
                continue
            
            # Get site info
            site_info = {}
            unique_site_ids = set(row[1] for row in batch)
            for sid in unique_site_ids:
                cursor.execute("SELECT max_pages FROM sites WHERE id = ?", (sid,))
                result = cursor.fetchone()
                if result:
                    site_info[sid] = result[0]
            
            # Process each URL
            for row_id, site_id, url in batch:
                max_pages = site_info.get(site_id, 500)
                result, error = process_url(url, site_id, max_pages)
                
                if result:
                    cursor.execute(
                        "UPDATE crawl_queue SET status = 'done' WHERE id = ?",
                        (row_id,)
                    )
                else:
                    retry_count = 0
                    cursor.execute("SELECT retry_count FROM crawl_queue WHERE id = ?", (row_id,))
                    retry_row = cursor.fetchone()
                    if retry_row:
                        retry_count = retry_row[0]
                    
                    if retry_count >= 2:
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
            
        except Exception as e:
            print(f"Worker A error: {e}")
            time.sleep(10)


def worker_b():
    """Worker B: Process pending URLs from crawl queue"""
    while True:
        try:
            conn = get_db()
            cursor = conn.cursor()
            
            # Lock a batch of pending URLs
            worker_name = threading.current_thread().name
            cursor.execute(
                "UPDATE crawl_queue SET status='locked', locked_by=? WHERE id IN "
                "(SELECT id FROM crawl_queue WHERE status='pending' AND retry_count < 3 LIMIT 5)",
                (worker_name,)
            )
            conn.commit()
            cursor.execute(
                "SELECT id, site_id, url FROM crawl_queue WHERE status='locked' AND locked_by=?",
                (worker_name,)
            )
            batch = cursor.fetchall()
            
            if not batch:
                conn.close()
                time.sleep(5)
                continue
            
            # Get site info
            site_info = {}
            for row_id, site_id, url in batch:
                cursor.execute("SELECT max_pages FROM sites WHERE id = ?", (site_id,))
                result = cursor.fetchone()
                if result:
                    site_info[site_id] = result[0]
            
            # Process each URL
            for row_id, site_id, url in batch:
                max_pages = site_info.get(site_id, 500)
                result, error = process_url(url, site_id, max_pages)
                
                if result:
                    cursor.execute(
                        "UPDATE crawl_queue SET status = 'done' WHERE id = ?",
                        (row_id,)
                    )
                else:
                    retry_count = 0
                    cursor.execute("SELECT retry_count FROM crawl_queue WHERE id = ?", (row_id,))
                    retry_row = cursor.fetchone()
                    if retry_row:
                        retry_count = retry_row[0]
                    
                    if retry_count >= 2:
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
            
        except Exception as e:
            print(f"Worker B error: {e}")
            time.sleep(10)


def start_workers():
    """Start background worker threads"""
    # Create and start worker threads
    thread_a = threading.Thread(target=worker_a, name="worker_a", daemon=True)
    thread_b = threading.Thread(target=worker_b, name="worker_b", daemon=True)
    
    thread_a.start()
    thread_b.start()
    
    print("✅ Workers started (worker_a, worker_b)")
    return thread_a, thread_b


def crawl_site(site_url, max_pages=500):
    """Main crawl function - triggers Phase 1 discovery"""
    init_db()
    site_url = normalize_domain(site_url)
    
    # Run Phase 1 discovery
    site_id, urls_added = phase_1_discovery(site_url, max_pages)
    
    print(f"[Bot] Objeveno {urls_added} URL pro {site_url}")
    
    # Update site last_crawled time
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE sites SET last_crawled = ? WHERE id = ?",
        (int(time.time()), site_id)
    )
    conn.commit()
    conn.close()
    
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
    
    canonical_url = site[0]
    doc_prefix = generate_doc_id(canonical_url)[:8]  # Use prefix for ChromaDB query
    
    # Delete from ChromaDB
    if CHROMA_AVAILABLE:
        try:
            # Get all documents for this site
            results = pages_collection.get(
                where={"site_id": str(site_id)},
                include=["metadatas", "documents", "ids"]
            )
            
            if results.get("ids"):
                pages_collection.delete(ids=results["ids"])
        except Exception as e:
            print(f"Error deleting from ChromaDB: {e}")
    
    # Delete from SQLite
    cursor.execute("DELETE FROM sites WHERE id = ?", (site_id,))
    cursor.execute("DELETE FROM crawl_queue WHERE site_id = ?", (site_id,))
    cursor.execute("DELETE FROM sitemaps_feeds WHERE site_id = ?", (site_id,))
    
    conn.commit()
    conn.close()
    
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
    
    # Delete existing queue and sitemaps/feeds
    cursor.execute("DELETE FROM crawl_queue WHERE site_id = ?", (site_id,))
    cursor.execute("DELETE FROM sitemaps_feeds WHERE site_id = ?", (site_id,))
    
    # Delete existing ChromaDB documents for this site
    if CHROMA_AVAILABLE:
        try:
            results = pages_collection.get(
                where={"site_id": str(site_id)},
                include=["ids"]
            )
            if results.get("ids"):
                pages_collection.delete(ids=results["ids"])
        except Exception as e:
            print(f"Error deleting from ChromaDB: {e}")
    
    conn.commit()
    conn.close()
    
    # Re-run Phase 1 discovery
    site_id_new, urls_added = phase_1_discovery(site_url, max_pages)
    
    return True


if __name__ == "__main__":
    init_db()
    start_workers()
    
    if len(sys.argv) > 1:
        crawl_site(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 500)
