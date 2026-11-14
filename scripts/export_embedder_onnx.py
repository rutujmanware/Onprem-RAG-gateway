#!/usr/bin/env python3
"""Export the MiniLM query/document embedder to ONNX and verify it matches PyTorch.

The document index (2.26 M chunks), the semantic cache and the router were all built with the PyTorch
model, so the ONNX model must produce the same vectors: the script refuses to finish unless every test
sentence agrees with PyTorch to cosine >= 0.99999 after mean pooling + L2 normalisation (fp32, no
quantisation, which would change the vectors).

Output: models/embeddings/all-MiniLM-L6-v2/onnx/model.onnx (gitignored with the rest of models/embeddings).
Usage:  python scripts/export_embedder_onnx.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "models" / "embeddings" / "all-MiniLM-L6-v2"
OUT = MODEL_DIR / "onnx" / "model.onnx"
MIN_COSINE = 0.99999

SENTENCES = [
    "What is graphene?", "explain transformers", "Are bacterial plasmids always essential for bacterial survival?",
    "How does the flare angle affect flame stability in high-shear injectors?",
    "Why does the power method always find the largest eigenvalue?", "x",
    "What happens to pH as NaOH is added to HCl in an acid-base titration?",
    "Experimental and theoretical studies of strongly correlated multiferroic oxides such as LuMnO3 " * 8,
    "पर्यावरण", "GMRES vs MINRES for non-symmetric systems; Krylov subspace methods (2019) [Document 3].",
]


def mean_pool(hidden: np.ndarray, mask: np.ndarray) -> np.ndarray:
    m = mask[..., None].astype(np.float32)
    v = (hidden * m).sum(1) / np.clip(m.sum(1), 1e-9, None)
    return v / np.clip(np.linalg.norm(v, axis=1, keepdims=True), 1e-12, None)


def main() -> int:
    tok = AutoTokenizer.from_pretrained(str(MODEL_DIR))
    model = AutoModel.from_pretrained(str(MODEL_DIR)).eval()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    enc = tok(["dummy input text", "a second, longer dummy input text"], padding=True, return_tensors="pt")
    names = ["input_ids", "attention_mask", "token_type_ids"]
    with torch.no_grad():
        torch.onnx.export(
            model, tuple(enc[n] for n in names), str(OUT), input_names=names, output_names=["last_hidden_state"],
            dynamic_axes={**{n: {0: "batch", 1: "seq"} for n in names}, "last_hidden_state": {0: "batch", 1: "seq"}},
            opset_version=17, do_constant_folding=True)
    print(f"exported -> {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")

    import onnxruntime as ort
    sess = ort.InferenceSession(str(OUT), providers=["CPUExecutionProvider"])
    worst = 1.0
    for batch in ([s] for s in SENTENCES):  # single queries, as served
        e = tok(batch, padding=True, truncation=True, max_length=512, return_tensors="np")
        onnx_vec = mean_pool(sess.run(None, {n: e[n].astype(np.int64) for n in names})[0], e["attention_mask"])
        with torch.no_grad():
            te = tok(batch, padding=True, truncation=True, max_length=512, return_tensors="pt")
            torch_vec = mean_pool(model(**te)[0].numpy(), te["attention_mask"].numpy())
        worst = min(worst, float((onnx_vec * torch_vec).sum(1).min()))
    e = tok(SENTENCES, padding=True, truncation=True, max_length=512, return_tensors="np")  # padded batch
    onnx_b = mean_pool(sess.run(None, {n: e[n].astype(np.int64) for n in names})[0], e["attention_mask"])
    with torch.no_grad():
        te = tok(SENTENCES, padding=True, truncation=True, max_length=512, return_tensors="pt")
        torch_b = mean_pool(model(**te)[0].numpy(), te["attention_mask"].numpy())
    worst = min(worst, float((onnx_b * torch_b).sum(1).min()))
    print(f"worst cosine ONNX vs PyTorch over {len(SENTENCES)} sentences (single + padded batch): {worst:.7f}")
    if worst < MIN_COSINE:
        OUT.unlink()
        print(f"FAILED: below {MIN_COSINE}; ONNX model removed")
        return 1
    print("OK: vectors match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
