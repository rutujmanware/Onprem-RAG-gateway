#!/usr/bin/env python3
"""NFR-1 load test: open-loop cache-hit traffic against the running gateway, optionally mixed with fresh
(uncached) questions, then a pass/fail against the semantic-hit p99 target.

  * warm-up: a fixed set of thesis-style questions is answered once for a synthetic user, then verified to
    be served from the cache both verbatim (exact hit) and rephrased (semantic hit, owner-scoped);
  * load: --qps requests/s for --duration s on an open-loop schedule (requests are sent on time even if
    earlier ones are slow), alternating exact and semantic hits;
  * --fresh-rate R additionally sends R unique uncached questions per second (full retrieval + generation),
    to see whether model load slows cache hits;
  * every entry created is purged at the end.

Usage: python scripts/load_test_cache.py --qps 100 --duration 600 [--fresh-rate 0.5] [--target-p99 35]
Exit code 1 when the semantic p99 exceeds the target or any request fails.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

QUESTIONS = [
    "What are the applications of graphene in electronics?",
    "How do perovskite solar cells degrade in humid conditions?",
    "What methods are used to model turbulent combustion in gas turbines?",
    "How is GMRES used to solve large sparse linear systems?",
    "What factors control the strength of high-performance concrete?",
    "How do zeolites adsorb carbon dioxide?",
    "What techniques are used to predict Indian monsoon rainfall?",
    "How do lithium-ion battery electrodes degrade over cycling?",
    "What are the challenges in wireless sensor network routing?",
    "How does mechanical alloying produce amorphous phases?",
    "What role do mycorrhizal fungi play in plant nutrition?",
    "How are metal organic frameworks synthesised?",
]
FRESH_TOPICS = ["soil liquefaction", "image segmentation", "drug delivery", "groundwater recharge", "speech recognition",
                "carbon nanotubes", "protein folding", "power transformers", "rainfall runoff", "photonic crystals"]


def pct(xs, p):
    return round(float(np.percentile(xs, p)), 2) if xs else None


async def main_async(a) -> int:
    run = uuid.uuid4().hex[:6]
    user = f"loadtest-{run}@iisc.ac.in"
    url = f"{a.gateway}/query"
    created = []
    limits = httpx.Limits(max_connections=400, max_keepalive_connections=400)
    async with httpx.AsyncClient(timeout=120, limits=limits) as cl:
        print(f"warm-up: {len(QUESTIONS)} questions for {user}")
        for q in QUESTIONS:
            created.append(q)
            await cl.post(url, json={"query": q, "max_tokens": 160}, headers={"X-Internal-User": user})
        work, ok = [], 0
        for q in QUESTIONS:
            e = (await cl.post(url, json={"query": q}, headers={"X-Internal-User": user})).json()
            s = (await cl.post(url, json={"query": q.rstrip("?")}, headers={"X-Internal-User": user})).json()
            if e.get("cached"):
                work.append(("exact", q))
            if s.get("cached"):
                work.append(("semantic", q.rstrip("?")))
            ok += bool(e.get("cached")) + bool(s.get("cached"))
        print(f"warm-up: {ok}/{2 * len(QUESTIONS)} served from cache (questions without sources are not cached)")
        if not any(k == "semantic" for k, _ in work):
            print("no semantic hits to test")
            return 1

        lat = {"exact": [], "semantic": [], "fresh": []}
        errors, not_cached, kinds = [], 0, {}

        async def hit(kind, q):
            nonlocal not_cached
            t0 = time.perf_counter()
            try:
                r = await cl.post(url, json={"query": q}, headers={"X-Internal-User": user})
                dt = (time.perf_counter() - t0) * 1e3
                if r.status_code != 200:
                    errors.append(r.status_code)
                    return
                d = r.json()
                if not d.get("cached"):
                    not_cached += 1
                    return
                kinds[d.get("cache_kind")] = kinds.get(d.get("cache_kind"), 0) + 1
                lat[kind].append(dt)
            except Exception as exc:
                errors.append(type(exc).__name__)

        async def fresh(i):
            q = f"What do the theses report about {FRESH_TOPICS[i % len(FRESH_TOPICS)]}? (load {run}-{i})"
            created.append(q)
            t0 = time.perf_counter()
            try:
                r = await cl.post(url, json={"query": q, "max_tokens": 120}, headers={"X-Internal-User": f"fresh-{run}-{i}@iisc.ac.in"},
                                  timeout=300)
                if r.status_code == 200:
                    lat["fresh"].append((time.perf_counter() - t0) * 1e3)
                else:
                    errors.append(f"fresh-{r.status_code}")
            except Exception as exc:
                errors.append(f"fresh-{type(exc).__name__}")

        print(f"load: {a.qps} QPS cache hits for {a.duration}s" + (f" + {a.fresh_rate} fresh questions/s" if a.fresh_rate else ""))
        tasks, start, i, nf = [], time.perf_counter(), 0, 0
        while (now := time.perf_counter() - start) < a.duration:
            due_hit = i / a.qps
            due_fresh = nf / a.fresh_rate if a.fresh_rate else float("inf")
            due = min(due_hit, due_fresh)
            if due > now:
                await asyncio.sleep(due - now)
            if due_fresh <= due_hit:
                tasks.append(asyncio.create_task(fresh(nf)))
                nf += 1
            else:
                kind, q = work[i % len(work)]
                tasks.append(asyncio.create_task(hit(kind, q)))
                i += 1
                if i % int(a.qps * 60) == 0:
                    print(f"  t={int(now)}s sent={i} errors={len(errors)} semantic p99 so far={pct(lat['semantic'], 99)} ms", flush=True)
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start

    res = {"qps_target": a.qps, "duration_s": a.duration, "fresh_rate": a.fresh_rate, "sent_hits": i, "sent_fresh": nf,
           "achieved_qps": round(i / elapsed, 1), "errors": len(errors), "error_kinds": sorted(set(map(str, errors))),
           "hits_not_served_from_cache": not_cached, "cache_kinds": kinds}
    for k, xs in lat.items():
        if xs:
            res[k] = {"n": len(xs), "p50_ms": pct(xs, 50), "p95_ms": pct(xs, 95), "p99_ms": pct(xs, 99), "max_ms": round(max(xs), 2)}
    from gateway.cache.redis_semantic_cache import RedisSemanticCache
    res["purged_entries"] = sum(RedisSemanticCache().purge(q, threshold=0.98) for q in set(created))
    print(json.dumps(res, indent=1))
    if a.out:
        Path(a.out).write_text(json.dumps(res, indent=1))
    sem_p99 = (res.get("semantic") or {}).get("p99_ms")
    passed = sem_p99 is not None and sem_p99 <= a.target_p99 and not errors and not not_cached
    print(f"\nNFR-1 semantic p99 {sem_p99} ms vs target {a.target_p99} ms -> {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gateway", default="http://127.0.0.1:8080")
    ap.add_argument("--qps", type=float, default=100.0)
    ap.add_argument("--duration", type=float, default=600.0)
    ap.add_argument("--fresh-rate", type=float, default=0.0)
    ap.add_argument("--target-p99", type=float, default=35.0)
    ap.add_argument("--out", default="")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
