"""Learned prompt-injection detector (DEFECT-32 / NFR-7), used together with the patterns in sanitizer.py.

Logistic regression over sentence embeddings. Version 2 (round-4 work, 2026-11-19) concatenates two L2-normalised
384-d embeddings of the message:
  * all-MiniLM-L6-v2 (English; the gateway already loads it for retrieval), and
  * paraphrase-multilingual-MiniLM-L12-v2 (50+ languages, trained so that translations land close together), so an
    attack written in German or Japanese scores like its English equivalent.
The weights are a small JSON file trained by `scripts/eval_injection_fence.py train-classifier`; its "features"
field says which embeddings it expects. Without a required model (e.g. a fresh clone before
`make download-models`) the classifier is disabled and only the patterns apply.
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
MULTILINGUAL_DIR = (Path(__file__).resolve().parent.parent / "models" / "embeddings"
                    / "paraphrase-multilingual-MiniLM-L12-v2")
_lock = threading.Lock()
_state = {"loaded": False, "w": None, "b": 0.0, "threshold": 1.0, "encoders": None}


class _MultilingualEncoder:
    """paraphrase-multilingual-MiniLM-L12-v2 on CPU (mean pooling over at most 128 tokens, L2-normalised).

    ONNX Runtime when <model dir>/onnx/model.onnx exists (scripts/export_embedder_onnx.py, verified against
    PyTorch to cosine >= 0.99999; ~4x faster), else sentence-transformers / PyTorch."""

    MAX_LEN = 128   # the model's max_seq_length

    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self.session = None
        onnx_path = path / "onnx" / "model.onnx"
        if onnx_path.exists():
            import onnxruntime as ort
            from transformers import AutoTokenizer
            so = ort.SessionOptions()
            so.intra_op_num_threads = int(os.getenv("INJECTION_ENCODER_THREADS", "4"))
            self.session = ort.InferenceSession(str(onnx_path), so, providers=["CPUExecutionProvider"])
            self.tokenizer = AutoTokenizer.from_pretrained(str(path))
            self.inputs = [i.name for i in self.session.get_inputs()]
            return
        from sentence_transformers import SentenceTransformer
        try:
            import torch
            torch.set_num_threads(max(1, min(int(os.getenv("INJECTION_ENCODER_THREADS", "4")), torch.get_num_threads())))
        except Exception:
            pass
        self.model = SentenceTransformer(str(path), device="cpu")

    def embed(self, texts: List[str]) -> List[List[float]]:
        if self.session is not None:
            import numpy as np
            enc = self.tokenizer(texts, padding=True, truncation=True, max_length=self.MAX_LEN, return_tensors="np")
            hidden = self.session.run(None, {n: enc[n].astype(np.int64) for n in self.inputs})[0]
            m = enc["attention_mask"][..., None].astype(np.float32)
            v = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
            v = v / np.clip(np.linalg.norm(v, axis=1, keepdims=True), 1e-12, None)
            return [list(map(float, row)) for row in v]
        with self._lock:   # one PyTorch encode at a time per process; requests run it in worker threads
            vecs = self.model.encode(texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False)
        return [list(map(float, v)) for v in vecs]


def _english_encoder():
    from pipeline.embeddings.embedder import LocalEmbedder
    emb = LocalEmbedder()
    if emb._model is None and getattr(emb, "_st_model", None) is None:
        raise RuntimeError("embedding model not available")
    return emb.embed_documents


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
            features = data.get("features", ["minilm_en"])
            encoders = []
            for f in features:
                if f == "minilm_en":
                    encoders.append(_english_encoder())
                elif f == "multilingual":
                    if not MULTILINGUAL_DIR.exists():
                        raise RuntimeError(f"multilingual model missing at {MULTILINGUAL_DIR}")
                    encoders.append(_MultilingualEncoder(MULTILINGUAL_DIR).embed)
                else:
                    raise RuntimeError(f"unknown feature {f!r}")
            _state.update(w=[float(x) for x in data["weights"]], b=float(data["bias"]),
                          threshold=float(data["threshold"]), encoders=encoders)
        except Exception as exc:  # never let the classifier break the request path
            logger.warning("Injection classifier disabled: %s", exc)
            _state["w"] = None
        return _state["w"] is not None


def features(texts: List[str], encoders) -> List[List[float]]:
    """Concatenated embeddings, one row per text (same order as training)."""
    parts = [enc(texts) for enc in encoders]
    return [sum((p[i] for p in parts), []) for i in range(len(texts))]


def score_vector(vec: List[float]) -> float:
    z = _state["b"] + sum(a * b for a, b in zip(_state["w"], vec))
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))


def injection_score(texts: List[str]) -> Optional[float]:
    """Highest injection probability over the given views of a message; None if the classifier is off."""
    if not texts or not _load():
        return None
    try:
        vecs = features([t[:2000] for t in texts], _state["encoders"])
    except Exception as exc:
        logger.warning("Injection classifier skipped: %s", exc)
        return None
    return max(score_vector(v) for v in vecs)


def is_injection(texts: List[str]) -> bool:
    s = injection_score(texts)
    return s is not None and s >= _state["threshold"]
