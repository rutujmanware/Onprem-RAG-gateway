import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional, Union

import numpy as np

from gateway.cache.entity_guard import EntityGuard, ScopeInfo
from gateway.cache.validator import (
    ThreeStageCacheValidator,
    ValidationResult,
    VolatileQueryDetector,
)

logger = logging.getLogger(__name__)


class L1CacheError(RuntimeError):
    """Raised for an invalid L1 cache input or Redis/RediSearch protocol error."""


class L1SemanticCache:
    """
    FR-3: L1 Semantic Cache backed by Redis Stack (RediSearch HNSW + TAG Pre-filtering)
    with strict Entity Guard verification on (thesis_id, chapter) and Three-Stage
    validation:
      - Stage 1: Semantic similarity filter (HNSW KNN vector search).
      - Stage 2: Context and metadata validation (model version/tier, conversation context,
                 language, scope parameters, numerical consistency, and dimension opposition).
      - Stage 3: Verification & reranking for uncertain matches (SLM judge or lexical-semantic verifier).
    Also incorporates a Volatile Information Gate to avoid caching changing data (stocks, weather, live availability).
    """

    def __init__(
        self,
        redis_client: Any = None,
        similarity_threshold: float = 0.88,
        vector_dimension: int = 384,
        ttl_seconds: int = 7200,
        index_name: str = "l1_semantic_cache_idx",
        prefix: str = "l1:cache:",
        enforce_entity_guard: bool = True,
        confident_threshold: float = 0.96,
        enable_stage2_validation: bool = True,
        enable_stage3_verification: bool = True,
        enable_volatile_guard: bool = True,
        slm_client: Optional[Any] = None,
    ):
        self.redis = redis_client
        self.similarity_threshold = float(similarity_threshold)
        self.vector_dimension = int(vector_dimension)
        self.ttl_seconds = int(ttl_seconds)
        self.index_name = index_name
        self.prefix = prefix
        self.enforce_entity_guard = enforce_entity_guard
        self.confident_threshold = float(confident_threshold)
        self.enable_stage2_validation = enable_stage2_validation
        self.enable_stage3_verification = enable_stage3_verification
        self.enable_volatile_guard = enable_volatile_guard
        self.slm_client = slm_client
        self._index_ready = False

        self.validator = ThreeStageCacheValidator(
            similarity_threshold=self.similarity_threshold,
            confident_threshold=self.confident_threshold,
            enable_volatile_guard=self.enable_volatile_guard,
            enable_stage2=self.enable_stage2_validation,
            enable_stage3=self.enable_stage3_verification,
        )

    async def init_index(self) -> None:
        """Ensures the RediSearch HNSW vector + TAG index exists."""
        if self.redis is None or self._index_ready:
            return

        try:
            await self.redis.execute_command("FT.INFO", self.index_name)
            self._index_ready = True
            logger.info("RediSearch index '%s' already exists.", self.index_name)
        except Exception as exc:
            msg = str(exc).lower()
            if "unknown index name" in msg or "no such index" in msg or "unknown index" in msg:
                try:
                    await self._create_redis_index()
                except Exception as create_exc:
                    # Another gateway worker can create the shared index between
                    # FT.INFO and FT.CREATE. Treat only that specific race as OK.
                    if "index already exists" not in str(create_exc).lower():
                        raise
                self._index_ready = True
            else:
                logger.warning("Could not verify RediSearch index '%s': %s", self.index_name, exc)
                raise

    async def _create_redis_index(self) -> None:
        """Creates the RediSearch index with TAG fields and HNSW FLOAT32 cosine vectors."""
        cmd = [
            "FT.CREATE",
            self.index_name,
            "ON", "HASH",
            "PREFIX", "1", self.prefix,
            "SCHEMA",
            "thesis_id", "TAG", "SORTABLE",
            "chapter", "TAG", "SORTABLE",
            "doc_version", "TAG",
            "model_tier", "TAG",
            "language", "TAG",
            "prompt", "TEXT",
            "response", "TEXT",
            "created_at", "NUMERIC",
            "vector", "VECTOR", "HNSW", "10",
            "TYPE", "FLOAT32",
            "DIM", str(self.vector_dimension),
            "DISTANCE_METRIC", "COSINE",
            "M", "16",
            "EF_CONSTRUCTION", "200",
        ]
        await self.redis.execute_command(*cmd)
        logger.info("Created RediSearch vector index '%s'.", self.index_name)

    @staticmethod
    def _to_float32_bytes(embedding: Union[List[float], np.ndarray]) -> bytes:
        arr = np.asarray(embedding, dtype=np.float32).reshape(-1)
        if arr.size == 0 or not np.isfinite(arr).all():
            raise L1CacheError("L1 embedding must contain finite values")
        norm = np.linalg.norm(arr)
        if norm == 0:
            raise L1CacheError("L1 embedding must not be the zero vector")
        arr = arr / norm
        return arr.tobytes()

    def _validate_embedding(self, embedding: Union[List[float], np.ndarray]) -> bytes:
        arr = np.asarray(embedding, dtype=np.float32).reshape(-1)
        if arr.size != self.vector_dimension:
            raise L1CacheError(
                f"L1 embedding has dimension {arr.size}; expected {self.vector_dimension}"
            )
        return self._to_float32_bytes(arr)

    @staticmethod
    def _decode(val: Any) -> str:
        if isinstance(val, bytes):
            return val.decode("utf-8", errors="replace")
        return str(val) if val is not None else ""

    async def get(
        self,
        query_embedding: Union[List[float], np.ndarray],
        thesis_id: str,
        chapter: str,
        doc_version: str = "v1",
        prompt: Optional[str] = None,
        model_tier: Optional[str] = None,
        conversation_context: Optional[str] = None,
        language: Optional[str] = "en",
        query_context: Optional[Dict[str, Any]] = None,
        slm_client: Optional[Any] = None,
        freshness_handled: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Executes Three-Stage Validation with Entity Guard:
          1. Volatile Check: Bypass cache for changing info (stocks, weather, live availability).
          2. Stage 1: Vector KNN lookup in RediSearch + similarity threshold.
          3. Stage 2: Context/metadata, numerical consistency, and dimension opposition check.
          4. Stage 3: Verification / reranking for uncertain matches.
        Returns cached entry dict on verified HIT, or None on MISS.
        """
        if self.redis is None:
            return None

        # Step 0: Check Volatile Information Gate
        if prompt and self.enable_volatile_guard and not freshness_handled:
            if self.validator.is_volatile(prompt):
                logger.info(
                    "L1 Cache bypassed: query involves rapidly changing information ('%s')",
                    prompt[:60],
                )
                return None

        if not self._index_ready:
            await self.init_index()

        scope = ScopeInfo(
            thesis_id=EntityGuard.normalize_thesis_id(thesis_id),
            chapter=EntityGuard.normalize_chapter(chapter),
            doc_version=doc_version,
        )

        vec_bytes = self._validate_embedding(query_embedding)

        if self.enforce_entity_guard:
            escaped_thesis = EntityGuard.escape_redis_tag(scope.thesis_id)
            escaped_chapter = EntityGuard.escape_redis_tag(scope.chapter)
            escaped_version = EntityGuard.escape_redis_tag(scope.doc_version)
            filter_expr = (
                f"(@thesis_id:{{{escaped_thesis}}} "
                f"@chapter:{{{escaped_chapter}}} "
                f"@doc_version:{{{escaped_version}}})"
            )
        else:
            filter_expr = "*"

        query_str = f"{filter_expr}=>[KNN 1 @vector $vec AS score]"

        raw_result = await self.redis.execute_command(
            "FT.SEARCH",
            self.index_name,
            query_str,
            "PARAMS", "2", "vec", vec_bytes,
            "SORTBY", "score", "ASC",
            "RETURN", "10", "prompt", "response", "thesis_id", "chapter", "doc_version", "model_tier", "language", "conversation_context", "metadata", "score",
            "DIALECT", "2",
        )

        doc_fields: Dict[str, str] = {}
        if isinstance(raw_result, dict):
            # Redis RESP3 mapping
            total = raw_result.get(b"total_results") or raw_result.get("total_results") or 0
            if not total:
                return None
            results_list = raw_result.get(b"results") or raw_result.get("results") or []
            if not results_list:
                return None
            first_doc = results_list[0]
            if isinstance(first_doc, dict):
                attrs = first_doc.get(b"extra_attributes") or first_doc.get("extra_attributes") or first_doc
                for k, v in attrs.items():
                    doc_fields[self._decode(k)] = self._decode(v)
        elif isinstance(raw_result, (list, tuple)):
            # Redis RESP2 array: [total_count, doc_key, [field1, val1, ...]]
            if not raw_result or int(raw_result[0]) == 0:
                return None
            if len(raw_result) >= 3:
                fields_data = raw_result[2]
                if isinstance(fields_data, dict):
                    for k, v in fields_data.items():
                        doc_fields[self._decode(k)] = self._decode(v)
                elif isinstance(fields_data, (list, tuple)):
                    for i in range(0, len(fields_data), 2):
                        k = self._decode(fields_data[i])
                        v = self._decode(fields_data[i + 1])
                        doc_fields[k] = v
        else:
            return None

        # Stage 1: Semantic Similarity Filter
        cosine_distance = float(doc_fields.get("score", 2.0))
        similarity = 1.0 - cosine_distance

        if similarity < self.similarity_threshold:
            return None

        cand_thesis = doc_fields.get("thesis_id", "")
        cand_chapter = doc_fields.get("chapter", "")
        cand_version = doc_fields.get("doc_version", "")
        cand_prompt = doc_fields.get("prompt", "")

        # Entity Guard verification gate
        if self.enforce_entity_guard and not EntityGuard.verify_match(
            scope, cand_thesis, cand_chapter, cand_version
        ):
            logger.warning(
                "Entity Guard rejected candidate despite tag filter: query=%s vs candidate=(%s, %s)",
                scope, cand_thesis, cand_chapter
            )
            return None

        # Stage 2 & Stage 3: Context/Metadata, Numbers, Dimensions & Verification
        validation_info: Dict[str, Any] = {
            "stage": "stage1_passed",
            "similarity": round(similarity, 4),
        }

        if prompt:
            combined_q_context = dict(query_context or {})
            if model_tier:
                combined_q_context["model_tier"] = model_tier
            if language:
                combined_q_context["language"] = language
            if conversation_context:
                combined_q_context["conversation_context"] = conversation_context

            cached_meta = {
                "model_tier": doc_fields.get("model_tier"),
                "language": doc_fields.get("language", "en"),
                "conversation_context": doc_fields.get("conversation_context"),
            }

            active_slm = slm_client or self.slm_client
            val_result: ValidationResult = await self.validator.validate_candidate(
                query=prompt,
                cached_prompt=cand_prompt,
                similarity=similarity,
                query_context=combined_q_context,
                cached_metadata=cached_meta,
                slm_client=active_slm,
                freshness_handled=freshness_handled,
            )

            if not val_result.is_valid:
                logger.info(
                    "L1 Cache match rejected by %s: %s (query='%s' vs cached='%s')",
                    val_result.stage,
                    val_result.reason,
                    prompt[:60],
                    cand_prompt[:60],
                )
                return None

            validation_info["stage"] = val_result.stage
            validation_info["reason"] = val_result.reason

        raw_response = doc_fields.get("response", "{}")
        try:
            parsed_response = json.loads(raw_response)
        except json.JSONDecodeError:
            parsed_response = {"answer": raw_response}

        return {
            "response": parsed_response,
            "similarity": round(similarity, 4),
            "matched_prompt": cand_prompt,
            "thesis_id": cand_thesis,
            "chapter": cand_chapter,
            "doc_version": doc_fields.get("doc_version", scope.doc_version),
            "validation": validation_info,
        }

    async def set(
        self,
        prompt: str,
        query_embedding: Union[List[float], np.ndarray],
        response: Any,
        thesis_id: str,
        chapter: str,
        doc_version: str = "v1",
        model_tier: Optional[str] = None,
        language: Optional[str] = "en",
        conversation_context: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        ttl_seconds: Optional[int] = None,
        freshness_handled: bool = False,
    ) -> str:
        """
        Writes a scoped vector entry to Redis Hashes with TTL.
        Skips caching if query involves changing/volatile information unless freshness is handled.
        """
        if self.redis is None:
            return ""

        # Avoid caching volatile/changing information
        if self.enable_volatile_guard and not freshness_handled and self.validator.is_volatile(prompt):
            logger.info("L1 Cache set skipped: volatile query '%s' is not cacheable", prompt[:60])
            return ""

        if not self._index_ready:
            await self.init_index()

        norm_thesis = EntityGuard.normalize_thesis_id(thesis_id)
        norm_chapter = EntityGuard.normalize_chapter(chapter)
        norm_version = str(doc_version).strip()
        if not norm_version:
            raise L1CacheError("L1 document version is required")
        prompt_hash = hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()[:16]
        # The revision is part of the physical key as well as the query filter,
        # so writing a new revision can never overwrite a still-auditable old one.
        key = f"{self.prefix}{norm_thesis}:{norm_chapter}:{norm_version}:{prompt_hash}"

        serialized_resp = (
            response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        )
        vec_bytes = self._validate_embedding(query_embedding)
        ttl = int(ttl_seconds if ttl_seconds is not None else self.ttl_seconds)
        if ttl <= 0:
            raise L1CacheError("L1 TTL must be positive")

        mapping = {
            b"thesis_id": norm_thesis.encode("utf-8"),
            b"chapter": norm_chapter.encode("utf-8"),
            b"doc_version": norm_version.encode("utf-8"),
            b"prompt": prompt.encode("utf-8"),
            b"response": serialized_resp.encode("utf-8"),
            b"created_at": str(int(time.time())).encode("utf-8"),
            b"vector": vec_bytes,
        }
        if model_tier:
            mapping[b"model_tier"] = str(model_tier).encode("utf-8")
        if language:
            mapping[b"language"] = str(language).encode("utf-8")
        if conversation_context:
            mapping[b"conversation_context"] = str(conversation_context).encode("utf-8")
        if metadata:
            mapping[b"metadata"] = json.dumps(metadata).encode("utf-8")

        pipe = self.redis.pipeline(transaction=True)
        pipe.hset(key, mapping=mapping)
        pipe.expire(key, ttl)
        await pipe.execute()

        return key

    async def invalidate_version(self, thesis_id: str, doc_version: str) -> int:
        """FR-6 Support: Invalidates cached entries matching a specific thesis_id and doc_version."""
        if self.redis is None:
            return 0
        if not self._index_ready:
            await self.init_index()

        norm_thesis = EntityGuard.escape_redis_tag(EntityGuard.normalize_thesis_id(thesis_id))
        escaped_ver = EntityGuard.escape_redis_tag(doc_version)
        query_str = f"(@thesis_id:{{{norm_thesis}}} @doc_version:{{{escaped_ver}}})"

        deleted = 0
        while True:
            res = await self.redis.execute_command(
                "FT.SEARCH", self.index_name, query_str, "NOCONTENT", "LIMIT", "0", "1000"
            )
            if not res:
                break

            keys_to_delete = []
            if isinstance(res, dict):
                total = res.get(b"total_results") or res.get("total_results") or 0
                if int(total) == 0:
                    break
                results = res.get(b"results") or res.get("results") or []
                for doc in results:
                    doc_id = doc.get(b"id") or doc.get("id")
                    if doc_id:
                        keys_to_delete.append(self._decode(doc_id))
            elif isinstance(res, (list, tuple)):
                if int(res[0]) == 0:
                    break
                keys_to_delete = [self._decode(k) for k in res[1:]]
            else:
                break

            if not keys_to_delete:
                break
            deleted += await self.redis.delete(*keys_to_delete)
        return deleted
