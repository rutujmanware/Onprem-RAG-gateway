"""DEFECT-36: a generation failure is never cached (Sub-FR-1.2 / NFR-6).

Reported against rt-baseline: with both model tiers down, streaming /query answered "[Inference complete.
Partial response recorded.]" with HTTP 200; because chunks had been retrieved, it was cached and served to the
next user after recovery. Since commit c50a120 such answers are flagged `incomplete` and not stored; the cache
gate now also rejects the placeholder text itself (any wording), on store and on lookup.
"""

import json

import pytest

import pipeline.rag_chain as rc
from gateway.cache.redis_semantic_cache import RedisSemanticCache as C
from pipeline.rag_chain import GENERATION_UNAVAILABLE_NOTE, PARTIAL_ANSWER_NOTE, RAGQAPipeline

CHUNKS = [{"doc_id": f"d{i}", "title": f"Thesis {i}", "content": "battery degradation", "similarity": 0.7} for i in range(3)]


@pytest.mark.parametrize("answer", [
    "\n\n[Inference complete. Partial response recorded.]",                   # rt-baseline wording
    "Some text" + PARTIAL_ANSWER_NOTE,
    GENERATION_UNAVAILABLE_NOTE,
])
def test_gate_rejects_failure_placeholders_even_with_chunks(answer):
    ok, reason = C.is_cacheable_response({"answer": answer, "chunks": [{"doc_id": "d1"}]})
    assert not ok, reason


def test_gate_still_accepts_a_real_answer():
    assert C.is_cacheable_response({"answer": "Capacity fade is driven by SEI growth [1].", "chunks": [{"doc_id": "d1"}]})[0]


class BrokenSLM:
    model_name = "broken"

    def answer_rag_stream(self, **k):
        raise RuntimeError("model file missing")
        yield  # pragma: no cover


def test_total_failure_is_flagged_incomplete_and_says_unavailable(monkeypatch):
    def down(*a, **k):
        raise ConnectionError("down")
    import httpx
    monkeypatch.setattr(httpx, "get", down)
    monkeypatch.setattr(rc.RAGQAPipeline, "_gpu_daemon_healthy", staticmethod(lambda: False))
    p = RAGQAPipeline(retriever=object(), router=object(), slm_engine=BrokenSLM(), gpu_engine=object())
    ev = list(p.answer_query_stream("Explain lithium-ion battery degradation", chunks=list(CHUNKS),
                                    route_decision={"route": "easy", "target_tier": "cpu_slm", "probability_hard": 0.1}))
    done = ev[-1]
    assert done["type"] == "done" and done.get("incomplete") is True
    assert done["full_answer"] == GENERATION_UNAVAILABLE_NOTE.strip()
    assert not C.is_cacheable_response({"answer": done["full_answer"], "chunks": done["chunks"] or [{"x": 1}]})[0]
