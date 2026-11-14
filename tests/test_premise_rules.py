"""Both RAG prompts must carry the premise-check and single-outcome rules (red-team finding: loaded
"Why does X always...?" questions were answered by confirming the false premise)."""

from pipeline.gpu_llm_runner import GPULLMEngine
from pipeline.sanitizer import PREMISE_RULES
from pipeline.slm_runner import CPUSLMEngine


def _system_prompt(engine):
    return engine.build_rag_messages("Why does X always happen?", [])[0]["content"]


def test_both_prompts_include_premise_rules():
    for engine in (CPUSLMEngine, GPULLMEngine):
        sp = _system_prompt(engine)
        assert PREMISE_RULES in sp, engine.__name__
        assert "Check the Question's Premise" in sp and "never explain why such a claim is true" in sp
        assert "do NOT challenge or doubt it" in sp
        assert "Single Outcome" in sp


def test_rules_are_numbered_consistently():
    for engine in (CPUSLMEngine, GPULLMEngine):
        sp = _system_prompt(engine)
        positions = [sp.index(f"{n}. ") for n in range(1, 7)]
        assert positions == sorted(positions), engine.__name__


def test_custom_system_prompt_still_respected():
    msgs = CPUSLMEngine.build_rag_messages("q", [], system_prompt="custom")
    assert msgs[0]["content"] == "custom"
