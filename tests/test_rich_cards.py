#!/usr/bin/env python3
"""Deterministic tests for Rich Results cards and automatic self-migration.

No network: pages are inserted straight into the database and rendered through
the real ``prepare_results``/``/`` pipeline. Covers card classification for each
type, the structured fields (price, rating, time, calories, address), XSS
hardening of rich fields, and the automatic upgrade+cleanup path.
"""
import json
import os
import sqlite3
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


def _seed(conn, site_id, url, title, schema_type, details, images="[]", body=None):
    import app_combined as m
    conn.execute(
        """INSERT INTO pages (site_id, url, url_hash, title, og_title, og_description,
           body_text, schema_type, schema_details, images, indexed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,strftime('%s','now'))""",
        (site_id, url, m.url_hash(url), title, title,
         f"{title} popis", body or f"Unikátní obsah stránky {title}.", schema_type,
         json.dumps(details), images))


def test_card_classification():
    """Each schema type maps to the right card kind and structured fields."""
    import app_combined as m

    tmpdir = tempfile.mkdtemp()
    os.chdir(tmpdir)
    m.DB_PATH = os.path.join(tmpdir, "console.db")
    m._db_conn = None
    m.close_db()
    m.get_db()

    site = m.add_site("https://example.com/", 50)
    conn = m.get_db()
    rows = [
        ("https://cs.wikipedia.org/wiki/Česko", "Česko", "",
         {"@type": "Article", "_meta": {"breadcrumbs": ["Hlavní", "Státy"]}}),
        ("https://shop.example.com/hrnek", "Hrnek", "Product",
         {"@type": "Product", "offers": {"price": "1 249,50",
                                         "priceCurrency": "CZK",
                                         "availability": "https://schema.org/InStock"},
          "aggregateRating": {"ratingValue": 4.5, "ratingCount": 128},
          "brand": {"name": "Clay"}, "sku": "HR-1"}),
        ("https://food.example.com/recept", "Svíčková", "Recipe",
         {"@type": "Recipe", "totalTime": "PT1H20M",
          "nutrition": {"calories": "540"}, "recipeYield": "4 lidi",
          "recipeCategory": "Hlavní jídlo", "recipeCuisine": "Česká",
          "aggregateRating": {"ratingValue": 4.8, "reviewCount": 52}}),
        ("https://firma.example.com/kontakt", "Firma", "LocalBusiness",
         {"@type": "LocalBusiness",
          "address": {"streetAddress": "Dlouhá 12", "addressLocality": "Praha"},
          "telephone": "+420 123 456 789",
          "logo": "https://firma.example.com/logo.png",
          "openingHours": "Po–Pá 9–17"}),
        ("https://blog.example.com/clanek", "Článek", "Article",
         {"@type": "Article", "_meta": {"author": "Jan Novák",
                                        "breadcrumbs": ["Blog"]}}),
    ]
    for url, title, stype, details in rows:
        _seed(conn, site, url, title, stype, details)
    conn.commit()

    all_rows = [dict(r) for r in m.execute_db_fetchall("SELECT * FROM pages")]
    by_title = {p["display_title"]: p for p in m.prepare_results(all_rows, "obsah")}

    check("all five cards prepared", len(by_title) == 5, str(list(by_title)))

    wiki = by_title["Česko"]
    check("wiki page detected by host", wiki["card_type"] == "wiki", wiki["card_type"])
    check("wiki badge is Czech label", wiki["card_badge"] == "Wikipedie",
          wiki["card_badge"])
    check("wiki metadata carries source + language",
          any(r["label"] == "Zdroj" for r in wiki["rich_metadata"])
          and any(r["label"] == "Jazyk" for r in wiki["rich_metadata"]),
          str(wiki["rich_metadata"]))

    product = by_title["Hrnek"]
    check("product card type", product["card_type"] == "product", product["card_type"])
    check("price formatted in Czech", product["price_display"] == "1 249,50 Kč",
          product["price_display"])
    check("availability localised", product["availability"] == "Skladem",
          product["availability"])
    check("availability marked in stock", product["availability_ok"] is True)
    check("rating value rendered", product["rating_value"] == "4.5",
          product["rating_value"])
    check("rating percent computed", product["rating_pct"] == 90,
          str(product["rating_pct"]))
    check("rating count kept", product["rating_count"] == "128", product["rating_count"])
    check("brand in metadata",
          {"label": "Značka", "value": "Clay"} in product["rich_metadata"],
          str(product["rich_metadata"]))

    recipe = by_title["Svíčková"]
    check("recipe card type", recipe["card_type"] == "recipe", recipe["card_type"])
    check("ISO duration rendered", recipe["time_display"] == "1 h 20 min",
          recipe["time_display"])
    check("calories rendered", recipe["calories"] == "540 kcal", recipe["calories"])
    check("recipe yield in metadata",
          {"label": "Porce", "value": "4 lidi"} in recipe["rich_metadata"],
          str(recipe["rich_metadata"]))

    org = by_title["Firma"]
    check("organization card type", org["card_type"] == "organization", org["card_type"])
    check("address composed", org["address"] == "Dlouhá 12, Praha", org["address"])
    check("phone captured", org["phone"] == "+420 123 456 789", org["phone"])
    check("logo used as thumbnail",
          org["thumb"] == "https://firma.example.com/logo.png", org["thumb"])

    article = by_title["Článek"]
    check("plain article fallback", article["card_type"] == "article",
          article["card_type"])
    check("author in metadata",
          {"label": "Autor", "value": "Jan Novák"} in article["rich_metadata"],
          str(article["rich_metadata"]))

    # --- rendering keeps the shared contract + XSS safety -------------------
    body = m.app.test_client().get("/?q=obsah").get_data(as_text=True)
    check("renders rich wrapper", 'class="card-rich card-wiki"' in body)
    check("renders price tag", 'class="price-tag"' in body)
    check("renders rating stars", 'class="rating-stars"' in body)
    check("renders rich metadata list", 'class="rich-metadata"' in body)
    check("renders thumbnail wrapper", 'class="card-thumbnail"' in body)
    check("keeps result-item contract",
          body.count('class="result-item"') == 5, str(body.count('class="result-item"')))

    # --- XSS: hostile JSON-LD must not inject markup -----------------------
    _seed(conn, site, "https://xss.example.com/x", "XSS", "Product",
          {"@type": "Product", "name": "<script>alert(1)</script>",
           "offers": {"price": "<img src=x onerror=alert(1)>",
                      "priceCurrency": "CZK"},
           "brand": {"name": "<b>bold</b>"},
           "address": "<iframe src=evil>"})
    conn.commit()
    xss_body = m.app.test_client().get("/?q=obsah").get_data(as_text=True)
    check("no script tag injected from JSON-LD", "<script>alert(1)</script>" not in xss_body)
    check("no executable onerror payload", "onerror=alert(1)" not in xss_body,
          "raw payload leaked")
    check("hostile iframe escaped", "<iframe src=evil>" not in xss_body)
    check("javascript: image url rejected",
          m._safe_image_url("javascript:alert(1)") == "")
    check("data: image url rejected", m._safe_image_url("data:text/html,x") == "")
    check("https image url allowed",
          m._safe_image_url("https://a.example/i.png") == "https://a.example/i.png")

    # --- numeric/duration helpers -----------------------------------------
    check("czech decimal price parsed", m._schema_number("1 249,50") == 1249.5)
    check("dot price parsed", m._schema_number("19.99") == 19.99)
    check("not-a-number is None", m._schema_number("zdarma") is None)
    check("duration hours+minutes", m._format_duration("PT2H5M") == "2 h 5 min")
    check("duration minutes only", m._format_duration("PT45M") == "45 min")
    check("duration plain number is minutes", m._format_duration("30") == "30 min")
    check("bad duration is empty", m._format_duration("nonsense") == "")
    check("dotted price formatted", m._format_price(19.99, "EUR") == "19,99 €")

    m.close_db()


def test_self_migration():
    """Startup upgrades in place and cleans legacy tables only once verified."""
    import app_combined as m

    tmpdir = tempfile.mkdtemp()
    old_path = os.path.join(tmpdir, "old.db")
    old = sqlite3.connect(old_path)
    old.execute("""CREATE TABLE sites (id INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_url TEXT UNIQUE, aliases TEXT DEFAULT '[]', status TEXT DEFAULT 'active',
        error_count INTEGER DEFAULT 0, last_crawled INTEGER DEFAULT 0,
        max_pages INTEGER DEFAULT 500, crawl_delay REAL DEFAULT 1.0,
        created_at INTEGER DEFAULT 0)""")
    old.execute("""CREATE TABLE site_sources (id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id INTEGER NOT NULL, url TEXT NOT NULL,
        source_type TEXT NOT NULL CHECK(source_type IN ('domain','url','sitemap','feed','rss','atom')),
        priority INTEGER DEFAULT 5, notes TEXT DEFAULT '', last_checked INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active', created_at INTEGER DEFAULT 0)""")
    old.execute("INSERT INTO sites (canonical_url) VALUES ('https://example.com')")
    old.execute("""INSERT INTO site_sources (site_id, url, source_type, notes)
                   VALUES (1, 'https://example.com/blog', 'url', 'starý zdroj')""")
    old.commit()
    old.close()

    m.close_db()
    m.DB_PATH = old_path
    m._db_conn = None
    try:
        m.get_db()
        # Upgrade happened automatically, and nothing is left behind.
        summary = m.run_self_migration()
        check("self-migration reports ok", summary == "ok", summary)
        tables = {r[0] for r in m.execute_db_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        check("no legacy table remains", "site_sources_old" not in tables, str(tables))
        check("original source survived",
              m.execute_db_fetchone(
                  "SELECT notes FROM site_sources WHERE url = ?",
                  ("https://example.com/blog",))[0] == "starý zdroj")

        # An interrupted rebuild must NOT be cleaned before it is verified safe:
        # a legacy table holding more rows than the live one means data may have
        # been lost, so it is kept for inspection.
        new_count = m.execute_db_fetchone("SELECT COUNT(*) FROM site_sources")[0]
        m.execute_db("CREATE TABLE site_sources_old (id INTEGER, junk TEXT)", commit=True)
        for i in range(new_count + 5):
            m.execute_db("INSERT INTO site_sources_old (id, junk) VALUES (?, 'x')",
                         (i,), commit=True)
        summary2 = m.run_self_migration()
        check("legacy kept when new table is smaller",
              "kept site_sources_old" in summary2, summary2)
        check("legacy still present", m.execute_db_fetchone(
            "SELECT name FROM sqlite_master WHERE name='site_sources_old'") is not None)
        m.execute_db("DROP TABLE site_sources_old", commit=True)

        # A verified-safe leftover is removed, proving the "old is deleted"
        # behaviour once the migration is confirmed.
        m.execute_db("CREATE TABLE site_sources_old (id INTEGER, junk TEXT)", commit=True)
        m.execute_db("INSERT INTO site_sources_old (id, junk) VALUES (1, 'x')", commit=True)
        summary3 = m.run_self_migration()
        check("verified legacy table dropped", "dropped legacy" in summary3, summary3)
        check("legacy gone after verification", m.execute_db_fetchone(
            "SELECT name FROM sqlite_master WHERE name='site_sources_old'") is None)
    finally:
        m.close_db()
        m.DB_PATH = os.path.join(tmpdir, "console.db")
        m._db_conn = None
        m.get_db()


def main():
    test_card_classification()
    test_self_migration()

    print()
    if failures:
        print(f"{len(failures)} TEST(S) FAILED: {failures}")
        raise SystemExit(1)
    print("ALL RICH CARD + SELF-MIGRATION TESTS PASSED")


if __name__ == "__main__":
    main()
