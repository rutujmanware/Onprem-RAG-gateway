"""DEFECT-26: a slow request must not block the gateway worker's event loop.

Reported: /query ran the synchronous rag.answer_query(...) directly inside the async handler for
hard-routed requests, so while one request was slow (GPU tier down) /health and every other request on
that worker waited (43 s). The blocking work now runs in the thread pool. Here the fallback is made
deliberately slow and blocking (time.sleep) and /health is called on the same event loop meanwhile.
No services needed.
"""

import asyncio
import inspect
import re
import threading
import time
from contextlib import asynccontextmanager

import httpx
import pytest

import gateway.main as gm
from gateway.dependencies import (get_admission_controller, get_complexity_scorer, get_dispatcher,
                                  get_semantic_cache, get_vllm_client)
from gateway.router.dispatcher import ModelTier, RouteDecision

SLOW = 1.5
STARTED = threading.Event()
CHUNKS = [{"doc_id": f"d{i}", "title": f"Thesis {i}", "content": f"content {i}", "similarity": 0.8} for i in range(8)]


class Cache:
    async def alookup(self, *a, **k):
        return None

    async def astore(self, *a, **k):
        return False


class VLLMDown:
    async def check_health(self):
        return False


class Scorer:
    async def score(self, q):
        return 0.95


class Dispatcher:
    def decide(self, score, **k):
        return RouteDecision(target_tier=ModelTier.GPU_LLM, score=score, alpha=0.5, target_model="m", reason="t")


class Admission:
    @asynccontextmanager
    async def slot(self, *a, **k):
        yield


class SlowPipeline:
    """Stands in for RAGQAPipeline: a slow, *blocking* CPU fallback."""
    def __init__(self, *a, **k):
        pass

    def answer_query(self, query, **k):
        STARTED.set()
        time.sleep(SLOW)
        return {"query": query, "answer": "CPU tier answer.", "route": "hard", "target_tier": "cpu_slm",
                "fallback": "gpu_unavailable", "chunks": CHUNKS[:3]}


@pytest.fixture
def app(monkeypatch):
    import pipeline.retriever as pr

    async def fake_asearch(self, query, top_k=None, **k):
        return list(CHUNKS)

    monkeypatch.setattr(pr.RedisChunkRetriever, "asearch", fake_asearch)
    from pipeline.text_processing.typo_normalizer import TypoNormalizer
    TypoNormalizer.get_instance().normalize("warm up")  # one-time dictionary load is not under test
    STARTED.clear()
    monkeypatch.setattr(gm, "RAGQAPipeline", SlowPipeline)
    over = gm.app.dependency_overrides
    saved = dict(over)
    over.update({get_semantic_cache: Cache, get_vllm_client: VLLMDown, get_complexity_scorer: Scorer,
                 get_dispatcher: Dispatcher, get_admission_controller: Admission})
    yield gm.app
    over.clear()
    over.update(saved)


def test_health_stays_responsive_while_a_hard_request_falls_back_slowly(app):
    async def main():
        lags, stop = [], asyncio.Event()

        async def ticker():  # measures how long the event loop is unavailable
            last = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                lags.append(now - last)
                last = now

        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
            tick = asyncio.create_task(ticker())
            q = asyncio.create_task(c.post("/query", json={"query": "Explain in depth how attention aggregation "
                                                           "compares with GCN baselines", "stream": False}))
            while not STARTED.is_set():         # wait until the request is inside the slow fallback
                await asyncio.sleep(0.01)
            t0 = time.perf_counter()
            h = await c.get("/health")
            health_s = time.perf_counter() - t0
            r = await q
            stop.set()
            await tick
            return h.status_code, health_s, max(lags), r
    status, health_s, max_lag, r = asyncio.run(main())
    assert max_lag < 0.5, f"event loop blocked for {max_lag:.2f}s by one request"
    assert status == 200 and health_s < 0.5, f"/health waited {health_s:.2f}s behind a blocking request"
    assert r.status_code == 200 and r.json()["fallback"] == "gpu_unavailable"


def test_no_blocking_model_calls_directly_in_async_handlers():
    """Model/pipeline calls inside the async route handlers must go through the thread pool."""
    blocking = re.compile(r"\b(rag\.answer_query\(|engine\.generate_chat|engine\.answer_rag|"
                          r"\.answer_query\(|GPULLMEngine\.get_instance\(\)\.)")
    for name in ("query_endpoint", "chat_completions"):
        src = inspect.getsource(getattr(gm, name))
        for n, line in enumerate(src.splitlines(), 1):
            if blocking.search(line) and "run_in_threadpool" not in line:
                window = "\n".join(src.splitlines()[max(0, n - 4):n])
                assert "run_in_threadpool" in window, f"{name}: blocking call outside the thread pool: {line.strip()}"
