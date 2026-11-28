"""Explain mode of both RAG prompts (2026-11-27).

On reasonably strong retrieval (best chunk similarity >= EXPLAIN_MIN_SIMILARITY), a term the theses mention or use
gets a short textbook explanation followed by what the theses do with it, and definition questions get a hint after
the documents. On weak retrieval the original strict rules apply unchanged: weak matches are where a thesis uses the
same word for something else ("GST rate on gold" -> a dataset named GST)."""

import pytest

from pipeline.gpu_llm_runner import GPULLMEngine
from pipeline.sanitizer import (DEFINITION_HINT, DOCUMENT_REMINDER, EXPLAIN_MIN_SIMILARITY, GROUNDING_RULES,
                                REFUSAL_RULE, REFUSAL_RULE_EXPLAIN, answer_hint, explain_allowed)
from pipeline.slm_runner import CPUSLMEngine

ENGINES = (CPUSLMEngine, GPULLMEngine)
STRONG = [{"title": "Low Reynolds Number Airfoil Aerodynamics", "content": "At low Reynolds numbers laminar separation",
           "similarity": 0.61}]
WEAK = [{"title": "Transformer Neural Operators", "content": "PSNR on the GST dataset", "similarity": 0.44}]


def _sp(engine, chunks, q="What is the Reynolds number?"):
    return engine.build_rag_messages(q, chunks)[0]["content"]


def test_mode_threshold():
    assert EXPLAIN_MIN_SIMILARITY == pytest.approx(0.47)
    assert explain_allowed(STRONG) and not explain_allowed(WEAK)
    assert not explain_allowed([]) and not explain_allowed(None)
    assert not explain_allowed([{"title": "x", "content": "no similarity field"}]), "other callers: strict"
    assert explain_allowed([{"similarity": "0.5"}, {"similarity": None}])


@pytest.mark.parametrize("engine", ENGINES)
def test_strong_retrieval_uses_explain_mode(engine):
    sp = _sp(engine, STRONG)
    assert GROUNDING_RULES + REFUSAL_RULE_EXPLAIN in sp
    assert "No General Knowledge Extrapolation" not in sp, "the strict rule made the models parrot excerpts"
    assert "Background knowledge alone is never an answer" in sp
    assert "Never attribute background knowledge to a document" in sp
    assert "same word or abbreviation for something different" in sp
    assert "ONLY when none of the provided documents is about the topic" in sp


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("chunks", [WEAK, []])
def test_weak_or_no_retrieval_keeps_the_original_strict_rules(engine, chunks):
    sp = _sp(engine, chunks)
    assert "1. Strict Repository Grounding" in sp and "2. No General Knowledge Extrapolation" in sp
    assert REFUSAL_RULE in sp and GROUNDING_RULES not in sp and REFUSAL_RULE_EXPLAIN not in sp
    assert DEFINITION_HINT not in engine.build_rag_messages("What is the GST rate on gold?", chunks)[-1]["content"]


@pytest.mark.parametrize("q", [
    "What is the Reynolds number?", "what's a Kalman filter", "Explain entropy", "Define GMRES",
    "Can you please explain photonic crystals?", "Tell me about spintronics", "Meaning of compressive sensing",
    "What are carbon nanotubes?", "Describe the finite element method",
])
def test_definition_questions_get_the_hint_on_strong_retrieval(q):
    assert answer_hint(q, STRONG) == DEFINITION_HINT
    assert answer_hint(q, WEAK) == "" and answer_hint(q) == ""


@pytest.mark.parametrize("q", [
    "How does MIMO improve wireless capacity?", "Why does graphene always conduct better than copper?",
    "Which theses discuss solar energy forecasting?", "What research has IISc done on electric vehicles?",
    "Compare GCN and GAT for molecules", "", None,
])
def test_other_questions_get_no_hint(q):
    assert answer_hint(q, STRONG) == ""


@pytest.mark.parametrize("engine", ENGINES)
def test_hint_sits_after_the_question_and_before_the_document_reminder(engine):
    user = engine.build_rag_messages("What is the Reynolds number?", STRONG)[-1]["content"]
    assert user.index("What is the Reynolds number?") < user.index(DEFINITION_HINT) < user.index(DOCUMENT_REMINDER)
    assert user.index("</context_data>") < user.index(DEFINITION_HINT), "after the untrusted documents"
    other = engine.build_rag_messages("How does MIMO improve wireless capacity?", STRONG)[-1]["content"]
    assert DEFINITION_HINT not in other and DOCUMENT_REMINDER in other
