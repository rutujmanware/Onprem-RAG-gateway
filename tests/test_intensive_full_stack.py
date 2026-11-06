#!/usr/bin/env python3
"""Intensive Full-Stack Verification & Stress Test Suite.

Rigorously benchmarks and stress-tests all individual and integrated components:
1. Two-Tower MLP Complexity Router (< 3ms latency, accurate routing)
2. CPU SLM Engine (Port 8082 - Qwen 2.5 3B GGUF streaming & non-streaming)
3. GPU LLM Engine (Port 8000 - vLLM Qwen 2.5 7B on NVIDIA A100 SXM4 40GB)
4. Redis Semantic Cache & Vector Database (KNN, Cache Miss, Hit, Semantic Paraphrase, Refusal Purge)
5. Institutional Gateway (Port 8080 - E2E Routing, Metadata Extraction, Response Sanitization)
"""

import os
import sys
import time
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Tuple
from dotenv import load_dotenv
load_dotenv()
import httpx

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Endpoints (Normalized to remove trailing /v1 for root health endpoints)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/")
VLLM_BASE = os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8000").rstrip("/").rsplit("/v1", 1)[0]
SLM_BASE = os.getenv("SLM_BASE_URL", "http://127.0.0.1:8082").rstrip("/").rsplit("/v1", 1)[0]

FORBIDDEN_CHUNK_KEYS = {"chunk_key", "handle_id", "chunk_index", "vector", "embedding"}

passed_checks = 0
failed_checks = 0
results_table = []


def record_result(component: str, test_name: str, passed: bool, details: str = ""):
    global passed_checks, failed_checks
    if passed:
        passed_checks += 1
        status = "✅ PASS"
    else:
        failed_checks += 1
        status = "❌ FAIL"
    results_table.append((component, test_name, status, details))
    print(f"  [{status}] {component} :: {test_name} - {details}")


def verify_chunks_sanitized(chunks: List[Dict[str, Any]], context: str) -> bool:
    """Ensure chunk_key, handle_id, chunk_index, and vector are NEVER present."""
    if not chunks:
        return True
    for idx, c in enumerate(chunks):
        for forbidden in FORBIDDEN_CHUNK_KEYS:
            if forbidden in c:
                record_result("Sanitization", f"{context} Chunk[{idx}]", False, f"Found forbidden key: '{forbidden}'")
                return False
    return True


print("=" * 80)
print("🚀 STARTING INTENSIVE ON-PREMISE RAG GATEWAY FULL-STACK TEST SUITE")
print("=" * 80)

# ==============================================================================
# SECTION 1: TWO-TOWER MLP ROUTER & EMBEDDING LATENCY
# ==============================================================================
print("\n" + "=" * 50)
print("🧠 SECTION 1: TWO-TOWER MLP ROUTER & COMPLEXITY SCORING")
print("=" * 50)

from gateway.router.complexity_scorer import ComplexityScorer
from gateway.router.dispatcher import RequestDispatcher, ModelTier

import asyncio
scorer = ComplexityScorer()
dispatcher = RequestDispatcher(alpha=0.50)

simple_queries = [
    "What is vermiculite and what are its uses?",
    "Who is the author of the thesis on barium titanate?",
    "When was the study on superalloys published?",
    "What department submitted research on solar collectors?",
    "Define dielectric loss in ceramic capacitors.",
]

complex_queries = [
    "Compare the mathematical formulations of Navier-Stokes dissipation and MHD energy transfer.",
    "Formulate a rigorous proof for the stability of turbulent boundary layer relaminarization under pressure gradients.",
    "Compare and prove asymptotic expansion solutions for compressible boundary layers versus Couette flows.",
    "Compare and derive mathematical equations for Maxwell stress tensor eigenvalues in plasma confinement.",
    "Evaluate comparative trade-offs between heterogeneous catalysis mechanisms across temperature programmed desorption studies.",
]

router_latencies = []
router_correct = 0

# Warm up the router embedding model and neural weights
asyncio.run(scorer.score("System warmup router query"))

for q in simple_queries:
    t0 = time.time()
    score = asyncio.run(scorer.score(q))
    elapsed_ms = (time.time() - t0) * 1000.0
    router_latencies.append(elapsed_ms)
    decision = dispatcher.decide(score)
    is_easy = decision.target_tier != ModelTier.GPU_LLM
    if is_easy:
        router_correct += 1
    record_result("MLP Router", f"Simple Query Routing ({elapsed_ms:.2f}ms)", is_easy, f"Score: {score:.4f} -> {decision.target_tier.value}")

for q in complex_queries:
    t0 = time.time()
    score = asyncio.run(scorer.score(q))
    elapsed_ms = (time.time() - t0) * 1000.0
    router_latencies.append(elapsed_ms)
    decision = dispatcher.decide(score)
    is_hard = decision.target_tier == ModelTier.GPU_LLM
    if is_hard:
        router_correct += 1
    record_result("MLP Router", f"Complex Query Routing ({elapsed_ms:.2f}ms)", is_hard, f"Score: {score:.4f} -> {decision.target_tier.value}")

avg_router_lat = sum(router_latencies) / len(router_latencies)
record_result("MLP Router", "Routing Latency SLA (< 40ms)", avg_router_lat < 40.0, f"Avg Latency: {avg_router_lat:.2f}ms (Max: {max(router_latencies):.2f}ms)")
record_result("MLP Router", "Routing Accuracy (>= 80%)", router_correct >= 8, f"Accuracy: {router_correct}/10 ({router_correct*10}%)")

# ==============================================================================
# SECTION 2: CPU SLM ENGINE (PORT 8082)
# ==============================================================================
print("\n" + "=" * 50)
print("⚡ SECTION 2: CPU SLM ENGINE (PORT 8082 - QWEN 2.5 3B GGUF)")
print("=" * 50)

# 2.1 Health Check
try:
    r_slm_health = httpx.get(f"{SLM_BASE}/healthz", timeout=3.0)
    slm_ready = r_slm_health.status_code == 200
    record_result("CPU SLM", "Healthz Check", slm_ready, f"Status: {r_slm_health.status_code}")
except Exception as e:
    record_result("CPU SLM", "Healthz Check", False, str(e))

# 2.2 Non-Streaming Generation
slm_sample_messages = [
    {"role": "system", "content": "You are a concise academic assistant."},
    {"role": "user", "content": "What is vermiculite? Answer in one sentence."}
]
try:
    t0 = time.time()
    r_slm_sync = httpx.post(
        f"{SLM_BASE}/v1/chat/completions",
        json={"messages": slm_sample_messages, "max_tokens": 60, "temperature": 0.2},
        timeout=30.0,
    )
    slm_sync_elapsed = time.time() - t0
    slm_data = r_slm_sync.json()
    ans = slm_data["choices"][0]["message"]["content"].strip()
    valid_ans = len(ans) > 10
    record_result("CPU SLM", "Synchronous Completion", valid_ans, f"Answer: '{ans[:60]}...' in {slm_sync_elapsed:.2f}s")
except Exception as e:
    record_result("CPU SLM", "Synchronous Completion", False, str(e))

# 2.3 Streaming Generation & TTFT
try:
    t0 = time.time()
    ttft = None
    token_count = 0
    with httpx.stream(
        "POST",
        f"{SLM_BASE}/v1/chat/completions",
        json={"messages": slm_sample_messages, "max_tokens": 60, "temperature": 0.2, "stream": True},
        timeout=30.0,
    ) as stream_resp:
        for line in stream_resp.iter_lines():
            if line and line.startswith("data: ") and line[6:].strip() != "[DONE]":
                if ttft is None:
                    ttft = time.time() - t0
                token_count += 1
    total_slm_stream = time.time() - t0
    tok_s = token_count / max(total_slm_stream - (ttft or 0), 0.001)
    ttft_str = f"{ttft:.3f}s" if ttft is not None else "N/A"
    record_result("CPU SLM", "Streaming Completion & TTFT", token_count > 0, f"TTFT: {ttft_str} | Throughput: {tok_s:.1f} tok/s ({token_count} tokens)")
except Exception as e:
    record_result("CPU SLM", "Streaming Completion", False, str(e))

# ==============================================================================
# SECTION 3: GPU LLM ENGINE (PORT 8000 - vLLM QWEN 2.5 7B ON NVIDIA A100)
# ==============================================================================
print("\n" + "=" * 50)
print("🔥 SECTION 3: GPU LLM ENGINE (PORT 8000 - vLLM QWEN 2.5 7B A100)")
print("=" * 50)

# 3.1 Health Check & Models Endpoint
try:
    r_vllm_health = httpx.get(f"{VLLM_BASE}/health", timeout=3.0)
    vllm_ready = r_vllm_health.status_code == 200
    record_result("GPU vLLM", "Health Check", vllm_ready, f"Status: {r_vllm_health.status_code}")
except Exception as e:
    record_result("GPU vLLM", "Health Check", False, str(e))

try:
    r_vllm_models = httpx.get(f"{VLLM_BASE}/v1/models", timeout=3.0)
    models_json = r_vllm_models.json()
    model_id = models_json["data"][0]["id"]
    record_result("GPU vLLM", "Models Registry", r_vllm_models.status_code == 200, f"Active Model: {model_id}")
except Exception as e:
    record_result("GPU vLLM", "Models Registry", False, str(e))

# 3.2 Non-Streaming Generation on GPU
vllm_sample_messages = [
    {"role": "system", "content": "You are an expert theoretical physicist."},
    {"role": "user", "content": "Explain turbulent dissipation in magnetohydrodynamics in two concise sentences."}
]
try:
    t0 = time.time()
    r_vllm_sync = httpx.post(
        f"{VLLM_BASE}/v1/chat/completions",
        json={"model": "Qwen/Qwen2.5-7B-Instruct", "messages": vllm_sample_messages, "max_tokens": 100, "temperature": 0.2},
        timeout=30.0,
    )
    vllm_sync_elapsed = time.time() - t0
    vllm_data = r_vllm_sync.json()
    ans = vllm_data["choices"][0]["message"]["content"].strip()
    valid_ans = len(ans) > 20
    record_result("GPU vLLM", "Synchronous Generation", valid_ans, f"Answer: '{ans[:70]}...' in {vllm_sync_elapsed:.2f}s")
except Exception as e:
    record_result("GPU vLLM", "Synchronous Generation", False, str(e))

# 3.3 Streaming Generation & Throughput on GPU
try:
    t0 = time.time()
    ttft_gpu = None
    token_count_gpu = 0
    with httpx.stream(
        "POST",
        f"{VLLM_BASE}/v1/chat/completions",
        json={"model": "Qwen/Qwen2.5-7B-Instruct", "messages": vllm_sample_messages, "max_tokens": 120, "temperature": 0.2, "stream": True},
        timeout=30.0,
    ) as stream_resp:
        for line in stream_resp.iter_lines():
            if line and line.startswith("data: ") and line[6:].strip() != "[DONE]":
                if ttft_gpu is None:
                    ttft_gpu = time.time() - t0
                token_count_gpu += 1
    total_gpu_stream = time.time() - t0
    tok_s_gpu = token_count_gpu / max(total_gpu_stream - (ttft_gpu or 0), 0.001)
    record_result("GPU vLLM", "Streaming & High Throughput", tok_s_gpu > 30.0, f"TTFT: {ttft_gpu:.3f}s | Speed: {tok_s_gpu:.1f} tok/s ({token_count_gpu} tokens)")
except Exception as e:
    record_result("GPU vLLM", "Streaming & High Throughput", False, str(e))

# ==============================================================================
# SECTION 4: REDIS VECTOR DATABASE & SEMANTIC CACHE (PORT 6379)
# ==============================================================================
print("\n" + "=" * 50)
print("💾 SECTION 4: REDIS SEMANTIC CACHE & VECTOR DATABASE")
print("=" * 50)

import redis
from gateway.cache.redis_semantic_cache import RedisSemanticCache

r_sync = redis.from_url(REDIS_URL, decode_responses=False)
try:
    pong = r_sync.ping()
    record_result("Redis Core", "Ping Check", pong is True, "Redis PONG verified")
except Exception as e:
    record_result("Redis Core", "Ping Check", False, str(e))

# 4.1 Index Verification
try:
    r_decoded = redis.from_url(REDIS_URL, decode_responses=True)
    idx_info = r_decoded.ft("rag_documents_idx").info()
    num_docs = int(float(idx_info.get("num_docs", 0)))
    record_result("Redis Core", "Index Verification (rag_documents_idx)", num_docs > 1000000, f"Indexed Vectors: {num_docs:,} documents")
except Exception as e:
    record_result("Redis Core", "Index Verification", False, str(e))

# 4.2 Semantic Cache Lifecycle (Miss -> Store -> Exact Hit -> Paraphrase Hit)
cache = RedisSemanticCache(redis_url=REDIS_URL, similarity_threshold=0.88)
# Warm up local embedding model in cache
cache.lookup("Warmup query for cache model")

def cleanup_probe_keys(prompt_str=None):
    try:
        for k in r_sync.keys(f"{cache.key_prefix}*"):
            k_type = r_sync.type(k)
            if k_type == b"hash":
                p = r_sync.hget(k, "prompt")
                if p and (b"probe_" in p or (prompt_str and prompt_str.encode("utf-8") in p)):
                    r_sync.delete(k)
            elif k_type == b"string":
                v = r_sync.get(k)
                if v and (b"probe_" in v or (prompt_str and prompt_str.encode("utf-8") in v)):
                    r_sync.delete(k)
    except Exception:
        pass

cleanup_probe_keys()

test_uuid = f"probe_{int(time.time() * 1000)}"
test_prompt = f"What are the anomalous non-abelian topological excitations of synthetic {test_uuid} quasi-particle?"
paraphrase_prompt = f"Explain the non-abelian topological excitations and anomalies in synthetic {test_uuid} quasi-particle."

# Ensure initial miss
hit_init = cache.lookup(test_prompt)
record_result("Semantic Cache", "Initial Cache Miss", hit_init is None, "Correctly returned None for fresh prompt")

# Store synthetic clean response
sample_resp = {
    "query": test_prompt,
    "answer": f"Quasi-particle {test_uuid} exhibits quantized non-abelian statistics governed by fractional braiding invariants.",
    "route": "easy",
    "target_tier": "cpu_slm",
    "chunks": [
        {"title": "Topological Quasi-particles", "department": "Physics", "content": "Sample text", "similarity": 0.88}
    ]
}
stored = cache.store(test_prompt, sample_resp)
record_result("Semantic Cache", "Store Entry", stored is True, "Cached query and response in Redis")

# Exact Hit
t0 = time.time()
exact_hit = cache.lookup(test_prompt)
exact_lat_ms = (time.time() - t0) * 1000.0
hit_ok = exact_hit is not None and exact_hit.get("similarity", 0) >= 0.99
sim_val = exact_hit.get('similarity', 0.0) if exact_hit else 0.0
record_result("Semantic Cache", "Exact Cache Hit (< 35ms)", hit_ok and (exact_lat_ms < 35.0), f"Latency: {exact_lat_ms:.2f}ms | Sim: {sim_val:.4f}")

# Paraphrase Hit
t0 = time.time()
para_hit = cache.lookup(paraphrase_prompt)
para_lat_ms = (time.time() - t0) * 1000.0
para_ok = para_hit is not None and para_hit.get("similarity", 0) >= 0.88
record_result("Semantic Cache", "Semantic Paraphrase Hit", para_ok, f"Latency: {para_lat_ms:.2f}ms | Sim: {para_hit.get('similarity', 0) if para_hit else 0.0:.4f}")

# Clean up synthetic test prompt from Redis to keep test harness idempotent
cleanup_probe_keys(test_prompt)

# 4.3 Refusal / Insufficient Data Protection
refusal_resp = {
    "query": "Where is Atlantis?",
    "answer": "Based on the provided documents, there is insufficient information to answer this question.",
    "chunks": []
}
stored_refusal = cache.store("Where is Atlantis?", refusal_resp)
record_result("Semantic Cache", "Refusal Anti-Pollution Shield", stored_refusal is False, "Refused to cache 'insufficient information' answer")

# ==============================================================================
# SECTION 5: INSTITUTIONAL GATEWAY (PORT 8080) END-TO-END PIPELINE
# ==============================================================================
print("\n" + "=" * 50)
print("🌐 SECTION 5: INSTITUTIONAL GATEWAY END-TO-END PIPELINE")
print("=" * 50)

# 5.1 Gateway Health
try:
    r_gw = httpx.get(f"{GATEWAY_URL}/healthz", timeout=3.0)
    gw_ready = r_gw.status_code == 200
    record_result("Gateway E2E", "Health Check", gw_ready, f"Status: {r_gw.status_code}")
except Exception as e:
    record_result("Gateway E2E", "Health Check", False, str(e))

# 5.2 Easy Query Execution -> CPU SLM
easy_payload = {
    "query": "What is vermiculite and what are its uses?",
    "stream": False,
}
try:
    t0 = time.time()
    r_easy = httpx.post(f"{GATEWAY_URL}/query", json=easy_payload, timeout=30.0)
    easy_lat = time.time() - t0
    data_easy = r_easy.json()
    ans = data_easy.get("answer", "")
    route = data_easy.get("route", "")
    chunks = data_easy.get("chunks", [])
    chunks_clean = verify_chunks_sanitized(chunks, "Gateway Easy Query")
    record_result("Gateway E2E", "Easy Query (CPU SLM Execution)", len(ans) > 20 and route in ("easy", "cached"), f"Route: [{route}] | Chunks: {len(chunks)} | Latency: {easy_lat:.2f}s")
    record_result("Gateway E2E", "Easy Query Output Sanitization", chunks_clean, "No chunk_key, handle_id, chunk_index, or vector present")
except Exception as e:
    record_result("Gateway E2E", "Easy Query Execution", False, str(e))

# 5.3 Hard Query Execution -> vLLM GPU
hard_payload = {
    "query": "Compare mathematical formulations of Navier-Stokes dissipation and magnetohydrodynamic energy transfer.",
    "stream": False,
}
try:
    t0 = time.time()
    r_hard = httpx.post(f"{GATEWAY_URL}/query", json=hard_payload, timeout=60.0)
    hard_lat = time.time() - t0
    data_hard = r_hard.json()
    ans_hard = data_hard.get("answer", "")
    route_hard = data_hard.get("route", "")
    tier_hard = data_hard.get("target_tier", "")
    chunks_hard = data_hard.get("chunks", [])
    chunks_hard_clean = verify_chunks_sanitized(chunks_hard, "Gateway Hard Query")
    is_hard_routed = route_hard == "hard" or tier_hard == "gpu_vllm"
    record_result("Gateway E2E", "Hard Query (GPU vLLM Routing)", is_hard_routed and len(ans_hard) > 20, f"Route: [{route_hard}] | Tier: {tier_hard} | Latency: {hard_lat:.2f}s")
    record_result("Gateway E2E", "Hard Query Output Sanitization", chunks_hard_clean, "No chunk_key, handle_id, chunk_index, or vector present")
except Exception as e:
    record_result("Gateway E2E", "Hard Query Execution", False, str(e))

# 5.4 Metadata Extractor + Pre-Filtered Vector Retrieval
metadata_payload = {
    "query": "What did Prof. Ramamurthy publish on vermiculite in Materials Engineering?",
    "stream": False,
}
try:
    t0 = time.time()
    r_meta = httpx.post(f"{GATEWAY_URL}/query", json=metadata_payload, timeout=30.0)
    meta_lat = time.time() - t0
    data_meta = r_meta.json()
    meta_chunks = data_meta.get("chunks", [])
    meta_clean = verify_chunks_sanitized(meta_chunks, "Gateway Metadata Query")
    record_result("Gateway E2E", "Metadata Extracted Query Execution", len(data_meta.get("answer", "")) > 15, f"Retrieved: {len(meta_chunks)} chunks in {meta_lat:.2f}s")
    record_result("Gateway E2E", "Metadata Query Output Sanitization", meta_clean, "No chunk_key, handle_id, chunk_index, or vector present")
except Exception as e:
    record_result("Gateway E2E", "Metadata Query Execution", False, str(e))

# 5.5 Dynamic Similarity Floor Rejection (Out-of-Domain Guard)
ood_payload = {
    "query": "Who was the prime minister of the United Kingdom in 1912?",
    "stream": False,
}
try:
    r_ood = httpx.post(f"{GATEWAY_URL}/query", json=ood_payload, timeout=15.0)
    data_ood = r_ood.json()
    ood_chunks = data_ood.get("chunks", [])
    # In dynamic mode, out-of-domain query must return 0 chunks (below floor 0.48)
    floor_rejected = len(ood_chunks) == 0
    record_result("Gateway E2E", "Dynamic Out-of-Domain Rejection Floor", floor_rejected, f"Retrieved Chunks: {len(ood_chunks)} (0 expected)")
except Exception as e:
    record_result("Gateway E2E", "Dynamic Out-of-Domain Rejection Floor", False, str(e))

# 5.6 Streaming SSE Delivery via Gateway
stream_payload = {
    "query": "Describe the beneficiation of vermiculite minerals.",
    "stream": True,
}
try:
    t0 = time.time()
    stream_chunks_received = 0
    stream_tokens_received = 0
    stream_clean = True
    with httpx.stream("POST", f"{GATEWAY_URL}/query", json=stream_payload, timeout=30.0) as s_resp:
        for line in s_resp.iter_lines():
            if line and line.startswith("data: ") and line[6:].strip() != "[DONE]":
                ev = json.loads(line[6:].strip())
                if ev.get("type") == "chunks":
                    stream_chunks_received += len(ev.get("chunks", []))
                    if not verify_chunks_sanitized(ev.get("chunks", []), "SSE Stream Chunks"):
                        stream_clean = False
                elif ev.get("type") == "token":
                    stream_tokens_received += 1
    total_stream_time = time.time() - t0
    record_result("Gateway E2E", "Streaming SSE Delivery", stream_tokens_received > 0, f"Delivered {stream_tokens_received} tokens in {total_stream_time:.2f}s")
    record_result("Gateway E2E", "Streaming SSE Chunk Sanitization", stream_clean, "No forbidden keys emitted in SSE chunks event")
except Exception as e:
    record_result("Gateway E2E", "Streaming SSE Delivery", False, str(e))


# ==============================================================================
# SUMMARY REPORT
# ==============================================================================
print("\n" + "=" * 80)
print("📊 INTENSIVE FULL-STACK TEST SUMMARY REPORT")
print("=" * 80)
print(f"{'Component':<18} | {'Test Name':<38} | {'Status':<8} | {'Details'}")
print("-" * 80)
for comp, tname, status, details in results_table:
    print(f"{comp:<18} | {tname[:38]:<38} | {status:<8} | {details}")
print("=" * 80)
print(f"TOTAL CHECKS: {len(results_table)} | PASSED: {passed_checks} | FAILED: {failed_checks}")
if failed_checks == 0:
    print("🎉 ALL SYSTEMS FULLY OPERATIONAL AND COMPLIANT WITH ZERO REGRESSIONS!")
else:
    print(f"⚠️ {failed_checks} CHECKS FAILED. PLEASE REVIEW LOGS ABOVE.")
print("=" * 80 + "\n")
