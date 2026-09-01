import sys
import sqlite3
import requests
import urllib3
import feedparser
import extruct
import hashlib
import time
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
MEILI_URL = "http://127.0.0.1:7700/indexes/pages/documents"
BOT_NAME = "UltimateGoogleBot"
HEADERS = {"User-Agent": f"Mozilla/5.0 (Compatible; {BOT_NAME}/4.0; +http://localhost)"}

def get_db():
    conn = sqlite3.connect("console.db", timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE,
            max_pages INTEGER,
            interval_hours INTEGER,
            last_crawled INTEGER
        )
    ''')
    conn.commit()
    conn.close()

def normalize_url(url):
    parsed = urlparse(url)
    clean_url = urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path, parsed.params, parsed.query, ''))
    if clean_url.endswith('/') and len(parsed.path) > 1:
        clean_url = clean_url[:-1]
    return clean_url

def generate_doc_id(url):
    return hashlib.md5(url.encode('utf-8')).hexdigest()

def extract_date(soup, feed_date=None):
    if feed_date:
        try:
            return int(time.mktime(feed_date))
        except Exception:
            pass
    for meta_name in ["article:published_time", "date", "dc.date", "og:updated_time"]:
        meta = soup.find("meta", property=meta_name) or soup.find("meta", attrs={"name": meta_name})
        if meta and meta.get("content"):
            try:
                dt = datetime.fromisoformat(meta["content"].replace("Z", "+00:00"))
                return int(dt.timestamp())
            except Exception:
                pass
    return int(time.time())

def extract_audio(soup, html, url):
    for audio in soup.find_all("audio"):
        if audio.get("src"):
            return urljoin(url, audio["src"])
        for source in audio.find_all("source"):
            if source.get("src"):
                return urljoin(url, source["src"])
    for a in soup.find_all("a", href=True):
        if a["href"].endswith((".mp3", ".m4a", ".wav", ".ogg")):
            return urljoin(url, a["href"])
    return ""

def extract_metadata(soup, html, url):
    meta_info = {"og": {}, "schema_types": [], "schema_details": {}}
    for meta in soup.find_all("meta"):
        prop = meta.get("property", "") or meta.get("name", "")
        if prop.startswith("og:"):
            meta_info["og"][prop[3:]] = meta.get("content", "").strip()

    try:
        extracted = extruct.extract(html, base_url=url, syntaxes=['json-ld', 'microdata'])
        for syntax in ['json-ld', 'microdata']:
            for item in extracted.get(syntax, []):
                if isinstance(item, dict):
                    stype = item.get("@type", "WebPage")
                    if isinstance(stype, list):
                        stype = stype[0]
                    meta_info["schema_types"].append(stype)
                    if stype in ["PodcastEpisode", "AudioObject", "Article", "BlogPosting", "NewsArticle"]:
                        meta_info["schema_type_override"] = stype
                    if "aggregateRating" in item:
                        r = item["aggregateRating"]
                        if isinstance(r, dict):
                            meta_info["schema_details"]["rating"] = r.get("ratingValue")
                            meta_info["schema_details"]["reviews"] = r.get("reviewCount")
                    if "offers" in item:
                        o = item["offers"]
                        if isinstance(o, dict):
                            meta_info["schema_details"]["price"] = o.get("price")
                            meta_info["schema_details"]["currency"] = o.get("priceCurrency")
                    if "author" in item:
                        auth = item["author"]
                        meta_info["schema_details"]["author"] = auth.get("name") if isinstance(auth, dict) else str(auth)
    except Exception:
        pass
    return meta_info

def get_robots_parser(base_url):
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rfp = RobotFileParser()
    rfp.set_url(robots_url)
    try:
        rfp.read()
    except Exception:
        pass
    return rfp

def crawl_site(site_url, max_pages=500):
    init_db()
    site_url = normalize_url(site_url)
    
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO sites (url, max_pages, interval_hours, last_crawled) VALUES (?, ?, 12, 0)", (site_url, max_pages))
    cur.execute("SELECT id FROM sites WHERE url = ?", (site_url,))
    site_id = cur.fetchone()[0]
    conn.commit()
    conn.close()

    rfp = get_robots_parser(site_url)
    visited = set()
    to_visit = []
    feed_dates = {}

    try:
        feed = feedparser.parse(site_url)
        if feed.entries:
            for e in feed.entries:
                clean = normalize_url(e.link)
                to_visit.append(clean)
                if hasattr(e, 'published_parsed'):
                    feed_dates[clean] = e.published_parsed
    except Exception:
        pass

    if not to_visit:
        to_visit.append(site_url)

    crawled_docs = []
    print(f"[Bot] Crawluji web: {site_url}", flush=True)

    while to_visit and len(visited) < (max_pages * 2):
        raw_url = to_visit.pop(0)
        url = normalize_url(raw_url)

        if url in visited:
            continue
        if not rfp.can_fetch(BOT_NAME, url):
            continue

        visited.add(url)

        try:
            res = requests.get(url, headers=HEADERS, timeout=6, verify=False)
            if res.status_code != 200 or "text/html" not in res.headers.get("Content-Type", ""):
                continue

            res.encoding = res.apparent_encoding or 'utf-8'
            html = res.text
            soup = BeautifulSoup(html, "html.parser")

            meta_info = extract_metadata(soup, html, url)
            pub_timestamp = extract_date(soup, feed_dates.get(url))
            audio_url = extract_audio(soup, html, url)

            for el in soup(["script", "style", "nav", "footer", "iframe"]):
                el.decompose()

            title = soup.title.string.strip() if soup.title else url
            if meta_info["og"].get("title"):
                title = meta_info["og"]["title"]

            body_text = ' '.join([p.get_text().strip() for p in soup.find_all(['p', 'h1', 'h2', 'h3', 'article']) if p.get_text().strip()])[:3500]

            image_url = meta_info["og"].get("image", "")
            if not image_url:
                for img in soup.find_all("img", src=True):
                    if not any(x in img["src"].lower() for x in ["logo", "icon", "svg", "1x1", "avatar"]):
                        image_url = urljoin(url, img["src"])
                        break

            schema_type = meta_info.get("schema_type_override") or (meta_info["schema_types"][0] if meta_info["schema_types"] else "WebPage")
            if audio_url and schema_type == "WebPage":
                schema_type = "PodcastEpisode"

            if body_text.strip():
                crawled_docs.append({
                    "id": generate_doc_id(url),
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
                    "og": meta_info["og"]
                })
                print(f"[{len(crawled_docs)}] Indexováno: {title[:35]}...", flush=True)

            for a in soup.find_all('a', href=True):
                link = normalize_url(urljoin(url, a['href']))
                if urlparse(link).netloc == urlparse(site_url).netloc and link not in visited:
                    to_visit.append(link)

        except Exception as e:
            pass

    # Seřazení od nejnovějších a omezení na přesně 500 nejnovějších stránek
    crawled_docs.sort(key=lambda x: x["published_timestamp"], reverse=True)
    final_docs = crawled_docs[:max_pages]

    if final_docs:
        requests.post(MEILI_URL, json=final_docs)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE sites SET last_crawled = ? WHERE id = ?", (int(time.time()), site_id))
    conn.commit()
    conn.close()
    print(f"[Bot] Hotovo. Uloženo 500 nejnovějších stránek.", flush=True)

if __name__ == "__main__":
    init_db()
    if len(sys.argv) > 1:
        crawl_site(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 500)
