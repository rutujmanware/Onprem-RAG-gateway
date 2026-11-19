#!/usr/bin/env python3
"""NFR-4: cross-account leak probes against an isolated measurement gateway (own cache prefix; cleaned up).

25 victim users each ask a thesis question in their own wording; their answers are cached (the wording is the
private part: a near-duplicate hit would show it to someone else as matched_prompt). Questions carry no digits,
because the intent guard refuses semantic hits whose numbers differ, which would hide the path under test.
Then 20 probes per victim (500 in total) are sent from attacker accounts:

  paraphrase/json (6), paraphrase/stream (2), paraphrase/v1 (2)   close rewordings of the victim's question
                                                                   (the 10 most similar of 12 written by the 7B)
  prefixed (2)        the victim's wording with a short prefix of the attacker's ("In IISc theses, ...")
  bare (1)            the question without any ID
  exact (2)           the victim's exact text (shared exact entry by design: must carry nothing of the victim)
  regen-theft (1)     the victim's regenerate token used by the attacker              -> must be 403
  forged-unsigned (1) X-Internal-User = victim, no signature                         -> must be 403
  forged-swap (1)     victim's signature with the attacker's name                    -> must be 403
  forged-expired (1)  a validly signed but 1-hour-old victim identity                -> must be 403
  admin (1)           GET /cache/stats without the admin key                         -> must be 404

Leak = a response that contains a victim's email or student ID the attacker did not type, serves a victim's
cached wording (matched_prompt) to someone else, or (security probes) is anything other than the refusal.
Attacker probes use max_tokens=16: never cached, cheap on a miss; the cache lookup runs in full.
Live-path control (not counted): afterwards each victim asks the same 10 paraphrases; those that hit the victim's
own entry are the probes that would have leaked without owner scoping (the intent guard alone lets them through).

Usage: PYTHONPATH=. python scripts/measure_cross_account.py [--victims 25] [--port 8099]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from eval_gateway import EvalGateway  # noqa: E402
from eval_injection_fence import parse_list, vllm_chat  # noqa: E402
from gateway.auth.internal import AUTH_HEADER, USER_HEADER, internal_user_headers  # noqa: E402


def norm(t):
    return " ".join(str(t or "").lower().split()).rstrip("?")


def wilson_upper(k, n, z=1.96):
    if n == 0:
        return 1.0
    p = k / n
    d = 1 + z * z / n
    return (p + z * z / (2 * n) + z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--victims", type=int, default=25)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--seed", type=int, default=20261119)
    args = ap.parse_args()
    rnd = random.Random(args.seed)
    run = uuid.uuid4().hex[:6]

    base = [json.loads(l)["question"] for l in open(ROOT / "logs/retrieval_eval/questions.jsonl") if l.strip()]
    base = rnd.sample([q for q in base if 40 <= len(q) <= 160 and not any(ch.isdigit() for ch in q)], args.victims * 2)

    # paraphrases written before any request, ranked by embedding similarity to the base question
    from pipeline.embeddings.embedder import LocalEmbedder
    emb = LocalEmbedder()

    def paraphrases(q):
        out = parse_list(vllm_chat("Rewrite this question in 12 different ways that keep exactly the same meaning "
                                   "(small wording changes, synonyms, word order). Output only a JSON list of strings."
                                   f"\n\n{q}", temperature=0.7, max_tokens=1500))
        out = [p for p in dict.fromkeys(out) if norm(p) != norm(q)]
        if len(out) < 11:
            return None
        vq = emb.embed_query(q)
        sims = [sum(a * b for a, b in zip(vq, v)) for v in emb.embed_documents(out)]
        ranked = [p for _, p in sorted(zip(sims, out), reverse=True)]
        return ranked[:11], sorted(sims, reverse=True)[:11]
    with ThreadPoolExecutor(8) as ex:
        paras = list(ex.map(paraphrases, base))
    victims = []
    for i, (q, p) in enumerate((q, p) for q, p in zip(base, paras) if p):
        if len(victims) == args.victims:
            break
        victims.append({"email": f"victim-{run}-{i}@iisc.ac.in", "sid": f"victim-{run}-{i}", "base": q,
                        "text": q, "paras": p[0], "para_sims": p[1]})
    print(f"{len(victims)} victims, paraphrase similarity to the base question: "
          f"min {min(min(v['para_sims']) for v in victims):.3f}, median "
          f"{sorted(s for v in victims for s in v['para_sims'])[len(victims) * 5]:.3f}")

    env = {"RATE_LIMIT_BURST": "100000", "RATE_LIMIT_RPS": "100000"}
    gw = EvalGateway(port=args.port, workers=2, env=env, tag="nfr4").start()
    cl = httpx.Client(timeout=300)
    url = gw.url
    results, controls = [], []
    try:
        def q(user_headers, text, stream=False, **extra):
            return cl.post(f"{url}/query", headers=user_headers,
                           json={"query": text, "stream": stream, **extra})

        # 1. victims ask; their answers are cached (standard settings)
        def seed(v):
            for _ in range(5):
                r = q(internal_user_headers(v["email"]), v["text"])
                if r.status_code == 200:
                    return r.json()
                time.sleep(3)
            return {}
        with ThreadPoolExecutor(4) as ex:
            seeded = list(ex.map(seed, victims))
        for v, s in zip(victims, seeded):
            v["cached"] = bool(s.get("chunks")) and not s.get("incomplete")
        print(f"victim answers with sources (cacheable): {sum(v['cached'] for v in victims)}/{len(victims)}")

        all_emails = {v["email"] for v in victims}
        all_sids = {v["sid"] for v in victims}
        victim_texts = {norm(v["text"]): v for v in victims}

        def leak_check(kind, attacker, typed, r, victim):
            body = r.text
            typed_l = typed.lower()
            leaked = [e for e in all_emails if e in body]
            leaked += [s for s in all_sids if s in body and s.lower() not in typed_l]
            mp = re.findall(r'"matched_prompt"\s*:\s*"((?:[^"\\]|\\.)*)"', body)
            served_other = [m for m in mp if norm(json.loads(f'"{m}"')) in victim_texts and norm(json.loads(f'"{m}"')) != norm(typed)]
            hit_kind = re.findall(r'"cache_kind"\s*:\s*"(\w+)"', body)
            return {"kind": kind, "victim": victim["email"], "attacker": attacker, "status": r.status_code,
                    "leak": bool(leaked or served_other), "leaked": leaked + served_other, "cache_kind": hit_kind[:1]}

        def probe_set(v, vi):
            out = []
            atk = lambda j: f"attacker-{run}-{vi}-{j}@iisc.ac.in"  # noqa: E731
            small = {"max_tokens": 16}
            paras = v["paras"]
            for j, p in enumerate(paras[:6]):
                out.append(leak_check("paraphrase/json", atk(j), p, q(internal_user_headers(atk(j)), p, **small), v))
            for j, p in enumerate(paras[6:8]):
                out.append(leak_check("paraphrase/stream", atk(6 + j), p, q(internal_user_headers(atk(6 + j)), p, stream=True, **small), v))
            for j, p in enumerate(paras[8:10]):
                r = cl.post(f"{url}/v1/chat/completions", headers=internal_user_headers(atk(8 + j)),
                            json={"messages": [{"role": "user", "content": p}], "max_tokens": 16})
                out.append(leak_check("paraphrase/v1", atk(8 + j), p, r, v))
            for j, pre in enumerate(("In IISc theses, ", "According to the theses, ")):
                t = pre + v["base"][0].lower() + v["base"][1:]
                out.append(leak_check("prefixed", atk(10 + j), t, q(internal_user_headers(atk(10 + j)), t, **small), v))
            out.append(leak_check("paraphrase/json", atk(12), paras[10], q(internal_user_headers(atk(12)), paras[10], **small), v))
            for j in range(2):
                out.append(leak_check("exact", atk(13 + j), v["text"], q(internal_user_headers(atk(13 + j)), v["text"], **small), v))
            # victim gets a regenerate token on a cache hit; the attacker tries to use it
            tok = q(internal_user_headers(v["email"]), v["text"]).json().get("regenerate_token")
            r = q(internal_user_headers(atk(15)), v["text"], regenerate_token=tok or "missing")
            out.append({**leak_check("regen-theft", atk(15), v["text"], r, v), "leak": r.status_code != 403, "had_token": bool(tok)})
            r = q({USER_HEADER: v["email"]}, v["base"], **small)
            out.append({**leak_check("forged-unsigned", "anonymous", v["base"], r, v), "leak": r.status_code != 403})
            h = internal_user_headers(v["email"])
            r = q({USER_HEADER: atk(16), AUTH_HEADER: h[AUTH_HEADER]}, v["base"], **small)
            out.append({**leak_check("forged-swap", atk(16), v["base"], r, v), "leak": r.status_code != 403})
            r = q(internal_user_headers(v["email"], now=time.time() - 3600), v["base"], **small)
            out.append({**leak_check("forged-expired", "replayer", v["base"], r, v), "leak": r.status_code != 403})
            r = cl.get(f"{url}/cache/stats", headers=internal_user_headers(atk(17)))
            out.append({**leak_check("admin", atk(17), "", r, v), "leak": r.status_code != 404})
            # live-path control (not counted): the victim asks the attackers' paraphrases; hits = probes that would
            # have leaked without owner scoping
            live = 0
            for p in paras[:11]:
                c = q(internal_user_headers(v["email"]), p, **small)
                live += bool(re.findall(r'"cache_kind"\s*:\s*"semantic"', c.text))
            return out, {"victim": v["email"], "cached": v["cached"], "live_paraphrases": live}

        with ThreadPoolExecutor(4) as ex:
            for out, ctl in ex.map(lambda a: probe_set(*a), [(v, i) for i, v in enumerate(victims)]):
                results += out
                controls.append(ctl)
    finally:
        gw.stop()

    n, k = len(results), sum(r["leak"] for r in results)
    by = {}
    for r in results:
        b = by.setdefault(r["kind"], [0, 0, {}])
        b[0] += 1
        b[1] += r["leak"]
        b[2][r["status"]] = b[2].get(r["status"], 0) + 1
    print(f"\nNFR-4 cross-account probes: {k} leaks / {n} probes (95% upper bound {wilson_upper(k, n):.2%}); target 0 / >= 500")
    for kind, (cnt, lk, st) in by.items():
        print(f"   {kind:18s} {cnt:4d} probes  leaks {lk}  status {st}")
    live = sum(c["live_paraphrases"] for c in controls if c["cached"])
    print(f"live-path control: of the {11 * sum(c['cached'] for c in controls)} paraphrase probes against cached victims, "
          f"{live} hit the victim's entry when the VICTIM asked them, i.e. would have leaked without owner scoping")
    for r in results:
        if r["leak"]:
            print("   LEAK", r)
    log = ROOT / "logs" / "nfr4" / f"cross_account_{time.strftime('%Y%m%d_%H%M%S')}.json"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"victims": [{k2: v[k2] for k2 in ("email", "sid", "text", "paras", "para_sims", "cached")}
                                           for v in victims], "results": results, "controls": controls}, indent=1))
    print(f"-> {log.relative_to(ROOT)}")
    return 1 if k else 0


if __name__ == "__main__":
    sys.exit(main())
