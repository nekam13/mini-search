#!/usr/bin/env python3
"""Tests for the Android bridge that feeds the native (WebView-free) screens.

The app has no browser: the home/search/sites/settings screens are rendered from
JSON returned by ``android_entry``. These tests pin that contract — every bridge
function the Kotlin UI calls must return parseable JSON with the keys the screen
reads, and the search payload must carry plain-text fields (no HTML), so a
snippet can never smuggle markup into a native TextView.
"""
import json
import os
import shutil
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name} {detail}")


def main():
    tmpdir = tempfile.mkdtemp()
    os.environ["MINISEARCH_DB"] = os.path.join(tmpdir, "console.db")
    os.environ["MINISEARCH_EMBEDDINGS"] = "off"
    os.environ["MINISEARCH_UPDATE_CHECK"] = "0"

    import android_entry as ae

    try:
        status = json.loads(ae.start(tmpdir))
        check("bridge start reports RUNNING", status.get("state") == "RUNNING", str(status))

        # ---- sources: add, list, delete ----
        added = json.loads(ae.add_source_json(json.dumps(
            {"url": "https://example.com", "type": "auto"})))
        check("add_source registers a source", "source_id" in added, str(added))

        sources = json.loads(ae.sources_json())
        check("sources_json lists the source", sources.get("count") == 1, str(sources))
        check("source carries a display type", sources["sources"][0]["type"] == "url",
              str(sources["sources"][0]))

        # ---- dashboard ----
        dash = json.loads(ae.dashboard_json())
        check("dashboard carries stats", set(dash.get("stats", {})) >= {
            "sites", "pages", "pending", "errors", "sources"}, str(dash))
        check("dashboard lists the site", len(dash.get("sites", [])) == 1, str(dash))
        site_id = dash["sites"][0]["id"]

        # ---- sites ----
        sites = json.loads(ae.sites_json())
        check("sites_json returns the site", sites.get("count") == 1, str(sites))
        check("site brief has the keys the UI reads", set(sites["sites"][0]) >= {
            "id", "url", "status", "is_local", "indexed", "pending", "errors",
            "sources", "max_pages", "priority", "last_crawled", "aliases"}, str(sites))

        detail = json.loads(ae.site_detail_json(site_id))
        check("site_detail carries sources/pages/errors/statuses",
              set(detail) >= {"site", "sources", "pages", "errors", "statuses"}, str(detail))
        check("site_detail exposes the editable bounds",
              detail["max_pages_min"] == 1 and detail["max_pages_max"] >= 1, str(detail))

        # ---- update / recrawl / delete ----
        upd = json.loads(ae.update_site_json(json.dumps({
            "id": site_id, "max_pages": 250, "status": "paused",
            "aliases": "a.com,b.com", "is_local": False, "priority": 2.5})))
        check("update_site saves", "error" not in upd, str(upd))

        after = json.loads(ae.site_detail_json(site_id))
        check("update persisted max_pages", after["site"]["max_pages"] == 250, str(after["site"]))
        check("update persisted status", after["site"]["status"] == "paused", str(after["site"]))
        check("update persisted aliases", after["site"]["aliases"] == ["a.com", "b.com"],
              str(after["site"]))
        check("update persisted priority", after["site"]["priority"] == 2.5, str(after["site"]))

        check("recrawl_site reports a message or a busy error",
              "message" in json.loads(ae.recrawl_site_json(site_id)) or
              "error" in json.loads(ae.recrawl_site_json(site_id)))

        # ---- quick actions ----
        for name, fn in (("pause_all_json", ae.pause_all_json),
                         ("resume_all_json", ae.resume_all_json),
                         ("recrawl_all_json", ae.recrawl_all_json)):
            payload = json.loads(fn())
            check(f"{name} returns a message", "message" in payload, str(payload))

        # ---- search: plain text, never HTML ----
        empty = json.loads(ae.search_json(json.dumps({"q": ""})))
        check("search with an empty query returns no results",
              empty.get("results") == [], str(empty))

        news = json.loads(ae.news_json())
        check("news_json returns items list", "items" in news, str(news))

        # A snippet must be plain text: a hostile page body must not reach the
        # TextView with markup intact.
        m = ae._module()
        page = {
            "site_id": site_id, "url": "https://example.com/x",
            "url_hash": m.url_hash("https://example.com/x"),
            "title": "X", "og_title": "", "og_description": "",
            "og_image": "", "favicon_url": "",
            "body_text": "<script>alert(1)</script> Praha je mesto &amp; Brno",
            "images": [], "schema_type": "", "schema_details": {},
            "audio_url": "", "has_audio": 0, "published_timestamp": 0,
        }
        page["seo_score"] = m.calculate_seo_score(page)
        page["embedding"] = m._embedding_for(page)
        m.store_page(page)

        results = json.loads(ae.search_json(json.dumps({"q": "Praha"})))
        check("search finds the seeded page", results.get("count", 0) >= 1, str(results))
        if results.get("results"):
            snippet = results["results"][0]["snippet"]
            check("snippet is plain text (no HTML tags)", "<" not in snippet and ">" not in snippet,
                  snippet)
            check("snippet decodes HTML entities", "&amp;" not in snippet and "&#" not in snippet,
                  snippet)
            check("result carries display fields", set(results["results"][0]) >= {
                "title", "url", "domain", "path", "snippet", "relevance", "date"}, str(results))

        # ---- delete cleans up ----
        deleted = json.loads(ae.delete_site_json(site_id))
        check("delete_site reports success", "error" not in deleted, str(deleted))
        check("site is gone", json.loads(ae.sites_json()).get("count") == 0)

        stopped = json.loads(ae.stop())
        check("bridge stop reports STOPPED", stopped.get("state") == "STOPPED", str(stopped))
    finally:
        try:
            ae.stop()
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {failures}")
        sys.exit(1)
    print("ALL ANDROID BRIDGE TESTS PASSED")


if __name__ == "__main__":
    main()
