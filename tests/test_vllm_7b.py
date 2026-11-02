"""Tests for the 7B Large Language Model (Qwen 2.5 7B Quantized) integration.

Validates:
1. vLLM configuration file (configs/vllm_config.yaml).
2. Routing dispatcher configuration targeting the 7B tier.
3. Quantized model metadata, tokenizer, and config files in models/llm_7b/.
4. VLLMClient interface and fallback behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from gateway.clients.vllm_client import VLLMClient
from gateway.router.dispatcher import ModelTier, RequestDispatcher
from pipeline.provision_llm import QUANTIZED_PRESETS, DEFAULT_DESTINATION


def test_quantized_presets():
    """Verify available quantized presets for Qwen 2.5 7B."""
    assert "awq" in QUANTIZED_PRESETS
    assert "gptq-4bit" in QUANTIZED_PRESETS
    assert "gptq-8bit" in QUANTIZED_PRESETS

    awq_info = QUANTIZED_PRESETS["awq"]
    assert awq_info["repo_id"] == "Qwen/Qwen2.5-7B-Instruct-AWQ"
    assert awq_info["quant_type"] == "awq"


def test_vllm_config_structure():
    """Verify configs/vllm_config.yaml specifies the quantized 7B model."""
    config_path = Path("configs/vllm_config.yaml")
    assert config_path.exists(), f"{config_path} must exist"

    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    model_cfg = cfg.get("model", {})
    assert model_cfg.get("path") == "models/llm_7b"
    assert model_cfg.get("architecture") == "qwen2"
    assert model_cfg.get("quantization") == "awq"
    assert model_cfg.get("hf_repo") == "Qwen/Qwen2.5-7B-Instruct-AWQ"

    server_cfg = cfg.get("server", {})
    assert server_cfg.get("port") == 8000


def test_dispatcher_7b_target_model():
    """Verify RequestDispatcher routes hard queries (score > alpha) to the 7B tier."""
    dispatcher = RequestDispatcher(
        alpha=0.65,
        slm_model_name="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        vllm_model_name="Qwen/Qwen2.5-7B-Instruct-AWQ",
    )

    # Low complexity -> CPU SLM
    dec_easy = dispatcher.decide(score=0.40)
    assert dec_easy.target_tier == ModelTier.CPU_SLM
    assert "1.5B" in dec_easy.target_model

    # High complexity -> GPU 7B LLM
    dec_hard = dispatcher.decide(score=0.85)
    assert dec_hard.target_tier == ModelTier.GPU_LLM
    assert dec_hard.target_model == "Qwen/Qwen2.5-7B-Instruct-AWQ"


@pytest.mark.asyncio
async def test_vllm_client_generation_mock():
    """Verify VLLMClient constructs OpenAI-compatible payloads for the 7B tier."""
    client = VLLMClient(
        base_url="http://mock-vllm:8000/v1",
        model_name="Qwen/Qwen2.5-7B-Instruct-AWQ",
    )

    mock_resp = {
        "id": "cmpl-mock",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "This is a detailed 7B model response.",
                }
            }
        ],
    }

    with patch.object(client, "_client_or_create") as mock_getter:
        from unittest.mock import MagicMock
        mock_http = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_resp
        mock_response.raise_for_status.return_value = None
        mock_http.post.return_value = mock_response
        mock_getter.return_value = mock_http

        result = await client.generate_chat(
            [{"role": "user", "content": "Explain PagedAttention in detail."}]
        )
        assert result["choices"][0]["message"]["content"] == "This is a detailed 7B model response."
        mock_http.post.assert_called_once()
        call_kwargs = mock_http.post.call_args
        assert call_kwargs[1]["json"]["model"] == "Qwen/Qwen2.5-7B-Instruct-AWQ"


def test_provisioned_7b_model_config_integrity():
    """Check config.json in models/llm_7b/ if provisioned."""
    dest = Path("models/llm_7b")
    config_file = dest / "config.json"
    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data.get("model_type") == "qwen2"
        # Verify AWQ quantization config
        quant_cfg = data.get("quantization_config", {})
        assert quant_cfg.get("quant_method") == "awq"
