import os
import inspect
import socket
from pathlib import Path
from typing import Any, Dict, Optional

import redis.asyncio as aioredis
import yaml

from gateway.auth.email_service import email_service
from gateway.auth.user_store import user_store
from gateway.cache.entity_guard import EntityGuard
from gateway.cache.l0_exact import L0ExactCache
from gateway.cache.l1_semantic import L1SemanticCache
from gateway.clients.cpu_slm_client import CPUSLMClient
from gateway.clients.vllm_client import VLLMClient
from gateway.queue.admission import AdmissionController
from gateway.retrieval import RedisRetriever
from gateway.router.complexity_scorer import ComplexityScorer
from gateway.router.dispatcher import RequestDispatcher
from pipeline.embeddings.embedder import LocalEmbedder


def _load_env_files() -> None:
    for env_path in [
        Path.cwd() / ".env",
        Path(__file__).resolve().parents[1] / ".env",
        Path(__file__).resolve().parents[2] / "env",
    ]:
        if env_path.exists():
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip().strip("'\"")
                        if k and k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass


class ServiceContainer:
    """Singleton service container managing shared gateway components."""

    def __init__(self, config_path: Optional[str] = None):
        default_path = (
            Path(__file__).resolve().parents[1] / "configs" / "gateway_config.yaml"
        )
        self.config_path = Path(config_path or os.getenv("GATEWAY_CONFIG", default_path))
        self.config: Dict[str, Any] = {}
        self.redis_client: Optional[Any] = None
        self.embedder: Optional[LocalEmbedder] = None
        self.entity_guard: EntityGuard = EntityGuard()
        self.l0_cache: Optional[L0ExactCache] = None
        self.l1_cache: Optional[L1SemanticCache] = None
        self.scorer: Optional[ComplexityScorer] = None
        self.dispatcher: Optional[RequestDispatcher] = None
        self.admission: Optional[AdmissionController] = None
        self.slm_client: Optional[CPUSLMClient] = None
        self.vllm_client: Optional[VLLMClient] = None
        self.retriever: Optional[RedisRetriever] = None

    def load_config(self) -> Dict[str, Any]:
        _load_env_files()
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = yaml.safe_load(f) or {}
        return self.config

    async def initialize(self, redis_override: Any = None) -> None:
        self.load_config()

        redis_cfg = self.config.get("redis", {})
        default_redis_url = redis_cfg.get("url", "redis://:Redis*2703!@localhost:6379/0")
        redis_url = os.getenv("REDIS_URL", default_redis_url)

        # Inject password if missing from URL
        redis_password = os.getenv("REDIS_PASSWORD", redis_cfg.get("password", "Redis*2703!"))
        if redis_password and "@" not in redis_url and redis_url.startswith("redis://"):
            redis_url = f"redis://:{redis_password}@" + redis_url[len("redis://"):]

        # Handle docker-internal host 'redis' when running on local host
        if "@redis:6379" in redis_url or redis_url.startswith("redis://redis:6379"):
            try:
                socket.gethostbyname("redis")
            except socket.gaierror:
                redis_url = redis_url.replace("@redis:6379", "@localhost:6379").replace(
                    "redis://redis:6379", "redis://localhost:6379"
                )

        # Preserve pre-injected fake_redis if already set before lifespan startup
        self.redis_client = (
            redis_override
            or self.redis_client
            or aioredis.from_url(redis_url, decode_responses=False)
        )
        user_store.set_redis(self.redis_client)
        email_service.set_redis(self.redis_client)

        emb_cfg = self.config.get("embedder", {})
        if self.embedder is None:
            self.embedder = LocalEmbedder(
                model_name_or_path=emb_cfg.get("model_name", "all-MiniLM-L6-v2"),
                cache_folder=emb_cfg.get("cache_folder", "models/embeddings"),
                dimension=emb_cfg.get("dimension", 384),
            )

        l0_cfg = self.config.get("cache", {}).get("l0_exact", {})
        self.l0_cache = L0ExactCache(
            redis_client=self.redis_client,
            ttl_seconds=l0_cfg.get("ttl_seconds", 3600),
            prefix=l0_cfg.get("prefix", "l0:exact:"),
        )

        l1_cfg = self.config.get("cache", {}).get("l1_semantic", {})
        self.l1_cache = L1SemanticCache(
            redis_client=self.redis_client,
            similarity_threshold=l1_cfg.get("similarity_threshold", 0.88),
            vector_dimension=l1_cfg.get("vector_dimension", 384),
            ttl_seconds=l1_cfg.get("ttl_seconds", 7200),
            index_name=l1_cfg.get("index_name", "l1_semantic_cache_idx"),
            prefix=l1_cfg.get("prefix", "l1:cache:"),
            enforce_entity_guard=l1_cfg.get("enforce_entity_guard", True),
            confident_threshold=l1_cfg.get("confident_threshold", 0.96),
            enable_stage2_validation=l1_cfg.get("enable_stage2_validation", True),
            enable_stage3_verification=l1_cfg.get("enable_stage3_verification", True),
            enable_volatile_guard=l1_cfg.get("enable_volatile_guard", True),
        )
        if hasattr(self.redis_client, "execute_command"):
            await self.l1_cache.init_index()

        routing_cfg = self.config.get("routing", {})
        self.scorer = ComplexityScorer(
            model_path=routing_cfg.get("complexity_model_path"),
            embedder=self.embedder,
        )
        self.dispatcher = RequestDispatcher(alpha=routing_cfg.get("alpha", 0.65))
        admission_cfg = self.config.get("admission", {})
        rate_cfg = self.config.get("rate_limiting", {}).get("token_bucket", {})
        from gateway.queue.admission import TokenBucketLimiter
        self.admission = AdmissionController(
            global_concurrency_cap=admission_cfg.get("global_concurrency_limit", 12),
            per_user_concurrency_cap=admission_cfg.get("per_user_concurrency_limit", 4),
            max_queue_depth=admission_cfg.get("max_queue_depth", 100),
            queue_wait_timeout_seconds=admission_cfg.get("queue_wait_timeout_seconds", 15.0),
            rate_limiter=TokenBucketLimiter(
                capacity=rate_cfg.get("capacity", 20),
                refill_rate=rate_cfg.get("refill_rate", 5.0),
            ),
        )

        slm_cfg = self.config.get("slm", {})
        slm_url = os.getenv("SLM_BASE_URL", slm_cfg.get("base_url", "http://cpu-slm:8080/v1"))
        slm_model = os.getenv("SLM_MODEL_NAME", slm_cfg.get("model_name", "Qwen/Qwen2.5-1.5B-Instruct-GGUF"))
        self.slm_client = CPUSLMClient(base_url=slm_url, model_name=slm_model)
        self.vllm_client = VLLMClient(
            base_url=os.getenv("VLLM_BASE_URL", "http://vllm:8000/v1")
        )
        retrieval_cfg = self.config.get("retrieval", {})
        self.retriever = RedisRetriever(
            self.redis_client,
            index_name=retrieval_cfg.get("index_name", "rag_documents_v2_idx"),
            top_k=retrieval_cfg.get("top_k", 5),
        )

    async def shutdown(self) -> None:
        for client in (self.slm_client, self.vllm_client):
            close = getattr(client, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result
        if self.redis_client is not None and hasattr(self.redis_client, "aclose"):
            await self.redis_client.aclose()

    async def readiness(self) -> Dict[str, bool]:
        """Probe live dependencies without making health depend on a client object existing."""
        redis_ok = False
        if self.redis_client is not None and hasattr(self.redis_client, "ping"):
            try:
                redis_ok = bool(await self.redis_client.ping())
            except Exception:
                redis_ok = False
        embedding_ok = bool(self.embedder is not None and getattr(self.embedder, "_model", None) is not None)
        slm_ok = False
        gpu_ok = False
        if self.slm_client is not None:
            try:
                slm_ok = bool(await self.slm_client.check_health())
            except Exception:
                slm_ok = False
        if self.vllm_client is not None:
            try:
                gpu_ok = bool(await self.vllm_client.check_health())
            except Exception:
                gpu_ok = False
        return {
            "redis": redis_ok,
            "embedding_model": embedding_ok,
            "cpu_slm": slm_ok,
            "gpu_vllm": gpu_ok,
        }


container = ServiceContainer()


def get_l0_cache():
    return container.l0_cache


def get_l1_cache():
    return container.l1_cache


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
