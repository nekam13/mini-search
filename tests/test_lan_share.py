#!/usr/bin/env python3
"""Regression tests for optional LAN sharing of the public search page.

The feature lets a PC on the same network open the search page while the admin
panel stays loopback-only. These tests drive the real server and, where the
container has a non-loopback address, make a genuine HTTP request from it —
never from 127.0.0.1 — so "LAN reachable" is proven rather than asserted.

Covered:
  * LAN sharing is off by default and the bind stays loopback;
  * with it on the search page answers on the machine's LAN address while
    /admin still refuses that remote client;
  * the setting persists in app_settings and survives a restart;
  * the reported lan_url matches the address a PC would use;
  * a public Origin cannot drive a mutating request through a LAN-bound server.
"""
import json
import os
import socket
import sys
import tempfile
import time
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name} {detail}")


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_state(module, wanted, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if module.server_status()["state"] == wanted:
            return True
        time.sleep(0.1)
    return False


def _http(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return None, str(e)


def _lan_address():
    """The container's non-loopback IPv4 address, or None when there is none."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            addr = probe.getsockname()[0]
        finally:
            probe.close()
    except Exception:
        return None
    if not addr or addr.startswith("127."):
        return None
    return addr


def main():
    tmpdir = tempfile.mkdtemp()
    os.environ["MINISEARCH_DB"] = os.path.join(tmpdir, "console.db")
    os.environ["MINISEARCH_MAINTENANCE_LOG"] = os.path.join(tmpdir, "maintenance.log")
    os.environ["MINISEARCH_ERROR_LOG"] = os.path.join(tmpdir, "errors.log")
    os.environ["MINISEARCH_EMBEDDING_BACKEND"] = "hash"
    os.environ["MINISEARCH_WORKERS"] = "1"
    os.environ["MINISEARCH_UPDATE_CHECK"] = "0"
    os.environ["MINISEARCH_RECRAWL_FEED_HOURS"] = "0"
    os.environ["MINISEARCH_RECRAWL_SITE_HOURS"] = "0"
    os.environ["MINISEARCH_RECRAWL_SITEMAP_HOURS"] = "0"
    # The admin panel must stay loopback-only, exactly as the APK sets it.
    os.environ["MINISEARCH_ADMIN_LOCAL_ONLY"] = "1"

    import app_combined as m

    m.DB_PATH = os.path.join(tmpdir, "console.db")

    # ------------------------------------------------------------------
    # 1. Off by default; the bind stays loopback
    # ------------------------------------------------------------------
    check("LAN sharing is off by default", m.LAN_SHARE_ENABLED is False,
          repr(m.LAN_SHARE_ENABLED))
    check("lan_url is empty while off", m.lan_share_url() == "",
          repr(m.lan_share_url()))

    port = _free_port()
    m.start_server(host="127.0.0.1", port=port, block=False)
    check("server reaches RUNNING", _wait_for_state(m, "RUNNING"),
          str(m.server_status()))
    status = m.server_status()
    check("status reports lan_share false", status.get("lan_share") is False, str(status))
    check("status reports the loopback host", m._server_host == "127.0.0.1",
          repr(m._server_host))

    # ------------------------------------------------------------------
    # 2. While bound to loopback, only loopback Origins are accepted: a LAN
    #    page reaching our loopback server is refused too (no DNS-rebinding hole)
    # ------------------------------------------------------------------
    client = m.app.test_client()
    resp = client.post(
        "/admin/api/sources",
        data=json.dumps({"url": "http://127.0.0.1/x", "source_type": "url"}),
        content_type="application/json",
        headers={"Origin": "http://evil.example.com"})
    check("public Origin refused", resp.status_code == 403,
          str(resp.status_code))
    resp = client.post(
        "/admin/api/sources",
        data=json.dumps({"url": "http://127.0.0.1/x", "source_type": "url"}),
        content_type="application/json",
        headers={"Origin": "http://192.168.10.200:8070"})
    check("LAN Origin refused while bound to loopback", resp.status_code == 403,
          str(resp.status_code))
    resp = client.post(
        "/admin/api/sources",
        data=json.dumps({"url": "http://127.0.0.1/x", "source_type": "url"}),
        content_type="application/json",
        headers={"Origin": f"http://127.0.0.1:{port}"})
    check("same-origin mutation still allowed", resp.status_code != 403,
          str(resp.status_code))

    m.stop_server()

    # ------------------------------------------------------------------
    # 3. Turning LAN sharing on persists and rebinds to 0.0.0.0
    # ------------------------------------------------------------------
    ok, errors = m.save_settings({"network.lan_share": "1"})
    check("saving the setting succeeds", ok, str(errors))
    check("global flipped to on", m.LAN_SHARE_ENABLED is True, repr(m.LAN_SHARE_ENABLED))

    m.start_server(host="127.0.0.1", port=port, block=False)
    check("restart reaches RUNNING", _wait_for_state(m, "RUNNING"),
          str(m.server_status()))
    # LAN sharing overrides the loopback host the Android wrapper passes.
    check("LAN mode overrides the loopback host", m._server_host == "0.0.0.0",
          repr(m._server_host))

    # Bound beyond loopback, a same-network Origin is accepted while an
    # arbitrary public Origin is still refused.
    client = m.app.test_client()
    resp = client.post(
        "/admin/api/sources",
        data=json.dumps({"url": "http://127.0.0.1/x", "source_type": "url"}),
        content_type="application/json",
        headers={"Origin": "http://192.168.10.200:8070"})
    check("LAN Origin accepted while LAN-bound", resp.status_code != 403,
          str(resp.status_code))
    resp = client.post(
        "/admin/api/sources",
        data=json.dumps({"url": "http://127.0.0.1/x", "source_type": "url"}),
        content_type="application/json",
        headers={"Origin": "http://evil.example.com"})
    check("public Origin still refused while LAN-bound", resp.status_code == 403,
          str(resp.status_code))

    ok, body = _http(f"http://127.0.0.1:{port}/")
    check("search page answers on loopback", ok == 200, str(ok))
    ok, body = _http(f"http://127.0.0.1:{port}/health")
    check("/health still answers", ok == 200, str(ok))

    # ------------------------------------------------------------------
    # 4. A real remote request from the machine's LAN address
    # ------------------------------------------------------------------
    lan = _lan_address()
    if lan:
        ok, body = _http(f"http://{lan}:{port}/")
        check(f"search page answers on the LAN address {lan}", ok == 200, str(ok))
        check("the shared page is the real search UI",
              "Mini Search" in body or "hled" in body.lower(), body[:120])

        ok, body = _http(f"http://{lan}:{port}/admin")
        check("admin refuses a remote LAN client", ok == 403, str(ok))
        ok, body = _http(f"http://{lan}:{port}/admin/api/sites")
        check("admin API refuses a remote LAN client", ok == 403, str(ok))

        status = m.server_status()
        check("status exposes a usable lan_url",
              status.get("lan_url", "").startswith("http://"),
              str(status.get("lan_url")))
        check("lan_url points at the LAN address and port",
              status.get("lan_url") == f"http://{lan}:{port}/",
              str(status.get("lan_url")))
    else:
        print("SKIP: no non-loopback address available; LAN reachability unverified")

    # Admin still reachable from loopback
    ok, _ = _http(f"http://127.0.0.1:{port}/admin")
    check("admin reachable from loopback", ok == 200, str(ok))

    # ------------------------------------------------------------------
    # 5. The setting survives a stop/start (persisted in app_settings)
    # ------------------------------------------------------------------
    m.stop_server()
    check("global stays on after stop", m.LAN_SHARE_ENABLED is True,
          repr(m.LAN_SHARE_ENABLED))
    m.get_db()
    row = m.execute_db_fetchone(
        "SELECT value FROM app_settings WHERE key = 'network.lan_share'")
    check("setting persisted in app_settings", row is not None and row[0] == "1",
          str(row))
    m.stop_server()

    # ------------------------------------------------------------------
    # 6. Turning it off again restores the loopback bind
    # ------------------------------------------------------------------
    ok, errors = m.save_settings({"network.lan_share": "0"})
    check("disabling the setting succeeds", ok, str(errors))
    check("global flipped back to off", m.LAN_SHARE_ENABLED is False,
          repr(m.LAN_SHARE_ENABLED))
    m.start_server(host="127.0.0.1", port=port, block=False)
    check("restart after disabling reaches RUNNING", _wait_for_state(m, "RUNNING"),
          str(m.server_status()))
    check("host is loopback again", m._server_host == "127.0.0.1", repr(m._server_host))
    if lan:
        ok, _ = _http(f"http://{lan}:{port}/")
        check("LAN address no longer serves the page", ok != 200, str(ok))
    m.stop_server()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("ALL LAN SHARE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
