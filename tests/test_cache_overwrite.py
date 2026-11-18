"""DEFECT-39: no user can replace or set the shared cached answer with a non-standard request (NFR-6).

Reported against rt-baseline: streaming /query ignored bypass_cache on store and the exact entry was overwritten,
so B's answer (temperature 2.0, 16 tokens) replaced A's for everyone. On main the store honours bypass_cache and
shared exact entries are write-once; remaining gap closed here: the FIRST asker could still set the shared entry
with a high-temperature or truncated answer. Only standard-settings answers are cached now.
"""

import inspect

import pytest

import gateway.main as gm
from gateway.main import standard_generation


@pytest.mark.parametrize("t,m,ok", [(None, None, True), (0.2, None, True), (0.3, 2048, True), (0.2, 512, True),
                                    (2.0, None, False), (0.5, None, False), (0.2, 16, False), (None, 511, False)])
def test_only_standard_generation_settings_are_cacheable(t, m, ok):
    assert standard_generation(t, m) is ok


def test_every_store_path_is_guarded():
    q = inspect.getsource(gm.query_endpoint)
    assert "may_store = ((not req.bypass_cache) or regen is not None) and standard_generation(" in q
    c = inspect.getsource(gm.chat_completions)
    assert c.count("standard_generation(req.temperature, req.max_tokens)") == 2


def test_shared_exact_entries_are_write_once():
    from gateway.cache import redis_semantic_cache as m
    src = inspect.getsource(m.RedisSemanticCache.store) + inspect.getsource(m.RedisSemanticCache.astore)
    assert src.count("nx=True") == 2


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
