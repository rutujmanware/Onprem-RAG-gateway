"""Run on the SERVER (or laptop) after copying chunks.npz:  PYTHONPATH=. python deploy/check_chunks.py chunks.npz [--verify-embedder]
Prints counts and, with --verify-embedder, checks that EMBED_MODEL reproduces the stored vectors (catches wrong embedder / query prefix)."""
import sys, collections
import numpy as np
from askiisc.retrieval import NpzRetriever
from askiisc import config as C
path = sys.argv[1]; r = NpzRetriever.load(path)
print(f"chunks: {len(r.meta)} | dim: {r.V.shape[1]} | theses: {len(set(r.thesis))}")
print("access tags:", dict(collections.Counter(r.access.tolist())))
if "" in set(r.access.tolist()): print("WARN: untagged chunks are hidden from the public tier. Re-export with --default-access, or set ACCESS_TIERS=0.")
if "--verify-embedder" in sys.argv:
    from askiisc.embedder import embed_sync
    rng = np.random.default_rng(0); worst = 1.0
    for i in rng.choice(len(r.meta), size=min(5, len(r.meta)), replace=False):
        e = embed_sync(r.meta[int(i)]["text"][:2000]); e = e / (np.linalg.norm(e) or 1); worst = min(worst, float(e @ r.V[int(i)]))
    print(f"embedder-vs-stored cosine (worst of 5): {worst:.4f}", "OK" if worst > 0.98 else "MISMATCH: wrong EMBED_MODEL / EMBED_QUERY_PREFIX / dimension")
