"""Deficit Round Robin (DRR) Fair Request Scheduler for On-Premise Institutional Gateway.

Prevents noisy-neighbor / starvation problems when multiple students or batch jobs
submit bursts of queries simultaneously.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional
import time


@dataclass
class QueuedRequest:
    request_id: str
    user_id: str
    future: asyncio.Future
    created_at: float = field(default_factory=time.time)
    cost: float = 1.0


class FairRequestScheduler:
    """Fair-queuing scheduler ensuring fair slot allocation across user accounts."""

    def __init__(self, max_queue_depth: int = 100, quantum: float = 1.0):
        self.max_queue_depth = max_queue_depth
        self.quantum = quantum
        self.user_queues: Dict[str, Deque[QueuedRequest]] = {}
        self.active_users: Deque[str] = deque()
        self.deficits: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._total_queued = 0

    @property
    def total_queued(self) -> int:
        return self._total_queued

    async def enqueue(self, user_id: str, request_id: str, cost: float = 1.0) -> asyncio.Future:
        async with self._lock:
            if self._total_queued >= self.max_queue_depth:
                raise OverflowError(f"Fair queue depth exceeded ({self._total_queued}/{self.max_queue_depth})")

            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            req = QueuedRequest(request_id=request_id, user_id=user_id, future=fut, cost=cost)

            if user_id not in self.user_queues:
                self.user_queues[user_id] = deque()
                self.active_users.append(user_id)
                self.deficits[user_id] = 0.0

            self.user_queues[user_id].append(req)
            self._total_queued += 1
            return fut

    async def schedule_next(self) -> Optional[QueuedRequest]:
        """Pulls the next request to execute according to Deficit Round Robin."""
        async with self._lock:
            if not self.active_users:
                return None

            scanned = 0
            num_users = len(self.active_users)

            while scanned < num_users:
                user_id = self.active_users[0]
                queue = self.user_queues.get(user_id)

                if not queue:
                    self.active_users.popleft()
                    self.user_queues.pop(user_id, None)
                    self.deficits.pop(user_id, None)
                    num_users = len(self.active_users)
                    continue

                self.deficits[user_id] += self.quantum
                req = queue[0]

                if self.deficits[user_id] >= req.cost:
                    self.deficits[user_id] -= req.cost
                    queue.popleft()
                    self._total_queued -= 1

                    if not queue:
                        self.active_users.popleft()
                        self.user_queues.pop(user_id, None)
                        self.deficits.pop(user_id, None)
                    else:
                        self.active_users.rotate(-1)
                    return req
                else:
                    self.active_users.rotate(-1)
                    scanned += 1

            return None
