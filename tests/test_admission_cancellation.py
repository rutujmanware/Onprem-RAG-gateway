"""DEFECT-34: admission permits are never lost on cancellation, timeout or early disconnect.

Reported: acquire() took the global permit and then awaited the lock twice before returning; a request cancelled
in that window (client disconnect) kept the permit forever, and after `global_cap` such leaks an idle worker
answered every request with 503. Also: the streaming routes acquire before returning the response and released
only inside the generator, which never runs if the client disconnects before streaming starts.
"""

import asyncio
import random

import pytest

from gateway.queue.admission import AdmissionController, TokenBucketLimiter


def controller(cap=1, timeout=1.0):
    return AdmissionController(global_concurrency_cap=cap, per_user_concurrency_cap=1000, max_queue_depth=1000,
                               queue_timeout_seconds=timeout,
                               rate_limiter=TokenBucketLimiter(capacity=10**6, refill_rate=10**6), use_redis=False)


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def assert_no_leak(ac, cap):
    assert ac.global_sem._value == cap, f"permits {ac.global_sem._value}/{cap}"
    assert ac._active_count == 0 and ac._queued_count == 0


@pytest.mark.parametrize("yields_before_cancel", range(0, 6))
def test_reported_scenario_cancel_right_after_the_permit_is_granted(yields_before_cancel):
    """Holder admitted, victim waiting; holder releases and the victim is cancelled at every possible point."""
    async def main():
        ac = controller(cap=1)
        holder = await ac.acquire("holder")
        victim = asyncio.ensure_future(ac.acquire("victim"))
        await settle()
        assert ac._queued_count == 1
        ac.release(holder)
        for _ in range(yields_before_cancel):
            await asyncio.sleep(0)
        victim.cancel()
        try:
            ticket = await victim          # it may have completed before the cancel landed
            ac.release(ticket)
        except asyncio.CancelledError:
            pass
        await settle()
        assert_no_leak(ac, 1)
        await asyncio.wait_for(ac.acquire("fresh"), 0.5)   # an idle controller admits immediately
    asyncio.run(main())


def test_cancel_while_waiting_and_timeout_keep_capacity():
    async def main():
        ac = controller(cap=1, timeout=0.1)
        holder = await ac.acquire("holder")
        waiter = asyncio.ensure_future(ac.acquire("w"))
        await settle()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        with pytest.raises(Exception) as e:                 # times out behind the holder
            await ac.acquire("late")
        assert getattr(e.value, "status_code", None) == 503
        ac.release(holder)
        await settle()
        assert_no_leak(ac, 1)
    asyncio.run(main())


def test_release_is_idempotent_per_ticket():
    async def main():
        ac = controller(cap=2)
        t = await ac.acquire("u")
        ac.release(t)
        ac.release(t)                                       # generator finally + response close
        await settle()
        assert_no_leak(ac, 2)
    asyncio.run(main())


def test_random_cancellations_and_timeouts_never_lose_a_permit():
    async def main():
        rnd = random.Random(7)
        cap = 3
        ac = controller(cap=cap, timeout=0.02)

        async def request(i):
            try:
                t = await ac.acquire(f"u{i % 17}")
            except Exception:
                return
            try:
                await asyncio.sleep(rnd.random() * 0.01)
            finally:
                ac.release(t)

        tasks = []
        for i in range(300):
            tasks.append(asyncio.ensure_future(request(i)))
            if rnd.random() < 0.5:
                await asyncio.sleep(0)
            if tasks and rnd.random() < 0.4:
                rnd.choice(tasks).cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0.05)
        assert_no_leak(ac, cap)
    asyncio.run(main())


# ------------------------------------------------------------ streaming response releases its slot
def test_streaming_response_releases_when_the_client_disconnects_before_streaming():
    from gateway.main import ReleasingStreamingResponse

    async def main():
        ac = controller(cap=1)
        ticket = await ac.acquire("u")
        ran = []

        def gen():                      # its finally never runs if streaming never starts
            try:
                ran.append(1)
                yield "data: x\n\n"
            finally:
                ac.release(ticket)

        resp = ReleasingStreamingResponse(gen(), media_type="text/event-stream", on_close=lambda: ac.release(ticket))

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            raise OSError("client went away")      # fails on the very first message

        with pytest.raises(BaseException):
            await resp({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        await settle()
        assert ran == [] and ac.global_sem._value == 1, "slot released although the generator never ran"
    asyncio.run(main())


def test_streaming_response_normal_completion_releases_exactly_once():
    from gateway.main import ReleasingStreamingResponse

    async def main():
        ac = controller(cap=1)
        ticket = await ac.acquire("u")

        def gen():
            try:
                yield "data: x\n\n"
            finally:
                ac.release(ticket)

        resp = ReleasingStreamingResponse(gen(), media_type="text/event-stream", on_close=lambda: ac.release(ticket))
        sent = []

        async def receive():
            await asyncio.sleep(10)
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message["type"])

        await resp({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        await settle()
        assert "http.response.body" in sent
        assert_no_leak(ac, 1)
    asyncio.run(main())
