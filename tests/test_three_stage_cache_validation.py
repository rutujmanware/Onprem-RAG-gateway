"""Acceptance tests for Three-Stage L1 Semantic Cache Validation.

Validates:
1. Time vs Space Complexity divergence rejection (the user's core challenge).
2. Numerical value consistency (slight number changes reject cache hits).
3. Context and metadata validation (model version/tier, language, conversation context).
4. Volatile information gate (stock prices, weather, live availability bypass cache).
5. Stage 3 verification (SLM judge and lexical-semantic verifier).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import numpy as np
import pytest

from gateway.cache.l1_semantic import L1SemanticCache
from gateway.cache.validator import (
    ContextAndMetadataValidator,
    DimensionOppositionChecker,
    NumericalConsistencyChecker,
    SemanticVerificationJudge,
    ThreeStageCacheValidator,
    VolatileQueryDetector,
)


class MockRedisServer:
    """Mock Redis RediSearch backend for unit testing L1 cache."""

    def __init__(self):
        self.hashes: Dict[str, Dict[bytes, bytes]] = {}
        self.kv: Dict[str, bytes] = {}

    async def delete(self, *keys: str) -> int:
        removed = 0
        for k in keys:
            if self.hashes.pop(k, None) is not None:
                removed += 1
        return removed

    def pipeline(self, transaction: bool = False):
        store = self

        class MockPipe:
            def __init__(self):
                self.ops = []

            def hset(self, key: str, mapping: Dict[bytes, bytes]):
                self.ops.append(("hset", key, mapping))
                return self

            def expire(self, key: str, ttl: int):
                self.ops.append(("expire", key, ttl))
                return self

            async def execute(self):
                for item in self.ops:
                    if item[0] == "hset":
                        _, key, mapping = item
                        store.hashes[key] = dict(mapping)
                self.ops.clear()
                return True

        return MockPipe()

    async def execute_command(self, *args) -> Any:
        cmd = str(args[0]).upper()
        if cmd == "FT.INFO":
            return [b"index_name", b"mock_idx"]
        if cmd == "FT.CREATE":
            return b"OK"
        if cmd == "FT.SEARCH":
            # Extract query vec
            args_list = list(args)
            vec_idx = args_list.index("vec") + 1
            q_vec = np.frombuffer(args_list[vec_idx], dtype=np.float32)
            q_norm = np.linalg.norm(q_vec)
            if q_norm > 0:
                q_vec = q_vec / q_norm

            best_key = None
            best_dist = 999.0
            best_hash = None

            for k, h in self.hashes.items():
                cand_vec = np.frombuffer(h[b"vector"], dtype=np.float32)
                c_norm = np.linalg.norm(cand_vec)
                if c_norm > 0:
                    cand_vec = cand_vec / c_norm

                cos_sim = float(np.dot(q_vec, cand_vec))
                cos_dist = 1.0 - cos_sim
                if cos_dist < best_dist:
                    best_dist = cos_dist
                    best_key = k
                    best_hash = h

            if best_hash is None:
                return [0]

            fields = [
                b"prompt", best_hash[b"prompt"],
                b"response", best_hash[b"response"],
                b"thesis_id", best_hash[b"thesis_id"],
                b"chapter", best_hash[b"chapter"],
                b"doc_version", best_hash[b"doc_version"],
                b"score", f"{best_dist:.6f}".encode("utf-8"),
            ]
            if b"model_tier" in best_hash:
                fields.extend([b"model_tier", best_hash[b"model_tier"]])
            if b"language" in best_hash:
                fields.extend([b"language", best_hash[b"language"]])

            return [1, best_key.encode("utf-8"), fields]

        raise NotImplementedError(f"Unsupported command: {cmd}")


# ============================================================================
# Test 1: Time Complexity vs Space Complexity Challenge
# ============================================================================

def test_dimension_opposition_time_vs_space():
    """Verify that opposing dimensions (time vs space complexity) are caught."""
    checker = DimensionOppositionChecker()

    # Time vs Space complexity
    q1 = "What is the time complexity of binary search?"
    q2 = "What is the space complexity of binary search?"
    is_valid, reason = checker.check_opposition(q1, q2)
    assert is_valid is False
    assert "time" in reason and "space" in reason

    # Best-case vs Worst-case
    is_valid, reason = checker.check_opposition(
        "What is the best-case running time of quicksort?",
        "What is the worst-case running time of quicksort?",
    )
    assert is_valid is False
    assert "worst" in reason and "best" in reason

    # Ascending vs Descending
    is_valid, reason = checker.check_opposition(
        "Sort the array in ascending order",
        "Sort the array in descending order",
    )
    assert is_valid is False

    # Encode vs Decode
    is_valid, reason = checker.check_opposition(
        "How to encode the input stream?",
        "How to decode the input stream?",
    )
    assert is_valid is False

    # Synchronous vs Asynchronous
    is_valid, reason = checker.check_opposition(
        "Is the RPC call synchronous?",
        "Is the RPC call asynchronous?",
    )
    assert is_valid is False

    # Identical dimensions should pass
    is_valid, reason = checker.check_opposition(
        "What is the time complexity of binary search?",
        "Explain binary search's time complexity in sorted arrays",
    )
    assert is_valid is True
    assert reason is None


# ============================================================================
# Test 2: Numerical Value Sensitivity Check
# ============================================================================

def test_numerical_values_consistency():
    """Verify that a slight change in numerical values invalidates cache hits."""
    checker = NumericalConsistencyChecker()

    # Different numbers (N=10 vs N=20)
    ok, reason = checker.validate_numbers_match(
        "What is the memory usage for batch size 10?",
        "What is the memory usage for batch size 20?",
    )
    assert ok is False
    assert "mismatch" in reason.lower()

    # Different years (2023 vs 2024)
    ok, reason = checker.validate_numbers_match(
        "What was the conclusion in the 2023 evaluation?",
        "What was the conclusion in the 2024 evaluation?",
    )
    assert ok is False

    # Different percentages (50% vs 75%)
    ok, reason = checker.validate_numbers_match(
        "Evaluate at 50% load",
        "Evaluate at 75% load",
    )
    assert ok is False

    # Number presence vs absence
    ok, reason = checker.validate_numbers_match(
        "Run the test on 5 nodes",
        "Run the test on all nodes",
    )
    assert ok is False

    # Matching numbers (paraphrased) should pass
    ok, reason = checker.validate_numbers_match(
        "What is the memory usage for batch size 16?",
        "Can you report memory usage when the batch size is 16?",
    )
    assert ok is True

    # No numbers in both should pass
    ok, reason = checker.validate_numbers_match(
        "What is the main hypothesis of the thesis?",
        "Explain the thesis's core hypothesis",
    )
    assert ok is True


# ============================================================================
# Test 3: Volatile Information Gate
# ============================================================================

def test_volatile_information_gate():
    """Verify that queries with changing info (stocks, weather, live availability) are detected."""
    detector = VolatileQueryDetector()

    # Stock prices
    assert detector.is_volatile("What is the current stock price of Apple?") is True
    assert detector.is_volatile("AAPL share price right now") is True
    assert detector.is_volatile("bitcoin price today") is True

    # Weather
    assert detector.is_volatile("What is the weather today in Bangalore?") is True
    assert detector.is_volatile("temperature right now") is True
    assert detector.is_volatile("is it raining right now?") is True

    # Live availability
    assert detector.is_volatile("live availability of GPU compute nodes") is True
    assert detector.is_volatile("check current availability of server slots") is True
    assert detector.is_volatile("seats available right now") is True
    assert detector.is_volatile("server load currently") is True

    # Real-time queries
    assert detector.is_volatile("what time is it right now?") is True
    assert detector.is_volatile("today's date") is True

    # Non-volatile technical queries MUST NOT be falsely flagged
    assert detector.is_volatile("What is the time complexity of binary search?") is False
    assert detector.is_volatile("Explain the convergence time of SGD") is False
    assert detector.is_volatile("What estimator is proposed in Chapter 3?") is False
    assert detector.is_volatile("How does the PagedAttention memory allocator work?") is False


# ============================================================================
# Test 4: End-to-End L1 Semantic Cache with Three-Stage Validation
# ============================================================================

@pytest.mark.asyncio
async def test_l1_end_to_end_rejects_time_vs_space():
    """
    Core user challenge test:
    Cache has 'What is the time complexity of binary search?'.
    Incoming query is 'What is the space complexity of binary search?'.
    Even with identical embeddings (cosine similarity 1.0), it MUST be a MISS.
    """
    mock_redis = MockRedisServer()
    cache = L1SemanticCache(redis_client=mock_redis, similarity_threshold=0.85)

    # Synthetic dummy embedding for binary search questions
    dummy_vec = np.zeros(384, dtype=np.float32)
    dummy_vec[0:10] = 1.0

    # 1. Populate cache with Question 1 (Time complexity)
    q1 = "What is the time complexity of binary search?"
    await cache.set(
        prompt=q1,
        query_embedding=dummy_vec,
        response={"answer": "The time complexity is O(log n)."},
        thesis_id="algorithms_thesis",
        chapter="ch_1",
    )

    # 2. Query with Question 2 (Space complexity) with the EXACT SAME embedding
    q2 = "What is the space complexity of binary search?"
    hit = await cache.get(
        query_embedding=dummy_vec,
        thesis_id="algorithms_thesis",
        chapter="ch_1",
        prompt=q2,
    )

    # MUST be a cache MISS due to Stage 2 dimension opposition check!
    assert hit is None, "Cache should reject time vs space complexity mismatch!"

    # 3. Query with a valid paraphrase of Question 1
    q1_paraphrase = "What is binary search's time complexity?"
    hit_valid = await cache.get(
        query_embedding=dummy_vec,
        thesis_id="algorithms_thesis",
        chapter="ch_1",
        prompt=q1_paraphrase,
    )
    assert hit_valid is not None
    assert hit_valid["response"]["answer"] == "The time complexity is O(log n)."


@pytest.mark.asyncio
async def test_l1_end_to_end_rejects_numerical_difference():
    """Verify that identical vectors with different numbers (N=10 vs N=20) reject cache hits."""
    mock_redis = MockRedisServer()
    cache = L1SemanticCache(redis_client=mock_redis, similarity_threshold=0.85)

    dummy_vec = np.zeros(384, dtype=np.float32)
    dummy_vec[10:20] = 1.0

    # Cache answer for N=10
    await cache.set(
        prompt="What is the throughput for batch size 10?",
        query_embedding=dummy_vec,
        response={"answer": "Throughput is 150 req/sec."},
        thesis_id="thesis_bench",
        chapter="ch_4",
    )

    # Query for N=20 with identical embedding
    hit_diff_num = await cache.get(
        query_embedding=dummy_vec,
        thesis_id="thesis_bench",
        chapter="ch_4",
        prompt="What is the throughput for batch size 20?",
    )
    assert hit_diff_num is None, "Different numerical value must cause a Cache MISS!"

    # Query for N=10 paraphrase
    hit_same_num = await cache.get(
        query_embedding=dummy_vec,
        thesis_id="thesis_bench",
        chapter="ch_4",
        prompt="Can you give the throughput for a batch size of 10?",
    )
    assert hit_same_num is not None
    assert hit_same_num["response"]["answer"] == "Throughput is 150 req/sec."


@pytest.mark.asyncio
async def test_l1_end_to_end_volatile_query_bypasses_cache():
    """Verify volatile queries (e.g. stock prices, weather) bypass cache completely."""
    mock_redis = MockRedisServer()
    cache = L1SemanticCache(redis_client=mock_redis)

    dummy_vec = np.zeros(384, dtype=np.float32)
    dummy_vec[0] = 1.0

    # Attempt to set volatile info
    key = await cache.set(
        prompt="What is the stock price of Apple right now?",
        query_embedding=dummy_vec,
        response={"answer": "$190"},
        thesis_id="global",
        chapter="all",
        freshness_handled=False,
    )
    assert key == "", "Volatile information must not be stored in cache"

    # Attempt to query volatile info
    hit = await cache.get(
        query_embedding=dummy_vec,
        thesis_id="global",
        chapter="all",
        prompt="What is the weather today in Bangalore?",
        freshness_handled=False,
    )
    assert hit is None, "Volatile information must bypass cache get"


@pytest.mark.asyncio
async def test_l1_context_and_model_tier_validation():
    """Verify that model_tier and language mismatches reject cache hits."""
    mock_redis = MockRedisServer()
    cache = L1SemanticCache(redis_client=mock_redis)

    dummy_vec = np.zeros(384, dtype=np.float32)
    dummy_vec[5] = 1.0

    # Cache answer generated by CPU SLM
    await cache.set(
        prompt="Explain theorem 3.1",
        query_embedding=dummy_vec,
        response={"answer": "SLM explanation"},
        thesis_id="math_thesis",
        chapter="ch_3",
        model_tier="cpu_slm",
        language="en",
    )

    # 1. Query with mismatched model tier (user explicitly requests gpu_vllm)
    hit_wrong_tier = await cache.get(
        query_embedding=dummy_vec,
        thesis_id="math_thesis",
        chapter="ch_3",
        prompt="Explain theorem 3.1",
        model_tier="gpu_vllm",
    )
    assert hit_wrong_tier is None, "Model tier mismatch must reject cache hit"

    # 2. Query with mismatched language
    hit_wrong_lang = await cache.get(
        query_embedding=dummy_vec,
        thesis_id="math_thesis",
        chapter="ch_3",
        prompt="Explain theorem 3.1",
        language="fr",
    )
    assert hit_wrong_lang is None, "Language mismatch must reject cache hit"

    # 3. Matching tier and language
    hit_ok = await cache.get(
        query_embedding=dummy_vec,
        thesis_id="math_thesis",
        chapter="ch_3",
        prompt="Explain theorem 3.1",
        model_tier="cpu_slm",
        language="en",
    )
    assert hit_ok is not None
    assert hit_ok["response"]["answer"] == "SLM explanation"


@pytest.mark.asyncio
async def test_stage3_slm_judge_verification():
    """Verify Stage 3 SLM judge can approve or reject uncertain matches."""
    judge = SemanticVerificationJudge()

    # Mock SLM returning "NO" (rejection)
    mock_slm_no = AsyncMock()
    mock_slm_no.generate_chat.return_value = {
        "choices": [{"message": {"content": "NO, different question focus"}}]
    }

    ok, reason = await judge.verify_with_slm(
        query="What is the time complexity of quicksort?",
        cached_prompt="What is the space complexity of quicksort?",
        slm_client=mock_slm_no,
    )
    assert ok is False
    assert "rejected" in reason.lower()

    # Mock SLM returning "YES" (approval)
    mock_slm_yes = AsyncMock()
    mock_slm_yes.generate_chat.return_value = {
        "choices": [{"message": {"content": "YES, they are asking for the same metric."}}]
    }

    ok, reason = await judge.verify_with_slm(
        query="What is the time complexity of quicksort?",
        cached_prompt="Explain the time complexity of quicksort.",
        slm_client=mock_slm_yes,
    )
    assert ok is True
    assert "confirmed" in reason.lower()


@pytest.mark.asyncio
async def test_invalidate_version_resp2_and_resp3():
    """Verify invalidate_version handles both RESP2 array and RESP3 mapping formats."""
    class Resp3RedisServer(MockRedisServer):
        async def execute_command(self, *args) -> Any:
            cmd = str(args[0]).upper()
            if cmd == "FT.SEARCH":
                keys = [k.encode() if isinstance(k, str) else k for k in self.hashes.keys()]
                return {
                    b"total_results": len(keys),
                    b"results": [{b"id": k} for k in keys],
                }
            return await super().execute_command(*args)

    class Resp2RedisServer(MockRedisServer):
        async def execute_command(self, *args) -> Any:
            cmd = str(args[0]).upper()
            if cmd == "FT.SEARCH":
                keys = [k.encode() if isinstance(k, str) else k for k in self.hashes.keys()]
                return [len(keys), *keys]
            return await super().execute_command(*args)

    # Test RESP3
    r3 = Resp3RedisServer()
    r3.hashes["l1:cache:test:v1:abc"] = {b"a": b"1"}
    r3.hashes["l1:cache:test:v1:def"] = {b"a": b"2"}
    cache3 = L1SemanticCache(redis_client=r3)
    del3 = await cache3.invalidate_version("test", "v1")
    assert del3 == 2
    assert len(r3.hashes) == 0

    # Test RESP2
    r2 = Resp2RedisServer()
    r2.hashes["l1:cache:test:v1:abc"] = {b"a": b"1"}
    r2.hashes["l1:cache:test:v1:def"] = {b"a": b"2"}
    cache2 = L1SemanticCache(redis_client=r2)
    del2 = await cache2.invalidate_version("test", "v1")
    assert del2 == 2
    assert len(r2.hashes) == 0


@pytest.mark.asyncio
async def test_real_redisearch_l1_cache():
    """Live integration test: verifies L1 semantic caching against the real RediSearch binary."""
    import os
    import subprocess
    import time
    import redis.asyncio as aioredis

    redis_bin = os.path.abspath("redis-stack-server-7.2.0-v9/bin/redis-server")
    redisearch_so = os.path.abspath("redis-stack-server-7.2.0-v9/lib/redisearch.so")
    rejson_so = os.path.abspath("redis-stack-server-7.2.0-v9/lib/rejson.so")

    if not (os.path.exists(redis_bin) and os.path.exists(redisearch_so)):
        pytest.skip("Standalone redis-server or redisearch.so not available")

    sock_path = f"/tmp/pytest_redis_{os.getpid()}.sock"
    if os.path.exists(sock_path):
        os.remove(sock_path)

    proc = subprocess.Popen([
        redis_bin,
        "--loadmodule", redisearch_so,
        "--loadmodule", rejson_so,
        "--port", "0",
        "--unixsocket", sock_path,
        "--unixsocketperm", "777",
        "--dir", "/tmp"
    ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    try:
        await asyncio.sleep(0.8)
        r = aioredis.Redis(unix_socket_path=sock_path)
        await r.ping()

        cache = L1SemanticCache(
            redis_client=r,
            index_name=f"test_l1_idx_{os.getpid()}",
            prefix=f"l1:test_{os.getpid()}:",
            similarity_threshold=0.85,
            confident_threshold=0.96,
        )
        await cache.init_index()

        # Seed embedding
        rng = np.random.default_rng(123)
        v1 = rng.normal(0, 1.0, size=384).astype(np.float32)
        v1 /= np.linalg.norm(v1)

        await cache.set(
            prompt="What is gradient descent?",
            query_embedding=v1,
            response={"answer": "First-order optimization algorithm."},
            thesis_id="ml_thesis",
            chapter="ch_2",
            doc_version="v1",
            model_tier="cpu_slm",
            language="en",
        )

        # 1. Exact Hit
        hit1 = await cache.get(
            query_embedding=v1,
            thesis_id="ml_thesis",
            chapter="ch_2",
            doc_version="v1",
            prompt="What is gradient descent?",
        )
        assert hit1 is not None
        assert hit1["similarity"] == 1.0
        assert hit1["response"]["answer"] == "First-order optimization algorithm."

        # 2. Paraphrase Hit (~0.94 similarity)
        ortho = rng.normal(0, 1.0, size=384).astype(np.float32)
        ortho -= np.dot(ortho, v1) * v1
        ortho /= np.linalg.norm(ortho)
        v_para = 0.94 * v1 + np.sqrt(1.0 - 0.94**2) * ortho
        v_para /= np.linalg.norm(v_para)

        hit_para = await cache.get(
            query_embedding=v_para,
            thesis_id="ml_thesis",
            chapter="ch_2",
            doc_version="v1",
            prompt="Can you explain gradient descent?",
        )
        assert hit_para is not None
        assert hit_para["similarity"] >= 0.85

        # 3. Entity Guard cross-thesis Miss
        hit_cross = await cache.get(
            query_embedding=v1,
            thesis_id="other_thesis",
            chapter="ch_2",
            doc_version="v1",
            prompt="What is gradient descent?",
        )
        assert hit_cross is None

        # 4. Context Model Tier Mismatch Miss
        hit_tier = await cache.get(
            query_embedding=v1,
            thesis_id="ml_thesis",
            chapter="ch_2",
            doc_version="v1",
            prompt="What is gradient descent?",
            model_tier="gpu_vllm",
        )
        assert hit_tier is None

        # 5. Invalidation
        del_count = await cache.invalidate_version("ml_thesis", "v1")
        assert del_count >= 1

        hit_after = await cache.get(
            query_embedding=v1,
            thesis_id="ml_thesis",
            chapter="ch_2",
            doc_version="v1",
            prompt="What is gradient descent?",
        )
        assert hit_after is None

        await r.aclose()
    finally:
        proc.terminate()
        proc.wait(timeout=3)
        if os.path.exists(sock_path):
            try:
                os.remove(sock_path)
            except OSError:
                pass


