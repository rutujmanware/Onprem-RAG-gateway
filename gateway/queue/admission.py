"""Enterprise Admission Controller and Concurrency Queue Manager for On-Premise RAG Gateway.

Protects downstream compute engines (vLLM GPU and CPU SLM) against overload:
1. Token-Bucket Rate Limiter per client/user.
2. Global Concurrency Cap for active model inferences.
3. Bounded FIFO/Fair Queue with maximum depth and wait timeout.
4. Clean HTTP 429 (Rate Limit) and HTTP 503 (Queue Full / Timeout) shedding.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

try:
    from fastapi import HTTPException, status
except ImportError:
    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str = "", headers: Optional[dict] = None):
            self.status_code = status_code
            self.detail = detail
            self.headers = headers or {}
    class status:
        HTTP_429_TOO_MANY_REQUESTS = 429
        HTTP_503_SERVICE_UNAVAILABLE = 503

logger = logging.getLogger("gateway.admission")


class TokenBucketLimiter:
    """Thread-safe and async-safe token bucket rate limiter per client/user."""

    def __init__(self, capacity: float = 60.0, refill_rate: float = 20.0):
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self._tokens: Dict[str, float] = {}
        self._last_refill: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, client_id: str, cost: float = 1.0) -> bool:
        # Loopback and internal frontend proxy are exempted from per-IP rate limit
        if client_id in ("127.0.0.1", "localhost", "internal", "::1"):
            return True

        async with self._lock:
            now = time.time()
            if client_id not in self._tokens:
                self._tokens[client_id] = self.capacity
                self._last_refill[client_id] = now
            else:
                elapsed = now - self._last_refill[client_id]
                self._tokens[client_id] = min(
                    self.capacity,
                    self._tokens[client_id] + elapsed * self.refill_rate
                )
                self._last_refill[client_id] = now

            if self._tokens[client_id] >= cost:
                self._tokens[client_id] -= cost
                return True
            return False


class AdmissionController:
    """Manages active inference slots and enqueues overflow requests under high load."""

    def __init__(
        self,
        global_concurrency_cap: Optional[int] = None,
        per_user_concurrency_cap: Optional[int] = None,
        max_queue_depth: Optional[int] = None,
        queue_timeout_seconds: Optional[float] = None,
        rate_limiter: Optional[TokenBucketLimiter] = None,
    ):
        total_global_cap = int(os.getenv("GATEWAY_MAX_ACTIVE_INFERENCES", "42"))
        num_workers = int(os.getenv("GATEWAY_WORKERS", os.getenv("UVICORN_WORKERS", "6")))
        default_worker_cap = max(1, total_global_cap // num_workers)
        self.global_cap = global_concurrency_cap or default_worker_cap

        total_queue_depth = int(os.getenv("GATEWAY_MAX_QUEUE_DEPTH", "120"))
        default_worker_queue = max(1, total_queue_depth // num_workers)
        self.max_queue_depth = max_queue_depth or default_worker_queue

        self.per_user_cap = per_user_concurrency_cap or int(os.getenv("GATEWAY_PER_USER_CAP", "6"))
        self.queue_timeout = queue_timeout_seconds or float(os.getenv("GATEWAY_QUEUE_TIMEOUT_SECS", "30.0"))

        self.global_sem = asyncio.Semaphore(self.global_cap)
        self.rate_limiter = rate_limiter or TokenBucketLimiter()

        self._active_count = 0
        self._queued_count = 0
        self._total_admitted = 0
        self._total_shed = 0
        self._total_timeouts = 0
        self._lock = asyncio.Lock()

    async def acquire(self, user_id: str = "anonymous", cost: float = 1.0) -> None:
        """Acquire an active inference slot or queue if at capacity.
        
        Raises:
            HTTPException(429): If client rate limit is exceeded.
            HTTPException(503): If the queue is at maximum capacity (shedding)
                                or queue wait times out.
        """
        # 1. Per-client Token Bucket Rate Limit
        allowed = await self.rate_limiter.acquire(user_id, cost=cost)
        if not allowed:
            self._total_shed += 1
            logger.warning("Rate limit exceeded for user %s", user_id)
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Please wait a moment before sending more queries.",
                headers={"Retry-After": "2"}
            )

        # 2. Queue Depth Check (Prevent unbounded memory backlog)
        async with self._lock:
            if self._queued_count >= self.max_queue_depth:
                self._total_shed += 1
                logger.warning(
                    "Gateway admission queue full (%d queued, %d active). Shedding load for %s.",
                    self._queued_count, self._active_count, user_id
                )
                raise HTTPException(
                    status_code=503,
                    detail="Campus Gateway is currently experiencing heavy load. Queue is full, please retry shortly.",
                    headers={"Retry-After": "5"}
                )
            self._queued_count += 1

        # 3. Wait in queue for available inference slot with timeout
        try:
            try:
                await asyncio.wait_for(self.global_sem.acquire(), timeout=self.queue_timeout)
            except asyncio.TimeoutError:
                self._total_timeouts += 1
                logger.warning(
                    "Request from user %s timed out after %.1fs waiting in admission queue.",
                    user_id, self.queue_timeout
                )
                raise HTTPException(
                    status_code=503,
                    detail="Inference queue wait timed out. Model servers are busy, please retry shortly.",
                    headers={"Retry-After": "3"}
                )
        finally:
            async with self._lock:
                self._queued_count -= 1

        # 4. In-flight Execution
        async with self._lock:
            self._active_count += 1
            self._total_admitted += 1

    def release(self) -> None:
        """Release the acquired inference slot."""
        self.global_sem.release()
        self._active_count = max(0, self._active_count - 1)

    @asynccontextmanager
    async def slot(self, user_id: str = "anonymous", cost: float = 1.0):
        """Async context manager: acquires a queue position and execution slot."""
        await self.acquire(user_id=user_id, cost=cost)
        try:
            yield
        finally:
            self.release()

    # Backward-compatibility helpers
    async def check_admission(self, user_id: str):
        if not await self.rate_limiter.acquire(user_id):
            raise HTTPException(status_code=429, detail="Rate limit exceeded", headers={"Retry-After": "2"})

    async def acquire_slot(self, user_id: str):
        await self.global_sem.acquire()

    async def release_slot(self, user_id: str):
        self.release()

    def get_stats(self) -> Dict[str, Any]:
        """Return real-time telemetry on active inference and queued requests."""
        return {
            "status": "active",
            "max_active_inferences": self.global_cap,
            "active_inferences": self._active_count,
            "max_queue_depth": self.max_queue_depth,
            "queued_requests": self._queued_count,
            "queue_timeout_seconds": self.queue_timeout,
            "total_admitted": self._total_admitted,
            "total_shed": self._total_shed,
            "total_timeouts": self._total_timeouts,
        }


_default_admission_controller: Optional[AdmissionController] = None


def get_admission_controller() -> AdmissionController:
    """FastAPI dependency provider for AdmissionController singleton."""
    global _default_admission_controller
    if _default_admission_controller is None:
        _default_admission_controller = AdmissionController()
    return _default_admission_controller
