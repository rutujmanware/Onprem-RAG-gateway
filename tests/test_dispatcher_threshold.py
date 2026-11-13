"""Regression tests for the FR-2 routing boundary: P(hard) >= alpha routes to the Tier-2 GPU LLM.

Red-team finding: at score == alpha == 0.65 the dispatcher selected CPU_SLM because the CPU
branch used `score <= alpha`.
"""

import pytest

from gateway.router.dispatcher import ModelTier, RequestDispatcher


@pytest.fixture
def dispatcher():
    return RequestDispatcher()  # default alpha = 0.65, as used by the gateway


def test_default_alpha_matches_prd(dispatcher):
    assert dispatcher.alpha == 0.65


def test_score_equal_to_alpha_routes_to_gpu(dispatcher):
    decision = dispatcher.decide(0.65)
    assert decision.target_tier == ModelTier.GPU_LLM
    assert decision.target_tier.value == "gpu_vllm"
    assert decision.target_model == dispatcher.vllm_model_name
    assert decision.fallback_applied is False


@pytest.mark.parametrize("score", [0.0, 0.3, 0.6499, 0.64999999])
def test_score_below_alpha_routes_to_cpu(dispatcher, score):
    decision = dispatcher.decide(score)
    assert decision.target_tier == ModelTier.CPU_SLM
    assert decision.target_tier.value == "cpu_slm"
    assert decision.fallback_applied is False


@pytest.mark.parametrize("score", [0.65, 0.6500001, 0.7, 0.99, 1.0])
def test_score_at_or_above_alpha_routes_to_gpu(dispatcher, score):
    assert dispatcher.decide(score).target_tier == ModelTier.GPU_LLM


@pytest.mark.parametrize("alpha", [0.3, 0.5, 0.65, 0.8])
def test_boundary_holds_for_custom_alpha(alpha):
    d = RequestDispatcher(alpha=alpha)
    assert d.decide(alpha).target_tier == ModelTier.GPU_LLM
    assert d.decide(alpha - 1e-6).target_tier == ModelTier.CPU_SLM


def test_gpu_saturation_fallback_applies_at_equality(dispatcher):
    decision = dispatcher.decide(0.65, gpu_saturated=True)
    assert decision.target_tier == ModelTier.CPU_SLM
    assert decision.fallback_applied is True


def test_no_fallback_when_disabled():
    d = RequestDispatcher(fallback_enabled=False)
    assert d.decide(0.65, gpu_saturated=True).target_tier == ModelTier.GPU_LLM


def test_force_tier_overrides_score(dispatcher):
    assert dispatcher.decide(0.99, force_tier=ModelTier.CPU_SLM).target_tier == ModelTier.CPU_SLM
    assert dispatcher.decide(0.0, force_tier=ModelTier.GPU_LLM).target_tier == ModelTier.GPU_LLM


def test_reason_strings_match_rule(dispatcher):
    assert dispatcher.decide(0.65).reason == "Score >= Alpha"
    assert dispatcher.decide(0.64).reason == "Score < Alpha"


def test_dispatcher_agrees_with_router_rule_across_sweep():
    """Dispatcher must use the same boundary as QueryRouter / serve_router (prob >= threshold -> GPU)."""
    alpha = 0.65
    d = RequestDispatcher(alpha=alpha)
    for i in range(0, 1001):
        score = i / 1000
        expected_gpu = score >= alpha
        assert (d.decide(score).target_tier == ModelTier.GPU_LLM) == expected_gpu, score
