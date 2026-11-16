#!/usr/bin/env python3
"""Parser fallback test: the backend must work without lxml (iOS/Briefcase).

lxml has no iOS wheel, so app_combined.py picks BeautifulSoup's stdlib
"html.parser" when lxml is missing. This runs the import in a subprocess with
lxml blocked, then exercises the sitemap parser and link discovery, so the real
fallback path is covered rather than mocked.
"""
import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHILD = r'''
import builtins
_real = builtins.__import__
def _fake(name, *a, **k):
    if name == "lxml" or name.startswith("lxml."):
        raise ImportError("no lxml (simulated iOS)")
    return _real(name, *a, **k)
builtins.__import__ = _fake

import app_combined as m

assert m.BS_PARSER == "html.parser", m.BS_PARSER

xml = (b'<?xml version="1.0"?>'
       b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
       b'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">'
       b'<url><loc>https://x.cz/a</loc>'
       b'<image:loc>https://x.cz/i.jpg</image:loc></url></urlset>')
locs, _ = m._parse_sitemap_locs(xml)
assert locs == ["https://x.cz/a"], locs

html = ('<html><head><meta property="og:title" content="T"></head><body>'
        '<a href="/b">B</a><a href="mailto:x@y.cz">m</a>'
        '<a href="https://other.cz/x">ext</a></body></html>')
soup = m.BeautifulSoup(html, m.BS_PARSER)
links = m.extract_links_from_soup(soup, "https://x.cz/", limit=10)
assert links == ["https://x.cz/b"], links

og = m.extract_page_content("https://x.cz/", None, html=html)
print("OK", m.BS_PARSER, og.get("title"))
'''


def main():
    proc = subprocess.run(
        [sys.executable, "-c", CHILD],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print("FAIL: backend works without lxml")
        print(proc.stdout)
        print(proc.stderr)
        raise SystemExit(1)
    print("PASS: backend works without lxml ->", proc.stdout.strip())
    print("ALL PARSER FALLBACK TESTS PASSED")


if __name__ == "__main__":
    main()
