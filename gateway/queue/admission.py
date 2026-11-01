import asyncio
import logging
import time
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
    def __init__(self, global_concurrency_cap=30, per_user_concurrency_cap=4, rate_limiter=None):
        self.global_sem = asyncio.Semaphore(global_concurrency_cap)
        self.per_user_cap = per_user_concurrency_cap
        self.user_sems: Dict[str, asyncio.Semaphore] = {}
        self.rate_limiter = rate_limiter or TokenBucketLimiter()
        self._lock = asyncio.Lock()

    async def check_admission(self, user_id: str):
        if not await self.rate_limiter.acquire(user_id):
            raise HTTPException(status_code=429, detail="Rate limit exceeded", headers={"Retry-After": "2"})

    async def acquire_slot(self, user_id: str):
        await self.global_sem.acquire()

    async def release_slot(self, user_id: str):
        self.global_sem.release()
