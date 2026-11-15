#!/usr/bin/env python3
"""Regression tests for the server lifecycle the Android wrapper drives.

Covers the things the app relies on and that a naive "spawn a thread" start
would get wrong:

  * start reaches RUNNING only after ``/health`` really answers;
  * a repeated start is a no-op (no second server / scheduler / workers);
  * stop tears everything down and allows a fresh start in the same process;
  * data survives a stop/start cycle (no database is dropped);
  * the admin API stays loopback-only and cross-site mutations are refused;
  * the embedding backend and vector space are identified, and a hash-space
    vector is never treated as a real-model vector.

The HTTP parts run against the real server on loopback; no external network is
touched.
"""
import json
import os
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
    import socket
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


def _http_ok(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200, resp.read().decode("utf-8", "replace")
    except Exception as e:
        return False, str(e)


def main():
    tmpdir = tempfile.mkdtemp()
    os.environ["MINISEARCH_DB"] = os.path.join(tmpdir, "console.db")
    os.environ["MINISEARCH_MAINTENANCE_LOG"] = os.path.join(tmpdir, "maintenance.log")
    os.environ["MINISEARCH_ERROR_LOG"] = os.path.join(tmpdir, "errors.log")
    os.environ["MINISEARCH_EMBEDDING_BACKEND"] = "hash"
    os.environ["MINISEARCH_WORKERS"] = "1"
    os.environ["MINISEARCH_UPDATE_CHECK"] = "0"
    # 0 disables the expensive background jobs entirely for this test.
    os.environ["MINISEARCH_RECRAWL_FEED_HOURS"] = "0"
    os.environ["MINISEARCH_RECRAWL_SITE_HOURS"] = "0"
    os.environ["MINISEARCH_RECRAWL_SITEMAP_HOURS"] = "0"

    import app_combined as m

    m.DB_PATH = os.path.join(tmpdir, "console.db")

    # ------------------------------------------------------------------
    # 1. Clean start reaches RUNNING and answers /health
    # ------------------------------------------------------------------
    port = _free_port()
    check("initial state is STOPPED", m.server_status()["state"] == "STOPPED",
          str(m.server_status()))

    m.start_server(host="127.0.0.1", port=port, block=False)
    check("state is RUNNING after start", _wait_for_state(m, "RUNNING"),
          str(m.server_status()))
    ok, body = _http_ok(f"http://127.0.0.1:{port}/health")
    check("/health answers 200", ok, body[:200])
    try:
        status = json.loads(body)
        check("/health reports RUNNING", status.get("state") == "RUNNING", str(status))
        check("/health reports the embedding backend",
              "embeddings" in status, str(status))
    except Exception as e:
        check("/health returns JSON", False, str(e))

    # ------------------------------------------------------------------
    # 2. A repeated start is a no-op (idempotent)
    # ------------------------------------------------------------------
    first_handle = m._server_handle
    worker_count = len([t for t in m._worker_threads if t.is_alive()])
    again = m.start_server(host="127.0.0.1", port=port, block=False)
    check("second start is a no-op", again is False, repr(again))
    check("no second server handle", m._server_handle is first_handle,
          "handle changed")
    check("no duplicated workers",
          len([t for t in m._worker_threads if t.is_alive()]) == worker_count,
          f"{worker_count} -> {len([t for t in m._worker_threads if t.is_alive()])}")
    check("still RUNNING after repeated start",
          m.server_status()["state"] == "RUNNING", str(m.server_status()))

    # ------------------------------------------------------------------
    # 3. Admin API is loopback-only; cross-site mutations are refused
    # ------------------------------------------------------------------
    client = m.app.test_client()
    resp = client.get("/admin/api/status")
    check("admin API reachable from loopback", resp.status_code == 200,
          str(resp.status_code))

    resp = client.post(
        "/admin/api/sources",
        data=json.dumps({"url": "http://127.0.0.1/x", "source_type": "url"}),
        content_type="application/json",
        headers={"Origin": "http://evil.example.com"})
    check("cross-site mutation refused", resp.status_code == 403,
          str(resp.status_code))

    resp = client.post(
        "/admin/api/sources",
        data=json.dumps({"url": "http://127.0.0.1/x", "source_type": "url"}),
        content_type="application/json",
        headers={"Origin": f"http://127.0.0.1:{port}"})
    check("same-origin mutation allowed", resp.status_code != 403,
          str(resp.status_code))

    # ------------------------------------------------------------------
    # 3b. A bundled runtime hides the misleading git-update controls
    # ------------------------------------------------------------------
    m.BUNDLED_RUNTIME = True
    bundled_html = client.get("/admin").get_data(as_text=True)
    check("bundled runtime hides the update card",
          "Stav aktualizací" not in bundled_html, "update card still shown")
    check("bundled runtime hides the check-update action",
          "/admin/check-update" not in bundled_html, "link still shown")
    m.BUNDLED_RUNTIME = False

    # ------------------------------------------------------------------
    # 4. Stop tears everything down
    # ------------------------------------------------------------------
    check("stop returns True", m.stop_server() is True)
    check("state is STOPPED after stop",
          _wait_for_state(m, "STOPPED"), str(m.server_status()))
    check("no HTTP handle left", m._server_handle is None)
    check("no scheduler left", m._scheduler is None, str(m._scheduler))
    check("no live workers after stop",
          not [t for t in m._worker_threads if t.is_alive()],
          str([t.name for t in m._worker_threads if t.is_alive()]))
    ok, _ = _http_ok(f"http://127.0.0.1:{port}/health")
    check("server no longer answers after stop", not ok)

    # ------------------------------------------------------------------
    # 5. Data survives a stop/start cycle, and the DB reopens
    # ------------------------------------------------------------------
    m.get_db()
    m.execute_db("INSERT INTO sites (canonical_url, status) VALUES (?, 'active')",
                 ("http://127.0.0.1/keepme",))
    check("site row written", m.execute_db_fetchone(
        "SELECT COUNT(*) FROM sites WHERE canonical_url = ?",
        ("http://127.0.0.1/keepme",))[0] == 1)

    m.start_server(host="127.0.0.1", port=port, block=False)
    check("restart reaches RUNNING", _wait_for_state(m, "RUNNING"),
          str(m.server_status()))
    ok, body = _http_ok(f"http://127.0.0.1:{port}/health")
    check("/health answers after restart", ok, body[:120])
    check("site row survived the restart", m.execute_db_fetchone(
        "SELECT COUNT(*) FROM sites WHERE canonical_url = ?",
        ("http://127.0.0.1/keepme",))[0] == 1)
    check("no duplicated workers after restart",
          len([t for t in m._worker_threads if t.is_alive()]) <= max(worker_count, 1),
          str(len([t for t in m._worker_threads if t.is_alive()])))

    # ------------------------------------------------------------------
    # 6. Embedding backend identity and vector-space separation
    # ------------------------------------------------------------------
    check("hash backend label is honest",
          "hash" in m.embedding_backend_label().lower(),
          m.embedding_backend_label())
    check("hash backend is not advertised as a real model",
          m.get_model() is not None and not m.get_model().is_real_model)
    check("active space identifies the hash space",
          m.active_embedding_space().startswith("hash:"),
          m.active_embedding_space())
    check("the real-model space differs from the hash space",
          m.MINILM_SPACE != m.HASH_EMBEDDING_SPACE,
          f"{m.MINILM_SPACE} == {m.HASH_EMBEDDING_SPACE}")

    # A row in the hash space must not be counted as a real-model vector.
    vec = m.generate_embedding("testovací text")
    check("generate_embedding returns a vector", vec is not None)
    if vec is not None:
        check("vector has the expected dimension",
              int(vec.shape[0]) == m.HASH_EMBEDDING_DIM, str(vec.shape))

    # ------------------------------------------------------------------
    # 6b. The Android dump entry imports a file:// path and stays idempotent
    # ------------------------------------------------------------------
    import bz2
    dump_path = os.path.join(tmpdir, "cswiki-latest-pages-articles.xml.bz2")
    with bz2.open(dump_path, "wb") as handle:
        handle.write(
            b"<?xml version=\"1.0\"?>\n<mediawiki>"
            b"<page><title>Praha</title><ns>0</ns>"
            b"<revision><text>Praha je hlavni mesto.</text></revision></page>"
            b"<page><title>Brno</title><ns>0</ns>"
            b"<revision><text>Brno je druhe mesto.</text></revision></page>"
            b"</mediawiki>")
    sys.path.insert(0, PROJECT_ROOT)
    import android_entry
    entry_result = json.loads(android_entry.import_wiki_dump(dump_path))
    check("android entry accepts a dump path",
          "source_id" in entry_result, str(entry_result))
    deadline = time.time() + 30
    while time.time() < deadline and m.execute_db_fetchone(
            "SELECT COUNT(*) FROM pages")[0] < 2:
        time.sleep(0.2)
    check("dump import indexed both articles",
          m.execute_db_fetchone("SELECT COUNT(*) FROM pages")[0] == 2,
          str(m.execute_db_fetchone("SELECT COUNT(*) FROM pages")[0]))
    android_entry.import_wiki_dump(dump_path)
    time.sleep(1.5)
    check("re-importing the dump is idempotent",
          m.execute_db_fetchone("SELECT COUNT(*) FROM pages")[0] == 2,
          str(m.execute_db_fetchone("SELECT COUNT(*) FROM pages")[0]))

    # ------------------------------------------------------------------
    # 7. Queue recovery: a locked row returns to pending after a crash
    # ------------------------------------------------------------------
    m.stop_server()
    m.get_db()
    m.execute_db(
        "INSERT INTO crawl_queue (site_id, url, url_hash, status, locked_by) "
        "VALUES (1, 'http://127.0.0.1/recover', 'h1', 'locked', 'dead-worker')")
    m._recover_interrupted_queue()
    row = m.execute_db_fetchone(
        "SELECT status FROM crawl_queue WHERE url_hash = 'h1'")
    check("interrupted queue row recovered to pending",
          row is not None and row[0] == "pending", str(row))

    m.stop_server()

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("ALL LIFECYCLE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
