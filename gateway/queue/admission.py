import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Dict, Optional
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

logger = logging.getLogger(__name__)

class TokenBucketLimiter:
    def __init__(self, capacity=20, refill_rate=5.0):
        self.capacity = float(capacity)
        self.refill_rate = refill_rate
        self._tokens: Dict[str, float] = {}
        self._last_refill: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, client_id: str, cost: float = 1.0) -> bool:
        async with self._lock:
            now = time.time()
            if client_id not in self._tokens:
                self._tokens[client_id] = self.capacity
                self._last_refill[client_id] = now
            else:
                elapsed = now - self._last_refill[client_id]
                self._tokens[client_id] = min(self.capacity, self._tokens[client_id] + elapsed * self.refill_rate)
                self._last_refill[client_id] = now
            if self._tokens[client_id] >= cost:
                self._tokens[client_id] -= cost
                return True
            return False

class AdmissionController:
    """Admission control for the scarce GPU tier.

    A request is rate-limited before it consumes a queue position, then must hold
    both a global and per-user semaphore for its whole model invocation.
    """

    def __init__(
        self,
        global_concurrency_cap=12,
        per_user_concurrency_cap=4,
        rate_limiter=None,
        max_queue_depth=100,
        queue_wait_timeout_seconds=15.0,
    ):
        self.global_sem = asyncio.Semaphore(global_concurrency_cap)
        self.per_user_cap = per_user_concurrency_cap
        self.user_sems: Dict[str, asyncio.Semaphore] = {}
        self.rate_limiter = rate_limiter or TokenBucketLimiter()
        self.queue_slots = asyncio.BoundedSemaphore(max_queue_depth)
        self.queue_wait_timeout_seconds = queue_wait_timeout_seconds
        self._lock = asyncio.Lock()

    async def check_admission(self, user_id: str):
        if not await self.rate_limiter.acquire(user_id):
            raise HTTPException(status_code=429, detail="Rate limit exceeded", headers={"Retry-After": "2"})

    async def _user_sem(self, user_id: str) -> asyncio.Semaphore:
        async with self._lock:
            return self.user_sems.setdefault(user_id, asyncio.Semaphore(self.per_user_cap))

    async def acquire_slot(self, user_id: str):
        try:
            await asyncio.wait_for(self.queue_slots.acquire(), timeout=0.01)
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GPU request queue is full",
            ) from exc

        user_sem = await self._user_sem(user_id)
        acquired_global = False
        acquired_user = False
        try:
            await asyncio.wait_for(self.global_sem.acquire(), self.queue_wait_timeout_seconds)
            acquired_global = True
            await asyncio.wait_for(user_sem.acquire(), self.queue_wait_timeout_seconds)
            acquired_user = True
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GPU queue wait timeout exceeded",
            ) from exc
        finally:
            if not acquired_user and acquired_global:
                self.global_sem.release()
            if not acquired_user:
                self.queue_slots.release()
        return user_sem

    async def release_slot(self, user_id: str, user_sem: Optional[asyncio.Semaphore] = None):
        (user_sem or await self._user_sem(user_id)).release()
        self.global_sem.release()
        self.queue_slots.release()

    @asynccontextmanager
    async def gpu_slot(self, user_id: str, check_rate: bool = True):
        if check_rate:
            await self.check_admission(user_id)
        user_sem = await self.acquire_slot(user_id)
        try:
            yield
        finally:
            await self.release_slot(user_id, user_sem)
