import os
from gateway.cache.l0_exact import L0ExactCache
from gateway.cache.l1_semantic import L1SemanticCache
from gateway.router.complexity_scorer import ComplexityScorer
from gateway.router.dispatcher import RequestDispatcher
from gateway.queue.admission import AdmissionController
from gateway.clients.cpu_slm_client import CPUSLMClient
from gateway.clients.vllm_client import VLLMClient

class ServiceContainer:
    def __init__(self):
        self.redis = None
        self.l0_cache = L0ExactCache()
        self.l1_cache = L1SemanticCache()
        self.scorer = ComplexityScorer()
        self.dispatcher = RequestDispatcher()
        self.admission = AdmissionController()
        self.slm_client = CPUSLMClient()
        self.vllm_client = VLLMClient()
    async def initialize(self): pass
    async def shutdown(self): pass

container = ServiceContainer()
def get_l0_cache(): return container.l0_cache
def get_l1_cache(): return container.l1_cache
def get_complexity_scorer(): return container.scorer
def get_dispatcher(): return container.dispatcher
def get_admission_controller(): return container.admission
def get_slm_client(): return container.slm_client
def get_vllm_client(): return container.vllm_client
