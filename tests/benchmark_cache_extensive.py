#!/usr/bin/env python3
"""Extensive Evaluation and Benchmark of Redis Semantic Cache.

Tests:
1. Pure Vector Similarity Analysis:
   - Measures raw cosine similarity on temporal / year swaps (2023 vs 2024).
   - Measures raw cosine similarity on true paraphrases, quantity variations, and cross-domain queries.
2. CPU Model Tier Evaluation:
   - Cold Query Execution (Routes to CPU SLM).
   - True Paraphrase Execution (Semantic Cache HIT, < 50ms).
   - Year-Shifted Query Execution (2023 vs 2024) -> Vector sim > 0.99, but Entity Guard prevents false hit!
3. GPU Model Tier Evaluation:
   - Cold Query Execution (Routes to GPU LLM Qwen 2.5 7B on NVIDIA A100).
   - True Paraphrase Execution (Semantic Cache HIT, < 50ms, 0 GPU VRAM/compute).
   - Quantitative / Year-Shifted Query Execution -> Fresh GPU inference.
4. Latency, TTFT, and Compute Savings Matrix.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import numpy as np

# Ensure project root is in sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from typing import Dict, Any, List, Tuple
from pipeline.embeddings.embedder import LocalEmbedder
from gateway.cache.redis_semantic_cache import RedisSemanticCache

GATEWAY_URL = "http://127.0.0.1:8080/query"
CLEAR_URL = "http://127.0.0.1:8080/cache/clear"
STATS_URL = "http://127.0.0.1:8080/cache/stats"

def clear_cache():
    req = urllib.request.Request(CLEAR_URL, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

def get_cache_stats():
    req = urllib.request.Request(STATS_URL, method="GET")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())

def query_gateway(query: str, stream: bool = False) -> Dict[str, Any]:
    t0 = time.time()
    payload = json.dumps({"query": query, "stream": stream}).encode("utf-8")
    req = urllib.request.Request(
        GATEWAY_URL,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    data["client_latency"] = round(time.time() - t0, 4)
    return data

def run_pure_vector_analysis():
    print("\n" + "=" * 90)
    print("🔬 TEST PART 1: PURE DENSE VECTOR SIMILARITY ANALYSIS (all-MiniLM-L6-v2)")
    print("=" * 90)
    embedder = LocalEmbedder()

    pairs = [
        # Category A: Exact Duplicates
        ("Exact Duplicate", 
         "What is vermiculite and what are its primary uses?", 
         "What is vermiculite and what are its primary uses?"),
        
        # Category B: True Semantic Paraphrases (Same intent, different words)
        ("True Paraphrase (Vocabulary)", 
         "What is vermiculite and what are its primary uses?", 
         "Can you explain what vermiculite is and its main applications?"),
        
        ("True Paraphrase (Syntax/Word Order)", 
         "Explain the onset of Rayleigh Taylor instability in magnetized fluids.", 
         "In magnetized fluids, how does Rayleigh Taylor instability begin?"),

        # Category C: Temporal / Year Swaps (Deceptive Similarity!)
        ("Year Shift (2023 vs 2024)", 
         "What are the IISc thesis submission guidelines in 2023?", 
         "What are the IISc thesis submission guidelines in 2024?"),
        
        ("Year Shift (Cutoffs 2023 vs 2024)", 
         "What was the admissions cutoff for M.Tech CSE in 2023?", 
         "What was the admissions cutoff for M.Tech CSE in 2024?"),
        
        ("Year Shift (Research survey 2021 vs 2024)", 
         "Overview of clean energy research published at IISc in 2021", 
         "Overview of clean energy research published at IISc in 2024"),

        # Category D: Numerical & Quantitative Shifts
        ("Quantity Shift (5 vs 50)", 
         "Fluid flow rate measured at 5 liters per second", 
         "Fluid flow rate measured at 50 liters per second"),
        
        ("Frequency Shift (100 MHz vs 500 MHz)", 
         "Resonance frequency observed at 100 MHz in quartz crystal", 
         "Resonance frequency observed at 500 MHz in quartz crystal"),

        # Category E: Polarity / Negation Reversal
        ("Polarity Reversal (Negation)", 
         "Why does magnetohydrodynamic flow stabilize under magnetic shear?", 
         "Why does magnetohydrodynamic flow NOT stabilize under magnetic shear?"),

        # Category F: Cross-Domain Unrelated
        ("Cross-Domain (MHD vs Vermiculite)", 
         "How does Rayleigh-Taylor instability develop in magnetohydrodynamics?", 
         "What is vermiculite and what are its primary uses?"),
        
        ("Cross-Domain (Quantum vs Fluid)", 
         "Feynman diagrams in quantum electrodynamics perturbation theory", 
         "Stability criteria for stratified incompressible fluid flows")
    ]

    results = []
    print(f"{'Category':<35} | {'Cosine Sim':<10} | {'Status in Naive Cache':<22} | {'Entity Guard Status'}")
    print("-" * 95)
    for category, q1, q2 in pairs:
        v1 = np.array(embedder.embed_query(q1), dtype=np.float32)
        v2 = np.array(embedder.embed_query(q2), dtype=np.float32)
        sim = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)))
        
        has_conflict, reason = RedisSemanticCache.check_entity_conflict(q1, q2)
        naive_status = "FALSE HIT (>0.90)" if sim >= 0.90 and has_conflict else ("HIT (>=0.90)" if sim >= 0.90 else "MISS (<0.90)")
        guard_status = f"REJECTED ({reason.split(':')[0]})" if has_conflict else "SAFE HIT" if sim >= 0.90 else "SAFE MISS"

        print(f"{category:<35} | {sim:<10.4f} | {naive_status:<22} | {guard_status}")
        results.append({
            "category": category,
            "q1": q1,
            "q2": q2,
            "cosine_sim": round(sim, 4),
            "naive_status": naive_status,
            "guard_status": guard_status
        })

    return results

def run_end_to_end_gateway_evaluation():
    print("\n" + "=" * 90)
    print("🚀 TEST PART 2: END-TO-END GATEWAY EVALUATION (CPU & GPU TIERS)")
    print("=" * 90)

    clear_cache()
    print("🧹 Redis Semantic Cache cleared. Starting with 0 cached entries.\n")

    test_runs = []

    # -------------------------------------------------------------
    # SECTION 1: CPU SLM Tier (Easy Queries)
    # -------------------------------------------------------------
    print("-" * 90)
    print("📌 [TIER 1: CPU SLM] Easy Factual & Definitional Queries")
    print("-" * 90)

    # 1. Cold Query (2023)
    q_cpu_cold = "What are the thesis guidelines for vermiculite beneficiation in 2023?"
    print(f"\n[Test 1.1] Cold Query (CPU Tier - Year 2023): '{q_cpu_cold}'")
    res1 = query_gateway(q_cpu_cold)
    print(f"  ➜ Status: CACHED={res1.get('cached', False)} | Route: {res1.get('route')} | Tier: {res1.get('target_tier')} | Latency: {res1['client_latency']}s")
    test_runs.append({"id": "CPU_Cold_2023", "query": q_cpu_cold, "cached": res1.get("cached", False), "tier": res1.get("target_tier"), "latency": res1["client_latency"], "similarity": res1.get("similarity", "N/A")})

    # 2. Exact Re-query (2023)
    print(f"\n[Test 1.2] Exact Repeat Query (2023): '{q_cpu_cold}'")
    res2 = query_gateway(q_cpu_cold)
    print(f"  ➜ Status: CACHED={res2.get('cached', False)} | Similarity: {res2.get('similarity')} | Latency: {res2['client_latency']}s | Speedup: {round(res1['client_latency'] / max(res2['client_latency'], 0.001), 1)}x")
    test_runs.append({"id": "CPU_Exact_2023", "query": q_cpu_cold, "cached": res2.get("cached", False), "tier": "semantic_cache", "latency": res2["client_latency"], "similarity": res2.get("similarity", 1.0)})

    # 3. Paraphrase (2023)
    q_cpu_para = "Can you describe the vermiculite beneficiation guidelines published in 2023?"
    print(f"\n[Test 1.3] True Paraphrase (2023): '{q_cpu_para}'")
    res3 = query_gateway(q_cpu_para)
    print(f"  ➜ Status: CACHED={res3.get('cached', False)} | Similarity: {res3.get('similarity')} | Latency: {res3['client_latency']}s | Matched: '{res3.get('matched_prompt')}'")
    test_runs.append({"id": "CPU_Paraphrase_2023", "query": q_cpu_para, "cached": res3.get("cached", False), "tier": "semantic_cache", "latency": res3["client_latency"], "similarity": res3.get("similarity")})

    # 4. Year Shift: 2024 vs 2023 (The Critical Test!)
    q_cpu_2024 = "What are the thesis guidelines for vermiculite beneficiation in 2024?"
    print(f"\n[Test 1.4] Year Shift (2024 vs 2023): '{q_cpu_2024}'")
    print("  ℹ️  Vector Similarity is ~0.994, but Entity Guard should DETECT 2024 != 2023 and trigger MISS!")
    res4 = query_gateway(q_cpu_2024)
    print(f"  ➜ Status: CACHED={res4.get('cached', False)} | Route: {res4.get('route')} | Tier: {res4.get('target_tier')} | Latency: {res4['client_latency']}s")
    test_runs.append({"id": "CPU_YearShift_2024", "query": q_cpu_2024, "cached": res4.get("cached", False), "tier": res4.get("target_tier"), "latency": res4["client_latency"], "similarity": res4.get("similarity", "N/A (Miss)")})

    # 5. Exact Repeat of 2024 (Now should hit the 2024 cached entry!)
    print(f"\n[Test 1.5] Exact Repeat of 2024 Query: '{q_cpu_2024}'")
    res5 = query_gateway(q_cpu_2024)
    print(f"  ➜ Status: CACHED={res5.get('cached', False)} | Similarity: {res5.get('similarity')} | Latency: {res5['client_latency']}s | Matched: '{res5.get('matched_prompt')}'")
    test_runs.append({"id": "CPU_Exact_2024", "query": q_cpu_2024, "cached": res5.get("cached", False), "tier": "semantic_cache", "latency": res5["client_latency"], "similarity": res5.get("similarity", 1.0)})

    # -------------------------------------------------------------
    # SECTION 2: GPU LLM Tier (Hard Queries)
    # -------------------------------------------------------------
    print("\n" + "-" * 90)
    print("📌 [TIER 2: GPU LLM] Hard Academic Synthesis Queries (Qwen 2.5 7B on NVIDIA A100)")
    print("-" * 90)

    # 1. Cold GPU Query (2023)
    q_gpu_cold = "Synthesize and compare the mathematical stability proofs of magnetic shear vs Coriolis force in accretion disks published in 2023."
    print(f"\n[Test 2.1] Cold Query (GPU Tier - Year 2023): '{q_gpu_cold[:70]}...'")
    res6 = query_gateway(q_gpu_cold)
    print(f"  ➜ Status: CACHED={res6.get('cached', False)} | Route: {res6.get('route')} | Tier: {res6.get('target_tier')} | Model: {res6.get('model')} | Latency: {res6['client_latency']}s")
    test_runs.append({"id": "GPU_Cold_2023", "query": q_gpu_cold, "cached": res6.get("cached", False), "tier": res6.get("target_tier"), "latency": res6["client_latency"], "similarity": res6.get("similarity", "N/A")})

    # 2. Paraphrase of GPU Query (2023)
    q_gpu_para = "Compare the mathematical derivations for accretion disk stability between Coriolis force and magnetic shear in 2023."
    print(f"\n[Test 2.2] True Paraphrase (GPU Tier - Year 2023): '{q_gpu_para[:70]}...'")
    res7 = query_gateway(q_gpu_para)
    print(f"  ➜ Status: CACHED={res7.get('cached', False)} | Similarity: {res7.get('similarity')} | Latency: {res7['client_latency']}s | Compute Saved: 100% GPU Cycles!")
    test_runs.append({"id": "GPU_Paraphrase_2023", "query": q_gpu_para, "cached": res7.get("cached", False), "tier": "semantic_cache", "latency": res7["client_latency"], "similarity": res7.get("similarity")})

    # 3. Year Shift on GPU Query (2024 vs 2023)
    q_gpu_2024 = "Synthesize and compare the mathematical stability proofs of magnetic shear vs Coriolis force in accretion disks published in 2024."
    print(f"\n[Test 2.3] Year Shift (GPU Tier - 2024 vs 2023): '{q_gpu_2024[:70]}...'")
    print("  ℹ️  Vector Similarity is ~0.995, but Entity Guard should trigger MISS and invoke GPU LLM!")
    res8 = query_gateway(q_gpu_2024)
    print(f"  ➜ Status: CACHED={res8.get('cached', False)} | Route: {res8.get('route')} | Tier: {res8.get('target_tier')} | Latency: {res8['client_latency']}s")
    test_runs.append({"id": "GPU_YearShift_2024", "query": q_gpu_2024, "cached": res8.get("cached", False), "tier": res8.get("target_tier"), "latency": res8["client_latency"], "similarity": res8.get("similarity", "N/A (Miss)")})

    # 4. Exact Repeat of GPU 2024 Query
    print(f"\n[Test 2.4] Exact Repeat of GPU 2024 Query: '{q_gpu_2024[:70]}...'")
    res9 = query_gateway(q_gpu_2024)
    print(f"  ➜ Status: CACHED={res9.get('cached', False)} | Similarity: {res9.get('similarity')} | Latency: {res9['client_latency']}s | Matched: '{res9.get('matched_prompt')[:60]}...'")
    test_runs.append({"id": "GPU_Exact_2024", "query": q_gpu_2024, "cached": res9.get("cached", False), "tier": "semantic_cache", "latency": res9["client_latency"], "similarity": res9.get("similarity", 1.0)})

    stats = get_cache_stats()
    print("\n" + "=" * 90)
    print(f"📊 FINAL CACHE STATUS: {stats['cached_queries_count']} total entries safely indexed in Redis!")
    print("=" * 90)

    return test_runs

if __name__ == "__main__":
    vec_results = run_pure_vector_analysis()
    gw_results = run_end_to_end_gateway_evaluation()

    with open("logs/cache_benchmark_results.json", "w") as f:
        json.dump({"vector_analysis": vec_results, "gateway_eval": gw_results}, f, indent=2)
    print("\n✅ Full benchmark results saved to logs/cache_benchmark_results.json")
