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
    def __init__(self, model_path=None):
        self.model_path = model_path or os.getenv("COMPLEXITY_SCORER_PATH", "gateway/router/models/scorer.onnx")

    async def score(self, query: str, embedding=None) -> float:
        lower = query.lower()
        score = 0.25
        words = query.strip().split()
        if len(words) > 30: score += 0.25
        elif len(words) > 15: score += 0.15
        hard_kw = ["compare", "contrast", "derive", "proof", "architecture", "tradeoff", "algorithm", "implement", "distributed", "optimize"]
        score += min(sum(0.12 for kw in hard_kw if kw in lower), 0.40)
        simple_kw = ["what is", "where is", "when was", "who is", "define", "contact"]
        if any(lower.startswith(skw) for skw in simple_kw): score -= 0.15
        if embedding:
            mean = sum(embedding) / max(1, len(embedding))
            std = math.sqrt(sum((x - mean) ** 2 for x in embedding) / max(1, len(embedding)))
            score += min(std * 1.5, 0.15)
        return max(0.01, min(0.99, score))
