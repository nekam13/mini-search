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
python3 tests/test_local_sites.py            # local-network detection, robots bypass, boost
python3 tests/test_local_indexing_smoke.py   # live local HTTP server: crawl + index end to end
python3 tests/test_wiki_import.py            # wiki dump/API importer, idempotency, resume
python3 tests/test_crawl_metadata.py         # Schema.org/OG, charset, retry/backoff, lowmem
python3 tests/test_rich_cards.py             # Rich Result cards, XSS, self-migration + cleanup
python3 tests/test_maintenance.py            # nightly maintenance: dedup, vectors, wiki, dead links
```

All suites are deterministic and offline: network behaviour runs against a
throwaway local HTTP server, and the wiki importer test writes a small bz2 dump
to a temp dir. Never point tests at the live Wikipedia.

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

### Rich Result cards

`prepare_results()` also sets `card_type` (`wiki` / `product` / `recipe` /
`organization` / `article`) via `_rich_card_fields()`, plus every structured
value already formatted for display (`card_badge`, `price_display`,
`availability`, `rating_value`/`rating_pct`, `time_display`, `calories`,
`address`, `phone`, `rich_metadata`, `breadcrumbs`). The template only picks a
class (`card-<type>`) and prints them — never parse `schema_details` in Jinja.

Rules when extending this:

- Every rich value must be a plain pre-formatted string; autoescaping is the only
  XSS defence, so never mark a rich field `|safe`.
- Image URLs must pass `_safe_image_url()` (http(s) / root-relative only) before
  reaching an `src` attribute.
- Card classification is best-effort; an unknown page must fall back to
  `article`, never raise.
- Card CSS lives in `clay.css` (`.card-rich`, `.card-wiki`, `.price-tag`,
  `.rating-stars`, `.rich-metadata`, `.card-thumbnail`, `.card-media-layout`…)
  and must use the existing tokens. Keep `class="result-item"` on the outer
  `<article>`: the search-page tests count that exact string.

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

## Czech Wikipedia importer

`wiki` is a source type served by the importer, not the crawler.
`normalize_wiki_source(url, importer)` maps `cs` / `wiki:cs` /
`cs.wikipedia.org` / `file:///…/cswiki-….xml.bz2` onto `(site_url, importer,
lang)`; the stored source URL is the language selector (or the dump path), and
`lang_from_wiki_url()` infers the language from a dump filename.

`run_wiki_import(source, max_pages, allow_indexed_refresh)` streams articles one
at a time through `_iter_wiki_articles()`, which dispatches to
`_iter_wiki_articles_dump()` (pull-mode `xml.etree.iterparse`, never loads the
whole dump) or `_iter_wiki_articles_api()` (batched `allpages` + `extracts`).
Facts to preserve:

- Import is **idempotent** via `url_hash` dedup; progress (`next_title`,
  `imported`) lives in `site_sources.import_state` so a paused/capped run
  resumes instead of restarting.
- Redirects become **site aliases**, not pages; non-encyclopaedic namespaces are
  skipped (`_WIKI_SKIP_NAMESPACES`).
- `max_pages` is honoured, and `index_source()` skips `status='paused'` sources.
- `phase_1_discovery()` returns early for a site that already has a wiki source,
  so the generic crawler never touches wikipedia.org.
- `xml.etree` elements are falsy when childless — use `_xml_child()` instead of
  `find(...) or find(...)`, which silently drops `<text>`.
- CLI: `python3 app_combined.py --import-wiki …` / `--source-id N` runs an
  import without starting the server.

## Crawler metadata

`extract_page_content()` parses the *decoded* text (not raw bytes) so a
`windows-1250` Czech page is not mangled; raw bytes still go to `_extract_jsonld`.
`_extract_jsonld()` tolerates malformed blocks by falling back to per-script
parsing (`_jsonld_scripts`). Transient HTTP statuses are retried centrally in
`_http_get()` (`RETRYABLE_HTTP_STATUSES`, `_retry_delay` / `_retry_after_delay`),
so callers must not re-implement backoff.

## Low-memory mode

`MINISEARCH_PROFILE=lowmem` (or `MINISEARCH_LOWMEM=1`) sets `LOW_MEMORY_MODE`,
which disables embeddings and drops the worker count. `embeddings_enabled()` is
the runtime check: `get_hnsw_index()` returns `None`, `generate_embedding()`
returns `None` and `vector_search()` returns `[]` when off, so `hybrid_search()`
falls back to FTS5. `_recover_interrupted_queue()` runs at `start_workers()`
and returns `locked` rows to `pending` after a crash.

## Nightly maintenance ("dreaming mode")

`run_maintenance()` runs one bounded pass in four stages, each independently
guarded and interruptible via `SHUTDOWN_FLAG`; the CLI entry point is
`--maintenance` / `--nightly` (`_cli_maintenance`), and `maintenance_scheduled()`
is the optional background job (`MINISEARCH_NIGHTLY=1`). A concurrent second run
is refused by `_maintenance_lock`.

- `maintenance_dedup_pages()` merges rows sharing a `url_hash` (exact URL dupe,
  keep newest) and rows with an identical `_content_signature()` (keep oldest).
  `_content_signature` skips bodies under `CONTENT_DUP_MIN_CHARS`/20 tokens, so
  short boilerplate never collapses. Deletes rely on the existing FTS5 delete
  trigger — do **not** delete from `pages` while bypassing triggers.
- `maintenance_optimize_db()` always runs `PRAGMA optimize`; `VACUUM` only when
  `_available_disk_mb()` clears `MAINTENANCE_VACUUM_MIN_MB` (and 1.2× the DB
  size), because VACUUM needs the DB's size in free space. It takes `_db_lock`
  and flips `isolation_level` to `None` — VACUUM cannot run in a transaction.
- `maintenance_backfill_embeddings()` only writes rows where
  `embedding IS NULL`, and re-checks `embeddings_enabled()` each iteration so a
  mid-run resource drop stops cleanly (the rest is picked up next night).
- `maintenance_import_wiki()` reuses `run_wiki_import()` (resume via
  `next_title`, skips `status='paused'`), bounded by `--max-pages`.
- `maintenance_check_dead_links()` probes oldest pages with HEAD, falls back to
  GET on 405, and only treats 404/410 as dead (403/5xx are inconclusive). It is
  opt-in via `MINISEARCH_MAINT_LINK_CHECK`; `MINISEARCH_MAINT_PURGE_DEAD=1`
  removes dead rows. New columns `pages.link_status` / `pages.last_link_check`
  are added by the in-place migration in `_migrate_db`.

Progress lines go through `maintenance_log()` to `logs/maintenance.log`
(gitignored), mirroring the best-effort contract of `log_error()`.

## Site stats keys

`get_source_stats(site_id)` is the single source of truth for a site's numbers
and returns `indexed`, `pending`, `errors`, `sources`, `total` — those are the
names templates read (`site.indexed`, `stats.sources`, …). `get_all_sites()` and
`get_filtered_sites()` must both merge that dict in; do not reintroduce the older
`indexed_count` / `pending_count` names, which never matched the templates.

## Local-network sites

`is_local_url(url)` / `extract_host(value)` decide whether an address lives on a
private network (localhost, private/CGNAT ranges, single-label hosts, `.local`,
`.lan`, `.internal`, `.home.arpa`). This flag rides along on `sites.is_local`.

Three behaviours depend on it, so keep them consistent:

- **robots.txt bypass** вЂ” `is_allowed(url, site_id, is_local)` returns `True` for
  local targets, because dev servers commonly serve `Disallow: /`. Callers that
  know the site should pass `site_id`/`is_local` instead of relying on the URL.
- **Search boost** вЂ” `sites.search_priority_multiplier` multiplies the final score.
  Local sites default to `LOCAL_SITE_PRIORITY_MULTIPLIER` (3.0), public to
  `PUBLIC_SITE_PRIORITY_MULTIPLIER` (1.0). `_site_priority_multiplier()` is the
  lookup; `vector_search()` and `hybrid_search()` both apply it *before* the final
  sort (the vector index returns approximate neighbours, so re-sorting matters).
- **Queue priority** вЂ” the crawl worker orders by `priority ASC`, so a *lower*
  number is crawled sooner. `_queue_url()` clamps local URLs to
  `LOCAL_QUEUE_PRIORITY` (1) and `add_source()` promotes the parent domain to
  local whenever a local child source is added.

`_decorate_site(site)` is the single place that adds derived display fields
(stats, parsed aliases, `is_local`, `search_priority_multiplier`); routes must
use it rather than re-deriving those keys by hand.

## Environment notes

- `sentence_transformers` is usually unavailable, so vector search silently
  falls back to FTS5. Tests must not assume vector search works.
- Expected console noise: `Model nelze nacist: ...` and `FTS5 table backfilled ...`.
- Existing databases are migrated in place; never drop `console.db`. Migration
  runs automatically inside `get_db()` and `run_self_migration()` only deletes a
  leftover `site_sources_old` after the new shape and the `wiki` CHECK are both
  verified and no rows were lost.
- `MINISEARCH_EMBEDDINGS` accepts `auto` (default in lowmem mode): embeddings are
  kept unless free RAM (`/proc/meminfo` or `os.sysconf`) is below
  `MINISEARCH_MIN_FREE_MB` or the battery is low and unplugged. Probes return
  `None`/unknown on platforms that expose neither, which must never block vector
  search. Tests patch `_available_memory_mb()` / `_battery_status()` rather than
  touching real hardware.

## Conventions

- Czech for all user-facing text, comments and docs.
- Parameterised SQL only (`?` placeholders) — SQL injection safety is a
  stated requirement.
- Prefer editing `app_combined.py` directly; the project deliberately keeps a
  single-file backend.
