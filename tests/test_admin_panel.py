#!/usr/bin/env python3
"""End-to-end tests for the new Clay admin panel (v7.1)."""
import json
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

tmpdir = tempfile.mkdtemp()
os.chdir(tmpdir)

import app_combined as m  # noqa: E402

m.DB_PATH = os.path.join(tmpdir, "console.db")
m._db_conn = None
m.close_db()
m.get_db()

client = m.app.test_client()

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name} {detail}")


# --- 1. add_source for a domain (creates site) ---
source_id, err = m.add_source(0, "https://example.com", "domain")
check("add domain source", source_id is not None and err is None, str(err))

site = m.get_site_by_id(1)
check("domain created site", site is not None and site["canonical_url"] == "example.com")

# --- 2. add url/sitemap/feed children ---
url_id, err = m.add_source(1, "https://example.com/special-page", "url", priority=7, notes="hlavni")
check("add url source", url_id is not None and err is None, str(err))
sm_id, err = m.add_source(1, "https://example.com/sitemap.xml", "sitemap")
check("add sitemap source", sm_id is not None and err is None, str(err))
feed_id, err = m.add_source(1, "https://example.com/feed.xml", "rss")
check("add rss source", feed_id is not None and err is None, str(err))

# --- 3. duplicate rejected ---
dup, err = m.add_source(1, "https://example.com/special-page", "url")
check("duplicate url rejected", dup is None and "již" in (err or ""), str(err))

# --- 4. validation ---
_, err = m.add_source(1, "not a url", "url")
check("invalid url rejected", err is not None, str(err))
_, err = m.add_source(1, "https://example.com/x", "url", priority=99)
check("invalid priority rejected", err is not None, str(err))
_, err = m.add_source(1, "https://example.com/y", "bogus")
check("invalid source type rejected", err is not None, str(err))

# --- 5. max_pages validation ---
ok, msg, val = m.validate_max_pages(0)
check("max_pages 0 rejected", not ok)
ok, msg, val = m.validate_max_pages(100000)
check("max_pages >10000 rejected", not ok)
ok, msg, val = m.validate_max_pages(750)
check("max_pages 750 accepted", ok and val == 750)

# --- 6. get_site_sources ---
sources = m.get_site_sources(1)
check("get_site_sources returns 4", len(sources) == 4, f"got {len(sources)}")
check("last_checked_str present", all('last_checked_str' in s for s in sources))

# --- 7. update_source incl max_pages ---
ok, msg = m.update_source(url_id, priority=2, notes="upraveno", max_pages=123)
check("update_source ok", ok, msg)
check("priority updated", m.get_source_by_id(url_id)["priority"] == 2)
check("notes updated", m.get_source_by_id(url_id)["notes"] == "upraveno")
check("max_pages updated", m.get_site_by_id(1)["max_pages"] == 123)

# --- 8. update site ---
ok, msg = m.update_site(1, status="paused", aliases="www.example.com, blog.example.com")
check("update_site status+aliases", ok, msg)
site = m.get_site_by_id(1)
check("status paused", site["status"] == "paused")
check("aliases stored", json.loads(site["aliases"]) == ["www.example.com", "blog.example.com"])

# --- 9. stats ---
stats = m.get_source_stats(1)
check("stats has all keys", set(stats) == {"indexed", "pending", "errors", "sources", "total"})
check("stats sources count", stats["sources"] == 4, str(stats))

# --- 10. filtered sites ---
check("filter by status", len(m.get_filtered_sites(status="paused")) == 1)
check("filter by source type", len(m.get_filtered_sites(source_type="sitemap")) == 1)
check("filter by search", len(m.get_filtered_sites(search="example")) == 1)
check("filter no match", len(m.get_filtered_sites(search="nonexistent")) == 0)

# --- 11. get_all_sources ---
all_sources = m.get_all_sources()
check("get_all_sources count", len(all_sources) == 4, str(len(all_sources)))

# --- 12. HTTP: admin pages ---
for path in ["/admin", "/admin/sites", "/admin/sites/1", "/admin/sites/1/edit",
             "/admin/sources/new", "/admin/sites/1/sources/new",
             f"/admin/sources/{url_id}/edit", "/admin/search",
             "/admin/search?q=test", "/admin/sites?q=example&status=paused&source_type=sitemap"]:
    resp = client.get(path)
    check(f"GET {path}", resp.status_code == 200, f"-> {resp.status_code}")

# --- 13. HTTP JSON API ---
resp = client.get("/admin/api/sources")
data = resp.get_json()
check("GET /admin/api/sources", resp.status_code == 200 and data["count"] == 4, str(data))

resp = client.post("/admin/api/sources", json={
    "site_id": 1, "url": "https://example.com/another", "source_type": "url", "priority": 4})
check("POST /admin/api/sources", resp.status_code == 201, resp.get_data(as_text=True))
new_id = resp.get_json()["id"]

resp = client.put(f"/admin/api/sources/{new_id}", json={"priority": 9, "notes": "api-update"})
check("PUT /admin/api/sources/<id>", resp.status_code == 200, resp.get_data(as_text=True))
check("PUT applied", m.get_source_by_id(new_id)["priority"] == 9)

resp = client.put(f"/admin/api/sources/{new_id}", json={"priority": 50})
check("PUT invalid rejected", resp.status_code == 400)

resp = client.get(f"/admin/api/sources/{new_id}")
check("GET /admin/api/sources/<id>", resp.status_code == 200)

resp = client.get("/admin/api/stats")
check("GET /admin/api/stats", resp.status_code == 200 and "sources" in resp.get_json())

resp = client.put("/admin/api/sites/1", json={"max_pages": 321})
check("PUT /admin/api/sites/<id>", resp.status_code == 200)
check("site max_pages via API", m.get_site_by_id(1)["max_pages"] == 321)

resp = client.get("/admin/api/sites/1")
check("GET /admin/api/sites/<id>", resp.status_code == 200 and "sources" in resp.get_json())

resp = client.delete(f"/admin/api/sources/{new_id}")
check("DELETE /admin/api/sources/<id>", resp.status_code == 200)
check("source removed", m.get_source_by_id(new_id) is None)

resp = client.delete("/admin/api/sources/999999")
check("DELETE missing 404", resp.status_code == 404)

# --- 14. POST via form (fallback) ---
resp = client.post("/admin/api/sources", data={
    "site_id": "1", "url": "https://example.com/form-added", "source_type": "url"})
check("POST form-encoded", resp.status_code == 201, resp.get_data(as_text=True))

# --- 15. recrawl route exists (no network) ---
resp = client.get("/admin/sites/1/recrawl", follow_redirects=False)
check("GET recrawl redirects", resp.status_code == 302)

# --- 16. pause/resume all ---
resp = client.get("/admin/pause-all", follow_redirects=False)
check("pause-all", resp.status_code == 302)
check("all paused", all(s["status"] == "paused" for s in m.get_all_sites()))
client.get("/admin/resume-all")
check("all active", all(s["status"] == "active" for s in m.get_all_sites()))

# --- 17. migration from legacy sites/sitemaps_feeds ---
legacy_conn = m.get_db()
legacy_conn.execute("INSERT INTO sites (canonical_url) VALUES ('legacy.test')")
legacy_id = legacy_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
legacy_conn.execute(
    "INSERT INTO sitemaps_feeds (site_id, url, type) VALUES (?, ?, 'sitemap')",
    (legacy_id, "https://legacy.test/sitemap.xml"))
legacy_conn.commit()
m._migrate_sources(legacy_conn)
migrated = [s for s in m.get_site_sources(legacy_id) if s["source_type"] == "sitemap"]
check("legacy sitemap migrated", len(migrated) == 1, str(migrated))
check("legacy domain source migrated",
      any(s["source_type"] == "domain" for s in m.get_site_sources(legacy_id)))

# --- 18. XSS escaping ---
resp = client.get("/admin/sites?q=<script>alert(1)</script>")
check("XSS not reflected raw", b"<script>alert(1)</script>" not in resp.data)

# --- 19. error logging ---
m.log_error("unit test error", ValueError("boom"))
check("error log written", os.path.exists(m.ERROR_LOG_PATH))

# --- 20. delete_source domain cascades ---
ok, msg = m.delete_source(1)
check("delete domain source", ok, msg)
check("site gone after domain delete", m.get_site_by_id(1) is None)

print()
if failures:
    print(f"{len(failures)} TEST(S) FAILED: {failures}")
    raise SystemExit(1)
print("ALL ADMIN TESTS PASSED")
