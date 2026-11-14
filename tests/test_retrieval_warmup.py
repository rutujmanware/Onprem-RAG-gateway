"""Retrieval speed: models are loaded once per process and at gateway startup, not per request.

The gateway builds a RAGQAPipeline for every fresh request; that used to reload the router and the
embedding model (~110 ms per request), and the spell dictionaries loaded on the first topic question
of each worker (~2.5 s).
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pipeline.embeddings.embedder import LocalEmbedder


def test_embedders_share_one_loaded_model():
    a, b = LocalEmbedder(), LocalEmbedder()
    if a._model is None:
        pytest.skip("embedding model not available")
    assert a._model is b._model and a._tokenizer is b._tokenizer and a._tok_lock is b._tok_lock


def test_shared_embedder_is_safe_under_concurrency():
    e = LocalEmbedder()
    if e._model is None:
        pytest.skip("embedding model not available")
    texts = [f"question number {i} about graphene and transformers" for i in range(64)]
    with ThreadPoolExecutor(16) as ex:
        vecs = list(ex.map(e.embed_query, texts))
    assert len(vecs) == 64 and all(len(v) == 384 for v in vecs)
    assert vecs[3] == e.embed_query(texts[3])


def test_pipeline_reuses_router_and_retriever():
    from pipeline.rag_chain import RAGQAPipeline
    p1, p2 = RAGQAPipeline(), RAGQAPipeline()
    assert p1.retriever is p2.retriever
    assert p1.router is p2.router


def test_custom_retriever_params_get_their_own_instance():
    from pipeline.rag_chain import RAGQAPipeline
    assert RAGQAPipeline(min_similarity=0.6).retriever is not RAGQAPipeline().retriever
    assert RAGQAPipeline(min_similarity=0.6).retriever is RAGQAPipeline(min_similarity=0.6).retriever


def test_explicit_router_is_respected():
    from pipeline.rag_chain import RAGQAPipeline
    stub = object()
    assert RAGQAPipeline(router=stub).router is stub


def test_new_pipeline_is_cheap_after_warm_up():
    from pipeline.rag_chain import RAGQAPipeline, warm_up
    try:
        warm_up()
    except Exception as exc:
        pytest.skip(f"retrieval stack unavailable: {exc}")
    t0 = time.perf_counter()
    for _ in range(5):
        RAGQAPipeline().retriever
    assert (time.perf_counter() - t0) / 5 < 0.02, "building a pipeline must not reload models"


def test_gateway_warms_up_at_startup():
    src = (Path(__file__).resolve().parent.parent / "gateway" / "main.py").read_text()
    life = src[src.index("async def lifespan"):src.index("ENABLE_API_DOCS")]
    assert "from pipeline.rag_chain import warm_up" in life and "await run_in_threadpool(warm_up)" in life
