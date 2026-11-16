"""Desktop launcher: data dir, environment wiring, start/readiness/stop.

Runs against the real backend on loopback (no mocks): it starts the server,
probes ``/health`` over HTTP, then stops and restarts it in the same process.
"""

import json
import os
import shutil
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "desktop"))

import launcher  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"PASS: {name}")
    else:
        FAILED.append(name)
        print(f"FAIL: {name} {detail}")


def _http_status(url, timeout=10.0):
    """GET ``url`` and return ``(status, parsed_json_or_None)``.

    HTML pages (e.g. ``/``) are not JSON, so parsing is best effort: the status
    code is what the caller asserts on for those.
    """
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, None


def main():
    tmp = tempfile.mkdtemp(prefix="minisearch-desktop-")
    port = launcher._free_port(0)
    app = None
    try:
        # -- data dir selection -------------------------------------------
        os.environ["MINISEARCH_DATA_DIR"] = tmp
        check("data dir honours MINISEARCH_DATA_DIR",
              launcher.app_data_dir() == os.path.abspath(tmp))
        del os.environ["MINISEARCH_DATA_DIR"]
        check("data dir is per-user, not the CWD",
              os.path.abspath(launcher.app_data_dir())
              != os.path.abspath(os.getcwd()))
        check("data dir is outside any bundle",
              "MiniSearch" in launcher.app_data_dir()
              or launcher.app_data_dir().endswith("mini-search"))

        # -- environment wiring -------------------------------------------
        launcher.configure_environment(tmp, port, "127.0.0.1", lan_share=False,
                                       lowmem=True)
        check("db lands in the data dir",
              os.environ["MINISEARCH_DB"] == os.path.join(tmp, "console.db"))
        check("bundled runtime disables self-update",
              os.environ["MINISEARCH_BUNDLED_RUNTIME"] == "1")
        check("update check is off for a packaged build",
              os.environ["MINISEARCH_UPDATE_CHECK"] == "0")
        check("lowmem profile set",
              os.environ.get("MINISEARCH_PROFILE") == "lowmem")
        check("loopback host by default",
              os.environ["MINISEARCH_HOST"] == "127.0.0.1")
        check("LAN sharing off unless asked",
              os.environ.get("MINISEARCH_LAN_SHARE") in (None, "0"))

        # -- start, readiness, HTTP --------------------------------------
        app = launcher.DesktopApp(port=port, host="127.0.0.1", open_ui=False,
                                  data_dir=tmp, window="none")
        app.start()
        status = launcher.wait_until_ready(port, "127.0.0.1", timeout=90.0,
                                           probe=app._probe)
        check("server becomes RUNNING", bool(status), f"status={status}")
        workers_before = (status or {}).get("workers")
        check("readiness reports a URL", bool(status and status.get("url")),
              f"url={status and status.get('url')}")
        code, body = _http_status(f"http://127.0.0.1:{port}/health")
        check("GET /health over HTTP is 200", code == 200)
        check("/health says RUNNING", body.get("state") == "RUNNING",
              f"state={body.get('state')}")
        check("db file was created in the data dir",
              os.path.exists(os.path.join(tmp, "console.db")))
        code, _ = _http_status(f"http://127.0.0.1:{port}/")
        check("public search page is served", code == 200)

        # -- stop and restart in the same process ------------------------
        app.stop()
        check("state is STOPPED after stop",
              app.module is None)
        try:
            _http_status(f"http://127.0.0.1:{port}/health", timeout=3.0)
            stopped = False
        except Exception:
            stopped = True
        check("HTTP no longer answers after stop", stopped)

        app.start()
        status2 = launcher.wait_until_ready(port, "127.0.0.1", timeout=90.0,
                                            probe=app._probe)
        check("server restarts in the same process", bool(status2))
        check("no duplicated workers after restart",
              bool(status2) and status2.get("workers") == workers_before,
              f"workers={status2 and status2.get('workers')} (before={workers_before})")
        app.stop()
    finally:
        if app is not None and app.module is not None:
            app.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
