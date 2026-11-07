"""Regression test suite for VULN-06: Denial of Service via Unbounded top_k and Query Allocation (CWE-400).
Verifies that API schema boundaries enforce strict length and numerical constraints, and retriever engine clamps k defensively.
"""
import pytest
from fastapi.testclient import TestClient

from gateway.main import app
from pipeline.retriever import RedisChunkRetriever

client = TestClient(app)
ADMIN_KEY = "test-admin-key"
AUTH_HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}"}


def test_query_top_k_unbounded_rejected_with_422():
    """Verify that submitting top_k > 50 or top_k < 1 to /query is rejected with HTTP 422."""
    # Attempting to allocate 500,000 vector records in Redis
    r_huge = client.post("/query", json={"query": "test query", "top_k": 500000}, headers=AUTH_HEADERS)
    assert r_huge.status_code == 422, f"Expected 422, got {r_huge.status_code}: {r_huge.text}"

    # Attempting top_k = 51 (just above bound)
    r_51 = client.post("/query", json={"query": "test query", "top_k": 51}, headers=AUTH_HEADERS)
    assert r_51.status_code == 422

    # Attempting top_k = 0 or negative
    r_zero = client.post("/query", json={"query": "test query", "top_k": 0}, headers=AUTH_HEADERS)
    assert r_zero.status_code == 422

    r_neg = client.post("/query", json={"query": "test query", "top_k": -10}, headers=AUTH_HEADERS)
    assert r_neg.status_code == 422


def test_query_length_bounds_enforced():
    """Verify that empty, 1-char, and oversized queries are rejected with HTTP 422."""
    # Empty query
    r_empty = client.post("/query", json={"query": ""}, headers=AUTH_HEADERS)
    assert r_empty.status_code == 422

    # Single char query (< min_length 2)
    r_short = client.post("/query", json={"query": "a"}, headers=AUTH_HEADERS)
    assert r_short.status_code == 422

    # Oversized query (> 1500 chars)
    oversized = "A" * 1501
    r_long = client.post("/query", json={"query": oversized}, headers=AUTH_HEADERS)
    assert r_long.status_code == 422


def test_chat_message_length_bounds_enforced():
    """Verify that chat endpoints reject empty and oversized messages with HTTP 422."""
    # Empty chat message
    r_empty = client.post("/chat", json={"message": ""})
    assert r_empty.status_code == 422

    # Oversized chat message (> 1500 chars)
    r_long = client.post("/chat", json={"message": "B" * 1501})
    assert r_long.status_code == 422


def test_chat_completions_bounds_enforced():
    """Verify that /v1/chat/completions enforces schema validation on temperature, max_tokens, and messages."""
    # Empty message list
    r_empty_msgs = client.post("/v1/chat/completions", json={"messages": []})
    assert r_empty_msgs.status_code == 422

    # Out of range max_tokens
    r_huge_tokens = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 100000,
    })
    assert r_huge_tokens.status_code == 422

    # Out of range temperature
    r_bad_temp = client.post("/v1/chat/completions", json={
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 5.0,
    })
    assert r_bad_temp.status_code == 422


def test_retriever_internal_k_clamping():
    """Verify defense-in-depth: retriever clamps k to [1, 50] internally even if invoked directly."""
    retriever = RedisChunkRetriever()

    class MockRedis:
        def __init__(self):
            self.last_query = None

        def execute_command(self, *args, **kwargs):
            # Capture the query string formatted into FT.SEARCH
            if len(args) >= 3 and args[0] == "FT.SEARCH":
                self.last_query = args[2]
            return [0]  # 0 hits

    mock_r = MockRedis()

    # Direct internal call with k=999999
    retriever._execute_knn_search(mock_r, "sample text", k=999999)
    assert mock_r.last_query is not None
    assert "KNN 50 @vector" in mock_r.last_query
    assert "999999" not in mock_r.last_query

    # Direct internal call with k=0
    retriever._execute_knn_search(mock_r, "sample text", k=0)
    assert "KNN 1 @vector" in mock_r.last_query

