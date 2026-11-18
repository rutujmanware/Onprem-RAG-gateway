"""DEFECT-30: X-Internal-User is honoured only when signed by the web chat bridge.

The gateway trusted `X-Internal-User` from any loopback caller; on this shared host every local user could
claim any identity (another user's owner-scoped cache hits, regenerate quota, concurrency slots). The header
now needs `X-Internal-Auth` (HMAC over user and time, keyed from SESSION_SECRET / INTERNAL_AUTH_KEY).
No services needed.
"""

import time
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

import gateway.auth.internal as gi
import gateway.main as gm
from gateway.dependencies import (get_admission_controller, get_complexity_scorer, get_dispatcher,
                                  get_semantic_cache, get_vllm_client)
from gateway.router.dispatcher import ModelTier, RouteDecision

SECRET = "s" * 48


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.delenv("INTERNAL_AUTH_KEY", raising=False)


# ------------------------------------------------------------------------------------------ scheme
def test_signed_identity_verifies():
    h = gi.internal_user_headers("alice@iisc.ac.in")
    assert gi.verify_internal_user("alice@iisc.ac.in", h["X-Internal-Auth"])


def test_signature_is_bound_to_the_user_and_cannot_be_forged():
    h = gi.internal_user_headers("alice@iisc.ac.in")
    assert not gi.verify_internal_user("bob@iisc.ac.in", h["X-Internal-Auth"])
    ts, sig = h["X-Internal-Auth"].split(".")
    assert not gi.verify_internal_user("alice@iisc.ac.in", f"{ts}.{'0' * len(sig)}")
    assert not gi.verify_internal_user("alice@iisc.ac.in", "")
    assert not gi.verify_internal_user("alice@iisc.ac.in", "garbage")


def test_old_signatures_expire():
    h = gi.internal_user_headers("alice@iisc.ac.in", now=time.time() - gi.MAX_AGE_SECONDS - 5)
    assert not gi.verify_internal_user("alice@iisc.ac.in", h["X-Internal-Auth"])


def test_a_different_secret_does_not_verify(monkeypatch):
    h = gi.internal_user_headers("alice@iisc.ac.in")
    monkeypatch.setenv("SESSION_SECRET", "t" * 48)
    assert not gi.verify_internal_user("alice@iisc.ac.in", h["X-Internal-Auth"])


def test_bridge_signs_exactly_like_the_gateway(monkeypatch):
    from askiisc_chat.askiisc import gateway_backend as gb
    fixed = 1_800_000_000
    monkeypatch.setattr(gb.time, "time", lambda: fixed)
    assert gb.internal_user_headers("alice@iisc.ac.in") == gi.internal_user_headers("alice@iisc.ac.in", now=fixed)


# ------------------------------------------------------------------------------------- route level
class Cache:
    owners = []

    async def alookup(self, q, owner=None, **k):
        Cache.owners.append(owner)
        return None

    async def astore(self, *a, **k):
        return False


class Scorer:
    async def score(self, q):
        return 0.2


class Dispatcher:
    def decide(self, score, **k):
        return RouteDecision(target_tier=ModelTier.CPU_SLM, score=score, alpha=0.5, target_model="m", reason="t")


class Admission:
    @asynccontextmanager
    async def slot(self, *a, **k):
        raise RuntimeError("stop after identification")  # the test only needs the identity
        yield


class VLLM:
    async def check_health(self):
        return False


@pytest.fixture
def client():
    Cache.owners = []
    over = gm.app.dependency_overrides
    saved = dict(over)
    over.update({get_semantic_cache: Cache, get_complexity_scorer: Scorer, get_dispatcher: Dispatcher,
                 get_admission_controller: Admission, get_vllm_client: VLLM})
    yield TestClient(gm.app, raise_server_exceptions=False)
    over.clear()
    over.update(saved)


BODY = {"query": "What is vermiculite used for?", "stream": False}


@pytest.mark.parametrize("path,body", [("/query", BODY),
                                       ("/v1/chat/completions", {"messages": [{"role": "user", "content": "What is vermiculite?"}]})])
def test_unsigned_or_forged_identity_from_loopback_is_refused(client, path, body):
    assert client.post(path, json=body, headers={"X-Internal-User": "victim@iisc.ac.in"}).status_code == 403
    forged = {"X-Internal-User": "victim@iisc.ac.in", "X-Internal-Auth": f"{int(time.time())}.{'a' * 64}"}
    assert client.post(path, json=body, headers=forged).status_code == 403
    alice = gi.internal_user_headers("alice@iisc.ac.in")
    replayed = {"X-Internal-User": "victim@iisc.ac.in", "X-Internal-Auth": alice["X-Internal-Auth"]}
    assert client.post(path, json=body, headers=replayed).status_code == 403
    assert "victim@iisc.ac.in" not in Cache.owners, "a refused identity never reaches the cache"


@pytest.mark.parametrize("path,body", [("/query", BODY),
                                       ("/v1/chat/completions", {"messages": [{"role": "user", "content": "What is vermiculite?"}]})])
def test_signed_identity_is_used_as_the_cache_owner(client, path, body):
    client.post(path, json=body, headers=gi.internal_user_headers("alice@iisc.ac.in"))
    assert Cache.owners and Cache.owners[0] == "alice@iisc.ac.in"


def test_without_the_header_a_loopback_caller_is_anonymous_loopback(client):
    client.post("/query", json=BODY)
    assert Cache.owners and Cache.owners[0] == "testclient"


def test_external_caller_with_the_header_is_still_hidden(client):
    h = {**gi.internal_user_headers("alice@iisc.ac.in"), "X-Forwarded-For": "203.0.113.9"}
    assert client.post("/query", json=BODY, headers=h).status_code == 404
