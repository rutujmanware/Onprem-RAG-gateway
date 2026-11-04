from gateway.cache.redis_semantic_cache import RedisSemanticCache

# Backward-compatible alias for existing imports
L1SemanticCache = RedisSemanticCache

__all__ = ["RedisSemanticCache", "L1SemanticCache"]
