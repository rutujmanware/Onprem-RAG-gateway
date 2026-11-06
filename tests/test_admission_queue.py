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

    print("✅ Admission queue concurrency and timeout test passed.")

if __name__ == "__main__":
    asyncio.run(test_admission_concurrency_and_timeout())
