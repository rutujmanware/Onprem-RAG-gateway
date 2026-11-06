"""Easy/hard router. Training and serving import the SAME feature function.

Model file contract (joblib dict):
  {"model": sklearn estimator with predict_proba, "alpha": float,
   "features": "emb+cheap", "embed_dim": int, "hard_label": 1}
Label 1 = hard -> 7B ; label 0 = easy -> SLM.
"""
import os, re
import numpy as np
from . import config as C

_Q_WORDS = ("why", "how", "compare", "contrast", "explain", "derive", "justify", "evaluate", "limitation", "assumption", "relationship", "trade")

def cheap_features(q: str) -> np.ndarray:
    ql = q.lower()
    toks = re.findall(r"\w+", ql)
    return np.array([
        min(len(toks), 200) / 200.0,
        min(len(q), 1000) / 1000.0,
        float(sum(w in ql for w in _Q_WORDS)),
        float(len(re.findall(r"\d", q)) > 0),
        float(q.count("?")),
        float(len(re.findall(r"\b(and|or|versus|vs|between)\b", ql))),
        float(ql.startswith(("who", "when", "where", "what is", "which"))),
    ], dtype=np.float32)

def featurize(q: str, emb: np.ndarray) -> np.ndarray:
    return np.concatenate([emb, cheap_features(q)]).astype(np.float32)

class Router:
    def __init__(self, path: str | None = None):
        self.path = path or C.ROUTER_PATH
        self.model, self.alpha, self.loaded = None, C.ALPHA, False
        if os.path.exists(self.path):
            import joblib
            blob = joblib.load(self.path)
            self.model = blob["model"]
            self.loaded = True
            if not C.ALPHA_FROM_ENV:
                self.alpha = float(blob.get("alpha", C.ALPHA))

    def score(self, q: str, emb: np.ndarray) -> float:
        """P(hard) in [0,1]. Without a trained model, falls back to a crude heuristic (logged as such)."""
        if self.model is None:
            f = cheap_features(q)
            return float(np.clip(0.15 + 0.5 * f[0] + 0.12 * f[2] + 0.1 * f[5], 0, 1))
        return float(self.model.predict_proba(featurize(q, emb).reshape(1, -1))[0, 1])

    def route(self, q: str, emb: np.ndarray, force: str | None = None) -> dict:
        s = self.score(q, emb)
        if force in ("slm", "llm"):
            return {"s": s, "route": force, "reason": "forced"}
        return {"s": s, "route": "llm" if s >= self.alpha else "slm",
                "reason": "mlp" if self.loaded else "heuristic-fallback(no model file)"}
