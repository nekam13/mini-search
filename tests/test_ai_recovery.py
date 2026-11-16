#!/usr/bin/env python3
"""Regression tests for AI/vector-search recovery after a backend switch.

The bug: ``vector_search`` only compares rows whose ``embedding_space`` matches
the active space. When the stored vectors belong to another space — a backend
switch (hash fallback <-> real model) or an upgrade that stamped untagged
vectors with the hash space — the search silently returned ``[]`` and hybrid
search fell back to FTS-only, with no way back except the nightly maintenance
job (off by default in the APK). These tests pin the recovery paths.
"""
import os
import shutil
import sys
import tempfile
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

failures = []


def check(name, condition, detail=""):
    if condition:
        print(f"PASS: {name}")
    else:
        failures.append(name)
        print(f"FAIL: {name} {detail}")


def _seed_page(m, site_id, url, body):
    page = {
        'site_id': site_id, 'url': url, 'url_hash': m.url_hash(url),
        'title': body.split()[0], 'og_title': '', 'og_description': '',
        'og_image': '', 'favicon_url': '', 'body_text': body, 'images': [],
        'schema_type': '', 'schema_details': {}, 'audio_url': '', 'has_audio': 0,
        'published_timestamp': 0,
    }
    page['seo_score'] = m.calculate_seo_score(page)
    page['embedding'] = m._embedding_for(page)
    m.store_page(page)


class _FakeRealModel:
    """A real-model stand-in that produces deterministic 384-dim vectors.

    ``is_real_model`` is what the recovery guards on; the values only need to be
    comparable with themselves, so the hash encoder is reused.
    """

    def __init__(self, m):
        self._m = m
        self.dimension = m.MINILM_DIM
        self.space = m.MINILM_SPACE
        self.is_real_model = True

    def get_sentence_embedding_dimension(self):
        return self.dimension

    def encode(self, text):
        return self._m._HashingEmbedding().encode(text)


def main():
    tmpdir = tempfile.mkdtemp()
    os.chdir(tmpdir)

    import app_combined as m

    orig_get_model = m.get_model

    m.DB_PATH = os.path.join(tmpdir, "console.db")
    m._db_conn = None
    m._model = None
    m._model_failed = False
    m._model_failed_at = 0.0
    m._embed_recovery_space = None
    m.close_db()
    m.get_db()

    site_id = m.add_site("http://recovery.example/", 50)
    _seed_page(m, site_id, "http://recovery.example/praha",
               "Praha je hlavni mesto Ceske republiky")
    _seed_page(m, site_id, "http://recovery.example/brno",
               "Brno je druhe nejvetsi mesto v zemi")

    # ---- phase 1: the hash fallback writes hash-space vectors ----
    m.get_model = lambda: m._HashingEmbedding()
    m._hnsw_index = None
    m._hnsw_space = None
    hits = m.vector_search("hlavni mesto", 5)
    check("hash backend finds its own vectors", len(hits) == 2, str(hits))

    # ---- phase 2: a real model becomes active (backend switch) ----
    m.get_model = lambda: _FakeRealModel(m)
    m._hnsw_index = None
    m._hnsw_space = None
    m._embed_recovery_space = None
    m._drop_connections()
    m.get_db()

    check("active space is the real model space",
          m.active_embedding_space() == m.MINILM_SPACE, m.active_embedding_space())
    check("stale vectors are detected", m._stale_embedding_count() == 2,
          str(m._stale_embedding_count()))

    # Before recovery the vectors are in the wrong space and are ignored.
    check("search is empty before recovery (the bug)", m.vector_search("hlavni mesto", 5) == [])

    # The hot-path trigger must kick the background re-embed even though the
    # search just returned nothing.
    deadline = time.time() + 10
    recovered = 0
    while time.time() < deadline:
        recovered = m._stale_embedding_count()
        if recovered == 0:
            break
        time.sleep(0.1)
    check("hot-path trigger re-embeds stale vectors", recovered == 0, str(recovered))

    hits = m.vector_search("hlavni mesto", 5)
    check("semantic search recovers after re-embed", len(hits) == 2, str(hits))

    combined = m.hybrid_search("hlavni mesto", 5)
    check("hybrid search returns results after recovery", len(combined) >= 1, str(combined))

    # ---- the direct helper is idempotent ----
    check("recovery is a no-op when nothing is stale",
          m.startup_embedding_recovery() == 0)

    # ---- get_model() must retry after a failure, not latch the fallback ----
    m.get_model = orig_get_model
    m._model = None
    m._model_failed = False
    m._model_failed_at = 0.0
    m.MODEL_RETRY_COOLDOWN = 0  # retry immediately for the test

    real_model = _FakeRealModel(m)
    m._android_onnx_model = lambda: None
    m._sentence_transformer_model = lambda: None
    first = m.get_model()
    check("a failed load falls back to the hash embedder",
          isinstance(first, m._HashingEmbedding))
    check("the failure is recorded", m._model_failed is True)

    # The bridge becomes ready on the next attempt.
    m._android_onnx_model = lambda: real_model
    second = m.get_model()
    check("get_model retries instead of latching the fallback",
          second is real_model, type(second).__name__)
    check("a successful load clears the failure flag", m._model_failed is False)

    # ---- recovery must never downgrade real vectors to the hash fallback ----
    m.get_model = lambda: m._HashingEmbedding()
    m._hnsw_index = None
    m._hnsw_space = None
    m._embed_recovery_space = None
    m._drop_connections()
    m.get_db()
    check("recovery refuses to run for the hash fallback",
          m.startup_embedding_recovery() == 0)
    check("real-model vectors are preserved (not overwritten as hash)",
          m._stale_embedding_count() == 2, str(m._stale_embedding_count()))

    shutil.rmtree(tmpdir, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} TEST(S) FAILED: {failures}")
        sys.exit(1)
    print("\nALL AI RECOVERY TESTS PASSED")


if __name__ == "__main__":
    main()
