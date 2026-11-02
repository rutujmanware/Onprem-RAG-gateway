"""CPU Small Language Model (SLM) Client.

Interfaces with the on-premise CPU SLM serving runtime (llama.cpp server or local
Hugging Face pipeline) for low-latency, zero-egress inference on campus hardware.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger("cpu_slm_client")


class CPUSLMClient:
    """Async client for CPU-quantized Small Language Model inference."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.base_url = (
            base_url
            or os.getenv("SLM_BASE_URL")
            or "http://cpu-slm:8080/v1"
        ).rstrip("/")
        self.model_name = (
            model_name
            or os.getenv("SLM_MODEL_NAME")
            or "Qwen/Qwen2.5-1.5B-Instruct-GGUF"
        )
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._hf_pipeline: Optional[Any] = None

    def _get_client(self) -> Optional[Any]:
        if httpx is None:
            return None
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def check_health(self) -> bool:
        """Check if the SLM inference runtime is responsive."""
        client = self._get_client()
        if client is None:
            return False
        try:
            # llama.cpp server OpenAI-compatible endpoints
            resp = await client.get(f"{self.base_url}/models")
            if resp.status_code == 200:
                return True
            resp_health = await client.get(f"{self.base_url}/health")
            return resp_health.status_code in (200, 204)
        except Exception:
            return False

    async def generate_chat(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 512,
        temperature: float = 0.7,
        **kwargs,
    ) -> Dict[str, Any]:
        """Generate a chat completion from the CPU SLM runtime.

        Calls the OpenAI-compatible local llama.cpp endpoint. A model failure is
        surfaced rather than cached as an invented institutional answer.
        """
        client = self._get_client()
        payload = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            **kwargs,
        }

        # 1. Attempt HTTP call to llama.cpp / vLLM OpenAI-compatible server
        if client is not None:
            try:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                )
                if resp.status_code == 200:
                    return resp.json()
                logger.warning(
                    f"SLM HTTP {resp.status_code}: {resp.text[:200]}"
                )
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as err:
                logger.debug(f"SLM HTTP connection unavailable ({err}). Attempting fallback.")

        # 2. In-process Hugging Face Transformers pipeline fallback (if initialized)
        if self._hf_pipeline is not None:
            try:
                hf_out = self._hf_pipeline(
                    messages,
                    max_new_tokens=max_tokens,
                    temperature=temperature,
                )
                text = hf_out[0]["generated_text"][-1]["content"]
                return {
                    "choices": [
                        {"message": {"role": "assistant", "content": text}}
                    ],
                    "model": self.model_name,
                    "engine": "transformers-cpu",
                }
            except Exception as hf_err:
                logger.warning(f"In-process Hugging Face inference failed: {hf_err}")

        raise RuntimeError("CPU SLM is unavailable and no local inference fallback is configured")

    async def close(self) -> None:
        """Close active HTTP resources."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
