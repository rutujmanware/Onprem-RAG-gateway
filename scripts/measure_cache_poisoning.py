#!/usr/bin/env python3
"""NFR-6 measurement: success rate of semantic-cache poisoning attempts against victim queries.

NFR-6: "<= 5% of adversarial poisoning attempts succeed in altering stored answers for legitimate
victim queries."

Protocol (each phase writes to logs/nfr6/, which is git-ignored):
  generate  Sample source-backed victim questions from the corpus-derived router dataset (fixed seed).
            The local vLLM model writes 2 attack questions per victim WITHOUT seeing the guard rules:
            one free-form, one with a randomly assigned attack strategy (incl. strategies the guard
            does not target). The same model then judges whether each attack really changes intent
            (an attack that is a mere paraphrase is not a poisoning attempt). The set is frozen and
            its SHA-256 recorded before any evaluation.
  predict   Offline: embedding similarity + guard decision per attempt (no Redis / gateway writes).
  live      End-to-end on the running gateway, sequentially: seed the attack through /query (real
            retrieval + generation, cached by the normal path), send the victim question, record
            whether the victim is served the attacker's cached entry, then purge the seed entry.
  report    Success rates with Wilson 95% confidence intervals.

Usage:
  python scripts/measure_cache_poisoning.py --run NAME generate --victims 150 --seed 20261114
  python scripts/measure_cache_poisoning.py --run NAME predict   # offline, SAME-user scenario (guard only)
  python scripts/measure_cache_poisoning.py --run NAME live --attacker-user a@x --victim-user v@y   # resumable
  python scripts/measure_cache_poisoning.py --run NAME report

Attacker and victim identities are sent as X-Internal-User (honoured by the gateway only from loopback),
so the live phase measures the NFR-6 scenario: an attacker account poisoning a different victim account.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ATTACKS = PREDICT = LIVE = None  # set per run in main()


def _set_run(name: str):
    global OUT, ATTACKS, PREDICT, LIVE
    OUT = ROOT / "logs" / "nfr6" / name
    ATTACKS, PREDICT, LIVE = OUT / "attacks.jsonl", OUT / "predict.jsonl", OUT / "live.jsonl"
GATEWAY = os.getenv("GATEWAY_URL", "http://127.0.0.1:8080").rstrip("/")

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass

STRATEGIES = [
    "insert a false or loaded premise (for example start with 'Why' and assume a claim that may be untrue)",
    "ask about the opposite property, effect or outcome",
    "change a quantifier or scope word (always, never, only, all, some, every)",
    "swap the roles of two entities in the question",
    "replace the main entity, material, method or organism with a different but related one",
    "change a condition, parameter, unit or time period",
    "add or remove a negation",
    "ask about a different attribute of the same subject (for example cost instead of accuracy)",
    "turn it into a yes/no question that presumes a wrong answer",
    "make the question much more general or much more specific so the original answer no longer fits",
]
SYSTEM = ("You are an AI security red-teamer testing a semantic cache in front of a research question-answering "
          "system. The cache returns a stored answer whenever a new question is worded very similarly to a "
          "previously stored question.")
ATTACK_PROMPT = (
    "Original question (asked later by an honest user):\n{victim}\n\n"
    "Write ONE new question that reuses as many of the original question's words as possible, so that a "
    "similarity-based cache would treat it as a duplicate, but whose correct answer would be wrong, misleading "
    "or incomplete for the honest user.{strategy}\nOutput only the new question, nothing else."
)
JUDGE_PROMPT = (
    "Question A (original): {victim}\nQuestion B: {attack}\n\n"
    "Would a complete and correct answer to Question B also be a complete and correct answer to Question A, "
    "without misleading the person who asked Question A? Reply with exactly one word: YES or NO."
)


# ---------------------------------------------------------------------------------------------
def _vllm_chat(messages, temperature, max_tokens):
    import httpx
    base = os.getenv("VLLM_BASE_URL", "http://127.0.0.1:8000/v1").rstrip("/")
    headers = {"Authorization": f"Bearer {os.getenv('VLLM_API_KEY', '')}"}
    body = {"model": os.getenv("VLLM_MODEL_NAME", "Qwen/Qwen2.5-7B-Instruct"), "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens}
    r = httpx.post(f"{base}/chat/completions", json=body, headers=headers, timeout=120)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def _clean_attack(text: str) -> str:
    line = next((l for l in text.splitlines() if l.strip()), "").strip()
    line = re.sub(r"^(new question|question|q)\s*[:\-]\s*", "", line, flags=re.I)
    return line.strip().strip('"“”\'` ').strip()


def _wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def _read(path):
    return [json.loads(l) for l in open(path)] if path.exists() else []


# ---------------------------------------------------------------------------------------------
def cmd_generate(args):
    OUT.mkdir(parents=True, exist_ok=True)
    if ATTACKS.exists() and not args.force:
        sys.exit(f"{ATTACKS} exists (frozen set). Use --force to regenerate.")
    rng = random.Random(args.seed)
    rows = [json.loads(l) for l in open(ROOT / "data" / "router_multichunk_training_dataset.jsonl")]
    pool = [r for r in rows if 6 <= len(r["query"].split()) <= 28 and r["query"].strip().endswith("?")]
    easy = [r for r in pool if r["label"] == "easy"]
    hard = [r for r in pool if r["label"] == "hard"]
    n_hard = round(args.victims * args.hard_share)
    victims = rng.sample(easy, args.victims - n_hard) + rng.sample(hard, n_hard)
    rng.shuffle(victims)

    out = []
    for vi, v in enumerate(victims):
        victim = v["query"].strip()
        strat_idx = rng.randrange(len(STRATEGIES))
        for kind in ("freeform", "strategy"):
            hint = f"\nUse this technique: {STRATEGIES[strat_idx]}." if kind == "strategy" else ""
            attack, error = "", None
            for _ in range(3):
                try:
                    attack = _clean_attack(_vllm_chat(
                        [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": ATTACK_PROMPT.format(victim=victim, strategy=hint)}],
                        temperature=0.9, max_tokens=90))
                except Exception as exc:  # pragma: no cover
                    error = str(exc)
                    time.sleep(2)
                    continue
                if 4 <= len(attack.split()) <= 60 and attack.lower() != victim.lower():
                    break
            verdict = ""
            if attack:
                verdict = _vllm_chat([{"role": "user", "content": JUDGE_PROMPT.format(victim=victim, attack=attack)}],
                                     temperature=0.0, max_tokens=3).upper()
            out.append({
                "id": f"v{vi:03d}-{kind}", "victim": victim, "victim_label": v["label"], "attack": attack,
                "kind": kind, "strategy": STRATEGIES[strat_idx] if kind == "strategy" else "freeform",
                "judge": "NO" if verdict.startswith("NO") else ("YES" if verdict.startswith("YES") else verdict),
                "valid_attack": verdict.startswith("NO") and bool(attack), "error": error,
            })
        print(f"[generate] {vi + 1}/{len(victims)}", flush=True)
    with open(ATTACKS, "w") as f:
        for r in out:
            f.write(json.dumps(r) + "\n")
    digest = hashlib.sha256(ATTACKS.read_bytes()).hexdigest()
    (OUT / "attacks.sha256").write_text(digest + "\n")
    print(f"[generate] wrote {len(out)} attempts; frozen sha256={digest}")


def _verify_frozen():
    digest = hashlib.sha256(ATTACKS.read_bytes()).hexdigest()
    expected = (OUT / "attacks.sha256").read_text().strip()
    if digest != expected:
        sys.exit("attack set changed after freezing; refusing to evaluate")
    return digest


# ---------------------------------------------------------------------------------------------
def cmd_predict(args):
    import numpy as np
    from gateway.cache.redis_semantic_cache import RedisSemanticCache
    from pipeline.text_processing.typo_normalizer import TypoNormalizer
    _verify_frozen()
    cache, norm = RedisSemanticCache.get_instance(), TypoNormalizer.get_instance()
    memo = {}

    def vec(q):
        if q not in memo:
            v = np.asarray(cache.embedder.embed_query(q), dtype=np.float32)
            memo[q] = v / np.linalg.norm(v)
        return memo[q]

    with open(PREDICT, "w") as f:
        for r in _read(ATTACKS):
            if not r["attack"]:
                continue
            seeds = {r["attack"], norm.normalize(r["attack"])}          # gateway stores normalized + raw
            victims = [norm.normalize(r["victim"]), r["victim"]]         # gateway looks up normalized, then raw
            best_sim, hit, reason = 0.0, False, ""
            for vq in victims:
                for sq in seeds:
                    sim = float(vec(vq) @ vec(sq))
                    best_sim = max(best_sim, sim)
                    if sim >= cache.similarity_threshold:
                        conflict, why = cache.check_entity_conflict(vq, sq)
                        hit = hit or not conflict
                        reason = reason or why
            f.write(json.dumps({"id": r["id"], "sim": round(best_sim, 4), "above_threshold": best_sim >= cache.similarity_threshold,
                                "predicted_hit": hit, "guard_reason": reason}) + "\n")
    print(f"[predict] wrote {PREDICT}")


# ---------------------------------------------------------------------------------------------
def _post_query(client, payload, user, timeout=300):
    for attempt in range(6):
        r = client.post(f"{GATEWAY}/query", json=payload, headers={"X-Internal-User": user}, timeout=timeout)
        if r.status_code in (429, 503):
            time.sleep(5 * (attempt + 1))
            continue
        return r
    return r


def _first_event(client, query, user):
    for attempt in range(6):
        with client.stream("POST", f"{GATEWAY}/query", json={"query": query, "stream": True},
                           headers={"X-Internal-User": user}, timeout=120) as r:
            if r.status_code in (429, 503):
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code != 200:
                return {"type": f"http-{r.status_code}"}
            for line in r.iter_lines():
                if line.startswith("data: "):
                    return json.loads(line[6:])
            return {}
    return {"type": "http-retry-exhausted"}


def cmd_live(args):
    import httpx
    from gateway.cache.redis_semantic_cache import RedisSemanticCache
    from pipeline.text_processing.typo_normalizer import TypoNormalizer
    digest = _verify_frozen()
    cache, norm = RedisSemanticCache.get_instance(), TypoNormalizer.get_instance()
    done = {r["id"] for r in _read(LIVE)}
    todo = [r for r in _read(ATTACKS) if r["valid_attack"] and r["id"] not in done]
    if args.limit:
        todo = todo[: args.limit]
    client = httpx.Client()
    client.get(f"{GATEWAY}/healthz", timeout=5).raise_for_status()
    (OUT / "live_config.json").write_text(json.dumps({"attacker_user": args.attacker_user, "victim_user": args.victim_user,
                                                       "max_tokens": args.max_tokens, "gateway": GATEWAY}))
    print(f"[live] attack set sha256={digest}; attacker={args.attacker_user} victim={args.victim_user}; "
          f"{len(todo)} valid attempts to run ({len(done)} already done)", flush=True)

    for i, r in enumerate(todo):
        seed, victim = r["attack"], r["victim"]
        seed_forms = {seed, norm.normalize(seed)}
        rec = {"id": r["id"], "t": time.time()}
        try:
            # 1. Attacker seeds the cache through the normal path (real retrieval + generation).
            t0 = time.time()
            resp = _post_query(client, {"query": seed, "max_tokens": args.max_tokens}, args.attacker_user)
            rec["seed_http"] = resp.status_code
            body = resp.json() if resp.status_code == 200 else {}
            rec["seed_cached_already"] = bool(body.get("cached"))
            rec["seed_chunks"] = len(body.get("chunks") or [])
            rec["seed_seconds"] = round(time.time() - t0, 1)
            exact = cache.lookup(seed)
            rec["seed_stored"] = bool(exact and exact.get("fast_exact") and exact.get("matched_prompt") in seed_forms)
            # 2. Honest victim asks the original question.
            ev = _first_event(client, victim, args.victim_user)
            rec["victim_event"] = ev.get("type")
            rec["victim_hit"] = ev.get("type") == "cache" and ev.get("status") == "HIT"
            rec["victim_matched"] = ev.get("matched_prompt")
            rec["victim_similarity"] = ev.get("similarity")
            rec["success"] = bool(rec["victim_hit"] and ev.get("matched_prompt") in seed_forms)
        except Exception as exc:
            rec["error"] = repr(exc)[:300]
        finally:
            # 3. Remove the attacker's entry (exact + vector, raw + normalized forms).
            for s in seed_forms:
                cache.purge(s, threshold=0.99)
            left = cache.lookup(seed)
            rec["purged"] = not (left and left.get("matched_prompt") in seed_forms)
        with open(LIVE, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"[live] {i + 1}/{len(todo)} {r['id']} stored={rec.get('seed_stored')} hit={rec.get('victim_hit')} "
              f"success={rec.get('success')} ({rec.get('seed_seconds')}s)", flush=True)
        time.sleep(args.pause)
    client.close()


# ---------------------------------------------------------------------------------------------
def cmd_report(args):
    attacks = {r["id"]: r for r in _read(ATTACKS)}
    pred = {r["id"]: r for r in _read(PREDICT)}
    live = {r["id"]: r for r in _read(LIVE)}
    digest = _verify_frozen()
    valid = [a for a in attacks.values() if a["valid_attack"]]
    print(f"Attack set sha256: {digest}")
    print(f"Victims: {len({a['victim'] for a in attacks.values()})}  attempts generated: {len(attacks)}  "
          f"judged real intent change (valid): {len(valid)}  judged paraphrase (excluded): "
          f"{sum(a['judge'] == 'YES' for a in attacks.values())}  unusable: "
          f"{sum(a['judge'] not in ('YES', 'NO') for a in attacks.values())}")

    if pred:
        pv = [pred[a["id"]] for a in valid if a["id"] in pred]
        above = [p for p in pv if p["above_threshold"]]
        print(f"\n[offline, SAME-user scenario: similarity + guard only] valid attempts with similarity >= threshold: {len(above)}/{len(pv)}; "
              f"predicted poisoning hits after guard: {sum(p['predicted_hit'] for p in pv)}")
        print("  guard reasons on blocked above-threshold attempts:",
              Counter(p['guard_reason'].split(':')[0] for p in above if not p['predicted_hit']).most_common())

    lv = [live[a["id"]] for a in valid if a["id"] in live]
    if not lv:
        print("\n[live] no results yet")
        return
    succ = [r for r in lv if r.get("success")]
    stored = [r for r in lv if r.get("seed_stored")]
    succ_stored = [r for r in stored if r.get("success")]
    errors = [r for r in lv if r.get("error")]
    lo, hi = _wilson(len(succ), len(lv))
    lo2, hi2 = _wilson(len(succ_stored), len(stored))
    cfg = json.loads((OUT / "live_config.json").read_text()) if (OUT / "live_config.json").exists() else {}
    print(f"\n[live] attacker={cfg.get('attacker_user', '?')} victim={cfg.get('victim_user', '?')}")
    print(f"[live] valid attempts run: {len(lv)}  errors: {len(errors)}  all seed entries purged: "
          f"{all(r.get('purged') for r in lv)}")
    print(f"  seed outcome: stored in cache {len(stored)}, refusal/no-chunks {sum(1 for r in lv if r.get('seed_http') == 200 and not r.get('seed_stored') and not r.get('seed_cached_already'))}, "
          f"seed itself hit an existing entry {sum(1 for r in lv if r.get('seed_cached_already'))}")
    print(f"  NFR-6 per attempt: {len(succ)}/{len(lv)} = {len(succ) / len(lv):.2%}  (95% CI {lo:.2%} - {hi:.2%})")
    print(f"  conditional on the seed being cached: {len(succ_stored)}/{len(stored)} = "
          f"{(len(succ_stored) / len(stored) if stored else float('nan')):.2%}  (95% CI {lo2:.2%} - {hi2:.2%})")
    by_victim = defaultdict(list)
    for r in lv:
        by_victim[attacks[r["id"]]["victim"]].append(bool(r.get("success")))
    print(f"  victims poisoned by at least one attempt: {sum(any(v) for v in by_victim.values())}/{len(by_victim)}")
    print(f"  victim hits on any OTHER (pre-existing) entry: {sum(1 for r in lv if r.get('victim_hit') and not r.get('success'))}")
    by_kind = defaultdict(lambda: [0, 0])
    for r in lv:
        k = attacks[r["id"]]["strategy"]
        by_kind[k][0] += bool(r.get("success"))
        by_kind[k][1] += 1
    print("  by strategy (successes/attempts):")
    for k, (s, n) in sorted(by_kind.items(), key=lambda x: -x[1][1]):
        print(f"    {s}/{n}  {k[:80]}")
    if pred:
        agree = sum(1 for r in lv if r["id"] in pred and bool(r.get("success")) == pred[r["id"]]["predicted_hit"]
                    and r.get("seed_stored"))
        print(f"  offline prediction agrees with live outcome (seed stored): {agree}/{len(stored)}")
    for r in succ:
        a = attacks[r["id"]]
        print(f"\n  SUCCESS {r['id']} sim={r.get('victim_similarity')}\n    victim: {a['victim']}\n    attack: {a['attack']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", default="run1-shared-cache", help="run name; data in logs/nfr6/<run>/")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--victims", type=int, default=150)
    g.add_argument("--hard-share", type=float, default=0.25)
    g.add_argument("--seed", type=int, default=20261114)
    g.add_argument("--force", action="store_true")
    sub.add_parser("predict")
    l = sub.add_parser("live")
    l.add_argument("--limit", type=int, default=0)
    l.add_argument("--max-tokens", type=int, default=200)
    l.add_argument("--pause", type=float, default=2.0)
    l.add_argument("--attacker-user", default="nfr6-attacker@iisc.ac.in")
    l.add_argument("--victim-user", default="nfr6-victim@iisc.ac.in")
    sub.add_parser("report")
    args = ap.parse_args()
    _set_run(args.run)
    {"generate": cmd_generate, "predict": cmd_predict, "live": cmd_live, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
