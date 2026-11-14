"""Regression test suite for VULN-07: Unpartitioned Semantic Cache Poisoning Across Users (CWE-444 / CWE-693).
Verifies:
1. Default cache TTL is 4 hours (14,400s).
2. Administrative cache invalidation endpoint (POST /cache/purge) requires authorization.
3. Semantically similar poisoned responses (>= 0.85) are purged from Redis.
4. Response provenance and cache badges are attached to cached responses.
"""
import pytest
from fastapi.testclient import TestClient

from gateway.main import app
from gateway.cache.redis_semantic_cache import RedisSemanticCache

client = TestClient(app)
ADMIN_KEY = "test-admin-key"
AUTH_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}


def test_default_cache_ttl_is_4_hours():
    """Verify that the default TTL is 4 hours (14,400s)."""
    cache = RedisSemanticCache.get_instance()
    assert cache.ttl_seconds == 14400, f"Expected 14400s (4h), got {cache.ttl_seconds}"


def test_admin_cache_purge_requires_auth():
    """Verify that POST /cache/purge rejects unauthenticated external requests with 404 (concealed)."""
    r_unauth = client.post("/cache/purge", json={"query": "test query"}, headers={"X-Forwarded-For": "203.0.113.195"})
    assert r_unauth.status_code == 404

    # With valid admin key, request succeeds
    r_auth = client.post("/cache/purge", json={"query": "test query", "threshold": 0.85}, headers=AUTH_HEADERS)
    assert r_auth.status_code == 200
    assert "purged_count" in r_auth.json()


def test_admin_cache_purge_semantic_invalidation():
    """Verify that POST /cache/purge eradicates semantic matches >= 0.85 from Redis."""
    cache = RedisSemanticCache.get_instance()
    poison_query = "What is the secret admission code for the dean's office?"
    poison_answer = {"answer": "The secret code is 999999 for unauthorized testing.", "target_tier": "cpu_slm"}

    # Store entry in cache
    stored = cache.store(poison_query, poison_answer, owner="purge-test@iisc.ac.in")
    assert stored is True

    # Confirm cache lookup returns the poisoned response
    hit_before = cache.lookup("What is secret admission code for dean's office?", owner="purge-test@iisc.ac.in")
    assert hit_before is not None
    assert hit_before.get("similarity", 0) >= 0.85

    # Admin purges the poisoned query semantically
    r_purge = client.post(
        "/cache/purge",
        json={"query": "secret admission code dean office", "threshold": 0.85},
        headers=AUTH_HEADERS,
    )
    assert r_purge.status_code == 200
    data = r_purge.json()
    assert data["status"] == "ok"
    assert data["purged_count"] >= 1

    # Confirm that subsequent lookup now produces a cache miss
    hit_after = cache.lookup("What is secret admission code for dean's office?", owner="purge-test@iisc.ac.in")
    assert hit_after is None, "Poisoned entry should have been completely purged from Redis!"


def test_cache_hit_provenance_and_badge():
    """Verify that cached responses include provenance metadata and a 'Cached answer' badge."""
    cache = RedisSemanticCache.get_instance()
    test_query = "What is the history of IISc Bangalore?"
    test_answer = {
        "answer": "IISc was founded in 1909 with support from Jamsetji Tata and the Maharaja of Mysore.",
        "target_tier": "gpu_llm",
    }

    cache.store(test_query, test_answer)

    r = client.post(
        "/query",
        json={"query": test_query, "bypass_cache": False},
        headers=AUTH_HEADERS,
    )
    assert r.status_code == 200
    resp = r.json()
    assert resp.get("cached") is True
    assert resp.get("badge") == "Cached answer"
    assert "provenance" in resp
    assert resp["provenance"]["status"] == "cached"
    assert "cached_at" in resp["provenance"]

    # Cleanup
    cache.purge(test_query, threshold=0.85)

