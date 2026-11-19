#!/usr/bin/env python3
"""Evaluation plan §7: configurations C0-C2 on one replayed workload, and the NFR-3 compute-cost comparison.

  C0  GPU only, no cache:      ROUTING_ALPHA=0 (every question routed to the 7B) and bypass_cache on every request
  C1  cache + GPU only:        ROUTING_ALPHA=0, cache on
  C2  full system:             production settings (cache, typo normaliser, router alpha 0.65, CPU + GPU tiers)
  (C3, the FP16-vs-Q4_K_M CPU ablation, is not run: there is no FP16 CPU deployment of the 3B model.)

Workload (fixed seed): N new questions from the frozen retrieval-evaluation set, then, shuffled: exact repeats by
other users, paraphrases by the same user (7B-written, most similar first), and misspelled repeats by the same
user. Each configuration runs on its own measurement gateway with an empty cache of its own (scripts/eval_gateway.py).

Cost is reported in measured model time, not money: GPU-seconds = sum of generation_seconds of answers produced
by the GPU tier (including CPU->GPU overflow/escalation), CPU-seconds likewise. NFR-3 target: the CPU tier
answers >= 70 % of generated answers and GPU-seconds fall by > 60 % versus C0.

Usage: PYTHONPATH=. python scripts/measure_configs.py [--new 40] [--concurrency 4] [--configs C0 C1 C2]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import statistics
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from eval_gateway import EvalGateway  # noqa: E402
from eval_injection_fence import parse_list, vllm_chat  # noqa: E402
from gateway.auth.internal import internal_user_headers  # noqa: E402

CONFIGS = {
    "C0": ({"ROUTING_ALPHA": "0.0"}, True),
    "C1": ({"ROUTING_ALPHA": "0.0"}, False),
    "C2": ({}, False),
}


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else float("nan")


def build_workload(n_new, seed):
    rnd = random.Random(seed)
    qs = [json.loads(l)["question"] for l in open(ROOT / "logs/retrieval_eval/questions.jsonl") if l.strip()]
    new = rnd.sample([q for q in qs if 30 <= len(q) <= 180], n_new)

    def para(q):
        out = parse_list(vllm_chat("Rewrite this question once, keeping exactly the same meaning with small wording "
                                   "changes. Output only a JSON list with one string.\n\n" + q, temperature=0.3,
                                   max_tokens=200))
        return out[0] if out else None

    def typo(q):
        def drop(m):
            w = m.group(0)
            if len(w) < 7 or rnd.random() > 0.5:
                return w
            i = rnd.randrange(1, len(w) - 1)
            return w[:i] + w[i + 1:]
        return re.sub(r"[A-Za-z]+", drop, q)
    first = [{"kind": "new", "q": q, "user": f"wl-user-{i % 10}"} for i, q in enumerate(new)]
    later = []
    for i, q in enumerate(new):
        u = f"wl-user-{i % 10}"
        later.append({"kind": "exact_other_user", "q": q, "user": f"wl-user-{(i + 3) % 10}"})
        if i % 4 != 3:
            p = para(q)
            if p:
                later.append({"kind": "paraphrase_same_user", "q": p, "user": u})
        if i % 2 == 0:
            later.append({"kind": "typo_same_user", "q": typo(q), "user": u})
    rnd.shuffle(later)
    return first + later


async def run_config(name, workload, concurrency, port):
    env, bypass = CONFIGS[name]
    gw = EvalGateway(port=port, workers=2, env={**env, "RATE_LIMIT_BURST": "100000", "RATE_LIMIT_RPS": "100000"},
                     tag=f"cfg{name.lower()}").start()
    out = []
    try:
        sem = asyncio.Semaphore(concurrency)
        async with httpx.AsyncClient(timeout=600) as cl:
            async def one(item):
                async with sem:
                    t0 = time.perf_counter()
                    r = await cl.post(f"{gw.url}/query", headers=internal_user_headers(item["user"]),
                                      json={"query": item["q"], "stream": False, "bypass_cache": bypass})
                    dt = time.perf_counter() - t0
                    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                    st = j.get("stats") or {}
                    out.append({**item, "status": r.status_code, "latency": dt, "cached": bool(j.get("cached")),
                                "cache_kind": j.get("cache_kind"), "tier": j.get("target_tier"),
                                "server": st.get("server", ""), "gen_s": float(st.get("generation_seconds") or 0),
                                "has_sources": bool(j.get("chunks")), "incomplete": bool(j.get("incomplete"))})
            # new questions first (they seed the cache), then the rest
            n_new = sum(1 for w in workload if w["kind"] == "new")
            await asyncio.gather(*(one(w) for w in workload[:n_new]))
            await asyncio.gather(*(one(w) for w in workload[n_new:]))
    finally:
        gw.stop()
    return out


def summarise(name, rows):
    ok = [r for r in rows if r["status"] == 200]
    gen = [r for r in ok if not r["cached"] and r["gen_s"] > 0]
    gpu = [r for r in gen if "gpu" in r["server"] or "vllm" in r["server"] or r["server"].startswith("gpu")]
    cpu = [r for r in gen if r not in gpu]
    s = {"requests": len(rows), "ok": len(ok), "errors": len(rows) - len(ok),
         "cache_hits": sum(r["cached"] for r in ok),
         "hits_by_kind": {k: f"{sum(r['cached'] for r in ok if r['kind'] == k)}/{sum(1 for r in ok if r['kind'] == k)}"
                          for k in ("new", "exact_other_user", "paraphrase_same_user", "typo_same_user")},
         "generated": len(gen), "gpu_answers": len(gpu), "cpu_answers": len(cpu),
         "gpu_seconds": round(sum(r["gen_s"] for r in gpu), 1), "cpu_seconds": round(sum(r["gen_s"] for r in cpu), 1),
         "latency_p50": round(pct([r["latency"] for r in ok], 50), 3),
         "latency_p95": round(pct([r["latency"] for r in ok], 95), 3),
         "hit_latency_p50_ms": round(1000 * pct([r["latency"] for r in ok if r["cached"]], 50), 1),
         "miss_latency_p50": round(pct([r["latency"] for r in ok if not r["cached"]], 50), 2),
         "with_sources": sum(r["has_sources"] for r in ok), "incomplete": sum(r["incomplete"] for r in ok)}
    print(f"\n{name}: " + json.dumps(s))
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", type=int, default=40)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--port", type=int, default=8097)
    ap.add_argument("--seed", type=int, default=20261119)
    ap.add_argument("--configs", nargs="+", default=["C0", "C1", "C2"])
    args = ap.parse_args()
    wl = build_workload(args.new, args.seed)
    kinds = {k: sum(1 for w in wl if w["kind"] == k) for k in ("new", "exact_other_user", "paraphrase_same_user", "typo_same_user")}
    print(f"workload: {len(wl)} requests {kinds}")
    res = {}
    for name in args.configs:
        rows = asyncio.run(run_config(name, wl, args.concurrency, args.port))
        res[name] = {"summary": summarise(name, rows), "rows": rows}
    if "C0" in res and "C2" in res:
        c0, c2 = res["C0"]["summary"], res["C2"]["summary"]
        red = 1 - c2["gpu_seconds"] / c0["gpu_seconds"] if c0["gpu_seconds"] else float("nan")
        share = c2["cpu_answers"] / c2["generated"] if c2["generated"] else float("nan")
        print(f"\nNFR-3: CPU tier answered {c2['cpu_answers']}/{c2['generated']} = {share:.0%} of generated answers in C2 "
              f"(target >= 70 %); GPU-seconds C2 {c2['gpu_seconds']} vs C0 {c0['gpu_seconds']} -> {red:.0%} less "
              f"(target > 60 %)")
    log = ROOT / "logs" / "perf" / f"configs_{time.strftime('%Y%m%d_%H%M%S')}.json"
    log.write_text(json.dumps({"workload": wl, "results": res}, indent=1))
    print(f"-> {log.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
