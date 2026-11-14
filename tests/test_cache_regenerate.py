"""'Regenerate' for cached answers.

Rules under test:
  * a regenerate token is issued only with a cache hit, is single-use, short-lived, and bound to the
    user and the question; the gateway rejects regenerate requests without a valid token;
  * regenerating a SHARED exact answer creates a private override for that user only; the shared
    entry is evicted only after REGEN_EVICT_USERS distinct users regenerate it;
  * a normal store never overwrites an existing shared exact answer (no "re-generation lottery");
  * per-user hourly cap; every regenerate is logged as quality feedback.
"""

import json
import re
import time
import uuid
from pathlib import Path

import pytest

import gateway.cache.redis_semantic_cache as M
from gateway.cache.redis_semantic_cache import RedisSemanticCache

ALICE, BOB, CAROL, DAVE = "alice@iisc.ac.in", "bob@iisc.ac.in", "carol@iisc.ac.in", "dave@iisc.ac.in"
ROOT = Path(__file__).resolve().parent.parent


def _ans(tag):
    return {"answer": f"[regen-test] answer {tag} used only by this test suite.", "chunks": [{"text": "t"}]}


@pytest.fixture(scope="module")
def cache():
    c = RedisSemanticCache.get_instance()
    try:
        c.sync_client.ping()
        c.init_index()
    except Exception as exc:
        pytest.skip(f"Redis not available: {exc}")
    return c


@pytest.fixture
def q(cache):
    text = f"What limits the zq{uuid.uuid4().hex[:8]} efficiency of perovskite solar cells?"
    yield text
    cache.purge(text, threshold=0.99)
    h = RedisSemanticCache._query_hash(text)
    cache.sync_client.delete(f"{M.REGEN_PREFIX}flags:{h}")


def _answer(hit):
    return hit["response"]["answer"] if hit else None


# ------------------------------------------------------------------ tokens
def test_token_requires_owner_and_known_kind(cache, q):
    assert cache.issue_regen_token(None, q, "exact", q) is None
    assert cache.issue_regen_token(ALICE, q, "bogus", q) is None
    assert cache.issue_regen_token(ALICE, q, "semantic", q)


def test_token_is_single_use(cache, q):
    tok = cache.issue_regen_token(ALICE, q, "exact", q)
    rec = cache.consume_regen_token(tok, ALICE, q)
    assert rec and rec["kind"] == "exact" and rec["matched_prompt"] == q
    assert cache.consume_regen_token(tok, ALICE, q) is None


def test_token_bound_to_user(cache, q):
    tok = cache.issue_regen_token(ALICE, q, "exact", q)
    assert cache.consume_regen_token(tok, BOB, q) is None
    assert cache.consume_regen_token(tok, ALICE, q) is None, "a token tried by another user is burned"


def test_token_bound_to_question(cache, q):
    tok = cache.issue_regen_token(ALICE, q, "exact", q)
    assert cache.consume_regen_token(tok, ALICE, q + " and stability") is None
    tok2 = cache.issue_regen_token(ALICE, q, "exact", q)
    assert cache.consume_regen_token(tok2, ALICE, "  " + q.upper() + " ") is not None  # same normalized text


def test_token_expires(cache, q):
    tok = cache.issue_regen_token(ALICE, q, "exact", q)
    cache.sync_client.pexpire(RedisSemanticCache._regen_token_key(tok), 50)
    time.sleep(0.2)
    assert cache.consume_regen_token(tok, ALICE, q) is None


@pytest.mark.parametrize("bad", ["", "x" * 129, "not-a-real-token", "*", "semregen:tok:*"])
def test_invalid_tokens_rejected(cache, q, bad):
    assert cache.consume_regen_token(bad, ALICE, q) is None


def test_token_ttl_is_short():
    assert 0 < M.REGEN_TOKEN_TTL <= 3600


# ------------------------------------------------------------------ store rules
def test_normal_store_never_overwrites_shared_exact(cache, q):
    assert cache.store(q, _ans("A"), owner=ALICE)
    cache.store(q, _ans("B"), owner=BOB)  # e.g. a second fresh generation of the same question
    assert "answer A" in _answer(cache.lookup(q))
    assert "answer A" in _answer(cache.lookup(q, owner=BOB))


def test_regenerated_shared_answer_is_private_override(cache, q):
    assert cache.store(q, _ans("A"), owner=ALICE)
    assert cache.store(q, _ans("B-fresh"), owner=BOB, regen_kind="exact")
    bob = cache.lookup(q, owner=BOB)
    assert "B-fresh" in _answer(bob) and bob["override"] is True
    assert "answer A" in _answer(cache.lookup(q, owner=CAROL)), "other users keep the shared answer"
    assert "answer A" in _answer(cache.lookup(q)), "anonymous lookups keep the shared answer"
    assert "answer A" in _answer(cache.lookup(q, owner=ALICE))


def test_regenerating_own_override_replaces_it(cache, q):
    assert cache.store(q, _ans("A"), owner=ALICE)
    cache.store(q, _ans("B1"), owner=BOB, regen_kind="exact")
    cache.store(q, _ans("B2"), owner=BOB, regen_kind="override")
    assert "B2" in _answer(cache.lookup(q, owner=BOB))
    assert "answer A" in _answer(cache.lookup(q, owner=CAROL))


def test_semantic_regenerate_creates_shared_exact_only_if_absent(cache, q):
    assert cache.store(q, _ans("fresh"), owner=ALICE, regen_kind="semantic")
    assert "fresh" in _answer(cache.lookup(q, owner=CAROL))
    cache.store(q, _ans("again"), owner=BOB, regen_kind="semantic")
    assert "fresh" in _answer(cache.lookup(q, owner=CAROL))


def test_purge_removes_overrides_too(cache, q):
    cache.store(q, _ans("A"), owner=ALICE)
    cache.store(q, _ans("B"), owner=BOB, regen_kind="exact")
    cache.purge(q, threshold=0.99)
    assert cache.lookup(q, owner=BOB) is None and cache.lookup(q) is None


# ------------------------------------------------------------------ eviction, quota, feedback
def test_shared_entry_evicted_only_after_distinct_users(cache, q, monkeypatch):
    monkeypatch.setattr(M, "REGEN_EVICT_USERS", 3)
    cache.store(q, _ans("A"), owner=ALICE)
    cache.store(q, _ans("B"), owner=BOB, regen_kind="exact")
    assert cache.record_shared_regen(q, BOB) is False
    assert cache.record_shared_regen(q, BOB) is False, "the same user counts once"
    assert cache.record_shared_regen(q, CAROL) is False
    assert cache.lookup(q) is not None
    assert cache.record_shared_regen(q, DAVE) is True
    assert cache.lookup(q) is None, "shared entry evicted"
    assert "answer B" in _answer(cache.lookup(q, owner=BOB)), "users' own overrides survive eviction"


def test_regenerate_quota_per_user(cache, monkeypatch):
    monkeypatch.setattr(M, "REGEN_MAX_PER_HOUR", 3)
    user, other = f"quota-{uuid.uuid4().hex[:6]}@iisc.ac.in", f"quota-{uuid.uuid4().hex[:6]}@iisc.ac.in"
    try:
        assert [cache.regen_quota_ok(user) for _ in range(4)] == [True, True, True, False]
        assert cache.regen_quota_ok(other) is True
        assert cache.regen_quota_ok(None) is False
    finally:
        cache.sync_client.delete(cache._quota_key(user), cache._quota_key(other))


def test_regenerate_events_are_logged(cache, q):
    before = cache.regen_event_count()
    cache.log_regen_event({"kind": "semantic", "similarity": 0.93, "matched_prompt": q}, ALICE)
    assert cache.regen_event_count() == before + 1


# ------------------------------------------------------------------ code-level guarantees
def test_gateway_wiring():
    src = (ROOT / "gateway" / "main.py").read_text()
    q_src = src[src.index('@app.post("/query"'):src.index("# Frontend Web Chat UI")]
    assert "aconsume_regen_token" in q_src and "aregen_quota_ok" in q_src
    assert "if may_store and full_text" in q_src, "streaming path must respect bypass_cache"
    assert "not req.bypass_cache and" not in q_src.replace("use_cache = (not req.bypass_cache) and", "")
    stores = re.findall(r"cache\.a?store\([^\n]*\)", q_src)
    assert stores and all("regen_kind=regen_kind" in s for s in stores)
    assert "regenerate_token=req.regenerate_token" in src, "web chat forwards the token"


def test_ui_offers_regenerate_only_on_cached_answers():
    html = (ROOT / "askiisc_chat" / "static" / "index.html").read_text()
    calls = [m.start() for m in re.finditer(r"(?<!function )addRegenerateButton\(foot", html)]
    assert len(calls) == 1, "the button must be added from exactly one place"
    assert "cacheInfo && cacheInfo.regenerate_token" in html[calls[0] - 80:calls[0]]
    assert "n.textContent = 'Reused answer from your earlier question" in html  # text, never innerHTML


# ------------------------------------------------------------------ live gateway
@pytest.fixture
def gw():
    httpx = pytest.importorskip("httpx")
    c = httpx.Client()
    try:
        c.get("http://127.0.0.1:8080/healthz", timeout=3).raise_for_status()
    except Exception:
        pytest.skip("gateway not running")
    yield c
    c.close()


def _first_event(client, query, user, **extra):
    with client.stream("POST", "http://127.0.0.1:8080/query", json={"query": query, "stream": True, **extra},
                       headers={"X-Internal-User": user}, timeout=60) as r:
        if r.status_code != 200:
            return {"http": r.status_code, "detail": json.loads(r.read() or b"{}").get("detail")}
        for line in r.iter_lines():
            if line.startswith("data: "):
                return json.loads(line[6:])
    return {}


def test_live_cache_hit_carries_token_and_regenerate_is_gated(cache, q, gw):
    assert cache.store(q, _ans("A"), owner=ALICE)
    hit = _first_event(gw, q, BOB)
    assert hit.get("type") == "cache" and hit.get("cache_kind") == "exact" and hit.get("regenerate_token")
    token = hit["regenerate_token"]

    assert _first_event(gw, q, BOB, regenerate_token="forged-token")["http"] == 403
    assert _first_event(gw, q + " now", BOB, regenerate_token=token)["http"] == 403, "bound to the question (and now burned)"

    hit2 = _first_event(gw, q, BOB)
    assert _first_event(gw, q, CAROL, regenerate_token=hit2["regenerate_token"])["http"] == 403, "bound to the user"

    hit3 = _first_event(gw, q, BOB)
    fresh = _first_event(gw, q, BOB, regenerate_token=hit3["regenerate_token"])
    assert fresh.get("type") != "cache" and "http" not in fresh, f"regenerate must bypass the cache: {fresh}"
    time.sleep(3)  # let the aborted cold stream release its admission slot
    assert _first_event(gw, q, BOB, regenerate_token=hit3["regenerate_token"])["http"] == 403, "single use"
    assert "answer A" in _answer(cache.lookup(q, owner=CAROL)), "shared answer untouched for others"


def test_live_web_chat_bridge_shows_cache_note_and_regenerates(cache, q, gw):
    """The browser-facing bridge (/chat -> stream_via_gateway) emits the cache note + token, and a
    regenerate request through the bridge is answered fresh (no cache event)."""
    import asyncio
    import sys
    sys.path.insert(0, str(ROOT / "askiisc_chat"))
    from askiisc.gateway_backend import stream_via_gateway

    async def first_events(**kw):
        evs = []
        agen = stream_via_gateway(q, "internal", user_email=BOB, **kw)
        async for chunk in agen:
            for blk in chunk.strip().split("\n\n"):
                lines = dict(l.split(": ", 1) for l in blk.split("\n") if ": " in l)
                if "event" in lines:
                    evs.append((lines["event"], json.loads(lines.get("data", "{}"))))
            if len(evs) >= 2:
                break
        await agen.aclose()
        return evs

    assert cache.store(q, _ans("A"), owner=ALICE)
    evs = asyncio.run(first_events())
    cache_ev = [d for e, d in evs if e == "cache"]
    assert cache_ev and cache_ev[0]["kind"] == "exact" and cache_ev[0]["regenerate_token"], evs
    fresh = asyncio.run(first_events(regenerate_token=cache_ev[0]["regenerate_token"]))
    assert fresh and all(e != "cache" for e, _ in fresh), fresh
    assert all(d.get("route") != "cache" for e, d in fresh if e == "meta"), fresh
    time.sleep(3)
