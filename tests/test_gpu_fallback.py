import httpx
import pytest

import pipeline.rag_chain as rc


CHUNKS = [{"title": "T", "text": "Relevant context", "similarity": 0.9}]
HARD = {"route": "hard", "target_tier": "gpu_vllm", "probability_hard": 0.9}


@pytest.fixture(autouse=True)
def no_router(monkeypatch):
    monkeypatch.setattr(rc, "QueryRouter", None)


class Response:
    status_code = 200


@pytest.mark.parametrize("health", [False, True])
@pytest.mark.parametrize("failure", ["connect", "timeout", "busy"])
def test_hard_request_falls_back_to_cpu(monkeypatch, health, failure):
    def get(url, **kwargs):
        if ":8000" in url and not health:
            raise httpx.ConnectError("gpu down")
        return Response()

    monkeypatch.setattr(httpx, "get", get)
    def post(*args, **kwargs):
        if failure == "timeout":
            raise httpx.ReadTimeout("gpu timeout")
        if failure == "busy":
            return httpx.Response(503, request=httpx.Request("POST", "http://localhost:8000"))
        raise httpx.ConnectError("gpu down")

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(rc, "_complete_chat_http", lambda *a, **k: ("CPU answer", 2, "cpu"))
    pipe = rc.RAGQAPipeline(router=None)
    result = pipe.answer_query("explain transformers", route_decision=HARD, chunks=CHUNKS)
    assert result["answer"] == "CPU answer"
    assert result["target_tier"] == "cpu_slm"


def test_previously_failed_gpu_is_not_retried(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return Response()

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(rc, "_complete_chat_http", lambda *a, **k: ("CPU answer", 2, "cpu"))
    pipe = rc.RAGQAPipeline(router=None)
    result = pipe.answer_query(
        "explain transformers", route_decision={**HARD, "gpu_failed": True}, chunks=CHUNKS
    )
    assert result["answer"] == "CPU answer"
    assert all(":8000" not in url for url in calls)


@pytest.mark.parametrize("health", [False, True])
@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout, rc.UpstreamBusy])
def test_hard_stream_falls_back_before_first_token(monkeypatch, health, failure):
    def get(url, **kwargs):
        if ":8000" in url and not health:
            raise httpx.ConnectError("gpu down")
        return Response()

    def stream(url, *args, **kwargs):
        if ":8000" in url:
            raise failure("gpu down")
        yield "CPU answer"

    monkeypatch.setattr(httpx, "get", get)
    monkeypatch.setattr(rc, "_stream_chat_http", stream)
    pipe = rc.RAGQAPipeline(router=None)
    events = list(pipe.answer_query_stream("explain transformers", route_decision=HARD, chunks=CHUNKS))
    assert events[-1]["full_answer"] == "CPU answer"
    assert events[-1]["stats"]["server"] == "cpu-daemon-port-8082"


def test_hard_stream_failure_after_output_is_not_restarted(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Response())
    calls = []

    def stream(url, *args, **kwargs):
        calls.append(url)
        yield "Partial answer"
        raise httpx.ReadTimeout("gpu timeout")

    monkeypatch.setattr(rc, "_stream_chat_http", stream)
    pipe = rc.RAGQAPipeline(router=None)
    events = list(pipe.answer_query_stream("explain transformers", route_decision=HARD, chunks=CHUNKS))
    assert events[-1]["incomplete"] is True
    assert events[-1]["full_answer"] == "Partial answer" + rc.PARTIAL_ANSWER_NOTE
    assert len(calls) == 1 and ":8000" in calls[0]


def test_hard_stream_success_keeps_gpu(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Response())
    calls = []

    def stream(url, *args, **kwargs):
        calls.append(url)
        yield "GPU answer"

    monkeypatch.setattr(rc, "_stream_chat_http", stream)
    pipe = rc.RAGQAPipeline(router=None)
    events = list(pipe.answer_query_stream("explain transformers", route_decision=HARD, chunks=CHUNKS))
    assert events[-1]["full_answer"] == "GPU answer"
    assert events[-1]["stats"]["server"] == "gpu-daemon-port-8000"
    assert len(calls) == 1 and ":8000" in calls[0]


def test_failed_gpu_is_not_retried_after_cpu_refusal(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Response())
    calls = []

    def stream(url, *args, **kwargs):
        calls.append(url)
        if ":8000" in url:
            raise httpx.ConnectError("gpu down")
        yield "No specific institutional repository theses were found on this topic."

    monkeypatch.setattr(rc, "_stream_chat_http", stream)
    pipe = rc.RAGQAPipeline(router=None)
    events = list(pipe.answer_query_stream("explain transformers", route_decision=HARD, chunks=CHUNKS))
    assert events[-1]["stats"]["server"] == "cpu-daemon-port-8082"
    assert len(calls) == 2
