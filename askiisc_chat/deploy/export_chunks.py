"""Run on the LAPTOP that holds your Redis data. Exports chunks to ONE file for the server (RETRIEVER_BACKEND=npz).
  PYTHONPATH=. python deploy/export_chunks.py --prefix "chunk:" --out chunks.npz [--default-access public]
Uses the same F_* field names and EMBED_DIM as the app (askiisc/config.py env vars). No pickle is used in the file."""
import argparse, json, sys
import numpy as np, redis
from askiisc import config as C

def export(client, prefix, out, default_access="", dim=None, batch=500):
    dim = dim or C.EMBED_DIM
    keys = list(client.scan_iter(match=prefix + "*", count=1000)); vecs, meta, bad = [], [], 0
    for i in range(0, len(keys), batch):
        pipe = client.pipeline(False)
        for k in keys[i:i + batch]: pipe.hgetall(k)
        for k, h in zip(keys[i:i + batch], pipe.execute()):
            h = {(x.decode() if isinstance(x, bytes) else x): v for x, v in h.items()}
            raw = h.get(C.F_VECTOR)
            if not raw or len(raw) != 4 * dim or not h.get(C.F_TEXT): bad += 1; continue
            g = lambda f: (h.get(f, b"").decode("utf-8", "replace") if isinstance(h.get(f, b""), bytes) else str(h.get(f, "")))
            vecs.append(np.frombuffer(raw, dtype=np.float32))
            meta.append({"id": k.decode() if isinstance(k, bytes) else k, "text": g(C.F_TEXT), "thesis_id": g(C.F_THESIS_ID),
                         "title": g(C.F_TITLE), "chapter": g(C.F_CHAPTER), "version": g(C.F_VERSION),
                         "access": g(C.F_ACCESS) or default_access})
    if not vecs: sys.exit(f"nothing exported ({len(keys)} keys, {bad} unusable). Check --prefix, F_* names, EMBED_DIM={dim}")
    np.savez_compressed(out, vectors=np.stack(vecs), meta_json=np.frombuffer(json.dumps(meta).encode("utf-8"), dtype=np.uint8))
    return len(vecs), bad

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--prefix", default="chunk:"); ap.add_argument("--out", default="chunks.npz")
    ap.add_argument("--default-access", default="", help="value for chunks lacking the access field; '' = leave untagged (hidden from public tier)")
    a = ap.parse_args(); n, bad = export(redis.from_url(C.REDIS_URL), a.prefix, a.out, a.default_access)
    print(f"exported {n} chunks to {a.out}; skipped {bad} unusable (missing text/vector or wrong vector size)")
