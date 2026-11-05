import asyncio, hashlib, re
import numpy as np
import httpx
from . import config as C

_st_model = None

def hash_embed(text: str, dim: int) -> np.ndarray:
    """Deterministic bag-of-words hashing embedder. Offline/testing only."""
    v = np.zeros(dim, dtype=np.float32)
    for tok in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        v[h % dim] += 1.0 if (h >> 64) & 1 else -1.0
    n = np.linalg.norm(v)
    return v / n if n else v

def _st_embed(text: str) -> np.ndarray:
    global _st_model
    if _st_model is None:
        from sentence_transformers import SentenceTransformer  # local weights; set HF_HUB_OFFLINE=1
        _st_model = SentenceTransformer(C.EMBED_MODEL)
    return np.asarray(_st_model.encode(text, normalize_embeddings=True), dtype=np.float32)

async def embed_query(text: str) -> np.ndarray:
    text = C.EMBED_QUERY_PREFIX + text
    b = C.EMBED_BACKEND
    if b == "hash":
        return hash_embed(text, C.EMBED_DIM)
    if b == "openai":
        async with httpx.AsyncClient(timeout=30) as cl:
            r = await cl.post(f"{C.EMBED_URL}/embeddings", json={"model": C.EMBED_MODEL, "input": text})
            r.raise_for_status()
            v = np.asarray(r.json()["data"][0]["embedding"], dtype=np.float32)
        n = np.linalg.norm(v)
        return v / n if n else v
    return await asyncio.to_thread(_st_embed, text)

def embed_sync(text: str) -> np.ndarray:
    """Used by train_router.py so training and serving share one code path."""
    return asyncio.run(embed_query(text))
