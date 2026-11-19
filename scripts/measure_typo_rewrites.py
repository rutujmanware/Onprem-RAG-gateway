#!/usr/bin/env python3
"""DEFECT-62: does the typo normaliser still correct typos while keeping technical coinages?

Part 1: misspelled copies of the frozen retrieval-evaluation questions (5 seeds; dropped, doubled, swapped and
substituted letters). For every typo the previous rule corrected, is it still corrected, or now kept as typed
because it looks like a term (looks_like_term)? Also which real coinages are kept.
Part 2: misspelled questions that the normaliser fully corrects (they hit the original's exact cache entry): how
many does the exact-path intent guard (gateway._cache_lookup) now refuse? Plus the reported cases.
Needs Redis with the thesis index (corpus checks).
Usage: PYTHONPATH=. python scripts/measure_typo_rewrites.py
Result 2026-11-19: 414/416 typos still corrected (graphine, aerospade kept); 0/37 typo exact hits refused; graphane,
graphyne, ferrielectric kept and their hits against graphene / ferroelectric refused.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---- part 1

import json, random, re
from collections import Counter
from pathlib import Path
from pipeline.text_processing import typo_normalizer as tn
qs = []
for f in ("questions", "rare_questions"):
    qs += [json.loads(l)["question"] for l in open(Path("logs/retrieval_eval") / f"{f}.jsonl") if l.strip()]
norm = tn.TypoNormalizer.get_instance()
tn.MAX_CORPUS_LOOKUPS = 10 ** 6
def mk(seed):
    rnd = random.Random(seed)
    def typo(w):
        if len(w) < 5 or not w.isalpha(): return w
        i = rnd.randrange(1, len(w) - 1); k = rnd.choice(["drop", "dup", "swap", "sub"])
        if k == "drop": return w[:i] + w[i + 1:]
        if k == "dup": return w[:i] + w[i] + w[i:]
        if k == "swap": return w[:i] + w[i + 1] + w[i] + w[i + 2:]
        return w[:i] + rnd.choice("aeiou" if w[i] in "aeiou" else "bcdfghklmnprst") + w[i + 1:]
    return typo
corrected, lost = {}, {}
for seed in (11, 12, 13, 14, 15):
    typo = mk(seed)
    for q in qs:
        for w in re.findall(r"[A-Za-z]+", q):
            t = typo(w.lower())
            if t == w.lower() or t in corrected or t in lost: continue
            # what would the pre-DEFECT-62 rule do? temporarily disable the new check
            saved = tn.looks_like_term; tn.looks_like_term = lambda *a, **k: False
            old = norm._correct_token(t)
            tn.looks_like_term = saved
            if old == t: continue                       # never corrected anyway
            new = norm._correct_token(t)
            (corrected if new != t else lost)[t] = old
print("typos the old rule corrected:", len(corrected) + len(lost), "| still corrected:", len(corrected),
      "| now kept (lost corrections):", len(lost))
print("lost:", lost)
real = ["ferrielectric", "graphane", "graphyne", "silicene", "germanene", "stanene", "borophene", "phosphorene",
        "nanoribbons", "antiferroelectric", "ferrimagnet", "nanographene", "polyanilines", "piezoresponse",
        "magnetoelectric", "thermoelectrics", "hydrogenase", "graphite", "graphdiyne", "nanocrystallites"]
print({w: (norm._correct_token(w), tn.looks_like_term(w, norm.vocab)) for w in real})

# ---- part 2

import json, random, re
from pathlib import Path
from pipeline.text_processing import typo_normalizer as tn
from gateway.cache.redis_semantic_cache import RedisSemanticCache as C

qs = []
for f in ("questions", "rare_questions"):
    qs += [json.loads(l)["question"] for l in open(Path("logs/retrieval_eval") / f"{f}.jsonl") if l.strip()]
norm = tn.TypoNormalizer.get_instance()
tn.MAX_CORPUS_LOOKUPS = 10 ** 6
total = refused = 0
examples = []
for seed in (11, 12, 13):
    rnd = random.Random(seed)
    def typo(w):
        if len(w) < 5 or not w.isalpha(): return w
        if rnd.random() > 0.35: return w          # misspell about a third of the longer words
        i = rnd.randrange(1, len(w) - 1); k = rnd.choice(["drop", "dup", "swap", "sub"])
        if k == "drop": return w[:i] + w[i + 1:]
        if k == "dup": return w[:i] + w[i] + w[i:]
        if k == "swap": return w[:i] + w[i + 1] + w[i] + w[i + 2:]
        return w[:i] + rnd.choice("aeiou" if w[i] in "aeiou" else "bcdfghklmnprst") + w[i + 1:]
    for q in qs:
        t = re.sub(r"[A-Za-z]+", lambda m: typo(m.group(0)), q)
        if t == q:
            continue
        nq, nt = norm.normalize(q), norm.normalize(t)
        if nt != nq:             # not fully corrected: would not hit the exact entry anyway
            continue
        total += 1
        conflict, reason = C.check_entity_conflict(t, nq)
        if conflict:
            refused += 1
            if len(examples) < 8:
                examples.append((t, reason))
print(f"fully corrected misspelled questions: {total}; exact hit now refused: {refused} ({100 * refused / max(total, 1):.1f} %)")
for e in examples:
    print("  ", e)
for typed, stored in (("What are graphane transistors used for?", "What are graphene transistors used for?"),
                      ("Explain ferrielectric ordering in thin films", "Explain ferroelectric ordering in thin films"),
                      ("What is graphyne?", "What is graphene?"),
                      ("what is neroscience?", "what is neuroscience?"),
                      ("What are grapheen transistors used for?", "What are graphene transistors used for?")):
    print(f"normalize({typed!r}) -> {norm.normalize(typed)!r}; guard(typed, stored) = {C.check_entity_conflict(typed, stored)}")
