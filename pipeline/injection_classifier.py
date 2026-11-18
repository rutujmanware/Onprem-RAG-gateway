"""Learned prompt-injection detector (DEFECT-32 / NFR-7), used together with the patterns in sanitizer.py.

Logistic regression on the sentence embedding the gateway already computes (all-MiniLM-L6-v2, 384-d,
L2-normalised). The weights are a small JSON file trained by `scripts/eval_injection_fence.py train-classifier`
on the round-1 suite only and evaluated on the separately generated round-2 suite. Without the embedding model
(e.g. a fresh clone before `make download-models`) the classifier is disabled and only the patterns apply.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

WEIGHTS_PATH = Path(__file__).resolve().parent / "data" / "injection_classifier.json"
_lock = threading.Lock()
_state = {"loaded": False, "w": None, "b": 0.0, "threshold": 1.0, "embedder": None}


def _load() -> bool:
    if _state["loaded"]:
        return _state["w"] is not None
    with _lock:
        if _state["loaded"]:
            return _state["w"] is not None
        _state["loaded"] = True
        if os.getenv("INJECTION_CLASSIFIER", "1") == "0" or not WEIGHTS_PATH.exists():
            return False
        try:
            data = json.loads(WEIGHTS_PATH.read_text())
            from pipeline.embeddings.embedder import LocalEmbedder
            emb = LocalEmbedder()
            if emb._model is None and getattr(emb, "_st_model", None) is None:
                logger.warning("Injection classifier disabled: embedding model not available")
                return False
            _state.update(w=[float(x) for x in data["weights"]], b=float(data["bias"]),
                          threshold=float(data["threshold"]), embedder=emb)
        except Exception as exc:  # never let the classifier break the request path
            logger.warning("Injection classifier disabled: %s", exc)
            _state["w"] = None
        return _state["w"] is not None


def score_vector(vec: List[float]) -> float:
    z = _state["b"] + sum(a * b for a, b in zip(_state["w"], vec))
    return 1.0 / (1.0 + math.exp(-z))


def injection_score(texts: List[str]) -> Optional[float]:
    """Highest injection probability over the given views of a message; None if the classifier is off."""
    if not texts or not _load():
        return None
    try:
        vecs = _state["embedder"].embed_documents([t[:2000] for t in texts])
    except Exception as exc:
        logger.warning("Injection classifier skipped: %s", exc)
        return None
    return max(score_vector(v) for v in vecs)


def is_injection(texts: List[str]) -> bool:
    s = injection_score(texts)
    return s is not None and s >= _state["threshold"]
