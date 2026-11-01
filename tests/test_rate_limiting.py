import asyncio
from gateway.queue.admission import TokenBucketLimiter

async def test_burst():
    limiter = TokenBucketLimiter(capacity=2, refill_rate=1.0)
    assert await limiter.acquire("u1") is True
    assert await limiter.acquire("u1") is True
    assert await limiter.acquire("u1") is False

if __name__ == "__main__":
    asyncio.run(test_burst())
    print("Rate limiting tests passed.")
