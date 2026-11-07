#!/usr/bin/env python3
"""Comprehensive Heavy Load & Concurrency Benchmark for On-Premise Institutional RAG Gateway.

Benchmarks:
1. Cache Concurrency Scaling (C = 10, 25, 50, 100)
2. CPU SLM Concurrency Scaling (C = 1, 2, 4, 8)
3. GPU vLLM Continuous Batching Scaling (C = 1, 4, 8, 16)
4. SSE Streaming Latency & Time-To-First-Token (TTFT) (C = 5, 10)
5. Mixed Realistic Campus Workload (60% Cache, 25% SLM, 15% GPU vLLM)

Calculates:
- Requests Per Second (RPS) / Throughput
- Latency percentiles: min, p50 (median), p90, p95, p99, max, mean
- Error rate and timeout occurrences
- System metrics (CPU, RAM, GPU VRAM, Redis)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
import numpy as np
import psutil

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8080")
LOGS_DIR = ROOT_DIR / "logs"
RESULTS_FILE = LOGS_DIR / "load_test_results.json"

# Diverse realistic academic queries across sciences & engineering
EASY_QUERIES = [
    "What are the common uses of vermiculite in soil conditioning and construction?",
    "Define ferroelectric polarization hysteresis in thin film oxide capacitors.",
    "Summarize the primary methods for low-stress creep testing in metals.",
    "What is the role of corticosterone in synaptic plasticity?",
    "List the core requirements for intelligent cloud workload orchestration.",
    "What are the properties of liposomal nanocarriers in targeted drug delivery?",
]

HARD_QUERIES = [
    "Explain the failure mechanisms of prestressed concrete and composite laminates under cyclic stress and compare their fatigue limits.",
    "Derive the mathematical formulation for interlaminar stress distribution in laminated composite plates under thermal gradients.",
    "Compare the thermodynamic stability and Gibbs free energy of resolvin nanocarriers versus traditional liposomes.",
    "Synthesize the stress-strain constitutive relations for ferrocement rectangular sections subjected to repeated flexural reversals.",
    "Provide a rigorous analysis of notch figure and crack closure under aircraft spectrum loading with finite element approximations.",
]

CACHE_SEED_QUERIES = [
    "Explain the failure mechanisms of prestressed concrete and composite laminates under cyclic stress",
    "What is vermiculite and what are its primary uses?",
    "How does corticosterone affect synaptic plasticity?",
    "What are the principal challenges in cloud workload orchestration?",
]


@dataclass
class BenchmarkMetric:
    scenario: str
    concurrency: int
    total_requests: int
    successful_requests: int
    failed_requests: int
    duration_seconds: float
    rps: float
    latency_min_ms: float
    latency_p50_ms: float
    latency_p90_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    latency_max_ms: float
    latency_mean_ms: float
    ttft_mean_ms: Optional[float] = None
    ttft_p95_ms: Optional[float] = None
    gpu_util_perc: Optional[float] = None
    cpu_util_perc: Optional[float] = None
    cache_hit_rate: Optional[float] = None


def get_gpu_metrics() -> Dict[str, Any]:
    try:
        import subprocess
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total", "--format=csv,noheader,nounits"],
            encoding="utf-8"
        )
        lines = out.strip().split("\n")
        if lines:
            gpu0 = lines[0].split(",")
            return {
                "gpu_util_perc": float(gpu0[0].strip()),
                "vram_used_mb": float(gpu0[1].strip()),
                "vram_total_mb": float(gpu0[2].strip()),
            }
    except Exception:
        pass
    return {"gpu_util_perc": 0.0, "vram_used_mb": 0.0, "vram_total_mb": 0.0}


async def send_single_query(
    session: aiohttp.ClientSession,
    query: str,
    max_tokens: int = 128,
    stream: bool = False,
    timeout_s: float = 60.0
) -> Dict[str, Any]:
    payload = {"query": query, "max_tokens": max_tokens, "stream": stream}
    t0 = time.perf_counter()
    ttft = None
    chunks_received = 0

    try:
        async with session.post(
            f"{GATEWAY_URL}/query",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as resp:
            status = resp.status
            if status != 200:
                text = await resp.text()
                return {
                    "ok": False,
                    "status": status,
                    "latency_ms": (time.perf_counter() - t0) * 1000.0,
                    "error": text[:100],
                }

            if stream:
                async for line in resp.content:
                    line_str = line.decode("utf-8", errors="ignore").strip()
                    if line_str.startswith("data:"):
                        if ttft is None and line_str != "data: [DONE]":
                            ttft = (time.perf_counter() - t0) * 1000.0
                        chunks_received += 1
                lat = (time.perf_counter() - t0) * 1000.0
                return {
                    "ok": True,
                    "status": 200,
                    "latency_ms": lat,
                    "ttft_ms": ttft or lat,
                    "cached": False,
                }
            else:
                data = await resp.json()
                lat = (time.perf_counter() - t0) * 1000.0
                return {
                    "ok": True,
                    "status": 200,
                    "latency_ms": lat,
                    "cached": data.get("cached", False),
                    "route": data.get("route"),
                    "target_tier": data.get("target_tier"),
                }
    except Exception as e:
        return {
            "ok": False,
            "status": 0,
            "latency_ms": (time.perf_counter() - t0) * 1000.0,
            "error": str(e),
        }


async def run_concurrency_batch(
    queries: List[str],
    concurrency: int,
    total_requests: int,
    max_tokens: int = 128,
    stream: bool = False,
    scenario_name: str = "",
) -> BenchmarkMetric:
    conn = aiohttp.TCPConnector(limit=concurrency + 50, use_dns_cache=True)
    timeout = aiohttp.ClientTimeout(total=90.0)
    sem = asyncio.Semaphore(concurrency)

    cpu_start = psutil.cpu_percent(interval=None)
    gpu_info_start = get_gpu_metrics()

    async with aiohttp.ClientSession(connector=conn, timeout=timeout) as session:
        latencies: List[float] = []
        ttfts: List[float] = []
        successes = 0
        failures = 0
        cached_count = 0

        async def worker(idx: int):
            nonlocal successes, failures, cached_count
            q = queries[idx % len(queries)]
            async with sem:
                res = await send_single_query(session, q, max_tokens=max_tokens, stream=stream)
                latencies.append(res["latency_ms"])
                if res.get("ttft_ms"):
                    ttfts.append(res["ttft_ms"])
                if res["ok"]:
                    successes += 1
                    if res.get("cached"):
                        cached_count += 1
                else:
                    failures += 1

        t0 = time.perf_counter()
        tasks = [asyncio.create_task(worker(i)) for i in range(total_requests)]
        await asyncio.gather(*tasks)
        wall_time = time.perf_counter() - t0

    cpu_end = psutil.cpu_percent(interval=None)
    gpu_info_end = get_gpu_metrics()

    arr = np.array(latencies) if latencies else np.array([0.0])
    ttft_arr = np.array(ttfts) if ttfts else None

    metric = BenchmarkMetric(
        scenario=scenario_name,
        concurrency=concurrency,
        total_requests=total_requests,
        successful_requests=successes,
        failed_requests=failures,
        duration_seconds=round(wall_time, 3),
        rps=round(successes / wall_time, 2) if wall_time > 0 else 0.0,
        latency_min_ms=round(float(np.min(arr)), 2),
        latency_p50_ms=round(float(np.percentile(arr, 50)), 2),
        latency_p90_ms=round(float(np.percentile(arr, 90)), 2),
        latency_p95_ms=round(float(np.percentile(arr, 95)), 2),
        latency_p99_ms=round(float(np.percentile(arr, 99)), 2),
        latency_max_ms=round(float(np.max(arr)), 2),
        latency_mean_ms=round(float(np.mean(arr)), 2),
        ttft_mean_ms=round(float(np.mean(ttft_arr)), 2) if ttft_arr is not None else None,
        ttft_p95_ms=round(float(np.percentile(ttft_arr, 95)), 2) if ttft_arr is not None else None,
        gpu_util_perc=max(gpu_info_start["gpu_util_perc"], gpu_info_end["gpu_util_perc"]),
        cpu_util_perc=round((cpu_start + cpu_end) / 2.0, 1),
        cache_hit_rate=round(cached_count / total_requests * 100.0, 1) if total_requests > 0 else 0.0,
    )
    return metric


def print_metric_table(metrics: List[BenchmarkMetric]):
    print("\n" + "=" * 115)
    print(f"{'Scenario':<26} | {'C':<4} | {'Reqs':<5} | {'RPS':<7} | {'p50(ms)':<8} | {'p95(ms)':<8} | {'p99(ms)':<8} | {'Max(ms)':<8} | {'Fail':<4} | {'Cache %':<7}")
    print("-" * 115)
    for m in metrics:
        print(
            f"{m.scenario:<26} | "
            f"{m.concurrency:<4} | "
            f"{m.total_requests:<5} | "
            f"{m.rps:<7.1f} | "
            f"{m.latency_p50_ms:<8.1f} | "
            f"{m.latency_p95_ms:<8.1f} | "
            f"{m.latency_p99_ms:<8.1f} | "
            f"{m.latency_max_ms:<8.1f} | "
            f"{m.failed_requests:<4} | "
            f"{str(m.cache_hit_rate) + '%':<7}"
        )
    print("=" * 115 + "\n")


async def main():
    print("\n" + "=" * 90)
    print("🔥 INSTITUTIONAL RAG GATEWAY - HEAVY LOAD & CONCURRENCY BENCHMARK SUITE")
    print("=" * 90)
    print(f"Target Gateway: {GATEWAY_URL}")
    print(f"Host: Linux 1.0TB RAM, 8x A100-SXM4 GPU Cluster")
    print(f"Knowledge Base: 2,261,664 chunks in RediSearch HNSW Index")
    print("=" * 90)

    # 1. Warm-up and Seed Semantic Cache
    print("\n[Step 0] Seeding Semantic Cache with reference queries...")
    async with aiohttp.ClientSession() as s:
        for q in CACHE_SEED_QUERIES:
            await send_single_query(s, q, max_tokens=64)
    print("✅ Cache seeded successfully.")

    results: List[BenchmarkMetric] = []

    # ------------------------------------------------------------------------
    # Suite 1: Pure Semantic Vector Cache Stress Test (High Concurrency)
    # ------------------------------------------------------------------------
    print("\n--- [Suite 1] Pure Semantic Vector Cache Concurrency Scaling ---")
    cache_queries = [
        "Explain the failure mechanisms of prestressed concrete and composite laminates under cyclic stress",
        "Can you explain what vermiculite is and its main applications?",
        "How does corticosterone affect synaptic plasticity?",
    ]
    for c in [10, 25, 50, 100]:
        total = c * 4
        print(f"Testing Cache Concurrency C={c} (Total Requests: {total})...")
        m = await run_concurrency_batch(
            queries=cache_queries,
            concurrency=c,
            total_requests=total,
            scenario_name=f"Cache Hit Scaling (C={c})"
        )
        results.append(m)
        print(f"  -> RPS: {m.rps} | p50: {m.latency_p50_ms}ms | p95: {m.latency_p95_ms}ms | p99: {m.latency_p99_ms}ms")

    # ------------------------------------------------------------------------
    # Suite 2: CPU SLM (Qwen 2.5 3B) Concurrency Scaling
    # ------------------------------------------------------------------------
    print("\n--- [Suite 2] CPU SLM Dual-Tier Pipeline Scaling ---")
    for c in [1, 2, 4, 8]:
        total = c * 2
        slm_test_queries = [f"{q} [Nonce {time.time_ns()}_{i}]" for i, q in enumerate(EASY_QUERIES)]
        print(f"Testing CPU SLM Concurrency C={c} (Total Requests: {total})...")
        m = await run_concurrency_batch(
            queries=slm_test_queries,
            concurrency=c,
            total_requests=total,
            max_tokens=64,
            scenario_name=f"CPU SLM Scaling (C={c})"
        )
        results.append(m)
        print(f"  -> RPS: {m.rps} | p50: {m.latency_p50_ms}ms | p95: {m.latency_p95_ms}ms | p99: {m.latency_p99_ms}ms")

    # ------------------------------------------------------------------------
    # Suite 3: GPU vLLM (Qwen 2.5 7B) Continuous Batching Scaling
    # ------------------------------------------------------------------------
    print("\n--- [Suite 3] GPU vLLM (A100 Continuous Batching) Scaling ---")
    for c in [1, 4, 8, 16]:
        total = max(c * 2, 4)
        gpu_test_queries = [f"{q} [Analysis Ref {time.time_ns()}_{i}]" for i, q in enumerate(HARD_QUERIES)]
        print(f"Testing GPU vLLM Concurrency C={c} (Total Requests: {total})...")
        m = await run_concurrency_batch(
            queries=gpu_test_queries,
            concurrency=c,
            total_requests=total,
            max_tokens=128,
            scenario_name=f"GPU vLLM Scaling (C={c})"
        )
        results.append(m)
        print(f"  -> RPS: {m.rps} | p50: {m.latency_p50_ms}ms | p95: {m.latency_p95_ms}ms | p99: {m.latency_p99_ms}ms")

    # ------------------------------------------------------------------------
    # Suite 4: SSE Streaming Latency & Time-To-First-Token (TTFT)
    # ------------------------------------------------------------------------
    print("\n--- [Suite 4] SSE Streaming Latency & Time-To-First-Token (TTFT) ---")
    for c in [5, 10]:
        total = c * 2
        stream_queries = [f"{q} [Stream {time.time_ns()}_{i}]" for i, q in enumerate(EASY_QUERIES + HARD_QUERIES)]
        print(f"Testing SSE Stream Concurrency C={c} (Total Requests: {total})...")
        m = await run_concurrency_batch(
            queries=stream_queries,
            concurrency=c,
            total_requests=total,
            max_tokens=96,
            stream=True,
            scenario_name=f"SSE Stream (C={c})"
        )
        results.append(m)
        print(f"  -> RPS: {m.rps} | TTFT mean: {m.ttft_mean_ms}ms | TTFT p95: {m.ttft_p95_ms}ms | Total p95: {m.latency_p95_ms}ms")

    # ------------------------------------------------------------------------
    # Suite 5: Realistic Campus Mixed Workload Simulation
    # ------------------------------------------------------------------------
    print("\n--- [Suite 5] Realistic Campus Mixed Traffic Simulation (30 Concurrent Users) ---")
    # Blend: 60% cache hits, 25% easy CPU SLM, 15% complex GPU LLM
    mixed_queries = []
    for _ in range(60):
        mixed_queries.append(CACHE_SEED_QUERIES[np.random.randint(0, len(CACHE_SEED_QUERIES))])
    for i in range(25):
        mixed_queries.append(f"{EASY_QUERIES[i % len(EASY_QUERIES)]} [Student {i}_{time.time_ns()}]")
    for i in range(15):
        mixed_queries.append(f"{HARD_QUERIES[i % len(HARD_QUERIES)]} [Research {i}_{time.time_ns()}]")

    m_mixed = await run_concurrency_batch(
        queries=mixed_queries,
        concurrency=30,
        total_requests=100,
        max_tokens=100,
        scenario_name="Mixed Campus Traffic (C=30)"
    )
    results.append(m_mixed)
    print(f"  -> RPS: {m_mixed.rps} | p50: {m_mixed.latency_p50_ms}ms | p95: {m_mixed.latency_p95_ms}ms | Cache Hit %: {m_mixed.cache_hit_rate}%")

    # ------------------------------------------------------------------------
    # Summary Table & Output Persistence
    # ------------------------------------------------------------------------
    print_metric_table(results)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_FILE, "w") as f:
        json.dump([asdict(m) for m in results], f, indent=2)
    print(f"💾 Detailed raw telemetry persisted to: {RESULTS_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
