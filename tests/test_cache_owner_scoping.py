"""Owner-scoped semantic cache (NFR-6): exact matches are shared, near-duplicate matches are per user.

A near-duplicate (semantic) hit may only return an entry created by the same user, so one user can
never poison another user's answers by seeding a similar-looking question. Exact (normalized-text)
matches stay shared because their answer was generated from that exact question.
"""

import asyncio
import json
import re
import time
import uuid
from pathlib import Path

import pytest

from gateway.cache.redis_semantic_cache import RedisSemanticCache

ALICE, BOB, MALLORY = "alice@iisc.ac.in", "bob@iisc.ac.in", "mallory@iisc.ac.in"
ANSWER = {"answer": "[owner-scope-test] temporary grounded answer used only by this test.", "chunks": [{"text": "t"}]}


@pytest.fixture(scope="module")
def cache():
    c = RedisSemanticCache.get_instance()
    try:
        c.sync_client.ping()
        c._index_initialized = False
        assert c.init_index()
    except Exception as exc:
        pytest.skip(f"Redis not available: {exc}")
    return c


@pytest.fixture
def q(cache):
    """Unique seed/paraphrase pair per test, purged afterwards."""
    marker = uuid.uuid4().hex[:8]
    seed = f"What is the role of zq{marker} mycorrhizal fungi in plant nutrition?"
    para = f"What role do zq{marker} mycorrhizal fungi play in plant nutrition?"
    created = [seed]
    yield seed, para, created
    for s in created:
        cache.purge(s, threshold=0.99)


def test_index_has_owner_tag_field(cache, q):
    """Functional check: a stored entry must be findable by its owner tag. (Never FT.INFO: it deadlocked
    this RediSearch build. And a bare search is no probe: a field missing from the schema gives 0 results,
    not an error.)"""
    seed, _, _ = q
    assert cache.store(seed, dict(ANSWER), owner=ALICE)
    res = cache.sync_client.execute_command(
        "FT.SEARCH", cache.index_name, "@owner:{%s}" % cache.owner_tag(ALICE), "LIMIT", "0", "0", "DIALECT", "2")
    total = res.get(b"total_results", res.get("total_results")) if isinstance(res, dict) else res[0]
    assert int(total) >= 1, "owner TAG field missing from the cache index: semantic hits are disabled"


def test_same_owner_gets_semantic_hit(cache, q):
    seed, para, _ = q
    assert cache.store(seed, dict(ANSWER), owner=ALICE)
    hit = cache.lookup(para, owner=ALICE)
    assert hit is not None and hit["matched_prompt"] == seed and not hit.get("fast_exact")


def test_other_owner_never_gets_semantic_hit(cache, q):
    seed, para, _ = q
    assert cache.store(seed, dict(ANSWER), owner=MALLORY)
    assert cache.lookup(para, owner=BOB) is None
    assert cache.lookup(para, owner=ALICE) is None


def test_no_owner_means_no_semantic_hit(cache, q):
    seed, para, _ = q
    assert cache.store(seed, dict(ANSWER), owner=ALICE)
    assert cache.lookup(para) is None
    assert cache.lookup(para, owner="") is None


def test_exact_match_is_shared_across_users(cache, q):
    seed, _, _ = q
    assert cache.store(seed, dict(ANSWER), owner=ALICE)
    for who in (ALICE, BOB, None):
        hit = cache.lookup(seed, owner=who)
        assert hit is not None and hit.get("fast_exact") is True


def test_legacy_entry_without_owner_is_exact_only(cache, q):
    seed, para, _ = q
    assert cache.store(seed, dict(ANSWER))  # stored without an owner (pre-scoping style)
    assert cache.lookup(seed, owner=BOB).get("fast_exact") is True
    assert cache.lookup(para, owner=ALICE) is None
    assert cache.lookup(para, owner=BOB) is None


def test_owner_tag_is_case_and_space_insensitive(cache, q):
    seed, para, _ = q
    assert cache.store(seed, dict(ANSWER), owner="Alice@IISc.ac.in ")
    assert cache.lookup(para, owner="alice@iisc.ac.in") is not None


def test_attacker_flood_cannot_displace_victims_own_entry(cache, q):
    """Pre-filtered KNN: closer attacker entries are invisible to the victim's lookup."""
    seed, para, created = q
    assert cache.store(seed, dict(ANSWER), owner=ALICE)
    for i in range(10):
        near = f"{para.rstrip('?')} v{i}?"
        created.append(near)
        cache.store(near, {"answer": "[owner-scope-test] attacker answer.", "chunks": [{"text": "t"}]}, owner=MALLORY)
    hit = cache.lookup(para, owner=ALICE)
    assert hit is not None and hit["matched_prompt"] == seed
    assert cache.lookup(para, owner=BOB) is None


def test_async_lookup_is_owner_scoped(cache, q):
    seed, para, _ = q

    async def run():
        assert await cache.astore(seed, dict(ANSWER), owner=ALICE)
        return await cache.alookup(para, owner=ALICE), await cache.alookup(para, owner=BOB), await cache.alookup(para)

    same, other, none = asyncio.run(run())
    assert same is not None and same["matched_prompt"] == seed
    assert other is None and none is None


@pytest.mark.parametrize("owner", ['a}|@owner:{*', "x) | (*", "*", "", "‮alice", "' OR 1=1 --"])
def test_hostile_owner_strings_are_hashed_not_injected(cache, q, owner):
    seed, para, _ = q
    assert cache.store(seed, dict(ANSWER), owner=ALICE)
    assert cache.lookup(para, owner=owner) is None


def test_knn_query_rejects_non_hex_tags():
    with pytest.raises(ValueError):
        RedisSemanticCache._owner_knn_query("*")
    assert "@owner:{0123456789abcdef}" in RedisSemanticCache._owner_knn_query("0123456789abcdef")


def test_gateway_passes_owner_on_every_lookup_and_store():
    src = (Path(__file__).resolve().parent.parent / "gateway" / "main.py").read_text()
    lookups = re.findall(r"cache\.a?lookup\([^\n]*\)", src)
    stores = re.findall(r"cache\.a?store\([^\n]*\)", src)
    assert lookups and stores
    assert all("owner=client_id" in c for c in lookups), lookups
    assert all("owner=client_id" in c for c in stores), stores


def test_live_gateway_does_not_share_semantic_hits_between_users(cache, q):
    httpx = pytest.importorskip("httpx")
    client = httpx.Client()
    try:
        client.get("http://127.0.0.1:8080/healthz", timeout=3).raise_for_status()
    except Exception:
        pytest.skip("gateway not running")
    seed, para, _ = q

    def first_event(user):
        with client.stream("POST", "http://127.0.0.1:8080/query", json={"query": para, "stream": True},
                           headers={"X-Internal-User": user}, timeout=60) as r:
            assert r.status_code == 200
            for line in r.iter_lines():
                if line.startswith("data: "):
                    return json.loads(line[6:])

    try:
        assert cache.store(seed, dict(ANSWER), owner=ALICE)
        mine = first_event(ALICE)
        assert mine.get("type") == "cache" and mine.get("status") == "HIT", mine
        theirs = first_event(BOB)
        assert not (theirs.get("type") == "cache" and theirs.get("status") == "HIT"), theirs
        time.sleep(3)  # let the aborted cold stream release its admission slot
    finally:
        client.close()
