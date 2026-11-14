"""Redis index helper. Run from the app dir:  PYTHONPATH=. python deploy/redis_index.py <create|backfill|check> [opts]

create    FT.CREATE on the existing chunk hashes (indexes ONLY thesis_id, access, embedding - text stays in the hash, saving RAM)
backfill  set the `access` field on chunks that lack it (needed whenever ACCESS_TIERS=1)
check     sanity-check the index AND that EMBED_* settings reproduce the stored vectors (catches wrong-embedder silently-bad retrieval)
"""
import argparse, sys
import numpy as np, redis
from askiisc import config as C

def build_create_cmd(index, prefix, dim, f_vec, f_thesis, f_access, m=16, efc=200):
    return ["FT.CREATE", index, "ON", "HASH", "PREFIX", "1", prefix, "SCHEMA",
            f_thesis, "TAG", f_access, "TAG",
            f_vec, "VECTOR", "HNSW", "10", "TYPE", "FLOAT32", "DIM", str(dim), "DISTANCE_METRIC", "COSINE",
            "M", str(m), "EF_CONSTRUCTION", str(efc)]

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("cmd", choices=["create", "backfill", "check"])
    ap.add_argument("--prefix", default="chunk:", help="key prefix of your chunk hashes")
    ap.add_argument("--access", default="public", help="value for backfill (public|restricted)")
    ap.add_argument("--dry-run", action="store_true"); ap.add_argument("--verify-embedder", action="store_true")
    a = ap.parse_args(); r = redis.from_url(C.REDIS_URL)
    if a.cmd == "create":
        cmd = build_create_cmd(C.INDEX_NAME, a.prefix, C.EMBED_DIM, C.F_VECTOR, C.F_THESIS_ID, C.F_ACCESS)
        print(" ".join(cmd))
        if not a.dry_run: print(r.execute_command(*cmd))
    elif a.cmd == "backfill":
        n = 0; pipe = r.pipeline(False)
        for k in r.scan_iter(match=a.prefix + "*", count=1000):
            pipe.hsetnx(k, C.F_ACCESS, a.access); n += 1
            if n % 1000 == 0: pipe.execute()
        pipe.execute(); print(f"checked {n} keys; set {C.F_ACCESS}={a.access} where it was missing")
    else:
        # FT.SEARCH count, not FT.INFO: FT.INFO on a vector index can deadlock this RediSearch build (2026-11-14).
        total = r.execute_command("FT.SEARCH", C.INDEX_NAME, "*", "LIMIT", "0", "0")
        print("num_docs:", total[0] if isinstance(total, list) else total)
        key = next(r.scan_iter(match=a.prefix + "*", count=100), None)
        if not key: sys.exit("FAIL: no keys with prefix " + a.prefix)
        h = {k.decode(): v for k, v in r.hgetall(key).items()}
        print("sample key:", key.decode(), "| fields:", sorted(h))
        for f in (C.F_TEXT, C.F_VECTOR, C.F_THESIS_ID):
            if f not in h: print(f"FAIL: field '{f}' missing on sample chunk (set F_* env vars to your real names)")
        if C.F_ACCESS not in h: print(f"WARN: no '{C.F_ACCESS}' field - run 'backfill' or set ACCESS_TIERS=0")
        v = np.frombuffer(h[C.F_VECTOR], dtype=np.float32) if C.F_VECTOR in h else np.array([])
        if v.size != C.EMBED_DIM: sys.exit(f"FAIL: stored vector has {v.size} dims, EMBED_DIM={C.EMBED_DIM} (or vectors are not raw FLOAT32 bytes)")
        print("vector dim OK:", v.size)
        res = r.execute_command("FT.SEARCH", C.INDEX_NAME, f"*=>[KNN 1 @{C.F_VECTOR} $B AS dist]", "PARAMS", "2", "B", v.tobytes(),
                                "SORTBY", "dist", "RETURN", "1", "dist", "DIALECT", "2")
        print("self-KNN distance (should be ~0):", res[2][1].decode())
        if a.verify_embedder:
            from askiisc.embedder import embed_sync
            e = embed_sync(h[C.F_TEXT].decode("utf-8", "replace")[:2000]); cos = float(np.dot(e, v) / (np.linalg.norm(e) * np.linalg.norm(v)))
            print(f"embedder-vs-stored cosine: {cos:.4f}", "OK" if cos > 0.98 else "MISMATCH: EMBED_MODEL/prefix differs from what built the index (note: if you embedded chunks WITH a passage prefix, expect ~0.95+)")

if __name__ == "__main__": main()
