"""DEFECT-28: a retrieval outage gives an intentional HTTP 503, not an unexplained 500.

Reported: with Redis unreachable or the thesis index missing, /query returned "Internal Server Error"
(the FT.SEARCH errors were not handled). Now: HTTP 503 + Retry-After and a short message (details only in
the server log); the streaming path sends an error event instead of a false "no theses found".
No services needed.
"""

import json
from contextlib import asynccontextmanager

import pytest
import redis.exceptions as rx
from fastapi.testclient import TestClient

import gateway.main as gm
import pipeline.retriever as pr
from gateway.dependencies import (get_admission_controller, get_complexity_scorer, get_dispatcher,
                                  get_semantic_cache, get_vllm_client)
from gateway.router.dispatcher import ModelTier, RouteDecision
from pipeline.rag_chain import RAGQAPipeline
from pipeline.retriever import RETRIEVAL_UNAVAILABLE_MESSAGE, RetrievalUnavailable, is_retrieval_outage

OUTAGES = [
    rx.ConnectionError("Error 111 connecting to localhost:6379. Connection refused."),
    rx.BusyLoadingError("Redis is loading the dataset in memory"),
    rx.TimeoutError("Timeout reading from socket"),
    rx.ResponseError("rag_documents_idx: no such index"),
]


@pytest.mark.parametrize("exc", OUTAGES, ids=lambda e: type(e).__name__ + ":" + str(e)[:20])
def test_outages_are_recognised(exc):
    assert is_retrieval_outage(exc)


@pytest.mark.parametrize("exc", [rx.ResponseError("Syntax error at offset 3"), ValueError("bad"), KeyError("x")])
def test_other_errors_are_not_called_outages(exc):
    assert not is_retrieval_outage(exc)


# ----------------------------------------------------------------------------------- route level
class Cache:
    async def alookup(self, *a, **k):
        return None

    async def astore(self, *a, **k):
        return False


class Scorer:
    async def score(self, q):
        return 0.2


class Dispatcher:
    def decide(self, score, **k):
        return RouteDecision(target_tier=ModelTier.CPU_SLM, score=score, alpha=0.5, target_model="m", reason="t")


class Admission:
    @asynccontextmanager
    async def slot(self, *a, **k):
        yield

    async def acquire(self, *a, **k):
        return None

    def release(self, *a, **k):
        pass


class VLLM:
    async def check_health(self):
        return False


@pytest.fixture
def client(monkeypatch):
    over = gm.app.dependency_overrides
    saved = dict(over)
    over.update({get_semantic_cache: Cache, get_complexity_scorer: Scorer, get_dispatcher: Dispatcher,
                 get_admission_controller: Admission, get_vllm_client: VLLM})
    yield TestClient(gm.app, raise_server_exceptions=False)
    over.clear()
    over.update(saved)


@pytest.mark.parametrize("exc", OUTAGES, ids=lambda e: type(e).__name__)
def test_query_returns_503_with_retry_after_during_an_outage(client, monkeypatch, exc):
    async def failing(self, *a, **k):
        raise exc
    monkeypatch.setattr(pr.RedisChunkRetriever, "asearch", failing)
    r = client.post("/query", json={"query": "What is vermiculite used for?", "stream": False})
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == RETRIEVAL_UNAVAILABLE_MESSAGE and r.headers.get("Retry-After")
    assert "6379" not in r.text and "rag_documents_idx" not in r.text, "no internals in the response"


def test_unexpected_retrieval_bug_is_not_disguised_as_an_outage(client, monkeypatch):
    async def failing(self, *a, **k):
        raise ValueError("bug")
    monkeypatch.setattr(pr.RedisChunkRetriever, "asearch", failing)
    assert client.post("/query", json={"query": "What is vermiculite used for?", "stream": False}).status_code == 500


def test_stray_redis_connection_error_anywhere_gives_503(client, monkeypatch):
    async def boom(*a, **k):
        raise rx.ConnectionError("Error 111 connecting to localhost:6379.")
    monkeypatch.setattr(Scorer, "score", boom)
    monkeypatch.setattr(pr.RedisChunkRetriever, "asearch", boom)
    r = client.post("/query", json={"query": "What is vermiculite used for?", "stream": False})
    assert r.status_code == 503 and "6379" not in r.text


# ----------------------------------------------------------------------------------- pipeline
class FailingRetriever:
    def __init__(self, exc):
        self.exc = exc

    def search(self, *a, **k):
        raise self.exc


EASY = {"route": "easy", "target_tier": "cpu_slm", "probability_hard": 0.2}


@pytest.mark.parametrize("exc", OUTAGES, ids=lambda e: type(e).__name__)
def test_streaming_reports_the_outage_instead_of_no_theses_found(exc):
    p = RAGQAPipeline(retriever=FailingRetriever(exc), router=object())
    ev = list(p.answer_query_stream("What is vermiculite used for?", route_decision=EASY))
    assert ev == [{"type": "error", "status": 503, "error": RETRIEVAL_UNAVAILABLE_MESSAGE}]


def test_non_streaming_pipeline_raises_retrieval_unavailable():
    p = RAGQAPipeline(retriever=FailingRetriever(OUTAGES[0]), router=object())
    with pytest.raises(RetrievalUnavailable):
        p.answer_query("What is vermiculite used for?", route_decision=EASY)


def test_streaming_query_route_relays_the_error_event(client, monkeypatch):
    monkeypatch.setattr(gm, "RAGQAPipeline", lambda *a, **k: RAGQAPipeline(retriever=FailingRetriever(OUTAGES[0]),
                                                                           router=object()))
    r = client.post("/query", json={"query": "What is vermiculite used for?", "stream": True})
    events = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: {")]
    errors = [e for e in events if e.get("type") == "error"]
    assert errors and errors[0]["error"] == RETRIEVAL_UNAVAILABLE_MESSAGE
    assert not any(e.get("type") == "token" for e in events), "no misleading refusal text"
