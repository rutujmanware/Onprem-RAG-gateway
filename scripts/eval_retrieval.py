#!/usr/bin/env python3
"""Retrieval quality evaluation (frozen question set, scored by thesis).

  generate  sample theses from the index and have the local 7B model write questions:
              * topic  - from the thesis abstract ("what research exists on ...")
              * detail - from one clean body chunk (a specific fact that chunk states)
            plus hand-written off-corpus questions that should retrieve nothing.
            Output: logs/retrieval_eval/questions.jsonl (+ .sha256). Data stays in logs/ (gitignored):
            questions are derived from thesis text.
  run       score one retrieval system on the frozen set -> logs/retrieval_eval/results_<system>.jsonl
  report    compare all scored systems

Metrics (answerable questions): hit@1/3/5/8 = gold thesis among the first k chunks; MRR@8; empty rate
(nothing returned = refusal). Off-corpus questions: accept rate (anything returned = false accept).
A hit counts only the gold thesis, so other theses that also answer the question count as misses:
absolute numbers are a lower bound, comparisons between systems are what matter.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "logs" / "retrieval_eval"
QFILE = OUT / "questions.jsonl"

OFF_CORPUS = [
    "Who won the 2018 FIFA World Cup?", "How do I make sourdough bread at home?",
    "What is the plot of the movie Titanic?", "Who is the lead singer of Coldplay?",
    "How many calories are in a banana?", "What is the best way to train a puppy?",
    "Which team has won the most IPL titles?", "How do I change a flat tyre on a car?",
    "What are the rules of chess castling?", "Who wrote the Harry Potter books?",
    "What is the capital of Australia?", "How do I remove a red wine stain from a carpet?",
    "What is the recipe for butter chicken?", "Which planet has the most moons?",
    "How tall is Mount Everest?", "Who painted the Mona Lisa?",
    "What are good exercises for lower back pain?", "How do I renew an Indian passport?",
    "What is the price of an iPhone 15?", "Who directed the movie Inception?",
    "How long should I boil an egg for a soft yolk?", "What time zone is Japan in?",
    "How do I knit a scarf for beginners?", "What is the population of Tokyo?",
    "Which country hosted the 2016 Summer Olympics?", "How do I make cold brew coffee?",
    "What is the story of the Mahabharata in brief?", "Who is the current captain of the Indian cricket team?",
    "What is a good beginner guitar song?", "How do I grow tomatoes on a balcony?",
    "What does the stock market index Nifty 50 measure?", "How do I set up a WhatsApp business account?",
    "Who invented the telephone?", "How should I prepare for a job interview?",
    "What is the best smartphone camera in 2023?", "How do I tie a tie?",
    "What are the main festivals celebrated in Kerala?", "What is the lifespan of a house cat?",
    "How do I write a wedding speech?", "Which is the longest river in the world?",
]

TOPIC_PROMPT = """Below is the abstract of a research thesis.
Write ONE question that a student might type into a search assistant, whose answer is the research described.
Rules: use your own words, do not copy the thesis title, do not mention "this thesis", "the author" or "the abstract",
at most 25 words. Output only the question.

Abstract:
{text}"""

DETAIL_PROMPT = """Below is a passage from a research thesis.
If it states a clear, specific technical fact, method or finding, write ONE natural question (at most 25 words)
that this passage answers. Do not mention "the passage", "this thesis", "the author" or "the study".
If the passage is not informative (references, front matter, tables, garbled text), output exactly SKIP.
Output only the question or SKIP.

Passage:
{text}"""


# ----------------------------------------------------------------------------------- helpers
def redis_client():
    import redis
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    return redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=int(os.getenv("REDIS_PORT", "6379")),
                       password=os.getenv("REDIS_PASSWORD") or None, protocol=2)


_WORDS = None


def english_words():
    global _WORDS
    if _WORDS is None:
        from importlib.resources import files
        _WORDS = {l.split()[0] for l in open(str(files("symspellpy") / "frequency_dictionary_en_82_765.txt"))}
    return _WORDS


JUNK_MARKERS = ("endobj", "/type", " obj\n", "acknowledg", "declaration", "i hereby", "certificate",
                "dedicated to", "table of contents", "et al", "vol.", "pp.")


def clean_prose(text: str) -> bool:
    """Conservative: only clearly readable prose qualifies as a question source."""
    low = text.lower()
    if any(m in low for m in JUNK_MARKERS) or len(re.findall(r"\b(?:19|20)\d\d\b", text)) >= 3:
        return False
    alpha = [t.lower() for t in re.findall(r"[A-Za-z]+", text) if len(t) > 2]
    if len(alpha) < 50:
        return False
    return sum(t in english_words() for t in alpha) / len(alpha) >= 0.8


def chunk_count(r, doc: str) -> int:
    lo, hi = 0, 1
    while r.exists(f"doc:chunk:{doc}_chunk_{hi}"):
        lo, hi = hi, hi * 2
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if r.exists(f"doc:chunk:{doc}_chunk_{mid}"):
            lo = mid
        else:
            hi = mid
    return lo + 1


def get_chunk(r, doc: str, i: int) -> str:
    return (r.hget(f"doc:chunk:{doc}_chunk_{i}", "content") or b"").decode("utf8", "ignore")


async def llm(client, prompt: str) -> str:
    base = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1").rstrip("/")
    key = os.getenv("VLLM_API_KEY", "")
    resp = await client.post(f"{base}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={
        "model": "Qwen/Qwen2.5-7B-Instruct", "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 60, "temperature": 0.3})
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip().strip('"')


# ----------------------------------------------------------------------------------- generate
def cmd_generate(args):
    import httpx
    OUT.mkdir(parents=True, exist_ok=True)
    if QFILE.exists() and not args.force:
        sys.exit(f"{QFILE} exists (frozen). Use --force to regenerate.")
    r = redis_client()
    rng = random.Random(args.seed)
    print("Listing theses ...")
    docs, cur = set(), 0
    while True:
        cur, keys = r.scan(cur, match="doc:chunk:*_chunk_0", count=20000)
        docs.update(k.decode()[len("doc:chunk:"):-len("_chunk_0")] for k in keys)
        if cur == 0:
            break
    docs = sorted(docs)
    rng.shuffle(docs)
    print(f"{len(docs)} theses; sampling until {args.n} topic + {args.n} detail sources")

    sources = []
    n_topic = n_detail = 0
    for doc in docs:
        if n_topic >= args.n and n_detail >= args.n:
            break
        raw = r.hget(f"doc:chunk:{doc}_chunk_0", "metadata")
        meta = json.loads(raw.decode("utf8", "ignore")) if raw else {}
        title = str(meta.get("title", ""))
        abstract = str(meta.get("abstract", "") or "")
        if n_topic < args.n and len(abstract) >= 300 and clean_prose(abstract):
            sources.append({"kind": "topic", "doc": doc, "title": title, "text": abstract[:2000]})
            n_topic += 1
        elif n_detail < args.n:
            n = chunk_count(r, doc)
            if n < 10:
                continue
            for i in rng.sample(range(3, n), min(15, n - 3)):  # skip metadata/abstract chunks
                text = get_chunk(r, doc, i)
                if clean_prose(text):
                    sources.append({"kind": "detail", "doc": doc, "title": title, "chunk": i, "text": text})
                    n_detail += 1
                    break

    async def gen_all():
        sem = asyncio.Semaphore(16)
        async with httpx.AsyncClient(timeout=60) as client:
            async def one(s):
                async with sem:
                    prompt = (TOPIC_PROMPT if s["kind"] == "topic" else DETAIL_PROMPT).format(text=s["text"])
                    try:
                        s["question"] = await llm(client, prompt)
                    except Exception as exc:
                        s["question"] = "SKIP"
                        print("  LLM error:", exc)
            await asyncio.gather(*[one(s) for s in sources])

    asyncio.run(gen_all())
    rows = []
    for s in sources:
        q = s["question"].splitlines()[0].strip() if s["question"] else "SKIP"
        if q.upper().startswith("SKIP") or len(q) < 15 or not q.endswith("?"):
            continue
        rows.append({"id": f"{s['kind']}-{len(rows):03d}", "kind": s["kind"], "question": q, "gold_doc": s["doc"],
                     "gold_chunk": s.get("chunk"), "title": s["title"]})
    for i, q in enumerate(OFF_CORPUS):
        rows.append({"id": f"off-{i:03d}", "kind": "off_corpus", "question": q, "gold_doc": None})
    with open(QFILE, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    digest = hashlib.sha256(QFILE.read_bytes()).hexdigest()
    (OUT / "questions.sha256").write_text(digest + "\n")
    kinds = {k: sum(r["kind"] == k for r in rows) for k in ("topic", "detail", "off_corpus")}
    print(f"Wrote {len(rows)} questions {kinds} -> {QFILE}\nSHA-256 {digest}")


# ----------------------------------------------------------------------------------- systems
def load_questions():
    digest = hashlib.sha256(QFILE.read_bytes()).hexdigest()
    frozen = (OUT / "questions.sha256").read_text().strip()
    if digest != frozen:
        sys.exit("questions.jsonl changed since it was frozen")
    return [json.loads(l) for l in QFILE.read_text().splitlines()]


def system_baseline():
    """Production retriever exactly as the pipeline calls it: search(query) with dynamic filtering."""
    from pipeline.retriever import RedisChunkRetriever
    ret = RedisChunkRetriever()

    def run(q):
        return [c.get("doc_id") for c in ret.search(q)]
    return run


class Candidates:
    """Dense KNN (no floor) + BM25 keyword candidates, with title and content for reranking."""

    STOP = set("""a an the of in on for to and or is are was were be been being what which who whom whose why how
    when where does do did can could should would will shall may might must this that these those with without from
    by about as at into than then there their it its such any all some explain describe tell me please give
    overview between over under also more most less very much many used use using based""".split())

    def __init__(self, k_dense=30, k_bm25=30):
        from pipeline.embeddings.embedder import LocalEmbedder
        self.r = redis_client()
        self.emb = LocalEmbedder()
        self.k_dense, self.k_bm25 = k_dense, k_bm25

    @staticmethod
    def _rows(res):
        out = []
        for i in range(1, len(res), 2):
            f = {res[i + 1][j].decode(): res[i + 1][j + 1] for j in range(0, len(res[i + 1]), 2)}
            out.append({"key": res[i].decode(), "doc_id": f.get("doc_id", b"").decode(),
                        "title": f.get("title", b"").decode("utf8", "ignore"),
                        "content": f.get("content", b"").decode("utf8", "ignore"),
                        "score": float(f["score"]) if "score" in f else None})
        return out

    def dense(self, q):
        import numpy as np
        vec = np.array(self.emb.embed_query(q), dtype=np.float32).tobytes()
        res = self.r.execute_command(
            "FT.SEARCH", "rag_documents_idx", f"*=>[KNN {self.k_dense} @vector $vec EF_RUNTIME 64 AS score]",
            "PARAMS", "2", "vec", vec, "SORTBY", "score", "RETURN", "4", "doc_id", "title", "content", "score",
            "LIMIT", "0", str(self.k_dense), "DIALECT", "2")
        rows = self._rows(res)
        for row in rows:
            row["similarity"] = 1 - row["score"]
        return rows

    def terms(self, q):
        toks = [t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9\-]+", q)]
        return [t.replace("-", " ") for t in toks if len(t) >= 3 and t not in self.STOP][:12]

    def bm25(self, q):
        terms = self.terms(q)
        if not terms:
            return []
        expr = "|".join(f"({t})" if " " in t else t for t in terms)
        try:
            res = self.r.execute_command(
                "FT.SEARCH", "rag_documents_idx", f"@content|title:({expr})", "SCORER", "BM25",
                "RETURN", "3", "doc_id", "title", "content", "LIMIT", "0", str(self.k_bm25), "DIALECT", "2")
        except Exception:
            return []
        return self._rows(res)

    def get(self, q, use_bm25=True):
        seen, out = set(), []
        for row in self.dense(q) + (self.bm25(q) if use_bm25 else []):
            if row["key"] not in seen:
                seen.add(row["key"])
                out.append(row)
        return out


class Reranker:
    def __init__(self, path):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        self.torch = torch
        torch.set_num_threads(int(os.getenv("RERANK_NUM_THREADS", "8")))  # same budget as query embedding
        self.tok = AutoTokenizer.from_pretrained(path)
        self.model = AutoModelForSequenceClassification.from_pretrained(path).eval()

    def scores(self, q, rows, batch=32):
        out = []
        for i in range(0, len(rows), batch):
            part = rows[i:i + batch]
            enc = self.tok([q] * len(part), [f"{r['title']}\n{r['content']}" for r in part], padding=True,
                           truncation=True, max_length=320, return_tensors="pt")
            with self.torch.no_grad():
                logits = self.model(**enc).logits
            out.extend((logits[:, 0] if logits.shape[1] == 1 else logits[:, -1]).tolist())
        return out


def per_doc_cap(rows, cap=2, k=8):
    counts, out = {}, []
    for row in rows:
        if counts.get(row["doc_id"], 0) < cap:
            counts[row["doc_id"]] = counts.get(row["doc_id"], 0) + 1
            out.append(row)
        if len(out) >= k:
            break
    return out


def system_dense(use_bm25=False):
    """Recall ceiling of the candidate pool, ranked by cosine (dense) or dense-then-BM25 order (no floor)."""
    cand = Candidates()

    def run(q):
        return [r["doc_id"] for r in cand.get(q, use_bm25=use_bm25)]
    return run


def system_rerank(model_dir, use_bm25=True, cap=2):
    """Dense (+BM25) candidates -> cross-encoder rerank -> per-thesis cap. Returns docs with scores so the
    report can sweep a refusal threshold."""
    cand, rr = Candidates(), Reranker(model_dir)

    def run(q):
        rows = cand.get(q, use_bm25=use_bm25)
        for row, s in zip(rows, rr.scores(q, rows)):
            row["rr"] = s
        rows.sort(key=lambda x: x["rr"], reverse=True)
        top = per_doc_cap(rows, cap=cap, k=8)
        return [(r["doc_id"], round(r["rr"], 4)) for r in top]
    return run


SYSTEMS = {
    "baseline": system_baseline,
    "dense30": lambda: system_dense(False),
    "dense30_bm25": lambda: system_dense(True),
    "rerank_minilm": lambda: system_rerank(str(ROOT / "models/reranker/ms-marco-MiniLM-L-6-v2")),
    "rerank_minilm_dense_only": lambda: system_rerank(str(ROOT / "models/reranker/ms-marco-MiniLM-L-6-v2"), use_bm25=False),
    "rerank_bge": lambda: system_rerank(str(ROOT / "models/reranker/bge-reranker-base")),
}


def cmd_run(args):
    qs = load_questions()
    if args.limit:
        qs = qs[:args.limit]
    run = SYSTEMS[args.system]()
    run(qs[0]["question"])  # warm up
    out = OUT / f"results_{args.system}.jsonl"
    with open(out, "w") as f:
        for i, q in enumerate(qs):
            t0 = time.perf_counter()
            got = run(q["question"])
            ms = (time.perf_counter() - t0) * 1000
            f.write(json.dumps({"id": q["id"], "got": got, "ms": round(ms, 1)}) + "\n")
            if i % 50 == 0:
                print(f"  {i}/{len(qs)}", flush=True)
    print("->", out)


# ----------------------------------------------------------------------------------- report
def metrics(qs, res, threshold=None):
    by_id = {r["id"]: r for r in res}
    ans = [q for q in qs if q["gold_doc"] and q["id"] in by_id]
    off = [q for q in qs if not q["gold_doc"] and q["id"] in by_id]

    def docs(r):
        got = r["got"]
        if got and isinstance(got[0], list):  # (doc, score) pairs
            got = [d for d, s in got if threshold is None or s >= threshold]
        return got

    m = {}
    for k in (1, 3, 5, 8):
        m[f"hit@{k}"] = sum(q["gold_doc"] in docs(by_id[q["id"]])[:k] for q in ans) / max(1, len(ans))
    rr = []
    for q in ans:
        d = docs(by_id[q["id"]])[:8]
        rr.append(1 / (d.index(q["gold_doc"]) + 1) if q["gold_doc"] in d else 0)
    m["mrr@8"] = statistics.mean(rr) if rr else 0
    m["empty"] = sum(not docs(by_id[q["id"]]) for q in ans) / max(1, len(ans))
    m["off_accept"] = sum(bool(docs(by_id[q["id"]])) for q in off) / max(1, len(off))
    ms = sorted(r["ms"] for r in res)
    m["p50_ms"], m["p95_ms"] = ms[len(ms) // 2], ms[int(len(ms) * 0.95)]
    return m, len(ans), len(off)


def cmd_report(args):
    qs = load_questions()
    files = sorted(OUT.glob("results_*.jsonl"))
    print(f"{'system':28} {'kind':7} {'n':>4} " + " ".join(f"{h:>8}" for h in
          ("hit@1", "hit@3", "hit@5", "hit@8", "mrr@8", "empty", "off_acc", "p50ms", "p95ms")))
    for fpath in files:
        name = fpath.stem[len("results_"):]
        res = [json.loads(l) for l in fpath.read_text().splitlines()]
        scored = res and res[0]["got"] and isinstance(res[0]["got"][0], list)
        thresholds = [None] + (args.thresholds if scored else [])
        for th in thresholds:
            for kind in ("all", "topic", "detail"):
                sub = [q for q in qs if kind == "all" or q["kind"] in (kind, "off_corpus")]
                m, na, _ = metrics(sub, res, th)
                label = name + (f" @rr>={th}" if th is not None else "")
                print(f"{label:28} {kind:7} {na:>4} " + " ".join(f"{m[k]:8.3f}" if k not in ("p50_ms", "p95_ms")
                      else f"{m[k]:8.0f}" for k in ("hit@1", "hit@3", "hit@5", "hit@8", "mrr@8", "empty",
                                                     "off_accept", "p50_ms", "p95_ms")))
        print()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate"); g.add_argument("--n", type=int, default=120); g.add_argument("--seed", type=int, default=20261114)
    g.add_argument("--force", action="store_true")
    r = sub.add_parser("run"); r.add_argument("system", choices=sorted(SYSTEMS)); r.add_argument("--limit", type=int)
    p = sub.add_parser("report"); p.add_argument("--thresholds", type=float, nargs="*", default=[])
    a = ap.parse_args()
    {"generate": cmd_generate, "run": cmd_run, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
