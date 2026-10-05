import asyncio
from collections import deque
from typing import Any, Deque, Dict, Optional

class FairRequestScheduler:
    def __init__(self, max_queue_depth=100):
        self.max_queue_depth = max_queue_depth
        self.user_queues: Dict[str, Deque] = {}
        self.active_users: Deque[str] = deque()
