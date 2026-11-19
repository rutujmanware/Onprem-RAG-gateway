#!/usr/bin/env python3
"""NFR-5: a 50x burst over baseline traffic, on a measurement gateway with the production configuration
(6 workers, production rate limits and admission caps, own empty cache; scripts/eval_gateway.py).

Open-loop arrivals at a fixed rate, three phases:
  baseline  --base-rps (default 2/s) for --phase-seconds
  burst     50 x base-rps (default 100/s) for --phase-seconds
  recovery  --base-rps again
Traffic mix in every phase: --cached-share repeats of questions already in the cache (asked again by their own
user, so per-user rate limits apply as in real use) and the rest new questions, from --users distinct users.
Pass criteria (NFR-5): the gateway stays up (/ready 200 after the burst), no HTTP 5xx other than 503 load
shedding, every recovery-phase request answered, cached answers stay fast during the burst.

Usage: PYTHONPATH=. python scripts/measure_burst.py [--base-rps 2] [--factor 50] [--phase-seconds 60]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from eval_gateway import EvalGateway  # noqa: E402
from gateway.auth.internal import internal_user_headers  # noqa: E402


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else float("nan")


async def main_async(args):
    rnd = random.Random(args.seed)
    qs = [json.loads(l)["question"] for l in open(ROOT / "logs/retrieval_eval/questions.jsonl") if l.strip()]
    qs += [json.loads(l)["question"] for l in open(ROOT / "logs/retrieval_eval/rare_questions.jsonl") if l.strip()]
    rnd.shuffle(qs)
    seed_qs, pool = qs[:args.seed_questions], qs[args.seed_questions:]
    gw = EvalGateway(port=args.port, workers=6, tag="burst").start()
    rows = []
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout, connect=10),
                                     limits=httpx.Limits(max_connections=args.max_connections,
                                                         max_keepalive_connections=500)) as cl, \
                httpx.AsyncClient(timeout=httpx.Timeout(args.timeout, connect=10)) as probe_cl:
            # warm the cache: each seed question cached for its owner
            owners = {}

            async def seed(i, q):
                u = f"burst-user-{i % args.users}"
                for _ in range(10):
                    r = await cl.post(f"{gw.url}/query", headers=internal_user_headers(u), json={"query": q})
                    if r.status_code == 200 and r.json().get("chunks"):
                        owners[q] = u
                        return
                    await asyncio.sleep(2)
            sem = asyncio.Semaphore(4)

            async def seeded(i, q):
                async with sem:
                    await seed(i, q)
            await asyncio.gather(*(seeded(i, q) for i, q in enumerate(seed_qs)))
            cached = list(owners.items())
            print(f"cache warmed with {len(cached)} answers")
            fresh_i = 0

            async def one(phase, kind, q, user):
                t0 = time.perf_counter()
                try:
                    r = await cl.post(f"{gw.url}/query", headers=internal_user_headers(user), json={"query": q})
                    status = r.status_code
                    hit = status == 200 and bool(r.json().get("cached"))
                    detail = r.text[:160] if status in (400, 403, 500) else ""
                except Exception as exc:
                    status, hit, detail = f"exc:{type(exc).__name__}", False, ""
                rows.append({"phase": phase, "kind": kind, "status": status, "hit": hit, "q": q if detail else "",
                             "detail": detail, "latency": time.perf_counter() - t0, "t": time.time()})

            # independent probe: one cached question per second on its own connection, so its latency is the
            # gateway's, not time spent waiting in the load client's connection pool
            probe_state = {"phase": "baseline", "stop": False}

            async def probe():
                while not probe_state["stop"]:
                    q, u = rnd.choice(cached)
                    t0 = time.perf_counter()
                    try:
                        r = await probe_cl.post(f"{gw.url}/query", headers=internal_user_headers(u), json={"query": q})
                        st = r.status_code
                    except Exception as exc:
                        st = f"exc:{type(exc).__name__}"
                    rows.append({"phase": probe_state["phase"], "kind": "probe", "status": st, "hit": False,
                                 "latency": time.perf_counter() - t0, "t": time.time()})
                    await asyncio.sleep(1.0)
            probe_task = asyncio.create_task(probe()) if cached else None
            tasks = []
            for phase, rps in (("baseline", args.base_rps), ("burst", args.base_rps * args.factor),
                               ("recovery", args.base_rps)):
                n = int(rps * args.phase_seconds)
                probe_state["phase"] = phase
                start = time.perf_counter()
                print(f"{phase}: {n} requests at {rps}/s", flush=True)
                for k in range(n):
                    delay = start + k / rps - time.perf_counter()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    if rnd.random() < args.cached_share and cached:
                        q, u = rnd.choice(cached)
                        tasks.append(asyncio.create_task(one(phase, "cached", q, u)))
                    else:
                        q = pool[fresh_i % len(pool)]
                        if fresh_i >= len(pool):
                            q = f"{fresh_i}. {q}"   # "(request N)" suffixes tripped the classifier (13/379)
                        fresh_i += 1
                        tasks.append(asyncio.create_task(one(phase, "new", q, f"burst-user-{rnd.randrange(args.users)}")))
                if phase == "burst":   # let the burst drain before measuring recovery
                    await asyncio.gather(*tasks)
            await asyncio.gather(*tasks)
            probe_state["stop"] = True
            if probe_task:
                await probe_task
            ready = (await cl.get(f"{gw.url}/ready")).status_code
    finally:
        gw.stop()

    print(f"\n/ready after the run: {ready}")
    summary = {}
    for phase in ("baseline", "burst", "recovery"):
        for kind in ("cached", "new", "probe"):
            rs = [r for r in rows if r["phase"] == phase and r["kind"] == kind]
            if not rs:
                continue
            st = Counter(str(r["status"]) for r in rs)
            ok = [r["latency"] for r in rs if r["status"] == 200]
            summary[f"{phase}/{kind}"] = {"n": len(rs), "status": dict(st), "p50": round(pct(ok, 50), 3),
                                          "p99": round(pct(ok, 99), 3), "cache_hits": sum(r["hit"] for r in rs)}
            print(f"{phase:9s} {kind:6s} n={len(rs):5d} status {dict(st)}  ok p50 {pct(ok, 50):.3f}s p99 {pct(ok, 99):.3f}s")
    bad = [r for r in rows if r["kind"] != "probe" and not (r["status"] in (200, 429, 503))]
    rec = [r for r in rows if r["phase"] == "recovery" and r["kind"] != "probe"]
    for r in rows:
        if r.get("detail") and r["status"] == 400:
            print(f"   400 on {r['kind']}: {r['detail'][:100]} | {r['q'][:100]}")
    print(f"\nNFR-5: unexpected outcomes (not 200/429/503): {len(bad)}; recovery answered "
          f"{sum(r['status'] == 200 for r in rec)}/{len(rec)}; gateway ready after burst: {ready == 200}")
    log = ROOT / "logs" / "perf" / f"burst_{time.strftime('%Y%m%d_%H%M%S')}.json"
    log.write_text(json.dumps({"args": vars(args), "ready_after": ready, "summary": summary, "rows": rows}))
    print(f"-> {log.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-rps", type=float, default=2.0)
    ap.add_argument("--factor", type=float, default=50.0)
    ap.add_argument("--phase-seconds", type=float, default=60.0)
    ap.add_argument("--cached-share", type=float, default=0.9)
    ap.add_argument("--users", type=int, default=200)
    ap.add_argument("--seed-questions", type=int, default=60)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--max-connections", type=int, default=8000, help="load client's connection pool")
    ap.add_argument("--port", type=int, default=8096)
    ap.add_argument("--seed", type=int, default=20261119)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
