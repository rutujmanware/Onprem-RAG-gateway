"""FR-2 / DEFECT-25: hard questions are answered by the CPU tier when the GPU tier cannot answer.

Reported: with vLLM down, a hard /query returned HTTP 500 because the chain fell back to loading the 7B
model inside the gateway process (no weights / no internet). Now a GPU tier that is down, times out or
fails before the first token reroutes to the CPU SLM (daemon, else in-process 3B); the in-process 7B is
opt-in only (GPU_INPROCESS_FALLBACK=1) and also reroutes if it fails. A request whose GPU attempt already
failed in the gateway is not sent to the GPU a second time (skip_gpu).
No services needed: vLLM, the SLM daemon and the models are faked.
"""

from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi.testclient import TestClient

import pipeline.rag_chain as rc
from pipeline.rag_chain import RAGQAPipeline
from pipeline.sanitizer import REFUSAL_SENTENCE

CHUNKS = [{"doc_id": f"d{i}", "chunk_index": 0, "title": f"Thesis {i}", "content": f"content {i}",
           "similarity": 0.8 - i * 0.01} for i in range(8)]
HARD = {"route": "hard", "target_tier": "gpu_vllm", "probability_hard": 0.9}
Q = "Explain in depth how attention based aggregation in graph neural networks compares with GCN baselines."


class FailIfUsedGPU:
    """In-process 7B engine: must not be loaded unless GPU_INPROCESS_FALLBACK=1."""
    def __init__(self, fail=False):
        self.calls, self.fail = 0, fail

    def _use(self):
        self.calls += 1
        if self.fail:
            raise OSError("We couldn't connect to 'https://huggingface.co' to load the files")

    def answer_rag(self, **k):
        self._use()
        return {"text": "in-process 7B answer about attention aggregation", "num_tokens": 7}

    def generate_chat_stream(self, *a, **k):
        self._use()
        yield "in-process 7B answer"


class FakeSLM:
    model_name = "fake-3b"

    def __init__(self):
        self.calls = []

    def answer_rag(self, question, context_chunks, **k):
        self.calls.append(len(context_chunks))
        return {"text": "CPU tier answer about attention aggregation in GNNs.", "num_tokens": 9}

    def answer_rag_stream(self, question, context_chunks, **k):
        self.calls.append(len(context_chunks))
        yield from ["CPU tier ", "answer."]


@pytest.fixture
def net(monkeypatch):
    """Fake network: GPU health/POST behaviour configurable; the SLM daemon is down by default."""
    # slm_script: answers the CPU daemon gives, one per call (default: a normal answer)
    state = {"gpu_health": False, "gpu_posts": 0, "gpu_streams": 0, "slm_up": False, "slm_calls": 0,
             "slm_script": [], "slm_contexts": []}

    def slm_answer(payload):
        state["slm_calls"] += 1
        state["slm_contexts"].append(payload["messages"][-1]["content"])
        return state["slm_script"].pop(0) if state["slm_script"] else "CPU daemon answer about attention aggregation."

    def fake_get(url, *a, **k):
        if ":8000" in url and state["gpu_health"]:
            return httpx.Response(200, request=httpx.Request("GET", url))
        if ":8082" in url and state["slm_up"]:
            return httpx.Response(200, request=httpx.Request("GET", url))
        raise httpx.ConnectError("connection refused")

    def fake_post(url, *a, **k):
        state["gpu_posts"] += 1
        raise httpx.ReadTimeout("GPU timed out")

    def fake_complete(url, payload, headers=None, timeout=None):
        if ":8000" in url:
            state["gpu_posts"] += 1
            raise httpx.ReadTimeout("GPU timed out")
        return slm_answer(payload), 6, "qwen2.5-3b-instruct-q4_k_m.gguf"

    def fake_stream(url, payload, headers=None, timeout=None):
        if ":8000" in url:
            state["gpu_streams"] += 1
            raise httpx.ConnectError("GPU refused")
            yield  # pragma: no cover
        text = slm_answer(payload)
        yield from ([text] if text == REFUSAL_SENTENCE else [text[:10], text[10:]])

    monkeypatch.setenv("VLLM_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setenv("SLM_BASE_URL", "http://localhost:8082/v1")
    monkeypatch.delenv("GPU_INPROCESS_FALLBACK", raising=False)
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(rc, "_complete_chat_http", fake_complete)
    monkeypatch.setattr(rc, "_stream_chat_http", fake_stream)
    return state


def pipeline(gpu=None, slm=None):
    return RAGQAPipeline(retriever=object(), router=object(), slm_engine=slm or FakeSLM(), gpu_engine=gpu or FailIfUsedGPU())


def assert_rerouted(res):
    assert res["route"] == "hard" and res["target_tier"] == "cpu_slm" and res["fallback"] == "gpu_unavailable"
    assert "CPU" in res["answer"] or "passages" in res["answer"]
    assert 0 < len(res["chunks"]) <= 3, "CPU-tier context size"


# ----------------------------------------------------------------------------------- non-streaming
def test_gpu_down_is_answered_by_the_cpu_tier_not_an_error(net):
    gpu, slm = FailIfUsedGPU(), FakeSLM()
    res = pipeline(gpu, slm).answer_query(Q, route_decision=HARD, chunks=list(CHUNKS), max_new_tokens=2048)
    assert_rerouted(res)
    assert gpu.calls == 0, "the 7B model must not be loaded inside the gateway"
    assert slm.calls == [3]


def test_gpu_down_uses_the_cpu_daemon_when_it_is_up(net):
    net["slm_up"] = True
    res = pipeline().answer_query(Q, route_decision=HARD, chunks=list(CHUNKS))
    assert_rerouted(res)
    assert net["slm_calls"] == 1 and res["stats"]["server"] == "cpu-daemon-port-8082"
    assert net["gpu_posts"] == 0, "no GPU overflow attempt for a request the GPU tier failed"


def test_gpu_timeout_is_rerouted_after_a_single_gpu_attempt(net):
    net["gpu_health"], net["slm_up"] = True, True
    res = pipeline().answer_query(Q, route_decision=HARD, chunks=list(CHUNKS))
    assert_rerouted(res)
    assert net["gpu_posts"] == 1


def test_skip_gpu_goes_straight_to_the_cpu_tier(net):
    net["gpu_health"], net["slm_up"] = True, True
    res = pipeline().answer_query(Q, route_decision={**HARD, "skip_gpu": True}, chunks=list(CHUNKS))
    assert_rerouted(res)
    assert net["gpu_posts"] == 0


def test_opt_in_inprocess_gpu_that_fails_still_reroutes(net, monkeypatch):
    monkeypatch.setenv("GPU_INPROCESS_FALLBACK", "1")
    gpu = FailIfUsedGPU(fail=True)
    res = pipeline(gpu).answer_query(Q, route_decision=HARD, chunks=list(CHUNKS))
    assert gpu.calls == 1
    assert_rerouted(res)


def test_opt_in_inprocess_gpu_answers_when_it_works(net, monkeypatch):
    monkeypatch.setenv("GPU_INPROCESS_FALLBACK", "1")
    res = pipeline(FailIfUsedGPU()).answer_query(Q, route_decision=HARD, chunks=list(CHUNKS))
    assert res["target_tier"] == "gpu_vllm" and "in-process 7B" in res["answer"]


# --------------------------------------------------------------------------------------- streaming
def _events(gen):
    return list(gen)


@pytest.mark.parametrize("gpu_health", [False, True])   # down, or up but failing before the first token
def test_streaming_gpu_failure_is_answered_by_the_cpu_tier(net, gpu_health):
    net["gpu_health"], net["slm_up"] = gpu_health, True
    gpu = FailIfUsedGPU()
    ev = _events(pipeline(gpu).answer_query_stream(Q, route_decision=HARD, chunks=list(CHUNKS)))
    routes = [e for e in ev if e["type"] == "route"]
    assert routes[-1]["target_tier"] == "cpu_slm" and routes[-1]["fallback"] == "gpu_unavailable"
    assert routes[-1]["route"] == "hard"
    done = ev[-1]
    assert done["type"] == "done" and "CPU daemon" in done["full_answer"] and not done.get("incomplete")
    assert 0 < len(done["chunks"]) <= 3
    last_chunks = [e for e in ev if e["type"] == "chunks"][-1]["chunks"]
    assert len(last_chunks) <= 3, "sources shown match the CPU-tier context"
    assert gpu.calls == 0
    assert net["gpu_streams"] == (1 if gpu_health else 0)


def test_streaming_gpu_down_and_cpu_daemon_down_uses_inprocess_3b(net):
    slm = FakeSLM()
    ev = _events(pipeline(slm=slm).answer_query_stream(Q, route_decision=HARD, chunks=list(CHUNKS)))
    assert ev[-1]["type"] == "done" and ev[-1]["full_answer"] == "CPU tier answer."
    assert slm.calls == [3]


# ------------------------------------------------------------------- route level: /v1/chat/completions
def test_chat_completions_hard_route_with_gpu_down_returns_200_from_cpu(monkeypatch):
    import gateway.main as gm
    from gateway.dependencies import (get_admission_controller, get_complexity_scorer, get_dispatcher,
                                      get_semantic_cache, get_slm_client, get_vllm_client)
    from gateway.router.dispatcher import ModelTier, RouteDecision

    class Cache:
        async def alookup(self, *a, **k):
            return None

        async def astore(self, *a, **k):
            return False

    class Down:
        async def check_health(self):
            return False

    class SLM:
        calls = 0

        async def check_health(self):
            return True

        async def generate_chat(self, messages, **k):
            SLM.calls += 1
            return {"choices": [{"index": 0, "message": {"role": "assistant", "content": "CPU answer"}}]}

    class Scorer:
        async def score(self, q):
            return 0.95

    class Disp:
        def decide(self, score, **k):
            return RouteDecision(target_tier=ModelTier.GPU_LLM, score=score, alpha=0.5, target_model="m", reason="t")

    class Adm:
        @asynccontextmanager
        async def slot(self, *a, **k):
            yield

    def boom():
        raise AssertionError("the in-process 7B engine must not be loaded")

    monkeypatch.delenv("GPU_INPROCESS_FALLBACK", raising=False)
    import pipeline.gpu_llm_runner as glr
    monkeypatch.setattr(glr.GPULLMEngine, "get_instance", staticmethod(boom))
    over = gm.app.dependency_overrides
    saved = dict(over)
    over.update({get_semantic_cache: Cache, get_vllm_client: Down, get_slm_client: SLM,
                 get_complexity_scorer: Scorer, get_dispatcher: Disp, get_admission_controller: Adm})
    try:
        r = TestClient(gm.app).post("/v1/chat/completions", json={"messages": [{"role": "user", "content": Q}]})
    finally:
        over.clear()
        over.update(saved)
    assert r.status_code == 200, r.text
    meta = r.json()["router_meta"]
    assert meta["route"] == "hard" and meta["tier"] == "cpu_slm" and meta["fallback"] == "gpu_unavailable"
    assert SLM.calls == 1


# ------------------------------------------- 3B refuses on-topic documents while the 7B is unavailable
RETRY_ANSWER = "From the next passages: attention aggregation lowers the error."


def _docs(chunks):
    return [c["title"].replace("Thesis ", "d") for c in chunks]  # served sources carry no doc_id


def test_refusal_is_retried_once_with_the_next_passages(net):
    net["slm_up"], net["slm_script"] = True, [REFUSAL_SENTENCE, RETRY_ANSWER]
    res = pipeline().answer_query(Q, route_decision=HARD, chunks=list(CHUNKS))
    assert res["answer"] == RETRY_ANSWER and res["fallback"] == "gpu_unavailable"
    assert _docs(res["chunks"]) == ["d3", "d4", "d5"], "sources are the passages the answer came from"
    assert "retry" in res["stats"]["server"] and net["slm_calls"] == 2
    assert "content 0" in net["slm_contexts"][0] and "content 3" in net["slm_contexts"][1]


def test_second_refusal_stands(net):
    net["slm_up"], net["slm_script"] = True, [REFUSAL_SENTENCE, REFUSAL_SENTENCE]
    res = pipeline().answer_query(Q, route_decision=HARD, chunks=list(CHUNKS))
    assert res["answer"] == REFUSAL_SENTENCE and res["chunks"] == [] and net["slm_calls"] == 2


def test_no_retry_without_further_passages(net):
    net["slm_up"], net["slm_script"] = True, [REFUSAL_SENTENCE]
    res = pipeline().answer_query(Q, route_decision=HARD, chunks=list(CHUNKS[:3]))
    assert res["answer"] == REFUSAL_SENTENCE and net["slm_calls"] == 1


def test_easy_route_refusal_during_gpu_outage_is_retried(net):
    net["slm_up"], net["slm_script"] = True, [REFUSAL_SENTENCE, RETRY_ANSWER]
    easy = {"route": "easy", "target_tier": "cpu_slm", "probability_hard": 0.2}
    res = pipeline().answer_query(Q, route_decision=easy, chunks=list(CHUNKS))
    assert res["answer"] == RETRY_ANSWER and res["route"] == "easy" and "fallback" not in res


def test_7b_is_still_preferred_when_available(net, monkeypatch):
    net["slm_up"], net["gpu_health"], net["slm_script"] = True, True, [REFUSAL_SENTENCE]
    easy = {"route": "easy", "target_tier": "cpu_slm", "probability_hard": 0.2}
    pipeline().answer_query(Q, route_decision=easy, chunks=list(CHUNKS))
    assert net["gpu_posts"] == 1 and net["slm_calls"] == 1, "refusal goes to the 7B, not a CPU retry"


@pytest.mark.parametrize("script,answer,docs", [
    ([REFUSAL_SENTENCE, RETRY_ANSWER], RETRY_ANSWER, ["d3", "d4", "d5"]),
    ([REFUSAL_SENTENCE, REFUSAL_SENTENCE], REFUSAL_SENTENCE, []),
])
def test_streaming_refusal_retry(net, script, answer, docs):
    net["slm_up"], net["slm_script"] = True, list(script)
    ev = _events(pipeline().answer_query_stream(Q, route_decision=HARD, chunks=list(CHUNKS)))
    done = ev[-1]
    assert done["type"] == "done" and done["full_answer"] == answer
    assert _docs(done["chunks"]) == docs
    tokens = "".join(e["delta"] for e in ev if e["type"] == "token")
    assert tokens == answer, "a refusal is never streamed before the retry's answer"
    assert net["slm_calls"] == 2
