"""Enterprise Admission Controller and Concurrency Queue Manager for On-Premise RAG Gateway.

Protects downstream compute engines (vLLM GPU and CPU SLM) against overload:
1. Token-Bucket Rate Limiter per client/user.
2. Global Concurrency Cap for active model inferences, and a per-user concurrency cap (FR-5) counted
   across all gateway workers in Redis (per-worker fallback when Redis is unreachable).
3. Bounded FIFO/Fair Queue with maximum depth and wait timeout.
4. Clean HTTP 429 (Rate Limit) and HTTP 503 (Queue Full / Timeout) shedding.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
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

    def __init__(self, capacity: Optional[float] = None, refill_rate: Optional[float] = None):
        if capacity is None:
            capacity = float(os.getenv("RATE_LIMIT_BURST", "60.0"))
        if refill_rate is None:
            refill_rate = float(os.getenv("RATE_LIMIT_RPS", "20.0"))
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self._tokens: Dict[str, float] = {}
        self._last_refill: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, client_id: str, cost: float = 1.0) -> bool:
        # VULN-03 Fix: Rate limit every client/user account fairly.
        # Do not allow unconditional rate limit bypass for loopback or web chat traffic.
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


# Per-user active inferences across workers: one sorted set per user, member = request id, score = start
# time. Entries older than USER_SLOT_MAX_AGE (a crashed worker never released them) are dropped first.
_USER_SLOT_ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return 1
"""
USER_SLOT_MAX_AGE = float(os.getenv("PER_USER_SLOT_MAX_AGE_SECONDS", "600"))


@dataclass
class AdmissionTicket:
    """Returned by acquire(); pass it to release() so the user's slot is freed too."""
    user_key: str
    member: str
    in_redis: bool
    has_global: bool = True


class AdmissionController:
    """Manages active inference slots and enqueues overflow requests under high load."""

    def __init__(
        self,
        global_concurrency_cap: Optional[int] = None,
        per_user_concurrency_cap: Optional[int] = None,
        max_queue_depth: Optional[int] = None,
        queue_timeout_seconds: Optional[float] = None,
        rate_limiter: Optional[TokenBucketLimiter] = None,
        redis_url: Optional[str] = None,
        use_redis: Optional[bool] = None,
    ):
        self._use_redis = (os.getenv("ADMISSION_USE_REDIS", "1") != "0") if use_redis is None else use_redis
        total_global_cap = int(os.getenv("GLOBAL_CONCURRENCY_LIMIT", os.getenv("GATEWAY_MAX_ACTIVE_INFERENCES", "30")))
        num_workers = int(os.getenv("GATEWAY_WORKERS", os.getenv("UVICORN_WORKERS", "6")))
        default_worker_cap = max(1, total_global_cap // num_workers)
        self.global_cap = global_concurrency_cap or default_worker_cap

        total_queue_depth = int(os.getenv("MAX_QUEUE_DEPTH", os.getenv("GATEWAY_MAX_QUEUE_DEPTH", "120")))
        default_worker_queue = max(1, total_queue_depth // num_workers)
        self.max_queue_depth = max_queue_depth or default_worker_queue

        self.per_user_cap = per_user_concurrency_cap or int(os.getenv("PER_USER_CONCURRENCY_LIMIT", os.getenv("GATEWAY_PER_USER_CAP", "6")))
        self.queue_timeout = queue_timeout_seconds or float(os.getenv("QUEUE_TIMEOUT_SECONDS", os.getenv("GATEWAY_QUEUE_TIMEOUT_SECS", "30.0")))

        self.global_sem = asyncio.Semaphore(self.global_cap)
        self.rate_limiter = rate_limiter or TokenBucketLimiter()

        # FR-5 per-user cap (DEFECT-27): shared by all workers through Redis; per-worker counts if Redis is down
        self._redis_url = redis_url
        self._redis = None
        self._redis_retry_at = 0.0
        self._local_user_active: Dict[str, int] = {}
        self._pending_releases: set = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._total_user_cap_rejections = 0

        self._active_count = 0
        self._queued_count = 0
        self._total_admitted = 0
        self._total_shed = 0
        self._total_timeouts = 0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ per-user cap (FR-5)
    @staticmethod
    def _user_key(user_id: str) -> str:
        return "admission:active:" + hashlib.sha256((user_id or "anonymous").encode()).hexdigest()[:24]

    async def _redis_client(self):
        if not self._use_redis or time.monotonic() < self._redis_retry_at:
            return None
        if self._redis is None:
            try:
                import redis.asyncio as aioredis
                url = self._redis_url
                if not url:
                    from gateway.cache.redis_semantic_cache import DEFAULT_REDIS_URL as url
                self._redis = aioredis.from_url(url, socket_connect_timeout=0.5, socket_timeout=0.5)
            except Exception as exc:
                logger.warning("Per-user admission counter: Redis unavailable (%s); counting per worker", exc)
                self._redis_retry_at = time.monotonic() + 30
                return None
        return self._redis

    async def _reserve_user_slot(self, user_id: str) -> AdmissionTicket:
        key, member = self._user_key(user_id), uuid.uuid4().hex
        r = await self._redis_client()
        if r is not None:
            try:
                now = time.time()
                ok = await r.eval(_USER_SLOT_ACQUIRE, 1, key, now - USER_SLOT_MAX_AGE, self.per_user_cap,
                                  now, member, int(USER_SLOT_MAX_AGE) + 60)
                if not int(ok):
                    self._reject_user(user_id)
                return AdmissionTicket(key, member, in_redis=True)
            except HTTPException:
                raise
            except Exception as exc:
                logger.warning("Per-user admission counter: Redis error (%s); counting per worker", exc)
                self._redis_retry_at = time.monotonic() + 30
        async with self._lock:
            if self._local_user_active.get(key, 0) >= self.per_user_cap:
                self._reject_user(user_id)
            self._local_user_active[key] = self._local_user_active.get(key, 0) + 1
        return AdmissionTicket(key, member, in_redis=False)

    def _reject_user(self, user_id: str):
        self._total_shed += 1
        self._total_user_cap_rejections += 1
        logger.warning("Per-user concurrency cap (%d) reached for user %s", self.per_user_cap, user_id)
        raise HTTPException(
            status_code=429,
            detail=(f"You already have {self.per_user_cap} request{'s' if self.per_user_cap != 1 else ''} in progress. "
                    "Please wait for one to finish."),
            headers={"Retry-After": "2"},
        )

    def _release_user_slot(self, ticket: AdmissionTicket) -> None:
        """Runs on the event loop (release() hands over from worker threads)."""
        if ticket.in_redis:
            r = self._redis
            if r is not None:
                async def _zrem():
                    try:
                        await r.zrem(ticket.user_key, ticket.member)
                    except Exception as exc:  # the entry expires after USER_SLOT_MAX_AGE anyway
                        logger.warning("Per-user admission counter: release failed (%s)", exc)
                task = asyncio.get_running_loop().create_task(_zrem())
                self._pending_releases.add(task)
                task.add_done_callback(self._pending_releases.discard)
            return
        n = self._local_user_active.get(ticket.user_key, 0) - 1
        if n > 0:
            self._local_user_active[ticket.user_key] = n
        else:
            self._local_user_active.pop(ticket.user_key, None)

    async def acquire(self, user_id: str = "anonymous", cost: float = 1.0) -> AdmissionTicket:
        """Acquire an active inference slot or queue if at capacity. Returns a ticket for release().

        Raises:
            HTTPException(429): If client rate limit or the per-user concurrency cap is exceeded.
            HTTPException(503): If the queue is at maximum capacity (shedding)
                                or queue wait times out.
        """
        self._loop = asyncio.get_running_loop()
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

        # 2. Per-user concurrency cap (FR-5), counted across workers
        ticket = await self._reserve_user_slot(user_id)
        try:
            await self._acquire_global(user_id)
        except BaseException:
            self._release_user_slot(ticket)
            raise
        return ticket

    async def _acquire_global(self, user_id: str) -> None:
        # 3. Queue Depth Check (Prevent unbounded memory backlog)
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

        # 4. Wait in queue for available inference slot with timeout
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

        # 5. In-flight Execution
        async with self._lock:
            self._active_count += 1
            self._total_admitted += 1

    def release(self, ticket: Optional[AdmissionTicket] = None) -> None:
        """Release the acquired inference slot (and the user's slot when the ticket is given).

        Safe from worker threads (the streaming routes release inside their generators, which run in
        threads): asyncio primitives are only touched on the event loop."""
        loop = self._loop
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if loop is not None and not on_loop and loop.is_running():
            loop.call_soon_threadsafe(self._release_now, ticket)
        else:
            self._release_now(ticket)

    def _release_now(self, ticket: Optional[AdmissionTicket]) -> None:
        self.global_sem.release()
        self._active_count = max(0, self._active_count - 1)
        if ticket is not None:
            try:
                self._release_user_slot(ticket)
            except RuntimeError:  # no running loop (sync callers / tests): Redis entry expires on its own
                pass

    @asynccontextmanager
    async def slot(self, user_id: str = "anonymous", cost: float = 1.0):
        """Async context manager: acquires a queue position and execution slot."""
        ticket = await self.acquire(user_id=user_id, cost=cost)
        try:
            yield
        finally:
            self.release(ticket)

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
            "per_user_cap": self.per_user_cap,
            "total_user_cap_rejections": self._total_user_cap_rejections,
        }


_default_admission_controller: Optional[AdmissionController] = None


def get_admission_controller() -> AdmissionController:
    """FastAPI dependency provider for AdmissionController singleton."""
    global _default_admission_controller
    if _default_admission_controller is None:
        _default_admission_controller = AdmissionController()
    return _default_admission_controller
