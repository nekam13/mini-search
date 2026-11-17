"""Android entry point for Mini Search (Chaquopy).

The Kotlin side owns the lifecycle and calls into this module from a background
thread:

    start(data_dir)   -> start the bundled Flask server (idempotent)
    stop()            -> stop server, workers, scheduler, DB and model
    restart()         -> stop then start in the same process
    status()          -> dict describing the real runtime state
    ready()           -> HTTP readiness probe used by the Android UI

Environment overrides are applied here *before* ``app_combined`` is imported,
because the module reads its configuration at import time.
"""

import html as html_module
import json
import os
import re
import threading

HOST = "127.0.0.1"
PORT = int(os.environ.get("MINISEARCH_PORT", "8070"))

_lock = threading.RLock()
_server_module = None


def _apply_android_defaults():
    """Low-memory, loopback-only defaults for the bundled Android server."""
    os.environ.setdefault("MINISEARCH_PROFILE", "lowmem")
    os.environ.setdefault("MINISEARCH_HOST", HOST)
    os.environ.setdefault("MINISEARCH_ADMIN_LOCAL_ONLY", "1")
    os.environ.setdefault("MINISEARCH_UPDATE_CHECK", "0")
    os.environ.setdefault("MINISEARCH_WORKERS", "1")
    # The APK must never try to git-pull or pip-install its own code.
    os.environ.setdefault("MINISEARCH_BUNDLED_RUNTIME", "1")


def _set_data_dir(data_dir):
    """Point the database, logs and tokenizer at the app's private storage."""
    if not data_dir:
        return
    os.makedirs(data_dir, exist_ok=True)
    os.environ.setdefault("MINISEARCH_DB", os.path.join(data_dir, "console.db"))
    os.environ.setdefault("MINISEARCH_MAINTENANCE_LOG",
                          os.path.join(data_dir, "maintenance.log"))
    os.environ.setdefault("MINISEARCH_ERROR_LOG",
                          os.path.join(data_dir, "errors.log"))


def _module():
    """Import ``app_combined`` once, after the environment is configured."""
    global _server_module
    with _lock:
        if _server_module is None:
            import app_combined
            _server_module = app_combined
        return _server_module


def start(data_dir=None):
    """Start the server without blocking; returns a JSON status string."""
    _set_data_dir(data_dir)
    _apply_android_defaults()
    with _lock:
        module = _module()
        module.start_server(host=HOST, port=PORT, block=False)
        return json.dumps(module.server_status())


def stop():
    """Stop the server and release its resources; returns a JSON status string."""
    with _lock:
        if _server_module is None:
            return json.dumps({"state": "STOPPED"})
        _server_module.stop_server()
        return json.dumps(_server_module.server_status())


def restart(data_dir=None):
    """Stop and start again in the same process."""
    _set_data_dir(data_dir)
    _apply_android_defaults()
    with _lock:
        module = _module()
        module.restart_server(host=HOST, port=PORT)
        return json.dumps(module.server_status())


def status():
    """Current runtime state (state, workers, embedding backend, error).

    Also carries ``lan_share`` / ``lan_url`` so the Android UI can show the
    address a PC on the same network should open.
    """
    with _lock:
        if _server_module is None:
            return json.dumps({"state": "STOPPED", "error": "", "workers": 0})
        return json.dumps(_server_module.server_status())


def settings_json():
    """Grouped, display-ready settings for the native Android settings screen.

    ``SETTINGS_SPEC`` in ``app_combined`` stays the single source of truth — the
    Kotlin UI renders exactly what this returns, so there is never a second,
    unsynchronised copy of the configuration.
    """
    _apply_android_defaults()
    with _lock:
        module = _module()
        groups = []
        for group in module.settings_for_ui():
            groups.append({
                'key': group['key'],
                'label': group['label'],
                'items': [
                    {
                        'key': item['key'], 'label': item['label'],
                        'help': item['help'], 'kind': item['kind'],
                        'value': item['value'], 'display': item['display'],
                        'choices': list(item['choices']),
                        'min': item['min'], 'max': item['max'],
                        'restart': item['restart'], 'env': item['env'],
                    }
                    for item in group['items']
                ],
            })
        return json.dumps({'groups': groups})


def save_settings_json(payload_json):
    """Persist settings posted from the native screen. Returns JSON.

    The payload is ``{"settings": {key: value, ...}}``. The result carries
    ``restart_needed``: true when a saved item is flagged ``restart`` in
    ``SETTINGS_SPEC``, so Kotlin can re-bind the server instead of leaving the
    user with a change that silently does nothing.
    """
    _apply_android_defaults()
    try:
        payload = json.loads(payload_json or '{}')
    except ValueError:
        return json.dumps({'error': 'Neplatná data nastavení.'})
    updates = payload.get('settings') or {}
    with _lock:
        module = _module()
        # Which restart-flagged keys are actually changing? A save that only
        # touches live settings must not bounce the server.
        restart_needed = False
        for key, raw in updates.items():
            spec = module.SETTINGS_SPEC.get(key)
            if not spec or not spec.get('restart'):
                continue
            new_value = module._coerce_setting(key, raw)
            if new_value is not None and new_value != module.get_setting(key):
                restart_needed = True
                break
        ok, errors = module.save_settings(updates)
        if not ok:
            return json.dumps({'error': '; '.join(errors)})
        return json.dumps({
            'message': 'Nastavení uloženo',
            'restart_needed': restart_needed,
        })


def reset_settings_json():
    """Reset every stored setting to its environment default."""
    _apply_android_defaults()
    with _lock:
        module = _module()
        module.reset_settings()
        return json.dumps({'message': 'Nastavení obnoveno', 'restart_needed': True})


def set_lan_share(enabled):
    """Persist the LAN-sharing flag and return the resulting status.

    The setting lives in ``app_settings`` (the same single source of truth the
    admin panel writes), so it survives restarts. The caller restarts the server
    afterwards because the bind address only changes on start.
    """
    _apply_android_defaults()
    with _lock:
        module = _module()
        module.save_settings({"network.lan_share": "1" if enabled else "0"})
        return json.dumps(module.server_status())


def lan_share_enabled():
    """True when the public search page is currently shared on the LAN."""
    with _lock:
        if _server_module is None:
            return False
        return bool(_server_module.LAN_SHARE_ENABLED)


def ready():
    """True once the HTTP server actually answers on loopback."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def page_count():
    """Number of indexed pages, used by the smoke tests to detect data loss."""
    with _lock:
        if _server_module is None:
            return 0
        row = _server_module.execute_db_fetchone("SELECT COUNT(*) FROM pages")
        return int(row[0]) if row else 0


def sources_json():
    """Display-ready source list for the native "Zdroje" screen.

    Reads the same ``site_sources`` rows the admin panel uses, enriched with the
    parent domain and the last-checked label, so the native screen renders real
    state instead of a second, unsynchronised copy. Returns JSON.
    """
    _apply_android_defaults()
    with _lock:
        module = _module()
        try:
            sources = module.get_all_sources()
        except Exception as exc:  # pragma: no cover - defensive
            return json.dumps({"error": str(exc), "sources": []})
        items = []
        for source in sources:
            items.append({
                "id": source.get("id"),
                "site_id": source.get("site_id"),
                "url": source.get("url") or "",
                "domain": source.get("site_domain") or "",
                "type": source.get("source_type") or "",
                "priority": source.get("priority"),
                "status": source.get("status") or "",
                "last_checked": source.get("last_checked_str") or "",
                "notes": source.get("notes") or "",
            })
        return json.dumps({"sources": items, "count": len(items)})


def add_source_json(payload_json):
    """Register a source typed on the native screen. Returns JSON.

    The payload is ``{"url": "...", "type": "auto"|"url"|"domain"|"sitemap"|
    "rss"|"wiki"}``. ``auto`` detects the type from the address, and a new
    domain is created when needed, so the user only has to paste an address.
    Indexing is kicked off asynchronously. The result carries ``message`` or
    ``error``; a duplicate is reported as a message, not an error.
    """
    _apply_android_defaults()
    try:
        payload = json.loads(payload_json or "{}")
    except ValueError:
        return json.dumps({"error": "Neplatná data."})
    raw_url = (payload.get("url") or "").strip()
    if not raw_url:
        return json.dumps({"error": "Zadejte adresu webu nebo zdroje."})

    with _lock:
        module = _module()
        requested = (payload.get("type") or "auto").strip().lower()
        if requested in ("", "auto"):
            source_type = module.detect_source_type(raw_url, fallback="url")
        else:
            source_type = requested
        if source_type not in module.VALID_SOURCE_TYPES:
            return json.dumps({"error": f"Nepodporovaný typ zdroje: {source_type}"})

        is_wiki = source_type == module.WIKI_SOURCE_TYPE
        if is_wiki:
            site_url, importer, _lang = module.normalize_wiki_source(raw_url)
            if not site_url:
                return json.dumps({"error": "Neplatný odkaz na Wikipedii."})
            value = site_url
        else:
            ok, message = module.validate_url(raw_url)
            if not ok:
                return json.dumps({"error": message})
            value = module.normalize_url(raw_url)

        site_id = module.add_site(value, is_local=False if is_wiki else None)
        if not site_id:
            return json.dumps({"error": "Doménu se nepodařilo založit."})

        source_id, error = module.add_source(site_id, value, source_type=source_type,
                                             importer="dump" if is_wiki else None)
        if source_id is None:
            if error and "již" in error:
                return json.dumps({
                    "message": "Tento zdroj je již zaregistrovaný a průběžně se indexuje."})
            return json.dumps({"error": error or "Zdroj se nepodařilo přidat."})

        module.index_source_async(source_id)
        return json.dumps({"source_id": source_id,
                           "message": "Zdroj byl přidán a indexace běží na pozadí."})


def delete_source_json(source_id):
    """Delete a source (or a whole domain for a ``domain`` source). Returns JSON."""
    _apply_android_defaults()
    try:
        source_id = int(source_id)
    except (TypeError, ValueError):
        return json.dumps({"error": "Neplatné ID zdroje."})
    with _lock:
        module = _module()
        ok, message = module.delete_source(source_id)
        if not ok:
            return json.dumps({"error": message or "Zdroj se nepodařilo smazat."})
        return json.dumps({"message": message or "Zdroj byl smazán."})


def recrawl_source_json(source_id):
    """Queue an immediate re-crawl of one source's site. Returns JSON."""
    _apply_android_defaults()
    try:
        source_id = int(source_id)
    except (TypeError, ValueError):
        return json.dumps({"error": "Neplatné ID zdroje."})
    with _lock:
        module = _module()
        source = module.get_source_by_id(source_id)
        if not source:
            return json.dumps({"error": "Zdroj nenalezen."})
        module.recrawl_async(source["site_id"])
        return json.dumps({"message": "Re-crawl byl spuštěn na pozadí."})


def base_url():
    """URL the WebView should load."""
    return f"http://{HOST}:{PORT}/"


# ---------------------------------------------------------------------------
# Native screens (the app has no WebView): overview, search and site admin.
# Everything below returns display-ready JSON built from the same backend
# helpers the web templates used, so there is still one source of truth.
# ---------------------------------------------------------------------------

def _site_brief(module, site):
    """Flatten one decorated site row into a display dict for the native list."""
    return {
        "id": site.get("id"),
        "url": site.get("canonical_url") or "",
        "status": site.get("status") or "",
        "is_local": bool(site.get("is_local")),
        "indexed": site.get("indexed", 0),
        "pending": site.get("pending", 0),
        "errors": site.get("errors", 0),
        "sources": site.get("sources", 0),
        "max_pages": site.get("max_pages", 0),
        "priority": site.get("search_priority_multiplier", 1.0),
        "last_crawled": site.get("last_crawled_str") or "",
        "aliases": site.get("aliases_list") or [],
    }


def dashboard_json():
    """Overview for the native home screen: global stats plus every site.

    Mirrors ``/admin`` (global counters) and ``/admin/sites`` (per-site cards)
    in one payload, so the native home screen renders real numbers instead of a
    second, unsynchronised copy.
    """
    _apply_android_defaults()
    with _lock:
        module = _module()
        stats = module.get_db_stats()
        sites = [_site_brief(module, site) for site in module.get_all_sites()]
        return json.dumps({
            "stats": {
                "sites": stats.get("sites", 0),
                "pages": stats.get("pages", 0),
                "pending": stats.get("pending", 0),
                "errors": stats.get("errors", 0),
                "sources": stats.get("sources", 0),
            },
            "sites": sites,
            "backend": module.embedding_backend_label(),
        })


def sites_json(search="", status="", local_only=False):
    """Filtered site list for the native "Weby" screen. Returns JSON."""
    _apply_android_defaults()
    with _lock:
        module = _module()
        try:
            sites = module.get_filtered_sites(
                search=search or "", status=status or "", local_only=bool(local_only))
        except Exception as exc:  # pragma: no cover - defensive
            return json.dumps({"error": str(exc), "sites": []})
        return json.dumps({
            "sites": [_site_brief(module, site) for site in sites],
            "count": len(sites),
        })


def site_detail_json(site_id):
    """One site with its sources, recent pages and crawl errors. Returns JSON."""
    _apply_android_defaults()
    try:
        site_id = int(site_id)
    except (TypeError, ValueError):
        return json.dumps({"error": "Neplatné ID webu."})
    with _lock:
        module = _module()
        site = module.get_site_by_id(site_id)
        if not site:
            return json.dumps({"error": "Web nenalezen."})
        module._decorate_site(site)
        sources = []
        for source in module.get_site_sources(site_id):
            sources.append({
                "id": source.get("id"),
                "url": source.get("url") or "",
                "type": source.get("source_type") or "",
                "status": source.get("status") or "",
                "priority": source.get("priority"),
                "last_checked": source.get("last_checked_str") or "",
            })
        pages = []
        for page in module.get_recent_pages(site_id, limit=15):
            pages.append({
                "title": _plain_text(page.get("title")),
                "url": page.get("url") or "",
                "indexed_at": page.get("indexed_at_str") or "",
            })
        errors = []
        for row in module.execute_db_fetchall(
            "SELECT url, error_reason, retry_count FROM crawl_queue "
            "WHERE site_id = ? AND status = 'error' ORDER BY id DESC LIMIT 20",
            (site_id,),
        ):
            errors.append({
                "url": row[0] or "",
                "reason": row[1] or "",
                "retries": row[2] or 0,
            })
        return json.dumps({
            "site": _site_brief(module, site),
            "sources": sources,
            "pages": pages,
            "errors": errors,
            "max_pages_min": module.MAX_PAGES_MIN,
            "max_pages_max": module.MAX_PAGES_MAX,
            "statuses": list(module.VALID_SITE_STATUSES),
        })


def update_site_json(payload_json):
    """Save the editable site fields from the native editor. Returns JSON."""
    _apply_android_defaults()
    try:
        payload = json.loads(payload_json or "{}")
    except ValueError:
        return json.dumps({"error": "Neplatná data."})
    try:
        site_id = int(payload.get("id"))
    except (TypeError, ValueError):
        return json.dumps({"error": "Neplatné ID webu."})
    with _lock:
        module = _module()
        ok, message = module.update_site(
            site_id,
            max_pages=payload.get("max_pages"),
            status=payload.get("status"),
            aliases=payload.get("aliases"),
            is_local=payload.get("is_local"),
            search_priority_multiplier=payload.get("priority"),
        )
        if not ok:
            return json.dumps({"error": message or "Web se nepodařilo uložit."})
        return json.dumps({"message": message or "Web byl uložen."})


def delete_site_json(site_id):
    """Delete a site and everything under it. Returns JSON."""
    _apply_android_defaults()
    try:
        site_id = int(site_id)
    except (TypeError, ValueError):
        return json.dumps({"error": "Neplatné ID webu."})
    with _lock:
        module = _module()
        if not module.get_site_by_id(site_id):
            return json.dumps({"error": "Web nenalezen."})
        module.delete_site(site_id)
        return json.dumps({"message": "Web byl smazán."})


def recrawl_site_json(site_id):
    """Queue an immediate re-crawl of a whole site. Returns JSON."""
    _apply_android_defaults()
    try:
        site_id = int(site_id)
    except (TypeError, ValueError):
        return json.dumps({"error": "Neplatné ID webu."})
    with _lock:
        module = _module()
        ok, message = module.recrawl_async(site_id)
        if not ok:
            return json.dumps({"error": message or "Re-crawl se nepodařilo spustit."})
        return json.dumps({"message": message or "Re-crawl byl spuštěn."})


def _set_all_status(status):
    _apply_android_defaults()
    with _lock:
        module = _module()
        if status == "paused":
            module.execute_db(
                "UPDATE sites SET status='paused' WHERE status='active'", commit=True)
            return json.dumps({"message": "Všechny aktivní weby byly pozastaveny."})
        module.execute_db(
            "UPDATE sites SET status='active' WHERE status='paused'", commit=True)
        return json.dumps({"message": "Všechny pozastavené weby byly obnoveny."})


def pause_all_json():
    """Pause every active site (native quick action). Returns JSON."""
    return _set_all_status("paused")


def resume_all_json():
    """Resume every paused site (native quick action). Returns JSON."""
    return _set_all_status("active")


def recrawl_all_json():
    """Queue a re-crawl of every active site on a background thread. Returns JSON."""
    _apply_android_defaults()
    with _lock:
        module = _module()
        threading.Thread(target=module.recrawl_all_sites,
                         name="minisearch-recrawl-all", daemon=True).start()
        return json.dumps({"message": "Re-crawl všech aktivních webů byl spuštěn."})


def _plain_text(value):
    """Strip HTML tags from a string bound for a native TextView.

    The web template escapes and highlights the snippet itself; a native
    ``TextView`` has no HTML step, so any leftover markup (a page whose body
    carried a literal ``<script>`` block) would show up as literal angle
    brackets. The native bridge is the display boundary, so it flattens the
    text here.
    """
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    text = html_module.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def search_json(payload_json):
    """Run a hybrid search for the native search screen. Returns JSON.

    The payload is ``{"q": "...", "filter": "all"|"articles"|"podcasts"|
    "audio"|"price", "limit": N}``. Results carry plain-text fields (no HTML),
    because the native screen renders them itself; the snippet is produced by
    the same ``_result_snippet`` helper the web page uses, so the windowing and
    diacritic-insensitive matching are identical.
    """
    _apply_android_defaults()
    try:
        payload = json.loads(payload_json or "{}")
    except ValueError:
        return json.dumps({"error": "Neplatná data."})
    query = (payload.get("q") or "").strip()
    if not query:
        return json.dumps({"results": [], "count": 0, "query": ""})
    filter_type = (payload.get("filter") or "all").strip()
    try:
        limit = int(payload.get("limit") or 25)
    except (TypeError, ValueError):
        limit = 25
    limit = max(1, min(limit, 100))
    with _lock:
        module = _module()
        raw = module.hybrid_search(
            query, limit=limit,
            filter_type=filter_type if filter_type != "all" else None)
        prepared = module.prepare_results(raw, query)
        results = []
        for item in prepared:
            results.append({
                "title": _plain_text(item.get("display_title")),
                "url": item.get("url") or "",
                "domain": item.get("domain") or "",
                "path": item.get("display_url_path") or "",
                "snippet": _plain_text(module._result_snippet(item, query)),
                "relevance": item.get("relevance_pct") or 0,
                "date": item.get("display_date") or "",
                "card_type": item.get("card_type") or "",
                "badge": item.get("card_badge") or "",
                "thumb": item.get("thumb") or "",
            })
        return json.dumps({"results": results, "count": len(results), "query": query})


def news_json(limit=12):
    """Most recently indexed pages, for the native search screen's empty state."""
    _apply_android_defaults()
    with _lock:
        module = _module()
        items = []
        for page in module.latest_indexed_pages(limit=limit):
            items.append({
                "title": _plain_text(page.get("og_title") or page.get("title")
                                     or page.get("url") or ""),
                "url": page.get("url") or "",
                "domain": page.get("site_domain") or "",
            })
        return json.dumps({"items": items})


def _wiki_dump_url(path):
    """Turn a private-storage dump path into the ``file://`` URL the importer wants."""
    if path.startswith("file://"):
        return path
    return "file://" + os.path.abspath(path)


def import_wiki_dump(path, max_pages=None):
    """Import a Wikipedia dump that Kotlin copied into private storage.

    The Android picker hands out a ``content://`` URI, which is not a filesystem
    path; Kotlin copies the stream into ``filesDir`` first and passes the real
    path here. The source is registered as a wiki dump source (so it is resumable
    and idempotent like any other import) and the work runs on a background
    thread, so the HTTP request that triggered it never blocks. Returns JSON.
    """
    _apply_android_defaults()
    if not path or not os.path.exists(path):
        return json.dumps({"error": "Dump nebyl nalezen."})
    with _lock:
        module = _module()
        source_url = _wiki_dump_url(path)
        source_id, error = module.add_source(
            0, source_url, module.WIKI_SOURCE_TYPE, importer="dump")
        if source_id is None:
            existing = module.execute_db_fetchone(
                "SELECT id FROM site_sources WHERE url = ? AND source_type = ?",
                (source_url, module.WIKI_SOURCE_TYPE))
            if existing:
                source_id = existing[0]
            else:
                return json.dumps({"error": error or "Zdroj nelze vytvořit."})

        def _run():
            try:
                source = module.get_source_by_id(source_id)
                module.run_wiki_import(source, max_pages=max_pages)
            except Exception as exc:  # pragma: no cover - background best effort
                module.log_error("Wiki dump import failed", exc)

        threading.Thread(target=_run, name="minisearch-wiki-import",
                         daemon=True).start()
        return json.dumps({"source_id": source_id,
                           "message": "Import dumpu byl spuštěn na pozadí."})
