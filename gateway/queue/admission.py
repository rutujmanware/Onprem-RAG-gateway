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

# DEFECT-59: repeat offenders. Each injection refusal of an identified user is a strike; this many strikes within an
# hour block that user for INJECTION_BLOCK_SECONDS (HTTP 429). 0 turns it off. Anthropic's guidance on jailbreaks:
# "consider throttling or banning users who repeatedly attempt to circumvent your application's guardrails".
INJECTION_STRIKES_PER_HOUR = int(os.getenv("INJECTION_STRIKES_PER_HOUR", "5"))
INJECTION_BLOCK_SECONDS = int(os.getenv("INJECTION_BLOCK_SECONDS", "900"))
_STRIKE_WINDOW_SECONDS = 3600
# KEYS: strikes, blocked; ARGV: window, limit, block seconds. Returns 1 when this strike starts a block.
_INJECTION_STRIKE = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
if n >= tonumber(ARGV[2]) then
  redis.call('SET', KEYS[2], '1', 'EX', ARGV[3])
  redis.call('DEL', KEYS[1])
  return 1
end
return 0
"""


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

    MAX_TRACKED = 20000      # DEFECT-47: idle clients are forgotten, so the state cannot grow without bound

    async def acquire(self, client_id: str, cost: float = 1.0) -> bool:
        # VULN-03 Fix: Rate limit every client/user account fairly.
        # Do not allow unconditional rate limit bypass for loopback or web chat traffic.
        async with self._lock:
            now = time.time()
            if len(self._tokens) > self.MAX_TRACKED:
                full_after = self.capacity / max(self.refill_rate, 1e-6)
                for k in [k for k, t in self._last_refill.items() if now - t > full_after]:
                    self._tokens.pop(k, None)
                    self._last_refill.pop(k, None)
                if len(self._tokens) > self.MAX_TRACKED:   # still too many: forget the least recently seen half
                    for k in sorted(self._last_refill, key=self._last_refill.get)[: len(self._tokens) // 2]:
                        self._tokens.pop(k, None)
                        self._last_refill.pop(k, None)
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
    released: bool = False   # release() is idempotent per ticket (DEFECT-34)


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
        # DEFECT-59 fallback when Redis is unreachable: per-worker strikes and blocks
        self._local_strikes: Dict[str, list] = {}
        self._local_blocked: Dict[str, float] = {}
        self._total_injection_blocks = 0

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

    # ------------------------------------------------------------------ repeat offenders (DEFECT-59)
    @staticmethod
    def _strike_keys(user_id: str):
        h = hashlib.sha256((user_id or "anonymous").encode()).hexdigest()[:24]
        return "admission:strikes:" + h, "admission:blocked:" + h

    async def injection_block_remaining(self, user_id: str) -> int:
        """Seconds left on this user's block for repeated injection attempts (0 = not blocked)."""
        if INJECTION_STRIKES_PER_HOUR <= 0:
            return 0
        _, bkey = self._strike_keys(user_id)
        r = await self._redis_client()
        if r is not None:
            try:
                return max(0, int(await r.ttl(bkey)))   # -2 missing, -1 no expiry (never set that way)
            except Exception as exc:
                logger.warning("Injection strike counter: Redis error (%s); counting per worker", exc)
                self._redis_retry_at = time.monotonic() + 30
        return max(0, int(self._local_blocked.get(bkey, 0) - time.time()))

    async def check_injection_block(self, user_id: str) -> None:
        remaining = await self.injection_block_remaining(user_id)
        if remaining > 0:
            self._total_shed += 1
            minutes = max(1, -(-remaining // 60))
            raise HTTPException(
                status_code=429,
                detail=(f"Too many of your recent requests were refused as prompt-injection attempts. "
                        f"Please try again in {minutes} minute{'s' if minutes != 1 else ''}."),
                headers={"Retry-After": str(remaining)},
            )

    async def record_injection_refusal(self, user_id: str) -> bool:
        """Count one strike; True if it started a block."""
        if INJECTION_STRIKES_PER_HOUR <= 0:
            return False
        skey, bkey = self._strike_keys(user_id)
        blocked = None
        r = await self._redis_client()
        if r is not None:
            try:
                blocked = bool(int(await r.eval(_INJECTION_STRIKE, 2, skey, bkey, _STRIKE_WINDOW_SECONDS,
                                                INJECTION_STRIKES_PER_HOUR, INJECTION_BLOCK_SECONDS)))
            except Exception as exc:
                logger.warning("Injection strike counter: Redis error (%s); counting per worker", exc)
                self._redis_retry_at = time.monotonic() + 30
        if blocked is None:
            now = time.time()
            strikes = [t for t in self._local_strikes.get(skey, []) if now - t < _STRIKE_WINDOW_SECONDS] + [now]
            blocked = len(strikes) >= INJECTION_STRIKES_PER_HOUR
            if blocked:
                self._local_blocked[bkey] = now + INJECTION_BLOCK_SECONDS
                self._local_strikes.pop(skey, None)
            else:
                self._local_strikes[skey] = strikes
        if blocked:
            self._total_injection_blocks += 1
            logger.warning("User %s blocked for %ds after %d prompt-injection refusals within an hour",
                           user_id, INJECTION_BLOCK_SECONDS, INJECTION_STRIKES_PER_HOUR)
        return blocked

    async def charge(self, user_id: str = "anonymous", cost: float = 1.0) -> None:
        """Token-bucket check alone (DEFECT-47): applied when a request arrives, before the cache lookup, so cache
        hits and the work done before admission (fence, embedding, cache) are rate limited too."""
        if not await self.rate_limiter.acquire(user_id, cost=cost):
            self._total_shed += 1
            logger.warning("Rate limit exceeded for user %s", user_id)
            raise HTTPException(
                status_code=429,
                detail="Rate limit exceeded. Please wait a moment before sending more queries.",
                headers={"Retry-After": "2"}
            )

    async def acquire(self, user_id: str = "anonymous", cost: float = 1.0,
                      charge_rate: bool = True) -> AdmissionTicket:
        """Acquire an active inference slot or queue if at capacity. Returns a ticket for release().

        Raises:
            HTTPException(429): If client rate limit or the per-user concurrency cap is exceeded.
            HTTPException(503): If the queue is at maximum capacity (shedding)
                                or queue wait times out.
        """
        self._loop = asyncio.get_running_loop()
        # 1. Per-client Token Bucket Rate Limit (skipped when charge() already ran for this request)
        allowed = (await self.rate_limiter.acquire(user_id, cost=cost)) if charge_rate else True
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
        """Wait for a global inference permit (DEFECT-34: cancellation-safe).

        Counters are plain ints updated without any await (one event loop, so these updates are atomic); the
        permit is taken by a separate task, and if this coroutine is cancelled or times out at any point, a
        permit that was (or still gets) granted is handed back. Also avoids asyncio.wait_for, which on
        Python < 3.11 can drop an already-granted permit on timeout."""
        # 3. Queue Depth Check (Prevent unbounded memory backlog)
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
        waiter = asyncio.ensure_future(self.global_sem.acquire())
        granted = False
        try:
            done, _ = await asyncio.wait({waiter}, timeout=self.queue_timeout)
            granted = waiter in done and not waiter.cancelled() and waiter.exception() is None
        except BaseException:          # cancelled while waiting (client disconnect)
            self._queued_count -= 1
            self._give_back_when_granted(waiter)
            raise
        if not granted:
            self._queued_count -= 1
            self._give_back_when_granted(waiter)
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
        # 5. In-flight Execution: no await between the grant and the return, so no cancellation point
        self._queued_count -= 1
        self._active_count += 1
        self._total_admitted += 1

    def _give_back_when_granted(self, waiter: "asyncio.Future") -> None:
        """Cancel a pending permit request; if it was (or is about to be) granted, return the permit."""
        def _done(t):
            if not t.cancelled() and t.exception() is None:
                self.global_sem.release()
        if waiter.done():
            _done(waiter)
        else:
            waiter.cancel()
            waiter.add_done_callback(_done)

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
        if ticket is not None:
            if ticket.released:      # already released (e.g. generator finally + response close)
                return
            ticket.released = True
        self.global_sem.release()
        self._active_count = max(0, self._active_count - 1)
        if ticket is not None:
            try:
                self._release_user_slot(ticket)
            except RuntimeError:  # no running loop (sync callers / tests): Redis entry expires on its own
                pass

    @asynccontextmanager
    async def slot(self, user_id: str = "anonymous", cost: float = 1.0, charge_rate: bool = True):
        """Async context manager: acquires a queue position and execution slot."""
        ticket = await self.acquire(user_id=user_id, cost=cost, charge_rate=charge_rate)
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
            "injection_strikes_per_hour": INJECTION_STRIKES_PER_HOUR,
            "injection_block_seconds": INJECTION_BLOCK_SECONDS,
            "total_injection_blocks": self._total_injection_blocks,   # this worker
        }


_default_admission_controller: Optional[AdmissionController] = None


def get_admission_controller() -> AdmissionController:
    """FastAPI dependency provider for AdmissionController singleton."""
    global _default_admission_controller
    if _default_admission_controller is None:
        _default_admission_controller = AdmissionController()
    return _default_admission_controller
