#!/usr/bin/env python3
"""NFR-1 / Sub-FR-1.1: typo-normaliser latency per question, including its corpus check (DEFECT-40/47).

Runs over the frozen retrieval-evaluation questions plus a misspelled copy of each (one random letter dropped
in every word of 7+ letters, so the corrector has real work). Cold = empty lookup caches (first time a word is
seen: dictionary + up to TYPO_MAX_CORPUS_LOOKUPS Redis counts); warm = second pass.
Usage: PYTHONPATH=. python scripts/measure_typo_latency.py
"""
import json
import random
import re
import statistics
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def main():
    from pipeline.text_processing import typo_normalizer as tn
    qs = []
    for f in ("questions", "rare_questions"):
        qs += [json.loads(l)["question"] for l in open(ROOT / "logs" / "retrieval_eval" / f"{f}.jsonl") if l.strip()]
    rnd = random.Random(7)

    def typo(w):
        if len(w) < 7:
            return w
        i = rnd.randrange(1, len(w) - 1)
        return w[:i] + w[i + 1:]
    mis = [re.sub(r"[A-Za-z]+", lambda m: typo(m.group(0)), q) for q in qs]
    norm = tn.TypoNormalizer.get_instance()
    norm.normalize("warm up the dictionaries")          # load SymSpell etc. outside the timing
    out = {}
    for label, texts in (("as asked", qs), ("misspelled", mis)):
        for phase in ("cold", "warm"):
            if phase == "cold":
                tn._in_corpus.cache_clear()
                for c in ("_correct_token_checked", "_correct_token_cached"):
                    getattr(getattr(norm, c, None), "cache_clear", lambda: None)()
            ts = []
            for q in texts:
                t0 = time.perf_counter()
                norm.normalize(q)
                ts.append((time.perf_counter() - t0) * 1000)
            out[f"{label} / {phase}"] = ts
            print(f"{label:11s} {phase}: n={len(ts)} p50 {statistics.median(ts):.3f} ms  p95 {pct(ts, 95):.3f} ms  "
                  f"p99 {pct(ts, 99):.3f} ms  max {max(ts):.3f} ms  (target p99 <= 0.5 ms)")
    log = ROOT / "logs" / "perf" / f"typo_latency_{time.strftime('%Y%m%d_%H%M%S')}.json"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({k: {"p50": statistics.median(v), "p95": pct(v, 95), "p99": pct(v, 99), "max": max(v),
                                   "n": len(v)} for k, v in out.items()}, indent=1))
    print(f"-> {log.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
