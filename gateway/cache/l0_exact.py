import hashlib
import json
from typing import Any, Dict, Optional

from gateway.cache.entity_guard import EntityGuard
from gateway.cache.validator import VolatileQueryDetector


class L0ExactCache:
    """FR-2: Exact-match cache scoped by (thesis_id, chapter, normalized_prompt)."""

    def __init__(
        self,
        redis_client: Any = None,
        ttl_seconds: int = 3600,
        prefix: str = "l0:exact:",
        enable_volatile_guard: bool = True,
    ):
        self.redis = redis_client
        self.ttl_seconds = int(ttl_seconds)
        self.prefix = prefix
        self.enable_volatile_guard = enable_volatile_guard

    def _make_key(
        self, prompt: str, thesis_id: str = "global", chapter: str = "all", doc_version: str = "v1"
    ) -> str:
        norm_prompt = " ".join(prompt.strip().lower().split())
        norm_thesis = EntityGuard.normalize_thesis_id(thesis_id)
        norm_chapter = EntityGuard.normalize_chapter(chapter)
        digest = hashlib.sha256(
            f"{norm_thesis}|{norm_chapter}|{doc_version}|{norm_prompt}".encode("utf-8")
        ).hexdigest()
        return f"{self.prefix}{norm_thesis}:{norm_chapter}:{doc_version}:{digest}"

    async def get(
        self,
        prompt: str,
        thesis_id: str = "global",
        chapter: str = "all",
        doc_version: str = "v1",
        freshness_handled: bool = False,
    ) -> Optional[Dict[str, Any]]:
        if self.redis is None:
            return None
        # Avoid caching volatile/changing information (stocks, weather, live availability)
        if self.enable_volatile_guard and not freshness_handled and VolatileQueryDetector.is_volatile(prompt):
            return None
        key = self._make_key(prompt, thesis_id, chapter, doc_version)
        raw = await self.redis.get(key)
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        return json.loads(raw)

    async def set(
        self,
        prompt: str,
        response: Dict[str, Any],
        thesis_id: str = "global",
        chapter: str = "all",
        doc_version: str = "v1",
        freshness_handled: bool = False,
    ) -> str:
        if self.redis is None:
            return ""
        # Avoid caching volatile/changing information
        if self.enable_volatile_guard and not freshness_handled and VolatileQueryDetector.is_volatile(prompt):
            return ""
        key = self._make_key(prompt, thesis_id, chapter, doc_version)
        payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
        await self.redis.setex(key, self.ttl_seconds, payload)
        version_set = f"{self.prefix}versions:{EntityGuard.normalize_thesis_id(thesis_id)}:{doc_version}"
        if hasattr(self.redis, "sadd"):
            await self.redis.sadd(version_set, key)
            if hasattr(self.redis, "expire"):
                await self.redis.expire(version_set, self.ttl_seconds)
        return key

    async def invalidate_version(self, thesis_id: str, doc_version: str) -> int:
        """Delete all exact entries derived from one superseded revision."""
        if self.redis is None or not hasattr(self.redis, "smembers"):
            return 0
        version_set = f"{self.prefix}versions:{EntityGuard.normalize_thesis_id(thesis_id)}:{doc_version}"
        keys = list(await self.redis.smembers(version_set))
        if keys:
            await self.redis.delete(*keys)
        await self.redis.delete(version_set)
        return len(keys)
