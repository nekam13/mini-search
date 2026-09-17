# AGENTS.md — mini-search

## Project

Single-file Flask + SQLite hybrid search engine (`app_combined.py`), Czech UI and docs.
Branch under active development: `beta-optimized`.

## Running tests

Each suite is a standalone script with its own `check()` helper; run them directly:

```bash
python3 tests/test_db.py                     # schema, indexes, FTS5, site_sources
python3 tests/test_admin.py                  # admin pages and JSON API
python3 tests/test_admin_panel.py            # end-to-end admin panel
python3 tests/test_discovery_integration.py  # sources -> discovery + scheduler
python3 tests/test_search_page.py            # public search page, filters, XSS, pagination
python3 tests/test_source_indexing.py        # new-source auto-indexing + dashboard data
```

`tests/test_search_page.py` prints `PASS:` lines and an `ALL ... PASSED` summary;
`test_admin_panel.py` prints `ALL ADMIN TESTS PASSED`.

## UI architecture

Two Flask apps worth of UI live in one file, so keep them sharing assets:

- `static/css/clay.css` — the shared Clay design system (tokens, layout, cards,
  buttons, forms, tables, pagination, responsive + reduced-motion rules).
  This is the only place design tokens (`--primary`, `--radius`, `--shadow`, …)
  should be declared.
- `static/css/admin.css` — admin-only rules. Loaded *in addition to* clay.css.
- `static/css/search.css` + `static/js/search.js` — public search page only.
- `templates/base_clay.html` — shared shell (top bar, nav, footer).
  Both `templates/admin/base.html` and `templates/search.html` extend it.

Nav highlighting uses an `active_page` template variable. Current keys:
`search`, `dashboard`, `sites`, `add`, `index_search`.

### Keep logic out of templates

`prepare_results()` in `app_combined.py` attaches display-only fields
(`domain`, `display_url_path`, `snippet_html`, `relevance_pct`, `display_title`,
`thumb`, …) so templates only render. Follow that pattern rather than adding
conditionals or filters to HTML.

### Escaping

`_highlight_snippet()` escapes with `markupsafe.escape` **before** inserting
`<mark>` tags, so indexed HTML cannot execute. Any new snippet/markup helper
must preserve this order.

### Czech pluralisation

Use the `plural_cz` Jinja filter: `{{ n|plural_cz('výsledek', 'výsledky', 'výsledků') }}`.
It handles the teens exception (12–14 → many) and the last-digit rule
(22 → few, 21 → many).

## Search / pagination

`hybrid_search(query, limit, filter_type)` has **no offset parameter**.
Pagination is done at the route by fetching `page * SEARCH_PAGE_SIZE + 1` rows
(the extra row detects whether a next page exists) and slicing.
`SEARCH_MAX_RESULTS` caps how deep pagination can go.

## Source indexing

Adding a source must start indexing it — a stored-but-unqueued source looks
broken to the user. `index_source(source_id)` holds the per-type queueing logic
(`url` → itself, `sitemap` → its URLs, feed → its entries) and is shared by
`phase_1_discovery()` and the `POST /admin/api/sources` route. Wrap network
calls in `index_source_async()` / `recrawl_async()` so the HTTP request returns
without waiting on robots.txt/sitemap fetches. Sources with `status='paused'`
are skipped.

## Site stats keys

`get_source_stats(site_id)` is the single source of truth for a site's numbers
and returns `indexed`, `pending`, `errors`, `sources`, `total` — those are the
names templates read (`site.indexed`, `stats.sources`, …). `get_all_sites()` and
`get_filtered_sites()` must both merge that dict in; do not reintroduce the older
`indexed_count` / `pending_count` names, which never matched the templates.

## Environment notes

- `sentence_transformers` is usually unavailable, so vector search silently
  falls back to FTS5. Tests must not assume vector search works.
- Expected console noise: `Model nelze nacist: ...` and `FTS5 table backfilled ...`.
- Existing databases are migrated in place; never drop `console.db`.

## Conventions

- Czech for all user-facing text, comments and docs.
- Parameterised SQL only (`?` placeholders) — SQL injection safety is a
  stated requirement.
- Prefer editing `app_combined.py` directly; the project deliberately keeps a
  single-file backend.
