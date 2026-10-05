import hashlib
import json
import logging
from typing import Any, Dict, Optional
try:
    import redis.asyncio as redis
except ImportError:
    redis = None

logger = logging.getLogger(__name__)

class L0ExactCache:
    def __init__(self, redis_client=None, default_ttl=3600, prefix="l0:exact:"):
        self.redis = redis_client
        self.default_ttl = default_ttl
        self.prefix = prefix
        self._local_fallback: Dict[str, str] = {}

    @staticmethod
    def compute_cache_key(prompt: str, model_params: Optional[Dict[str, Any]] = None) -> str:
        normalized = " ".join(prompt.strip().lower().split())
        payload = {"prompt": normalized}
        if model_params:
            payload["params"] = {k: v for k, v in sorted(model_params.items())}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    async def get(self, prompt: str, model_params=None) -> Optional[Dict[str, Any]]:
        key = self.prefix + self.compute_cache_key(prompt, model_params)
        if self.redis:
            try:
                data = await self.redis.get(key)
                if data: return json.loads(data)
            except Exception: pass
        if key in self._local_fallback:
            return json.loads(self._local_fallback[key])
        return None

    async def set(self, prompt: str, response: Dict[str, Any], model_params=None, ttl=None) -> None:
        key = self.prefix + self.compute_cache_key(prompt, model_params)
        val = json.dumps(response)
        if self.redis:
            try:
                await self.redis.set(key, val, ex=ttl or self.default_ttl)
                return
            except Exception: pass
        self._local_fallback[key] = val
