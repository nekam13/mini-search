"""Android entry point for Mini Search (Chaquopy).

The Kotlin side calls :func:`start` once, from a background thread, and the
Flask server then serves the whole app on ``127.0.0.1:8070``. The Android UI
loads that address in a WebView: the public search page for everyone, the
admin panel only from the device itself.

Environment overrides are set here *before* ``app_combined`` is imported,
because the module reads its configuration at import time.
"""

import os
import threading

HOST = "127.0.0.1"
PORT = int(os.environ.get("MINISEARCH_PORT", "8070"))

_started = threading.Event()
_lock = threading.Lock()


def _apply_android_defaults():
    """Low-memory, loopback-only defaults for the bundled Android server."""
    os.environ.setdefault("MINISEARCH_PROFILE", "lowmem")
    os.environ.setdefault("MINISEARCH_HOST", HOST)
    os.environ.setdefault("MINISEARCH_ADMIN_LOCAL_ONLY", "1")
    os.environ.setdefault("MINISEARCH_UPDATE_CHECK", "0")
    # No git checkout on a phone: auto-update must not try to touch one.
    os.environ.setdefault("MINISEARCH_WORKERS", "1")


def start(data_dir=None):
    """Start the Mini Search server in a daemon thread (idempotent).

    ``data_dir`` is the app's private storage; the SQLite database and logs are
    kept there so they survive app updates and stay out of shared storage.
    """
    if data_dir:
        os.environ.setdefault("MINISEARCH_DB", os.path.join(data_dir, "console.db"))
        os.environ.setdefault("MINISEARCH_MAINTENANCE_LOG",
                              os.path.join(data_dir, "maintenance.log"))
        os.environ.setdefault("MINISEARCH_ERROR_LOG",
                              os.path.join(data_dir, "errors.log"))
    _apply_android_defaults()
    with _lock:
        if _started.is_set():
            return f"already running on http://{HOST}:{PORT}"

        import app_combined

        thread = threading.Thread(
            target=app_combined.run_server,
            kwargs={"host": HOST, "port": PORT},
            name="minisearch-server",
            daemon=True,
        )
        thread.start()
        _started.set()
        return f"started on http://{HOST}:{PORT}"


def base_url():
    """URL the WebView should load."""
    return f"http://{HOST}:{PORT}/"
