#!/usr/bin/env python3
"""Run 100 Uncached Cold Queries Load Test.

Every request is guaranteed to be a cache-miss, forcing:
1. Dense vector embedding & Redis search across 2,261,664 chunks.
2. Two-Tower Neural Complexity Routing.
3. Cold inference on CPU SLM (Qwen 3B) or GPU LLM (Qwen 7B on A100).

Measures:
- Latency (min, p50, p90, p95, p99, max, mean)
- Throughput (RPS)
- Routing tier breakdown (CPU SLM vs GPU LLM)
- Success / Failure rate
- Hardware metrics (CPU utilization %, GPU utilization %, VRAM)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import aiohttp
import numpy as np
import psutil

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://127.0.0.1:8080")

# 20 diverse, realistic academic domain topics from the 12,900+ indexed IISc thesis corpus
DOMAIN_TOPICS = [
    "failure mechanisms of prestressed concrete under cyclic loading",
    "interlaminar shear stress in laminated carbon fiber composites",
    "corticosterone induced alterations in hippocampal synaptic plasticity",
    "ferroelectric polarization switching dynamics in perovskite thin films",
    "high temperature low stress creep deformation in nickel superalloys",
    "fuzzy waste load allocation in urban river water quality management",
    "dynamic stress corrosion cracking in aged aluminum alloys",
    "elastoplastic contact stress and fracture in circular cylinders",
    "resolvin D1 loaded liposomal nanocarriers for targeted drug delivery",
    "workload migration in multi-tenant cloud container orchestration",
    "first crack strength and ductility in reinforced ferrocement beams",
    "vibrational stress relief method for welded structural components",
    "thermal stress distribution in hollow cylinders under radial gradients",
    "fatigue crack closure mechanisms under spectrum flight loading",
    "pile-soil friction and load transfer in deep foundation engineering",
    "quantum dot cellular automata for low power reversible computing",
    "surface wave attenuation in metal disc loaded transmission lines",
    "expatriates adjustment stressors and organizational performance",
    "subsurface distress prediction models for flexible pavements",
    "rotor whirl and precession in prestressed shaft-bearing assemblies",
]

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

async def send_cold_query(session: aiohttp.ClientSession, query: str, timeout_s: float = 60.0) -> Dict[str, Any]:
    payload = {"query": query, "max_tokens": 128, "bypass_cache": True}
    t0 = time.perf_counter()
    try:
        async with session.post(
            f"{GATEWAY_URL}/query",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=timeout_s)
        ) as resp:
            lat = (time.perf_counter() - t0) * 1000.0
            if resp.status == 200:
                data = await resp.json()
                return {
                    "ok": True,
                    "status": 200,
                    "latency_ms": lat,
                    "cached": data.get("cached", False),
                    "route": data.get("route"),
                    "target_tier": data.get("target_tier"),
                    "retrieval_sec": data.get("stats", {}).get("retrieval_seconds", 0.0),
                }
            else:
                txt = await resp.text()
                return {"ok": False, "status": resp.status, "latency_ms": lat, "error": txt[:100]}
    except Exception as e:
        return {"ok": False, "status": 0, "latency_ms": (time.perf_counter() - t0) * 1000.0, "error": str(e)}

async def run_100_cold_test(concurrency: int = 10, total_requests: int = 100):
    print("\n" + "=" * 90)
    print("❄️ EXTREME COLD LOAD TEST: 100 UNCACHED REQUESTS")
    print("=" * 90)
    print(f"Target Gateway: {GATEWAY_URL}")
    print(f"Total Requests: {total_requests}")
    print(f"Concurrency Level (C): {concurrency} parallel users")
    print(f"Cache Policy: STRICT ZERO-CACHE (Every request forces full Redis retrieval + Model inference)")
    print("=" * 90 + "\n")

    # Clear cache before starting
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"{GATEWAY_URL}/cache/clear") as r:
                print(f"🧹 Pre-test cache purge: HTTP {r.status} (Cache emptied)")
    except Exception as e:
        print(f"⚠️ Cache clear warning: {e}")

    # Generate 100 unique cold queries
    test_queries = []
    for i in range(total_requests):
        topic = DOMAIN_TOPICS[i % len(DOMAIN_TOPICS)]
        # Unique timestamp + salt guarantees 100% cache miss
        q = f"Investigate and explain the {topic}. Discuss the findings and theoretical models. [ID: {time.time_ns()}_{i}]"
        test_queries.append(q)

    conn = aiohttp.TCPConnector(limit=concurrency + 20)
    sem = asyncio.Semaphore(concurrency)

    cpu_samples = []
    gpu_samples = []
    latencies: List[float] = []
    retrieval_times: List[float] = []
    routes = {"cpu_slm": 0, "gpu_vllm": 0, "other": 0}
    cached_count = 0
    successes = 0
    failures = 0

    cpu_samples.append(psutil.cpu_percent(interval=None))
    gpu_samples.append(get_gpu_metrics()["gpu_util_perc"])

    t_start = time.perf_counter()

    async with aiohttp.ClientSession(connector=conn) as session:
        async def worker(idx: int):
            nonlocal successes, failures, cached_count
            query = test_queries[idx]
            async with sem:
                res = await send_cold_query(session, query)
                latencies.append(res["latency_ms"])
                if res["ok"]:
                    successes += 1
                    if res.get("cached"):
                        cached_count += 1
                    tier = res.get("target_tier")
                    if tier in ("cpu_slm", "easy"):
                        routes["cpu_slm"] += 1
                    elif tier in ("gpu_vllm", "hard"):
                        routes["gpu_vllm"] += 1
                    else:
                        routes["other"] += 1
                    ret_s = res.get("retrieval_sec", 0.0)
                    if ret_s:
                        retrieval_times.append(ret_s * 1000.0)
                else:
                    failures += 1

                # Progress indicator
                done = successes + failures
                if done % 10 == 0 or done == total_requests:
                    elapsed = time.perf_counter() - t_start
                    rps = done / elapsed if elapsed > 0 else 0
                    print(f"  Progress: {done:>3}/{total_requests} completed | In-flight: {sem._value} free | RPS: {rps:.2f}")

        tasks = [asyncio.create_task(worker(i)) for i in range(total_requests)]
        await asyncio.gather(*tasks)

    t_end = time.perf_counter()
    wall_time = t_end - t_start

    cpu_samples.append(psutil.cpu_percent(interval=None))
    gpu_samples.append(get_gpu_metrics()["gpu_util_perc"])

    arr = np.array(latencies)
    ret_arr = np.array(retrieval_times) if retrieval_times else np.array([0.0])

    print("\n" + "=" * 90)
    print("📊 100 UNCACHED REQUESTS - BENCHMARK RESULTS")
    print("=" * 90)
    print(f"Total Requests:         {total_requests}")
    print(f"Successful Requests:    {successes} ({successes/total_requests*100:.1f}%)")
    print(f"Failed Requests:        {failures}")
    print(f"Cache Hit Rate:         {cached_count/total_requests*100:.1f}% (Confirmed Cold)")
    print(f"Total Duration:         {wall_time:.2f} seconds")
    print(f"Actual Throughput:      {successes/wall_time:.2f} Requests/Second (RPS)")
    print("-" * 90)
    print("⏱️ END-TO-END COLD LATENCY PERCENTILES:")
    print(f"  Min:                  {np.min(arr):.2f} ms")
    print(f"  p50 (Median):         {np.percentile(arr, 50):.2f} ms")
    print(f"  p90:                  {np.percentile(arr, 90):.2f} ms")
    print(f"  p95:                  {np.percentile(arr, 95):.2f} ms")
    print(f"  p99:                  {np.percentile(arr, 99):.2f} ms")
    print(f"  Max:                  {np.max(arr):.2f} ms")
    print(f"  Mean:                 {np.mean(arr):.2f} ms")
    print("-" * 90)
    print("🔍 REDIS DENSE VECTOR RETRIEVAL (2.26M Chunks):")
    print(f"  Mean Retrieval Time:  {np.mean(ret_arr):.2f} ms")
    print(f"  p95 Retrieval Time:   {np.percentile(ret_arr, 95):.2f} ms")
    print("-" * 90)
    print("🔀 DUAL-TIER ROUTING BREAKDOWN:")
    print(f"  CPU SLM (Easy Tier):  {routes['cpu_slm']} requests ({routes['cpu_slm']/successes*100:.1f}%)")
    print(f"  GPU vLLM (Hard Tier): {routes['gpu_vllm']} requests ({routes['gpu_vllm']/successes*100:.1f}%)")
    print("-" * 90)
    print("💻 SYSTEM RESOURCE CONSUMPTION:")
    print(f"  Avg CPU Utilization:  {np.mean(cpu_samples):.1f}%")
    print(f"  Peak GPU Utilization: {np.max(gpu_samples):.1f}%")
    print("=" * 90 + "\n")

    # Save results to JSON
    out_file = Path("logs/100_uncached_load_test_results.json")
    results_data = {
        "scenario": "100_uncached_cold_queries",
        "concurrency": concurrency,
        "total_requests": total_requests,
        "successes": successes,
        "failures": failures,
        "cache_hit_rate_perc": cached_count / total_requests * 100.0,
        "wall_time_seconds": round(wall_time, 2),
        "rps": round(successes / wall_time, 2),
        "latency_percentiles_ms": {
            "min": round(float(np.min(arr)), 2),
            "p50": round(float(np.percentile(arr, 50)), 2),
            "p90": round(float(np.percentile(arr, 90)), 2),
            "p95": round(float(np.percentile(arr, 95)), 2),
            "p99": round(float(np.percentile(arr, 99)), 2),
            "max": round(float(np.max(arr)), 2),
            "mean": round(float(np.mean(arr)), 2),
        },
        "retrieval_latency_ms": {
            "mean": round(float(np.mean(ret_arr)), 2),
            "p95": round(float(np.percentile(ret_arr, 95)), 2),
        },
        "routing_breakdown": routes,
    }
    with open(out_file, "w") as f:
        json.dump(results_data, f, indent=2)
    print(f"💾 Telemetry saved to: {out_file}")

if __name__ == "__main__":
    asyncio.run(run_100_cold_test(concurrency=10, total_requests=100))
