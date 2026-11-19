"""DEFECT-39: no user can replace or set the shared cached answer with a non-standard request (NFR-6).

Reported against rt-baseline: streaming /query ignored bypass_cache on store and the exact entry was overwritten,
so B's answer (temperature 2.0, 16 tokens) replaced A's for everyone. On main the store honours bypass_cache and
shared exact entries are write-once; remaining gap closed here: the FIRST asker could still set the shared entry
with a high-temperature or truncated answer. Only standard-settings answers are cached now.
DEFECT-56: the same for a caller-chosen `top_k` (number of source documents, /query only).
"""

import inspect

import pytest

import gateway.main as gm
from gateway.main import standard_generation


@pytest.mark.parametrize("t,m,ok", [(None, None, True), (0.2, None, True), (0.3, 2048, True), (0.2, 512, True),
                                    (2.0, None, False), (0.5, None, False), (0.2, 16, False), (None, 511, False)])
def test_only_standard_generation_settings_are_cacheable(t, m, ok):
    assert standard_generation(t, m) is ok


@pytest.mark.parametrize("k", [1, 3, 10, 50])
def test_a_caller_chosen_top_k_is_never_cacheable(k):
    assert standard_generation(None, None, None) is True
    assert standard_generation(None, None, k) is False


def test_every_store_path_is_guarded():
    q = inspect.getsource(gm.query_endpoint)
    assert "standard_generation(req.temperature, req.max_tokens, req.top_k)" in q
    assert "may_store = (((not req.bypass_cache) or regen is not None)" in q
    c = inspect.getsource(gm.chat_completions)
    assert c.count("standard_generation(req.temperature, req.max_tokens)") == 2


def test_shared_exact_entries_are_write_once():
    from gateway.cache import redis_semantic_cache as m
    src = inspect.getsource(m.RedisSemanticCache.store) + inspect.getsource(m.RedisSemanticCache.astore)
    assert src.count("nx=True") == 2


def test_query_with_top_k_is_answered_but_not_stored(monkeypatch):
    """DEFECT-56, route level with fakes: `top_k: 1` used to become the shared cached answer (write-once)."""
    from contextlib import asynccontextmanager
    from fastapi.testclient import TestClient
    import pipeline.rag_chain as rc
    import pipeline.retriever as pr
    from gateway.dependencies import (get_admission_controller, get_complexity_scorer, get_dispatcher,
                                      get_semantic_cache, get_vllm_client)
    from gateway.router.dispatcher import RequestDispatcher

    chunks = [{"title": f"T{i}", "content": f"content {i}", "similarity": 0.8 - i / 100, "doc_id": str(i)}
              for i in range(10)]
    stored, sent = [], []

    class Cache:
        async def alookup(self, *a, **k): return None
        async def astore(self, q, resp, owner=None, regen_kind=None):
            stored.append(len(resp.get("chunks") or [])); return True

    class Adm:
        async def charge(self, *a, **k): pass
        @asynccontextmanager
        async def slot(self, *a, **k): yield

    class Scorer:
        async def score(self, q): return 0.1

    class Vllm:
        async def check_health(self): return False

    async def asearch(self, q, top_k=None, **k): return chunks

    def answer_query(self, query, top_k=None, chunks=None, **k):
        sent.append(len(chunks or []))
        return {"answer": "Graphene is used in supercapacitor electrodes [Document 1].", "chunks": chunks}

    monkeypatch.setenv("TYPO_CORPUS_PROTECTION", "0")
    monkeypatch.setattr(pr.RedisChunkRetriever, "asearch", asearch)
    monkeypatch.setattr(rc.RAGQAPipeline, "answer_query", answer_query)
    monkeypatch.setattr(gm, "classifier_fence", lambda *a, **k: None)
    monkeypatch.setitem(gm.app.dependency_overrides, get_semantic_cache, Cache)
    monkeypatch.setitem(gm.app.dependency_overrides, get_admission_controller, Adm)
    monkeypatch.setitem(gm.app.dependency_overrides, get_complexity_scorer, Scorer)
    monkeypatch.setitem(gm.app.dependency_overrides, get_dispatcher, RequestDispatcher)
    monkeypatch.setitem(gm.app.dependency_overrides, get_vllm_client, Vllm)
    c = TestClient(gm.app)

    r = c.post("/query", json={"query": "What is graphene used in?", "top_k": 1})
    assert r.status_code == 200 and sent[-1] == 1 and stored == []     # answered from 1 document, not stored
    r = c.post("/query", json={"query": "What is graphene used for?"})
    assert r.status_code == 200 and len(stored) >= 1                   # the standard request is still cached


# --------------------------------------------------------------------------- live (needs Redis)
@pytest.fixture
def cache():
    from tests._live import SEMANTIC_REASON, embedder_available, redis_available
    if not (redis_available() and embedder_available()):
        pytest.skip(SEMANTIC_REASON)
    from gateway.cache.redis_semantic_cache import RedisSemanticCache
    return RedisSemanticCache.get_instance()


def test_a_later_store_cannot_replace_the_shared_answer(cache):
    import uuid
    q = f"What limits the cycle life of zq{uuid.uuid4().hex[:6]} batteries?"
    try:
        assert cache.store(q, {"answer": "GENUINE shared answer from user A.", "chunks": [{"t": 1}]}, owner="a@iisc.ac.in")
        cache.store(q, {"answer": "POISONED answer from user B.", "chunks": [{"t": 1}]}, owner="b@iisc.ac.in")
        hit = cache.lookup(q, owner="c@iisc.ac.in")
        ans = hit.get("response", {}).get("answer") if hit else None
        assert ans == "GENUINE shared answer from user A."
    finally:
        cache.purge(q, threshold=0.99)
