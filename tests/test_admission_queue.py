import asyncio
import pytest
from gateway.queue.admission import AdmissionController, TokenBucketLimiter

@pytest.mark.asyncio
async def test_admission_concurrency_and_timeout():
    # Setup controller with max 2 active slots, max 2 queue depth, 0.2s queue timeout
    controller = AdmissionController(
        global_concurrency_cap=2,
        max_queue_depth=2,
        queue_timeout_seconds=0.3,
        rate_limiter=TokenBucketLimiter(capacity=100, refill_rate=100)
    )

    active_tasks = []
    task_results = []

    async def mock_worker(worker_id: int, hold_time: float):
        try:
            async with controller.slot(user_id=f"user_{worker_id}"):
                task_results.append((worker_id, "started"))
                await asyncio.sleep(hold_time)
                task_results.append((worker_id, "finished"))
        except Exception as e:
            task_results.append((worker_id, f"error: {type(e).__name__}"))

    # Launch 2 workers that hold slots for 0.5s
    t1 = asyncio.create_task(mock_worker(1, 0.5))
    t2 = asyncio.create_task(mock_worker(2, 0.5))
    await asyncio.sleep(0.05)

    stats = controller.get_stats()
    assert stats["active_inferences"] == 2
    assert stats["queued_requests"] == 0

    # Launch 3rd worker (should queue and timeout because hold_time 0.5s > timeout 0.3s)
    t3 = asyncio.create_task(mock_worker(3, 0.1))
    await asyncio.sleep(0.1)

    stats_q = controller.get_stats()
    assert stats_q["queued_requests"] == 1

    await asyncio.gather(t1, t2, t3)

    results_dict = dict(task_results)
    assert results_dict[1] == "finished"
    assert results_dict[2] == "finished"
    assert "error: HTTPException" in results_dict[3]  # Timed out with HTTP 503

    # Now verify slots are released
    stats_after = controller.get_stats()
    assert stats_after["active_inferences"] == 0
    assert stats_after["queued_requests"] == 0

    await controller.acquire(user_id="recovery")
    assert controller.get_stats()["active_inferences"] == 1
    controller.release()
    assert controller.get_stats()["active_inferences"] == 0

    print("✅ Admission queue concurrency and timeout test passed.")


@pytest.mark.asyncio
async def test_cancel_after_semaphore_acquire_returns_permit(monkeypatch):
    controller = AdmissionController(
        global_concurrency_cap=1,
        max_queue_depth=2,
        queue_timeout_seconds=1,
        rate_limiter=TokenBucketLimiter(capacity=10, refill_rate=10),
    )
    real_wait = asyncio.wait

    async def cancel_after_permit(
        futures, *, timeout=None, return_when=asyncio.ALL_COMPLETED
    ):
        done, pending = await real_wait(futures, timeout=timeout, return_when=return_when)
        assert done
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)
        return done, pending

    monkeypatch.setattr(asyncio, "wait", cancel_after_permit)
    task = asyncio.create_task(controller.acquire(user_id="cancelled"))
    with pytest.raises(asyncio.CancelledError):
        await task

    monkeypatch.setattr(asyncio, "wait", real_wait)
    assert controller.global_sem._value == 1
    assert controller.get_stats()["active_inferences"] == 0
    assert controller.get_stats()["queued_requests"] == 0

    await controller.acquire(user_id="recovery")
    assert controller.get_stats()["active_inferences"] == 1
    controller.release()
    assert controller.global_sem._value == 1
    assert controller.get_stats()["active_inferences"] == 0


@pytest.mark.asyncio
async def test_cancelled_queued_waiter_clears_queue_count():
    controller = AdmissionController(
        global_concurrency_cap=1,
        max_queue_depth=2,
        queue_timeout_seconds=1,
        rate_limiter=TokenBucketLimiter(capacity=10, refill_rate=10),
    )
    await controller.acquire(user_id="holder")
    waiter = asyncio.create_task(controller.acquire(user_id="cancelled"))
    while controller.get_stats()["queued_requests"] == 0:
        await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert controller.get_stats()["active_inferences"] == 1
    assert controller.get_stats()["queued_requests"] == 0
    controller.release()
    await controller.acquire(user_id="recovery")
    assert controller.get_stats()["active_inferences"] == 1
    controller.release()
    assert controller.global_sem._value == 1

if __name__ == "__main__":
    asyncio.run(test_admission_concurrency_and_timeout())
