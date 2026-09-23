#!/usr/bin/env python3
"""Tests for the public search page (Clay design) and its display helpers."""
import os
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
    os.chdir(tmpdir)

    import app_combined as m

    m.DB_PATH = os.path.join(tmpdir, "console.db")
    m._db_conn = None
    m.close_db()
    m.get_db()

    site_id = m.add_site("https://example.com/", 50)
    conn = m.get_db()
    conn.execute("""INSERT INTO pages (site_id, url, title, og_title, og_description,
                    body_text, schema_type, has_audio, audio_url, seo_score, indexed_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,strftime('%s','now'))""",
                 (site_id, "https://example.com/clanek",
                  "Jak funguje <b>vyhledávání</b>", "Jak funguje vyhledávání",
                  "Vysvětlení vyhledávání a indexace v praxi.",
                  "Tento článek popisuje vyhledávání a indexaci stránek do lokálního indexu.",
                  "Article", 1, "https://example.com/a.mp3", 80))
    conn.execute("""INSERT INTO pages (site_id, url, title, body_text, indexed_at)
                    VALUES (?,?,?,?,strftime('%s','now'))""",
                 (site_id, "https://example.com/jiny", "Jiné téma", "Nic o hledání."))
    conn.commit()

    client = m.app.test_client()

    # ---- helpers ----
    check("_result_domain strips www",
          m._result_domain("https://www.example.com/a") == "example.com")
    check("_result_url_path keeps query",
          m._result_url_path("https://example.com/a?x=1") == "/a?x=1")

    snippet = m._result_snippet({"body_text": "a" * 400 + " vyhledávání"}, "vyhledávání", 100)
    check("snippet windows around match", "vyhledávání" in snippet and snippet.startswith("…"))

    plural = m.app.jinja_env.filters['plural_cz']
    check("plural 1 -> one", plural(1, "výsledek", "výsledky", "výsledků") == "výsledek")
    check("plural 3 -> few", plural(3, "výsledek", "výsledky", "výsledků") == "výsledky")
    check("plural 22 -> few", plural(22, "výsledek", "výsledky", "výsledků") == "výsledky")
    check("plural 12 -> many", plural(12, "výsledek", "výsledky", "výsledků") == "výsledků")
    check("plural 0 -> many", plural(0, "výsledek", "výsledky", "výsledků") == "výsledků")

    html = m._highlight_snippet("o vyhledávání zde", "vyhledávání")
    check("highlight wraps term in mark", "<mark>vyhledávání</mark>" in html)

    # XSS: markup in stored text must be escaped, not injected
    xss = m._highlight_snippet("<script>alert(1)</script> vyhledávání", "vyhledávání")
    check("highlight escapes script tags", "<script>" not in xss, xss)
    check("highlight still marks term", "<mark>vyhledávání</mark>" in xss, xss)

    # ---- page rendering ----
    resp = client.get("/")
    body = resp.get_data(as_text=True)
    check("GET / returns 200", resp.status_code == 200, str(resp.status_code))
    check("initial state shows prompt", "Zadejte dotaz" in body)
    check("uses shared Clay stylesheet", "css/clay.css" in body)
    check("loads search.css", "css/search.css" in body)
    check("loads search.js", "js/search.js" in body)
    check("no inline <style> block left", "<style>" not in body)

    resp = client.get("/?q=vyhled%C3%A1v%C3%A1n%C3%AD")
    body = resp.get_data(as_text=True)
    check("query returns 200", resp.status_code == 200, str(resp.status_code))
    check("shows result title", "Jak funguje" in body)
    check("shows highlighted snippet", "<mark>" in body)
    check("shows domain breadcrumb", 'class="domain">example.com' in body)
    check("shows relevance meter", "relevance-fill" in body)
    check("shows audio player for audio page", "<audio" in body)
    check("result cards rendered", body.count('class="result-item"') >= 1)

    # Stored XSS in title must be escaped
    check("title is escaped", "<b>vyhledávání</b>" not in body, "raw markup leaked")

    # ---- filters ----
    for name, value in [("all", "all"), ("articles", "articles"),
                        ("podcasts", "podcasts"), ("audio", "audio"), ("price", "price")]:
        resp = client.get(f"/?q=vyhled%C3%A1v%C3%A1n%C3%AD&filter={value}")
        check(f"filter {name} returns 200", resp.status_code == 200, str(resp.status_code))

    resp = client.get("/?q=vyhled%C3%A1v%C3%A1n%C3%AD&filter=articles")
    check("articles filter keeps Article",
          "Jak funguje" in resp.get_data(as_text=True))

    # ---- empty results ----
    resp = client.get("/?q=zzzzneexistuje")
    body = resp.get_data(as_text=True)
    check("empty result state", "Žádné výsledky" in body)
    check("empty state offers admin link", "/admin/sources/new" in body)

    # ---- pagination ----
    # 30 extra matches force a second page (SEARCH_PAGE_SIZE is 25).
    for i in range(30):
        conn.execute(
            """INSERT INTO pages (site_id,url,title,body_text,indexed_at)
               VALUES (?,?,?,?,strftime('%s','now'))""",
            (site_id, f"https://example.com/paginace-{i}",
             f"Stránkování {i}", f"Stránkování výsledků, díl {i}."))
    conn.commit()

    resp = client.get("/?q=str%C3%A1nkov%C3%A1n%C3%AD&page=1")
    body = resp.get_data(as_text=True)
    check("page 1 returns 200", resp.status_code == 200)
    check("page 1 caps at page size",
          body.count('class="result-item"') == m.SEARCH_PAGE_SIZE,
          str(body.count('class="result-item"')))
    check("page 1 renders next link", "Další" in body and "Strana 1" in body)
    check("page 1 disables previous", 'class="disabled">Předchozí' in body)

    resp = client.get("/?q=str%C3%A1nkov%C3%A1n%C3%AD&page=2")
    body = resp.get_data(as_text=True)
    check("page 2 returns 200", resp.status_code == 200)
    check("page 2 has remaining results",
          body.count('class="result-item"') == 5,
          str(body.count('class="result-item"')))
    check("page 2 renders previous link", "Předchozí" in body and "Strana 2" in body)
    check("page 2 disables next", 'class="disabled">Další' in body)

    resp = client.get("/?q=vyhled%C3%A1v%C3%A1n%C3%AD&page=abc")
    check("invalid page falls back to 1", resp.status_code == 200)
    check("invalid page shows first page",
          "Jak funguje" in resp.get_data(as_text=True))

    resp = client.get("/?q=vyhled%C3%A1v%C3%A1n%C3%AD&page=0")
    check("page below 1 falls back to 1", resp.status_code == 200)
    check("no pagination when a single page suffices",
          "pagination" not in resp.get_data(as_text=True))

    # ---- autocomplete ----
    resp = client.get("/autocomplete?q=vy")
    check("autocomplete returns 200", resp.status_code == 200)
    check("autocomplete returns JSON list",
          isinstance(resp.get_json().get("results"), list))
    resp = client.get("/autocomplete?q=v")
    check("autocomplete ignores 1-char query", resp.get_json() == {"results": []})

    # ---- admin still shares the design ----
    body = client.get("/admin").get_data(as_text=True)
    check("admin uses clay.css", "css/clay.css" in body)
    check("admin still loads admin.css", "css/admin.css" in body)
    check("admin does not load search.css", "css/search.css" not in body)

    print()
    if failures:
        print(f"{len(failures)} TEST(S) FAILED: {failures}")
        raise SystemExit(1)
    print("ALL SEARCH PAGE TESTS PASSED")


if __name__ == "__main__":
    main()
