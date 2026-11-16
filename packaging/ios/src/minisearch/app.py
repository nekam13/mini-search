"""Mini Search for iOS / iPadOS.

Starts the very same Flask backend the Android app and the desktop builds use,
then shows it in a WebView. There is no separate search implementation here:
``app_combined.py``, ``templates/`` and ``static/`` are copied into this
package by ``build_ios.sh`` at build time.

The server binds to ``127.0.0.1`` only, so nothing is exposed to the network;
the WebView loads that loopback address.
"""

import os
import threading
import time
import urllib.request

import toga
from toga.style import Pack
from toga.style.pack import COLUMN

HOST = "127.0.0.1"
PORT = int(os.environ.get("MINISEARCH_PORT", "8070"))
READY_TIMEOUT = 60.0

_server_module = None
_lock = threading.RLock()


def _data_dir(app):
    """The app's private, writable storage directory."""
    path = app.paths.data
    os.makedirs(path, exist_ok=True)
    return str(path)


def _apply_ios_defaults(data_dir):
    os.environ.setdefault("MINISEARCH_PROFILE", "lowmem")
    os.environ.setdefault("MINISEARCH_HOST", HOST)
    os.environ.setdefault("MINISEARCH_ADMIN_LOCAL_ONLY", "1")
    os.environ.setdefault("MINISEARCH_UPDATE_CHECK", "0")
    os.environ.setdefault("MINISEARCH_BUNDLED_RUNTIME", "1")
    os.environ.setdefault("MINISEARCH_WORKERS", "1")
    os.environ.setdefault("MINISEARCH_DB", os.path.join(data_dir, "console.db"))
    os.environ.setdefault(
        "MINISEARCH_ERROR_LOG", os.path.join(data_dir, "errors.log"))
    os.environ.setdefault(
        "MINISEARCH_MAINTENANCE_LOG", os.path.join(data_dir, "maintenance.log"))


def _start_server(data_dir):
    global _server_module
    with _lock:
        _apply_ios_defaults(data_dir)
        if _server_module is None:
            import app_combined
            _server_module = app_combined
        _server_module.start_server(host=HOST, port=PORT, block=False)


def _wait_ready(timeout=READY_TIMEOUT):
    """Block until /health answers 200, so the WebView never loads a dead page."""
    deadline = time.time() + timeout
    url = f"http://{HOST}:{PORT}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def main():
    return MiniSearchApp()


class MiniSearchApp(toga.App):
    def startup(self):
        self.main_window = toga.MainWindow(title=self.formal_name)
        self.webview = toga.WebView(style=Pack(flex=1))
        self.status = toga.Label("Spouštím server…",
                                 style=Pack(padding=12, text_align="center"))
        box = toga.Box(style=Pack(direction=COLUMN, flex=1))
        box.add(self.status)
        box.add(self.webview)
        self.main_window.content = box
        self.main_window.show()
        threading.Thread(target=self._boot, daemon=True).start()

    def _boot(self):
        data_dir = _data_dir(self)
        try:
            _start_server(data_dir)
        except Exception as exc:  # show a real message, never a blank WebView
            self.status.text = f"Server se nepodařilo spustit: {exc}"
            return
        if not _wait_ready():
            self.status.text = ("Server neodpovídá. Zkuste aplikaci zavřít "
                                "a otevřít znovu.")
            return
        self.status.text = ""
        self.webview.url = f"http://{HOST}:{PORT}/"

    def on_exit(self):
        if _server_module is not None:
            try:
                _server_module.stop_server()
            except Exception:
                pass
        return True
