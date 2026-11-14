#!/usr/bin/env python3
"""Live end-to-end check of the semantic cache's safety properties against the running gateway.

Uses synthetic users (X-Internal-User, honoured only from loopback) and generic intent-flip question
pairs (not the red-team set). Every entry it creates is purged at the end.

Checks, per pair (seed, flipped follow-up):
  1. intent guard      user A asks the seed (answer cached), then the flipped follow-up: it must NOT be
                       served the seed's answer.
  2. cache works       user A rephrases the seed: it SHOULD be a semantic hit on A's own entry.
  3. owner scoping     user B rephrases the seed: it must NOT get A's semantic entry.
  4. exact sharing     user B asks the identical seed: served from the shared exact entry (by design).
And once, on a cached answer:
  5. regenerate        the hit carries a single-use token; using it gives a fresh answer; reusing it,
                       using it as another user, or for another question is rejected (403).

Usage: python scripts/check_cache_isolation.py [--gateway http://127.0.0.1:8080]   (exit 1 on any failure)
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PAIRS = [
    ("What are the advantages of solar photovoltaic systems?", "What are the disadvantages of solar photovoltaic systems?"),
    ("How does increasing temperature affect the viscosity of liquids?", "How does decreasing temperature affect the viscosity of liquids?"),
    ("Why does adding carbon increase the hardness of steel?", "Why does adding carbon decrease the hardness of steel?"),
    ("What is the maximum efficiency of a Carnot engine?", "What is the minimum efficiency of a Carnot engine?"),
    ("Is graphene a good conductor of electricity?", "Is graphene a good insulator of electricity?"),
    ("What are the benefits of using GMRES for sparse linear systems?", "What are the drawbacks of using GMRES for sparse linear systems?"),
    ("How does adding 2 percent copper change aluminium alloys?", "How does adding 5 percent copper change aluminium alloys?"),
    ("Does a higher Reynolds number make flow more turbulent?", "Does a higher Reynolds number make flow less turbulent?"),
]


def norm(t) -> str:
    return " ".join(str(t or "").lower().split()).rstrip("?")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gateway", default="http://127.0.0.1:8080")
    args = ap.parse_args()
    run = uuid.uuid4().hex[:6]
    A, B = f"cachecheck-a-{run}@iisc.ac.in", f"cachecheck-b-{run}@iisc.ac.in"
    cl = httpx.Client(timeout=300)
    asked, failures = [], []

    def ask(q, user, **extra):
        asked.append(q)
        for _ in range(8):
            r = cl.post(f"{args.gateway}/query", json={"query": q, "max_tokens": 160, **extra}, headers={"X-Internal-User": user})
            if r.status_code in (429, 503) and "regenerate_token" not in extra:
                time.sleep(5)
                continue
            return r
        return r

    def check(ok, label):
        print(f"   {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            failures.append(label)

    regen_case = None
    for i, (seed, flip) in enumerate(PAIRS):
        print(f"[{i}] {seed}")
        s = ask(seed, A).json()
        check(not s.get("cached"), "seed answered fresh")
        f = ask(flip, A).json()
        check(not (f.get("cached") and norm(f.get("matched_prompt")) == norm(seed)), "flipped follow-up not served the seed's answer")
        para = "Could you tell me " + seed[0].lower() + seed[1:]
        if s.get("chunks"):  # only answers with sources are cached
            pa = ask(para, A).json()
            check(bool(pa.get("cached")) and pa.get("cache_kind") == "semantic", f"same-user rephrase served from cache (sim {pa.get('similarity')})")
            pb = ask(para, B).json()
            check(not (pb.get("cached") and pb.get("cache_kind") == "semantic"), "other user's rephrase not served A's semantic entry")
            eb = ask(seed, B).json()
            check(bool(eb.get("cached")) and eb.get("cache_kind") == "exact", "other user's identical question served the shared exact entry")
            if regen_case is None and pa.get("regenerate_token"):
                regen_case = (para, pa["regenerate_token"])
        else:
            print("   skip  seed answer had no sources (not cached by design)")

    print("[regenerate]")
    if regen_case is None:
        check(False, "a cached answer carried a regenerate token")
    else:
        para, token = regen_case
        r = ask(para, B, regenerate_token=token)
        check(r.status_code == 403, f"token rejected for another user (HTTP {r.status_code})")
        # B's attempt consumed the single-use token; get a new one for A
        hit = ask(para, A).json()
        token = hit.get("regenerate_token")
        r = ask(PAIRS[0][1], A, regenerate_token=token)
        check(r.status_code == 403, f"token rejected for a different question (HTTP {r.status_code})")
        token = ask(para, A).json().get("regenerate_token")
        r = ask(para, A, regenerate_token=token)
        check(r.status_code == 200 and not r.json().get("cached"), "regenerate gives a fresh answer")
        r = ask(para, A, regenerate_token=token)
        check(r.status_code == 403, f"token is single-use (HTTP {r.status_code})")

    from gateway.cache.redis_semantic_cache import RedisSemanticCache
    c = RedisSemanticCache()
    purged = sum(c.purge(q, threshold=0.98) for q in set(asked))
    print(f"\npurged {purged} test entries | {len(failures)} failure(s)")
    for f in failures:
        print("   FAILED:", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
