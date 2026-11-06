import asyncio, json
from dataclasses import dataclass, asdict
import numpy as np
from . import config as C

@dataclass
class Chunk:
    id: str
    text: str
    thesis_id: str = ""
    title: str = ""
    chapter: str = ""
    version: str = ""
    distance: float = 0.0   # cosine distance (lower = closer)
    def cite(self): return asdict(self) | {"text": self.text[:300]}

def _s(x):
    return x.decode("utf-8", "replace") if isinstance(x, (bytes, bytearray)) else str(x)

def escape_tag(v: str) -> str:
    out = []
    for ch in v:
        out.append("\\" + ch if not (ch.isalnum() or ch == "_") else ch)
    return "".join(out)

def build_query(thesis_id: str | None, k: int, access: str | None = None) -> str:
    parts = []
    if access: parts.append(f"@{C.F_ACCESS}:{{{escape_tag(access)}}}")
    if thesis_id: parts.append(f"@{C.F_THESIS_ID}:{{{escape_tag(thesis_id)}}}")
    flt = "(" + " ".join(parts) + ")" if parts else "*"
    return f"{flt}=>[KNN {k} @{C.F_VECTOR} $B AS dist]"

def parse_reply(reply) -> list[Chunk]:
    """FT.SEARCH reply (RESP2): [total, key1, [f, v, f, v...], key2, [...]]"""
    out = []
    for i in range(1, len(reply), 2):
        key, fields = _s(reply[i]), reply[i + 1]
        d = {_s(fields[j]): fields[j + 1] for j in range(0, len(fields), 2)}
        out.append(Chunk(
            id=key, text=_s(d.get(C.F_TEXT, "")),
            thesis_id=_s(d.get(C.F_THESIS_ID, "")), title=_s(d.get(C.F_TITLE, "")),
            chapter=_s(d.get(C.F_CHAPTER, "")), version=_s(d.get(C.F_VERSION, "")),
            distance=float(_s(d.get("dist", 0.0)))))
    return out

class Retriever:
    def __init__(self, redis_client):
        self.r = redis_client

    async def ping(self) -> bool:
        return bool(await self.r.ping())

    async def search(self, qvec: np.ndarray, thesis_id: str | None = None, k: int | None = None,
                     access: str | None = None) -> list[Chunk]:
        """access="public" restricts to public chunks (the ONLY thing stopping anonymous users seeing restricted text)."""
        k = k or C.TOP_K
        args = ["FT.SEARCH", C.INDEX_NAME, build_query(thesis_id, k, access),
                "PARAMS", "2", "B", qvec.astype(np.float32).tobytes(),
                "SORTBY", "dist", "ASC", "LIMIT", "0", str(k),
                "RETURN", "6", C.F_TEXT, C.F_THESIS_ID, C.F_TITLE, C.F_CHAPTER, C.F_VERSION, "dist",
                "DIALECT", "2"]
        return parse_reply(await self.r.execute_command(*args))

    async def theses(self, access: str | None = None) -> list[str]:
        """Distinct thesis ids for the UI filter. Public tier only sees ids that have public chunks."""
        try:
            if access is None:
                return sorted(_s(x) for x in await self.r.execute_command("FT.TAGVALS", C.INDEX_NAME, C.F_THESIS_ID))
            rep = await self.r.execute_command("FT.AGGREGATE", C.INDEX_NAME, f"@{C.F_ACCESS}:{{{escape_tag(access)}}}",
                                               "GROUPBY", "1", f"@{C.F_THESIS_ID}", "LIMIT", "0", "1000")
            return sorted(_s(row[1]) for row in rep[1:])
        except Exception:
            return []


class NpzRetriever:
    """In-RAM cosine search over chunks exported by deploy/export_chunks.py. Same interface as Retriever.
    Fine up to ~1M chunks x 384 dims on a CPU (a few ms to ~100 ms per query). Untagged chunks (access="") are invisible to access="public"."""
    def __init__(self, vectors: np.ndarray, meta: list[dict]):
        if len(vectors) != len(meta): raise ValueError("vectors/meta length mismatch")
        if vectors.ndim != 2 or vectors.shape[1] != C.EMBED_DIM:
            raise ValueError(f"chunk vectors have dim {vectors.shape[-1]}, EMBED_DIM={C.EMBED_DIM}: wrong embedder/dimension")
        n = np.linalg.norm(vectors, axis=1, keepdims=True); n[n == 0] = 1
        self.V = np.ascontiguousarray(vectors / n, dtype=np.float32); self.meta = meta
        self.thesis = np.array([m.get("thesis_id", "") for m in meta]); self.access = np.array([m.get("access", "") for m in meta])

    @classmethod
    def load(cls, path: str):
        z = np.load(path, allow_pickle=False)          # no pickle: the file crosses machines
        return cls(z["vectors"], json.loads(bytes(z["meta_json"]).decode("utf-8")))

    async def ping(self) -> bool: return len(self.meta) > 0

    def _search(self, qvec, thesis_id, k, access):
        if qvec.shape[0] != self.V.shape[1]: raise ValueError("query/chunk dimension mismatch")
        q = (qvec / (np.linalg.norm(qvec) or 1.0)).astype(np.float32)
        mask = np.ones(len(self.meta), dtype=bool)
        if thesis_id: mask &= self.thesis == thesis_id
        if access: mask &= self.access == access
        if not mask.any(): return []
        idx = np.flatnonzero(mask)
        sims = self.V @ q if mask.all() else self.V[idx] @ q
        top = np.argsort(-sims)[:k]
        out = []
        for t in top:
            m = self.meta[int(idx[t])]
            out.append(Chunk(id=str(m.get("id", "")), text=m.get("text", ""), thesis_id=m.get("thesis_id", ""), title=m.get("title", ""),
                             chapter=str(m.get("chapter", "")), version=str(m.get("version", "")), distance=float(1.0 - sims[t])))
        return out

    async def search(self, qvec, thesis_id=None, k=None, access=None) -> list[Chunk]:
        return await asyncio.to_thread(self._search, qvec, thesis_id, k or C.TOP_K, access)

    async def theses(self, access=None) -> list[str]:
        sel = self.thesis if not access else self.thesis[self.access == access]
        return sorted(set(str(x) for x in sel if x))
