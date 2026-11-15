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

import json
import os
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
    """Current runtime state (state, workers, embedding backend, error)."""
    with _lock:
        if _server_module is None:
            return json.dumps({"state": "STOPPED", "error": "", "workers": 0})
        return json.dumps(_server_module.server_status())


def ready():
    """True once the HTTP server actually answers on loopback."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def base_url():
    """URL the WebView should load."""
    return f"http://{HOST}:{PORT}/"


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
