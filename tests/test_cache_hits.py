import asyncio
import pytest
from gateway.cache.redis_semantic_cache import RedisSemanticCache

@pytest.mark.asyncio
async def test_semantic_cache():
    cache = RedisSemanticCache.get_instance()
    query = "What is the primary function of vermiculite in industry?"
    response = {
        "answer": "Vermiculite is used in thermal insulation, fireproofing, and agricultural soil conditioning.",
        "route": "cached"
    }

    # Test sync store and lookup
    cache.store(query, response)
    hit = cache.lookup(query)
    assert hit is not None, "Cache lookup failed for exact query."
    assert hit["cached"] is True, "Result was not marked as cached."

    # Test async store and lookup
    await cache.astore(query, response)
    async_hit = await cache.alookup(query)
    assert async_hit is not None, "Async cache lookup failed."
    assert async_hit["cached"] is True, "Async result was not marked as cached."

if __name__ == "__main__":
    asyncio.run(test_semantic_cache())
    print("Redis semantic cache hit tests passed.")
