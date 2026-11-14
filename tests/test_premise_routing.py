"""Loaded 'why' claims are escalated to the GPU tier on both routing paths; alpha comes from ROUTING_ALPHA."""

import pytest

from gateway.router.dispatcher import ModelTier, RequestDispatcher
from gateway.router.premise_check import premise_check_reason

LOADED = [
    "Why does X always happen?",
    "Why can a perpetual motion machine violate conservation of energy?",
    "How come every graph is planar?",
    "Explain why all metals are magnetic",
    "Why do larger batches guarantee better accuracy?",
    "Why can't a heat engine be perfectly efficient?",
    "Could you explain why steel never corrodes?",
    "Why is a catalyst the only factor that matters?",
]
NOT_LOADED = [
    "Why does steel corrode in seawater?",
    "What is the power method?",
    "Does Newton's method always converge to a root?",  # yes/no form: no embedded 'why' claim
    "Why is the sky blue?",
    "Why isn't the sky green?",
    "Explain the Krebs cycle",
    "",
    None,
]


@pytest.mark.parametrize("q", LOADED)
def test_loaded_why_claims_detected(q):
    assert premise_check_reason(q)


@pytest.mark.parametrize("q", NOT_LOADED)
def test_ordinary_questions_not_flagged(q):
    assert premise_check_reason(q) is None


def test_dispatcher_escalates_loaded_question_below_alpha():
    d = RequestDispatcher(alpha=0.65)
    dec = d.decide(0.01, query="Why does X always happen?")
    assert dec.target_tier == ModelTier.GPU_LLM and dec.reason.startswith("Premise check")


def test_dispatcher_keeps_ordinary_low_score_on_cpu():
    d = RequestDispatcher(alpha=0.65)
    assert d.decide(0.01, query="Why does steel corrode in seawater?").target_tier == ModelTier.CPU_SLM
    assert d.decide(0.01).target_tier == ModelTier.CPU_SLM  # no query: unchanged behaviour


def test_dispatcher_escalation_respects_gpu_saturation_fallback():
    d = RequestDispatcher(alpha=0.65)
    dec = d.decide(0.01, gpu_saturated=True, query="Why does X always happen?")
    assert dec.target_tier == ModelTier.CPU_SLM and dec.fallback_applied


def test_dispatcher_boundary_unchanged_with_query():
    d = RequestDispatcher(alpha=0.65)
    assert d.decide(0.65, query="Why does steel corrode?").target_tier == ModelTier.GPU_LLM
    assert d.decide(0.64, query="Why does steel corrode?").target_tier == ModelTier.CPU_SLM


class _StubRouter:
    def __init__(self, route="easy"):
        self.route = route

    def predict(self, q):
        return {"query": q, "route": self.route, "target_tier": "gpu_vllm" if self.route == "hard" else "cpu_slm",
                "probability_hard": 0.01}


@pytest.fixture
def pipeline_cls():
    from pipeline.rag_chain import RAGQAPipeline
    return RAGQAPipeline


def test_streaming_path_escalates_when_gpu_daemon_healthy(pipeline_cls, monkeypatch):
    monkeypatch.setattr(pipeline_cls, "_gpu_daemon_healthy", staticmethod(lambda: True))
    p = pipeline_cls(router=_StubRouter("easy"))
    dec = p.route_query("Why does X always happen?")
    assert dec["route"] == "hard" and dec["target_tier"] == "gpu_vllm" and "premise_check" in dec["escalation"]
    assert p.route_query("Why does steel corrode?")["route"] == "easy"


def test_streaming_path_does_not_escalate_when_gpu_daemon_down(pipeline_cls, monkeypatch):
    monkeypatch.setattr(pipeline_cls, "_gpu_daemon_healthy", staticmethod(lambda: False))
    p = pipeline_cls(router=_StubRouter("easy"))
    assert p.route_query("Why does X always happen?")["route"] == "easy"


def test_streaming_router_uses_routing_alpha(pipeline_cls, monkeypatch):
    monkeypatch.setenv("ROUTING_ALPHA", "0.65")
    p = pipeline_cls()
    if p.router is None:
        pytest.skip("MLP router model not available")
    assert p.router.threshold == 0.65


def test_gateway_dispatcher_uses_routing_alpha():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "gateway" / "dependencies.py").read_text()
    assert 'RequestDispatcher(alpha=float(os.getenv("ROUTING_ALPHA", "0.65")))' in src
    main = (Path(__file__).resolve().parent.parent / "gateway" / "main.py").read_text()
    assert main.count("dispatcher.decide(score, query=query)") == 2
