import asyncio
import pytest
from gateway.queue.admission import TokenBucketLimiter

@pytest.mark.asyncio
async def test_burst():
    limiter = TokenBucketLimiter(capacity=2, refill_rate=1.0)
    assert await limiter.acquire("u1") is True
    assert await limiter.acquire("u1") is True
    assert await limiter.acquire("u1") is False

@pytest.mark.asyncio
async def test_loopback_rate_limited():
    """Verify that loopback client IDs (127.0.0.1, localhost) are fairly bounded and cannot spam infinitely (VULN-03 fix)."""
    limiter = TokenBucketLimiter(capacity=2, refill_rate=1.0)
    assert await limiter.acquire("127.0.0.1") is True
    assert await limiter.acquire("127.0.0.1") is True
    # Previously, loopback was unconditionally exempted and would always return True
    assert await limiter.acquire("127.0.0.1") is False

@pytest.mark.asyncio
async def test_distinct_user_accounts_isolated():
    """Verify that rate limits are tracked independently per user account."""
    limiter = TokenBucketLimiter(capacity=2, refill_rate=1.0)
    # User A exhausts their bucket
    assert await limiter.acquire("user_a@iisc.ac.in") is True
    assert await limiter.acquire("user_a@iisc.ac.in") is True
    assert await limiter.acquire("user_a@iisc.ac.in") is False

    # User B still has their full quota
    assert await limiter.acquire("user_b@iisc.ac.in") is True
    assert await limiter.acquire("user_b@iisc.ac.in") is True
    assert await limiter.acquire("user_b@iisc.ac.in") is False

if __name__ == "__main__":
    asyncio.run(test_burst())
    asyncio.run(test_loopback_rate_limited())
    asyncio.run(test_distinct_user_accounts_isolated())
    print("Rate limiting tests passed.")
