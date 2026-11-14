"""ONNX Runtime embedder (NFR-1): must give the same vectors as PyTorch, because the document index,
the semantic cache and the router were built with PyTorch vectors."""

import os

import numpy as np
import pytest

import pipeline.embeddings.embedder as emb

ONNX = os.path.join(os.path.abspath("models/embeddings/all-MiniLM-L6-v2"), "onnx", "model.onnx")
pytestmark = pytest.mark.skipif(not os.path.exists(ONNX), reason="run scripts/export_embedder_onnx.py first")

QS = ["What is graphene?", "explain transformers", "Are mycorrhizal fungi always essential for plant nutrition?",
      "GMRES vs MINRES (2019) [Document 3]", "x", "long " * 700]


def _make(monkeypatch, backend):
    monkeypatch.setenv("EMBED_BACKEND", backend)
    return emb.LocalEmbedder()


def test_auto_uses_onnx_when_exported(monkeypatch):
    assert _make(monkeypatch, "auto").backend == "onnx"


def test_torch_can_be_forced(monkeypatch):
    assert _make(monkeypatch, "torch").backend == "torch"


def test_onnx_and_torch_vectors_match(monkeypatch):
    o, t = _make(monkeypatch, "onnx"), _make(monkeypatch, "torch")
    a, b = np.array([o.embed_query(q) for q in QS]), np.array([t.embed_query(q) for q in QS])
    assert a.shape == (len(QS), 384)
    assert float((a * b).sum(1).min()) >= 0.99999
    da, db = np.array(o.embed_documents(QS)), np.array(t.embed_documents(QS))
    assert float((da * db).sum(1).min()) >= 0.99999


def test_onnx_vectors_are_unit_length(monkeypatch):
    v = np.array(_make(monkeypatch, "onnx").embed_query("unit length check"))
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-5


def test_onnx_session_shared_and_thread_safe(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    a, b = _make(monkeypatch, "onnx"), _make(monkeypatch, "onnx")
    assert a._ort is b._ort
    with ThreadPoolExecutor(16) as ex:
        out = list(ex.map(a.embed_query, QS * 10))
    assert out[0] == a.embed_query(QS[0])


def test_forced_onnx_without_model_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setenv("EMBED_BACKEND", "onnx")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")  # never download in tests
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.chdir(tmp_path)  # no models/ here -> remote name, no onnx file
    with pytest.raises(RuntimeError, match="EMBED_BACKEND=onnx"):
        emb.LocalEmbedder(model_name_or_path="sentence-transformers/all-MiniLM-L6-v2")
