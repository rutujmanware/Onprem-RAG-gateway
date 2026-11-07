import logging
import math
import os
from typing import List, Optional

try:
    import numpy as np
except ImportError:
    np = None

logger = logging.getLogger(__name__)

class ComplexityScorer:
    """
    Gateway Complexity Scorer:
    Evaluates incoming user query text (single-query input) and produces
    a normalized routing score between 0.01 and 0.99.
    
    Priority 1: Learned Two-Tower Hybrid Neural RouterMLP (< 10ms on GPU/CPU)
    Priority 2: Fast rule-based lexical & embedding heuristic fallback (< 0.1ms)
    """
    def __init__(self, model_path=None):
        self.router = None
        try:
            from gateway.router.mlp_router.inference import QueryRouter
            self.router = QueryRouter(model_path=model_path)
            logger.info("ComplexityScorer: Loaded neural Two-Tower QueryRouter.")
        except Exception as e:
            logger.warning(f"ComplexityScorer: Neural router not loaded ({e}), falling back to heuristic scorer.")

    async def score(self, query: str, embedding=None) -> float:
        # Priority 0: Dedicated Router Microservice on Port 8090 (if running)
        router_url = os.getenv("ROUTER_BASE_URL", "http://127.0.0.1:8090").rstrip("/")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=0.8) as client:
                r = await client.post(f"{router_url}/score", json={"query": query})
                if r.status_code == 200:
                    data = r.json()
                    return max(0.01, min(0.99, float(data["score"])))
        except Exception:
            pass

        # Priority 1: In-Process Neural MLP Router (< 5ms)
        if self.router is not None:
            try:
                res = self.router.predict(query)
                return max(0.01, min(0.99, float(res["probability_hard"])))
            except Exception as e:
                logger.error(f"Error during neural router scoring: {e}. Using fallback.")

        # Priority 2: Heuristic Scorer Fallback
        lower = query.lower()
        score = 0.25
        words = query.strip().split()
        if len(words) > 30:
            score += 0.25
        elif len(words) > 15:
            score += 0.15

        hard_kw = ["compare", "contrast", "derive", "proof", "architecture", "tradeoff", "algorithm", "implement", "distributed", "optimize"]
        score += min(sum(0.12 for kw in hard_kw if kw in lower), 0.40)

        simple_kw = ["what is", "where is", "when was", "who is", "define", "contact"]
        if any(lower.startswith(skw) for skw in simple_kw):
            score -= 0.15

        if embedding:
            mean = sum(embedding) / max(1, len(embedding))
            std = math.sqrt(sum((x - mean) ** 2 for x in embedding) / max(1, len(embedding)))
            score += min(std * 1.5, 0.15)

        return max(0.01, min(0.99, score))
