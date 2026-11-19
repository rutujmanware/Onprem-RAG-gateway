"""DEFECT-33: counting / listing theses by metadata is not supported, and is said so.

The catalog path (count and list by department, author, advisor, degree, year) produced invented similarity
scores and undercounted "exact" totals; it was removed. Such requests now get a short notice (no model call,
no sources, never cached). Implicit metadata filtering of ordinary questions is off by default: measured on the
frozen 244-question set it lowered hit@3 (0.711 without, 0.691 with; 4 better, 0 worse without).
"""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import gateway.main as gm
from gateway.dependencies import (get_admission_controller, get_complexity_scorer, get_dispatcher,
                                  get_semantic_cache, get_slm_client, get_vllm_client)
from pipeline.metadata_queries import METADATA_QUERY_MESSAGE, is_metadata_query
from pipeline.rag_chain import RAGQAPipeline
from pipeline.retriever import RedisChunkRetriever

ROOT = Path(__file__).resolve().parent.parent

METADATA = ["how many papers exist in the materials department", "how many theses are there about machine learning",
            "list theses by Prof. Rao in 2015", "theses in the CDS department on graphene",
            "Show me all PhD theses supervised by Dr. Kumar", "number of dissertations from the physics department",
            "Which theses were submitted in 2019 in the aerospace engineering department?",
            "Give me the papers written by Ramesh", "list all theses from 2020", "total number of theses in 2018"]
CONTENT = ["How many permissible vibrations does the lattice have, and what is the degeneracy factor?",
           "How many calories are in a banana?", "How many layers does the proposed network use?",
           "What factors affect the mechanical properties of pearlitic spheroidal graphite iron?",
           "Who supervised the thesis on graphene?", "What does the thesis on GMRES conclude?",
           "Which papers discuss perovskite stability?", "explain transformers", "Compare GMRES and MINRES",
           "What experimental studies were conducted on multiferroic materials in 2015?"]


@pytest.mark.parametrize("q", METADATA)
def test_metadata_requests_are_recognised(q):
    assert is_metadata_query(q)


@pytest.mark.parametrize("q", CONTENT)
def test_content_questions_are_not(q):
    assert not is_metadata_query(q)


def test_no_frozen_question_is_flagged():
    qs = []
    for d in ("tests/data/injection_eval", "tests/data/injection_eval/round2", "tests/data/injection_eval/round3"):
        qs += [json.loads(l)["text"] for l in (ROOT / d / "hard_negatives.jsonl").read_text().splitlines() if l.strip()]
    for f in ("questions", "rare_questions"):
        p = ROOT / "logs" / "retrieval_eval" / f"{f}.jsonl"
        if p.exists():
            qs += [json.loads(l)["question"] for l in p.read_text().splitlines() if l.strip()]
    assert [q for q in qs if is_metadata_query(q)] == []


def test_implicit_metadata_filtering_is_off_by_default():
    assert RedisChunkRetriever.DEFAULT_METADATA_FILTER is False
    assert not hasattr(RedisChunkRetriever, "_execute_catalog_search"), "catalog path removed"


# ------------------------------------------------------------------------------------ pipeline
class Untouchable:
    def __getattr__(self, name):
        raise AssertionError(f"must not be used for a metadata request ({name})")


@pytest.mark.parametrize("streaming", [False, True])
def test_pipeline_replies_with_the_notice_without_retrieval_or_model(streaming):
    p = RAGQAPipeline(retriever=Untouchable(), router=Untouchable(), slm_engine=Untouchable(), gpu_engine=Untouchable())
    q = "how many papers exist in the materials department"
    if streaming:
        ev = list(p.answer_query_stream(q))
        assert "".join(e["delta"] for e in ev if e["type"] == "token") == METADATA_QUERY_MESSAGE
        assert ev[-1]["type"] == "done" and ev[-1]["chunks"] == []
    else:
        res = p.answer_query(q)
        assert res["answer"] == METADATA_QUERY_MESSAGE and res["chunks"] == [] and res["metadata_query"]


# -------------------------------------------------------------------------------------- gateway
class Cache:
    calls = []

    async def alookup(self, *a, **k):
        Cache.calls.append("lookup")
        return None

    async def astore(self, *a, **k):
        Cache.calls.append("store")
        return False


class Admission:
    async def charge(self, *a, **k):   # DEFECT-47: rate limit charged on arrival
        return None

    @asynccontextmanager
    async def slot(self, *a, **k):
        raise AssertionError("no admission slot for a metadata request")
        yield

    async def acquire(self, *a, **k):
        raise AssertionError("no admission slot for a metadata request")


@pytest.fixture
def client():
    Cache.calls = []
    over = gm.app.dependency_overrides
    saved = dict(over)
    over.update({get_semantic_cache: Cache, get_admission_controller: Admission, get_slm_client: Untouchable,
                 get_vllm_client: Untouchable, get_complexity_scorer: Untouchable, get_dispatcher: Untouchable})
    yield TestClient(gm.app)
    over.clear()
    over.update(saved)


@pytest.mark.parametrize("stream", [False, True])
def test_query_route_replies_with_the_notice_and_caches_nothing(client, stream):
    r = client.post("/query", json={"query": "how many papers exist in the materials department", "stream": stream})
    assert r.status_code == 200
    if stream:
        tokens = [json.loads(l[6:]) for l in r.text.splitlines() if l.startswith("data: {")]
        assert "".join(e.get("delta", "") for e in tokens if e.get("type") == "token") == METADATA_QUERY_MESSAGE
    else:
        assert r.json()["answer"] == METADATA_QUERY_MESSAGE and r.json()["chunks"] == []
    assert Cache.calls == [], "neither looked up nor stored in the cache"


@pytest.mark.parametrize("stream", [False, True])
def test_chat_completions_replies_with_the_notice(client, stream):
    r = client.post("/v1/chat/completions", json={"stream": stream,
                    "messages": [{"role": "user", "content": "list theses by Prof. Rao in 2015"}]})
    assert r.status_code == 200 and METADATA_QUERY_MESSAGE.split(".")[0] in r.text
