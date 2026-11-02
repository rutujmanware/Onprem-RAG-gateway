"""Async client for the intranet vLLM OpenAI-compatible endpoint."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import httpx


class VLLMClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
        timeout: float = 90.0,
    ):
        self.base_url = (base_url or os.getenv("VLLM_BASE_URL", "http://vllm:8000/v1")).rstrip("/")
        self.model_name = model_name or os.getenv("VLLM_MODEL_NAME", "Qwen2.5-7B-Instruct")
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def check_health(self) -> bool:
        try:
            response = await self._client_or_create().get(f"{self.base_url}/models")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def generate_chat(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 512,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        payload = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            **kwargs,
        }
        response = await self._client_or_create().post(
            f"{self.base_url}/chat/completions", json=payload
        )
        response.raise_for_status()
        return response.json()

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
