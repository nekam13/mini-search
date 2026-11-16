"""Shared desktop launcher for Mini Search (Linux / Windows / macOS).

The Android wrapper (``android_entry.py``) and this launcher both drive the very
same backend in ``app_combined.py`` through ``start_server()`` / ``stop_server()``;
no search logic is duplicated here. What differs is only the host environment:

* where the writable data directory lives (``console.db``, logs),
* how the UI is shown (a native WebView window when ``pywebview`` is available,
  otherwise the user's default browser),
* how the process learns it should quit (SIGINT/SIGTERM, the window closing).

Everything the server needs is decided *before* ``app_combined`` is imported,
because that module reads its configuration at import time.
"""

import argparse
import json
import os
import platform
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

APP_NAME = "mini-search"
APP_TITLE = "Mini Search"
DEFAULT_PORT = 8070
READY_TIMEOUT = 90.0


def _is_frozen():
    """True when running from a PyInstaller bundle (no source tree next to us)."""
    return getattr(sys, "frozen", False)


def app_data_dir():
    """Per-user writable directory for the database, logs and settings.

    Never the current working directory and never the (possibly read-only)
    bundle: a frozen app must not try to write next to its own executable.
    """
    override = os.environ.get("MINISEARCH_DATA_DIR")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "MiniSearch")
    if system == "Darwin":
        return os.path.expanduser("~/Library/Application Support/MiniSearch")
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, APP_NAME)


def configure_environment(data_dir, port, host, lan_share=False, lowmem=None):
    """Point the backend at a private data dir and sane desktop defaults.

    ``setdefault`` is deliberate: an explicit ``MINISEARCH_*`` in the user's
    environment always wins, so the launcher never overrides a deliberate
    configuration.
    """
    os.makedirs(data_dir, exist_ok=True)
    os.environ.setdefault("MINISEARCH_DB", os.path.join(data_dir, "console.db"))
    os.environ.setdefault(
        "MINISEARCH_MAINTENANCE_LOG", os.path.join(data_dir, "maintenance.log"))
    os.environ.setdefault(
        "MINISEARCH_ERROR_LOG", os.path.join(data_dir, "errors.log"))
    os.environ.setdefault("MINISEARCH_PORT", str(port))
    # The desktop server is loopback by default too; LAN sharing is an explicit
    # opt-in (the launcher flag, or the admin setting which overrides this).
    os.environ.setdefault("MINISEARCH_HOST", host)
    # A packaged desktop build is not a git checkout: never try to update itself.
    os.environ.setdefault("MINISEARCH_BUNDLED_RUNTIME", "1")
    os.environ.setdefault("MINISEARCH_UPDATE_CHECK", "0")
    if lan_share:
        os.environ["MINISEARCH_LAN_SHARE"] = "1"
    if lowmem is not None:
        os.environ["MINISEARCH_PROFILE"] = "lowmem" if lowmem else "desktop"


def _free_port(preferred):
    """Return a usable TCP port.

    ``preferred`` is used when it is free; otherwise (or when it is 0 or
    invalid) an ephemeral port is chosen and *that* number is returned — never
    0, which the backend would clamp to 1.
    """
    if preferred and 0 < preferred < 65536:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until_ready(port, host="127.0.0.1", timeout=READY_TIMEOUT, probe=None):
    """Poll ``/health`` over HTTP until it answers 200, or the timeout expires.

    Readiness is a *real HTTP* request, never "the thread object exists". When
    ``probe`` is given it is only consulted to detect an early ``ERROR`` state
    so a failed start does not wait out the whole timeout.

    Returns the parsed JSON status dict on success, ``None`` on timeout.
    """
    deadline = time.time() + timeout
    url = f"http://{host}:{port}/health"
    while time.time() < deadline:
        if probe is not None:
            local = probe() or {}
            if local.get("state") == "ERROR":
                return None
        status = _http_json(url)
        if status and status.get("state") == "RUNNING":
            return status
        time.sleep(0.4)
    return None


def _http_json(url, timeout=5.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


class DesktopApp:
    """Owns the backend lifecycle and the UI window for one process."""

    def __init__(self, port=DEFAULT_PORT, host="127.0.0.1", open_ui=True,
                 lan_share=False, data_dir=None, lowmem=False, window=None):
        self.data_dir = data_dir or app_data_dir()
        self.port = port
        self.host = host
        self.open_ui = open_ui
        self.lan_share = lan_share
        self.lowmem = lowmem
        self.window = window  # 'webview' (default), 'browser', or 'none'
        self.module = None
        self.url = ""
        self._stopping = threading.Event()

    # -- backend ---------------------------------------------------------
    def start(self):
        """Import the backend and start the server; return the readiness status."""
        configure_environment(self.data_dir, self.port, self.host,
                              lan_share=self.lan_share, lowmem=self.lowmem)
        if _is_frozen():
            # In a bundle the package root is unpacked next to the executable.
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import app_combined  # noqa: E402  (imported after env is configured)
        self.module = app_combined
        # ``start_server`` binds before returning, so a port clash raises here.
        self.module.start_server(host=self.host, port=self.port, block=False)
        status = self.module.server_status()
        self.url = status.get("url") or f"http://{self.host}:{self.port}"
        return status

    def stop(self):
        """Stop the server, workers, scheduler, DB and model; allow a restart."""
        if self.module is None:
            return
        try:
            self.module.stop_server()
        except Exception as exc:  # never mask the real shutdown reason
            print(f"Shutdown reported: {exc}", file=sys.stderr)
        finally:
            self.module = None

    # -- UI --------------------------------------------------------------
    def run(self):
        """Start the backend, show the UI, block until the user quits."""
        status = self.start()
        ready = wait_until_ready(self.port, self.host, probe=self._probe)
        if not ready:
            print("Server se nepodařilo nastartovat do limitu.", file=sys.stderr)
            self.stop()
            return 2
        self.url = ready.get("url") or self.url
        print(f"Mini Search běží na {self.url}")
        if ready.get("lan_url"):
            print(f"Sdíleno v síti: {ready['lan_url']}")
        self._install_signal_handlers()
        try:
            self._show_ui(ready)
        finally:
            self.stop()
        return 0

    def _probe(self):
        return self.module.server_status()

    def _show_ui(self, status):
        mode = self.window or ("webview" if _has_webview() else "browser")
        if not self.open_ui or mode == "none":
            self._wait_for_signal()
            return
        if mode == "webview" and _has_webview():
            self._run_webview()
        else:
            self._run_browser()

    def _run_webview(self):
        import webview  # type: ignore

        window = webview.create_window(APP_TITLE, self.url, width=1100, height=800)
        self._window = window
        # ``webview.start`` blocks until the window is closed; stopping the
        # backend afterwards keeps the shutdown order identical to the CLI.
        webview.start()
        self._stopping.set()

    def _run_browser(self):
        import webbrowser

        print("Otevírám výchozí prohlížeč. Ukončete aplikaci klávesou Ctrl+C.")
        webbrowser.open(self.url)
        self._wait_for_signal()

    def _wait_for_signal(self):
        try:
            while not self._stopping.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass

    def _install_signal_handlers(self):
        def handler(signum, frame):  # noqa: ARG001
            self._stopping.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                # Not on the main thread (e.g. a test): skip silently.
                pass


def _has_webview():
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mini-search", description="Mini Search – desktopová aplikace")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("MINISEARCH_PORT", DEFAULT_PORT)),
                        help=f"Port (výchozí {DEFAULT_PORT}, jinak volný)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Adresa pro bind (výchozí 127.0.0.1)")
    parser.add_argument("--lan-share", action="store_true",
                        help="Sdílet veřejné hledání v lokální síti (admin zůstává lokálně)")
    parser.add_argument("--lowmem", action="store_true",
                        help="Úsporný režim (vypne vektorové vyhledávání)")
    parser.add_argument("--no-ui", action="store_true",
                        help="Jen server, bez okna i prohlížeče")
    parser.add_argument("--window", choices=["webview", "browser", "none"],
                        help="Jak zobrazit UI (výchozí: webview, jinak prohlížeč)")
    parser.add_argument("--data-dir", help="Adresář pro data (DB, logy)")
    parser.add_argument("--print-url", action="store_true",
                        help="Vypsat URL a skončit (pro skripty a testy)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    port = _free_port(args.port)
    app = DesktopApp(port=port, host=args.host, open_ui=not args.no_ui,
                     lan_share=args.lan_share, data_dir=args.data_dir,
                     lowmem=args.lowmem, window=args.window)
    if args.print_url:
        status = app.start()
        ready = wait_until_ready(port, args.host, probe=app._probe)
        print((ready or status).get("url", f"http://{args.host}:{port}"))
        app.stop()
        return 0
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
