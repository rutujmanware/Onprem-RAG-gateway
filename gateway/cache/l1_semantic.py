import json
import logging
import math
import time
from typing import Any, Dict, List, Optional
try:
    import numpy as np
except ImportError:
    np = None
try:
    import redis.asyncio as redis
except ImportError:
    redis = None

logger = logging.getLogger(__name__)

class L1SemanticCache:
    def __init__(self, redis_client=None, similarity_threshold=0.88, vector_dim=384, ttl_seconds=7200, index_name="l1_semantic_cache_idx"):
        self.redis = redis_client
        self.similarity_threshold = similarity_threshold
        self.vector_dim = vector_dim
        self.ttl_seconds = ttl_seconds
        self.index_name = index_name
        self._local_cache: List[Dict[str, Any]] = []

    async def init_index(self): pass

    async def get(self, query_embedding: List[float], top_k: int = 1) -> Optional[Dict[str, Any]]:
        best_match = None
        best_sim = -1.0
        now = time.time()
        for item in self._local_cache:
            if now > item["expires_at"]: continue
            v = item["vector"]
            dot = sum(a * b for a, b in zip(query_embedding, v))
            norm_a = math.sqrt(sum(a * a for a in query_embedding))
            norm_b = math.sqrt(sum(b * b for b in v))
            sim = dot / (norm_a * norm_b + 1e-9)
            if sim > best_sim:
                best_sim = sim
                best_match = item
        if best_match and best_sim >= self.similarity_threshold:
            return {"response": best_match["response"], "similarity": best_sim, "matched_prompt": best_match["prompt"]}
        return None

    async def set(self, prompt: str, query_embedding: List[float], response: Dict[str, Any], ttl=None):
        self._local_cache.append({
            "prompt": prompt,
            "vector": list(query_embedding),
            "response": response,
            "expires_at": time.time() + (ttl or self.ttl_seconds)
        })
