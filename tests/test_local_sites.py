#!/usr/bin/env python3
"""Tests for local-network site support (v7.4).

Covers local URL detection, robots.txt bypass, the 3x search boost and the
admin/API plumbing that exposes the ``is_local`` flag.
"""
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

tmpdir = tempfile.mkdtemp()
os.chdir(tmpdir)

import app_combined as m  # noqa: E402

m.DB_PATH = os.path.join(tmpdir, "local.db")
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


# --- 1. Local URL detection -------------------------------------------------
local_urls = [
    "http://localhost",
    "http://localhost:8000",
    "http://127.0.0.1:5000/page",
    "http://192.168.1.100/admin",
    "http://10.0.0.5",
    "http://172.16.4.4:8080",
    "http://nas:5000",
    "http://server.local",
    "http://[::1]:9000",
]
public_urls = [
    "https://example.com",
    "https://www.google.com/search",
    "http://8.8.8.8",
    "https://blog.example.org/feed.xml",
]

for url in local_urls:
    check(f"local: {url}", m.is_local_url(url), "expected local")
for url in public_urls:
    check(f"public: {url}", not m.is_local_url(url), "expected public")

check("bare localhost normalizes to http",
      m.normalize_url("localhost:8000").startswith("http://"))
check("bare public normalizes to https",
      m.normalize_url("example.com").startswith("https://"))

# --- 2. validate_url accepts local shapes -----------------------------------
for value in ["localhost", "localhost:8000", "192.168.1.100", "http://nas:5000"]:
    ok, msg = m.validate_url(value)
    check(f"validate_url accepts {value}", ok, msg)

# --- 3. robots.txt bypass for local sites -----------------------------------
# Cache a blanket "Disallow: /" for a public-looking host so we can prove the
# bypass is what lets the request through.
_rp = m.RobotFileParser()
_rp.parse(["User-agent: *", "Disallow: /"])
m._robots_cache["devbox.example.com"] = _rp
check("public host blocked by robots",
      not m.is_allowed("http://devbox.example.com/secret"))
check("is_local=True bypasses robots",
      m.is_allowed("http://devbox.example.com/secret", is_local=True))
check("is_allowed local autodetected",
      m.is_allowed("http://192.168.1.100/secret"))

# --- 4. add_site stores is_local + boost ------------------------------------
site_id = m.add_site("http://192.168.1.100", 500)
check("add_site returns id", site_id is not None)
site = m.get_site_by_id(site_id)
check("local site flagged", site["is_local"] == 1, repr(site.get("is_local")))
check("local site boosted",
      float(site["search_priority_multiplier"]) == m.LOCAL_SITE_PRIORITY_MULTIPLIER,
      repr(site.get("search_priority_multiplier")))

public_id = m.add_site("https://public-example.com", 500)
public_site = m.get_site_by_id(public_id)
check("public site not flagged", public_site["is_local"] == 0)
check("public site unboosted",
      float(public_site["search_priority_multiplier"]) == m.PUBLIC_SITE_PRIORITY_MULTIPLIER)

# Explicit override should win over detection.
forced_id = m.add_site("https://intranet-box", 500, is_local=True)
check("explicit is_local honored",
      m.get_site_by_id(forced_id)["is_local"] == 1)

# --- 5. add_source inherits / propagates is_local ---------------------------
src_id, err = m.add_source(0, "http://10.0.0.5", "domain")
check("local domain source created", src_id is not None and err is None, str(err))
local_site = m.get_site_by_id(m.get_source_by_id(src_id)["site_id"])
check("domain from local url flagged", local_site["is_local"] == 1)

# Marking an existing public domain as local upgrades it.
up_id, _ = m.add_source(0, "https://upgrade-me.example", "domain")
up_site_id = m.get_source_by_id(up_id)["site_id"]
m.add_source(up_site_id, "https://upgrade-me.example/local.xml", "sitemap", is_local=True)
check("existing domain upgraded to local",
      m.get_site_by_id(up_site_id)["is_local"] == 1)
check("upgraded domain boosted",
      float(m.get_site_by_id(up_site_id)["search_priority_multiplier"])
      == m.LOCAL_SITE_PRIORITY_MULTIPLIER)

# --- 6. update_site toggling ------------------------------------------------
ok, msg = m.update_site(public_id, is_local=True)
check("update_site marks local", ok and m.get_site_by_id(public_id)["is_local"] == 1, msg)
ok, msg = m.update_site(public_id, is_local=False)
check("update_site unmarks local", ok and m.get_site_by_id(public_id)["is_local"] == 0, msg)
check("unmarking resets multiplier",
      float(m.get_site_by_id(public_id)["search_priority_multiplier"])
      == m.PUBLIC_SITE_PRIORITY_MULTIPLIER)

ok, msg = m.update_site(site_id, max_pages=1234)
check("update_site max_pages", ok and m.get_site_by_id(site_id)["max_pages"] == 1234, msg)
ok, msg = m.update_site(site_id, search_priority_multiplier=99)
check("multiplier validated", not ok, "99 should be rejected")

# --- 7. Queueing uses the urgent local priority -----------------------------
m._queue_url(site_id, "http://192.168.1.100/page-1")
row = m.execute_db_fetchone(
    "SELECT priority FROM crawl_queue WHERE url = ?", ("http://192.168.1.100/page-1",))
check("local url queued with urgent priority",
      row is not None and row[0] == m.LOCAL_QUEUE_PRIORITY, repr(row))

# --- 8. API surface ---------------------------------------------------------
resp = client.post("/admin/api/sources", json={
    "url": "http://localhost:8080", "source_type": "domain", "is_local": "1"})
check("api POST local source", resp.status_code == 201, resp.get_data(as_text=True))

resp = client.get("/admin/api/sites")
data = resp.get_json()
sites_by_url = {s["canonical_url"]: s for s in data["sites"]}
check("api sites exposes is_local", "localhost:8080" in sites_by_url
      and sites_by_url["localhost:8080"]["is_local"] == 1,
      str(list(sites_by_url)))

resp = client.put(f"/admin/api/sites/{site_id}", json={"search_priority_multiplier": 4.5})
check("api site update multiplier", resp.status_code == 200, resp.get_data(as_text=True))
check("multiplier persisted",
      float(m.get_site_by_id(site_id)["search_priority_multiplier"]) == 4.5)

# --- 9. Local sites sort first ---------------------------------------------
ordered = m.get_all_sites()
check("local sites sorted first", ordered[0]["is_local"] == 1, str([s["is_local"] for s in ordered]))
filtered = m.get_filtered_sites(local_only=True)
check("local_only filter", filtered and all(s["is_local"] for s in filtered))

# --- 10. Admin pages render -------------------------------------------------
for path in ["/admin/", "/admin/sites", "/admin/sites?local=1",
             f"/admin/sites/{site_id}", f"/admin/sites/{site_id}/edit"]:
    r = client.get(path)
    check(f"GET {path} -> 200", r.status_code == 200, str(r.status_code))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All local-site tests passed.")