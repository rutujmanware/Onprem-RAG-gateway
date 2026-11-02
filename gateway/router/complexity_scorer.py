import logging
import math
import os
import re
from typing import List, Optional

try:
    import numpy as np
except ImportError:
    np = None

try:
    import onnxruntime as ort
except ImportError:
    ort = None

logger = logging.getLogger(__name__)

COMPLEX_KEYWORDS = re.compile(
    r"\b(compare|contrast|synthesize|difference|versus|vs|why|derive|"
    r"exception|waiver|eligibility|multi|across|both|implications)\b",
    re.IGNORECASE,
)

def extract_query_scalars(query: str):
    """Extract normalized scalar features matching MLP training (dim=6)."""
    q = query or ""
    words = q.split()
    num_words = len(words)
    num_chars = len(q)
    feats = [
        min(num_words, 60) / 60.0,
        min(num_chars, 400) / 400.0,
        (num_chars / max(num_words, 1)) / 10.0,
        min(q.count(",") + q.count(";") + q.count("?"), 5) / 5.0,
        1.0 if COMPLEX_KEYWORDS.search(q) else 0.0,
        1.0 if any(c.isdigit() for c in q) else 0.0,
    ]
    if np is not None:
        return np.array(feats, dtype=np.float32)
    return feats


class ComplexityScorer:
    def __init__(self, model_path: Optional[str] = None, embedder=None):
        self.model_path = model_path or os.getenv("COMPLEXITY_SCORER_PATH", "gateway/router/models/scorer.onnx")
        self.embedder = embedder
        self.session = None
        self.input_name = None
        self._init_session()

    def _init_session(self):
        if ort and os.path.exists(self.model_path):
            try:
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = 1
                opts.inter_op_num_threads = 1
                self.session = ort.InferenceSession(self.model_path, sess_options=opts, providers=["CPUExecutionProvider"])
                self.input_name = self.session.get_inputs()[0].name
                logger.info("Loaded ONNX router model from %s", self.model_path)
            except Exception as e:
                logger.warning("Could not initialize ONNX session for %s (%s). Using heuristic fallback.", self.model_path, e)
                self.session = None
        else:
            logger.info("ONNX router model '%s' not found. Using heuristic fallback.", self.model_path)

    async def score(self, query: str, embedding: Optional[List[float]] = None) -> float:
        """
        Calculates a complexity score in [0.01, 0.99] for routing.
        Uses ONNX MLP model (384 emb + 6 scalars) if available; falls back to heuristic.
        """
        # If embedding not provided but an embedder is available, compute it
        if embedding is None and self.embedder is not None:
            try:
                embedding = self.embedder.embed_query(query)
            except Exception as e:
                logger.debug("Failed to embed query on the fly: %s", e)

        # 1. Try ONNX MLP inference
        if self.session is not None and embedding is not None and np is not None:
            try:
                emb_arr = np.asarray(embedding, dtype=np.float32).reshape(-1)
                scalars = extract_query_scalars(query)
                features = np.concatenate([emb_arr, scalars]).reshape(1, -1)
                outputs = self.session.run(None, {self.input_name: features})
                score = float(np.squeeze(outputs[0]))
                return max(0.01, min(0.99, score))
            except Exception as e:
                logger.warning("ONNX scoring failed (%s). Falling back to heuristic.", e)

        # 2. Heuristic fallback
        return self._heuristic_score(query, embedding)

    def _heuristic_score(self, query: str, embedding: Optional[List[float]] = None) -> float:
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
