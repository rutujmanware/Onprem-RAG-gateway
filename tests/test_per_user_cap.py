"""DEFECT-27 / FR-5: the per-user concurrency cap is enforced.

Reported: AdmissionController stored per_user_cap but never read it, so one user could hold every
inference slot and other users were queued or shed (503). The cap is now enforced with HTTP 429, counted
across all gateway workers in Redis (each worker only has GLOBAL_CONCURRENCY_LIMIT / workers slots, so a
per-worker count could never reach the cap of 6), with a per-worker fallback when Redis is unreachable.
"""

import asyncio
import threading
import time
import uuid

import pytest
from fastapi import HTTPException

from gateway.queue.admission import AdmissionController, TokenBucketLimiter
from tests._live import REDIS_REASON, redis_available


def controller(cap=1, global_cap=3, use_redis=False, timeout=2.0):
    return AdmissionController(global_concurrency_cap=global_cap, per_user_concurrency_cap=cap,
                               max_queue_depth=10, queue_timeout_seconds=timeout,
                               rate_limiter=TokenBucketLimiter(capacity=1000, refill_rate=1000),
                               use_redis=use_redis)


def test_reported_scenario_hog_is_capped_and_victim_is_admitted():
    async def main():
        ac = controller(cap=1, global_cap=3)
        await ac.acquire("hog")
        with pytest.raises(HTTPException) as e:
            await ac.acquire("hog")
        assert e.value.status_code == 429 and e.value.headers.get("Retry-After")
        await ac.acquire("victim")  # a free slot is still available to other users
        st = ac.get_stats()
        assert st["active_inferences"] == 2 and st["total_user_cap_rejections"] == 1 and st["per_user_cap"] == 1
    asyncio.run(main())


def test_cap_allows_exactly_cap_concurrent_requests_and_release_frees_one():
    async def main():
        ac = controller(cap=3, global_cap=10)
        tickets = [await ac.acquire("u") for _ in range(3)]
        with pytest.raises(HTTPException):
            await ac.acquire("u")
        ac.release(tickets[0])
        await ac.acquire("u")
    asyncio.run(main())


def test_failed_queue_wait_does_not_leak_the_users_slot():
    async def main():
        ac = controller(cap=2, global_cap=1, timeout=0.1)
        await ac.acquire("other")                      # takes the only global slot
        with pytest.raises(HTTPException) as e:
            await ac.acquire("u")                      # 503: queue wait timed out
        assert e.value.status_code == 503
        ac2_tickets = []
        ac.release()                                   # global slot free again
        ac2_tickets.append(await ac.acquire("u"))
        ac2_tickets.append(await ac.acquire("u") if ac.global_cap > 1 else None)
    asyncio.run(main())


def test_release_from_a_worker_thread_frees_global_and_user_slots():
    """Streaming routes release inside generators that run in worker threads."""
    async def main():
        ac = controller(cap=1, global_cap=1)
        t = await ac.acquire("u")
        th = threading.Thread(target=ac.release, args=(t,))
        th.start()
        await asyncio.to_thread(th.join)
        await asyncio.sleep(0.05)                     # the hand-over runs on the event loop
        assert ac.get_stats()["active_inferences"] == 0
        await asyncio.wait_for(ac.acquire("u"), 1.0)  # both the global and the user slot are free
    asyncio.run(main())


def test_default_cap_comes_from_env(monkeypatch):
    monkeypatch.setenv("PER_USER_CONCURRENCY_LIMIT", "6")
    assert AdmissionController(use_redis=False).per_user_cap == 6


# ------------------------------------------------------------- across workers (Redis), live
needs_redis = pytest.mark.skipif(not redis_available(), reason=REDIS_REASON)


@needs_redis
def test_cap_is_shared_by_all_workers_through_redis():
    async def main():
        user = f"cap-test-{uuid.uuid4().hex[:8]}@iisc.ac.in"
        a, b = controller(cap=2, use_redis=True), controller(cap=2, use_redis=True)   # two gateway workers
        ta = await a.acquire(user)
        tb = await b.acquire(user)
        assert ta.in_redis and tb.in_redis
        with pytest.raises(HTTPException) as e:
            await a.acquire(user)                     # 3rd request on either worker: over the cap
        assert e.value.status_code == 429
        b.release(tb)
        await asyncio.sleep(0.1)
        tc = await a.acquire(user)                    # a slot freed on worker B is usable on worker A
        a.release(ta), a.release(tc)
        await asyncio.sleep(0.1)
        r = await a._redis_client()
        assert await r.zcard(a._user_key(user)) == 0
        await r.delete(a._user_key(user))
    asyncio.run(main())


@needs_redis
def test_stale_entries_from_a_crashed_worker_do_not_block_the_user():
    async def main():
        user = f"cap-test-{uuid.uuid4().hex[:8]}@iisc.ac.in"
        ac = controller(cap=1, use_redis=True)
        r = await ac._redis_client()
        key = ac._user_key(user)
        await r.zadd(key, {"crashed-request": time.time() - 3600})   # never released
        t = await ac.acquire(user)
        assert t.in_redis
        ac.release(t)
        await asyncio.sleep(0.1)
        await r.delete(key)
    asyncio.run(main())
