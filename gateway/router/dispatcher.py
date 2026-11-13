from enum import Enum
from typing import Any, Dict, Optional
try:
    from pydantic import BaseModel
except ImportError:
    class BaseModel:
        def __init__(self, **kwargs):
            for k, v in kwargs.items(): setattr(self, k, v)
        def dict(self): return self.__dict__

class ModelTier(str, Enum):
    CPU_SLM = "cpu_slm"
    GPU_LLM = "gpu_vllm"

class RouteDecision(BaseModel):
    target_tier: ModelTier
    score: float
    alpha: float
    target_model: str
    reason: str
    fallback_applied: bool = False

class RequestDispatcher:
    def __init__(self, alpha=0.65, slm_model_name="Qwen/Qwen2.5-3B-Instruct", vllm_model_name="meta-llama/Llama-2-7b-chat-hf", fallback_enabled=True):
        self.alpha = alpha
        self.slm_model_name = slm_model_name
        self.vllm_model_name = vllm_model_name
        self.fallback_enabled = fallback_enabled

    def decide(self, score: float, gpu_saturated: bool = False, force_tier=None) -> RouteDecision:
        if force_tier:
            return RouteDecision(target_tier=force_tier, score=score, alpha=self.alpha, target_model=self.slm_model_name if force_tier == ModelTier.CPU_SLM else self.vllm_model_name, reason=f"Manual: {force_tier}")
        # FR-2: P(hard) >= alpha routes to the Tier-2 GPU LLM, so a score exactly at alpha is GPU-bound.
        # Same rule as QueryRouter (prob >= threshold) and serve_router (score >= threshold).
        if score < self.alpha:
            return RouteDecision(target_tier=ModelTier.CPU_SLM, score=score, alpha=self.alpha, target_model=self.slm_model_name, reason="Score < Alpha")
        else:
            if gpu_saturated and self.fallback_enabled:
                return RouteDecision(target_tier=ModelTier.CPU_SLM, score=score, alpha=self.alpha, target_model=self.slm_model_name, reason="GPU saturated", fallback_applied=True)
            return RouteDecision(target_tier=ModelTier.GPU_LLM, score=score, alpha=self.alpha, target_model=self.vllm_model_name, reason="Score >= Alpha")
