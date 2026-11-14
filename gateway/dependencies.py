import os
import os
from gateway.cache.redis_semantic_cache import RedisSemanticCache
from gateway.router.complexity_scorer import ComplexityScorer
from gateway.router.dispatcher import RequestDispatcher
from gateway.queue.admission import AdmissionController
from gateway.clients.cpu_slm_client import CPUSLMClient
from gateway.clients.vllm_client import VLLMClient

class ServiceContainer:
    def __init__(self):
        self.semantic_cache = RedisSemanticCache.get_instance()
        self.scorer = ComplexityScorer()
        self.dispatcher = RequestDispatcher(alpha=float(os.getenv("ROUTING_ALPHA", "0.65")))  # FR-2 alpha
        self.admission = AdmissionController()
        self.slm_client = CPUSLMClient()
        self.vllm_client = VLLMClient()

    async def initialize(self):
        """Startup routine: initialize Redis semantic vector index."""
        try:
            await self.semantic_cache.ainit_index()
        except Exception:
            pass

    async def shutdown(self):
        pass

container = ServiceContainer()

def get_semantic_cache() -> RedisSemanticCache:
    return container.semantic_cache

# Backward-compatible helpers
def get_l1_cache() -> RedisSemanticCache:
    return container.semantic_cache

def get_l0_cache():
    return None

def get_complexity_scorer():
    return container.scorer

def get_dispatcher():
    return container.dispatcher

def get_admission_controller():
    return container.admission

def get_slm_client():
    return container.slm_client

def get_vllm_client():
    return container.vllm_client
