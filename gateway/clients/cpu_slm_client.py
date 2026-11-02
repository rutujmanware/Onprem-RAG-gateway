import os
from typing import Any, Dict, List, Optional
import httpx

DEFAULT_SLM_BASE_URL = os.getenv("SLM_BASE_URL", "http://localhost:8082/v1")
DEFAULT_SLM_MODEL = os.getenv("SLM_MODEL_NAME", "Qwen/Qwen2.5-3B-Instruct")


class CPUSLMClient:
    """Asynchronous HTTP client for the on-premise CPU SLM server."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        model_name: Optional[str] = None,
        timeout: float = 60.0,
    ):
        self.base_url = (base_url or DEFAULT_SLM_BASE_URL).rstrip("/")
        self.model_name = model_name or DEFAULT_SLM_MODEL
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    async def check_health(self) -> bool:
        """Check if CPU SLM service is up and responsive."""
        try:
            # First try root /healthz on base server
            server_root = self.base_url.rsplit("/v1", 1)[0]
            async with httpx.AsyncClient(timeout=3.0) as temp_client:
                r = await temp_client.get(f"{server_root}/healthz")
                if r.status_code == 200:
                    return True
            # Fallback to /models
            resp = await self.client.get("/models")
            return resp.status_code == 200
        except Exception:
            return False

    async def generate_chat(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 160,
        temperature: float = 0.2,
        top_p: float = 0.8,
        **kwargs,
    ) -> Dict[str, Any]:
        """Call OpenAI-compatible /chat/completions on CPU SLM server."""
        payload = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stream": False,
            **kwargs,
        }

        resp = await self.client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        return resp.json()

    async def generate_chat_stream(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 160,
        temperature: float = 0.2,
        top_p: float = 0.8,
        **kwargs,
    ):
        """Asynchronously stream token deltas from CPU SLM server."""
        import json

        payload = {
            "model": self.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stream": True,
            **kwargs,
        }

        async with self.client.stream("POST", "/chat/completions", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                line = line.strip()
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta
                except Exception:
                    continue

    async def close(self):
        """Close HTTP client session."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
