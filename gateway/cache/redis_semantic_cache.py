#!/usr/bin/env python3
"""Enterprise-Grade Redis Semantic Vector Cache for On-Premise RAG Gateway.

Features:
- Dense vector similarity search using RediSearch HNSW Cosine indexing.
- Semantic cache HIT when cosine similarity >= threshold (default 0.90 / 0.88).
- Drops redundant GPU/SLM compute, returning responses in < 5ms.
- Built-in TTL expiration (default 7 days) and non-blocking async execution.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import uuid
import hashlib
import hmac
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# Ensure project root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None

try:
    import redis
except ImportError:
    redis = None

from pipeline.embeddings.embedder import LocalEmbedder

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logger = logging.getLogger("gateway.semantic_cache")

_redis_pw = os.getenv("REDIS_PASSWORD")
_pw_str = f":{_redis_pw}@" if _redis_pw else ""
DEFAULT_REDIS_URL = os.getenv(
    "REDIS_URL",
    f"redis://{_pw_str}{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}/0"
)
DEFAULT_INDEX_NAME = os.getenv("REDIS_CACHE_INDEX_NAME", "semantic_cache_idx")
DEFAULT_KEY_PREFIX = os.getenv("REDIS_CACHE_KEY_PREFIX", "semcache:")
DEFAULT_SIMILARITY_THRESHOLD = float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.90"))
DEFAULT_TTL_SECONDS = int(os.getenv("SEMANTIC_CACHE_TTL", str(24 * 3600)))  # 24 hours (86,400s)


class RedisSemanticCache:
    """Production Semantic Cache backed by RediSearch vector similarity."""

    _instance: Optional["RedisSemanticCache"] = None

    def __init__(
        self,
        redis_url: Optional[str] = None,
        index_name: str = DEFAULT_INDEX_NAME,
        key_prefix: str = DEFAULT_KEY_PREFIX,
        similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        vector_dim: int = 384,
    ):
        self.redis_url = redis_url or DEFAULT_REDIS_URL
        self.index_name = index_name
        self.key_prefix = key_prefix
        self.similarity_threshold = similarity_threshold
        self.ttl_seconds = ttl_seconds
        self.vector_dim = vector_dim

        # Lazy handles
        self._embedder: Optional[LocalEmbedder] = None
        self._sync_redis: Optional[redis.Redis] = None
        self._async_redis: Optional[aioredis.Redis] = None
        self._index_initialized = False

    @classmethod
    def get_instance(cls, **kwargs) -> "RedisSemanticCache":
        if cls._instance is None:
            cls._instance = cls(**kwargs)
        return cls._instance

    @property
    def embedder(self) -> LocalEmbedder:
        if self._embedder is None:
            self._embedder = LocalEmbedder(dimension=self.vector_dim, device="cpu")
        return self._embedder

    @property
    def sync_client(self) -> redis.Redis:
        if self._sync_redis is None:
            if not redis:
                raise RuntimeError("Package 'redis' is not installed.")
            self._sync_redis = redis.Redis.from_url(
                self.redis_url,
                decode_responses=False,
                socket_timeout=5.0,
            )
        return self._sync_redis

    @property
    def async_client(self) -> aioredis.Redis:
        current_loop = None
        try:
            import asyncio
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        if self._async_redis is not None and current_loop is not None:
            if getattr(self, "_async_redis_loop", None) is not current_loop:
                self._async_redis = None

        if self._async_redis is None:
            if not aioredis:
                raise RuntimeError("Package 'redis.asyncio' is not installed.")
            self._async_redis = aioredis.from_url(
                self.redis_url,
                decode_responses=False,
                socket_timeout=5.0,
            )
            self._async_redis_loop = current_loop
        return self._async_redis

    def init_index(self) -> bool:
        """Create RediSearch HNSW vector index for semantic cache if not already present."""
        if self._index_initialized:
            return True

        client = self.sync_client
        try:
            indexes = client.execute_command("FT._LIST")
            indexes_str = [idx.decode() if isinstance(idx, bytes) else str(idx) for idx in indexes]
            if self.index_name in indexes_str:
                self._index_initialized = True
                return True

            logger.info("Creating RediSearch index [%s] for semantic cache...", self.index_name)
            client.execute_command(
                "FT.CREATE", self.index_name,
                "ON", "HASH",
                "PREFIX", "1", self.key_prefix,
                "SCHEMA",
                "prompt", "TEXT", "NOSTEM",
                "response", "TEXT", "NOSTEM",
                "vector", "VECTOR", "HNSW", "6",
                "TYPE", "FLOAT32",
                "DIM", str(self.vector_dim),
                "DISTANCE_METRIC", "COSINE",
                "created_at", "NUMERIC",
            )
            self._index_initialized = True
            logger.info("✅ Semantic cache index [%s] initialized successfully!", self.index_name)
            return True
        except Exception as e:
            if "Index already exists" in str(e):
                self._index_initialized = True
                return True
            logger.warning("Failed to initialize semantic cache index: %s", e)
            return False

    async def ainit_index(self) -> bool:
        """Async variant of index initialization."""
        if self._index_initialized:
            return True
        try:
            client = self.async_client
            indexes = await client.execute_command("FT._LIST")
            indexes_str = [idx.decode() if isinstance(idx, bytes) else str(idx) for idx in indexes]
            if self.index_name in indexes_str:
                self._index_initialized = True
                return True

            logger.info("Async creating RediSearch index [%s] for semantic cache...", self.index_name)
            await client.execute_command(
                "FT.CREATE", self.index_name,
                "ON", "HASH",
                "PREFIX", "1", self.key_prefix,
                "SCHEMA",
                "prompt", "TEXT", "NOSTEM",
                "response", "TEXT", "NOSTEM",
                "vector", "VECTOR", "HNSW", "6",
                "TYPE", "FLOAT32",
                "DIM", str(self.vector_dim),
                "DISTANCE_METRIC", "COSINE",
                "created_at", "NUMERIC",
            )
            self._index_initialized = True
            return True
        except Exception as e:
            if "Index already exists" in str(e):
                self._index_initialized = True
                return True
            logger.warning("Async index init warning: %s", e)
            return False

    INSUFFICIENT_DATA_PHRASES = (
        "insufficient information",
        "insufficient data",
        "insufficient context",
        "not enough information",
        "not have enough information",
        "do not have enough information",
        "does not have enough information",
        "does not contain enough information",
        "do not contain enough information",
        "does not provide enough information",
        "do not provide enough information",
        "no relevant documents",
        "no relevant information",
        "no documents were found",
        "no information provided",
        "cannot find information",
        "unable to find information",
        "cannot answer based on",
        "could not find any information",
        "information is not available",
        "retrieval failed",
        # Fallback / unretrieved documents phrases
        "no specific institutional repository",
        "no institutional repository theses were found",
        "no theses were found on this topic",
        "based on general academic knowledge",
        "based on general technical knowledge",
        "no documents directly cover this topic",
        "no relevant documents were found in the knowledge base",
        "no specific institutional repository documents",
    )

    @classmethod
    def is_cacheable_response(cls, response: Any) -> Tuple[bool, str]:
        """Determine whether an answer payload should be saved to or retrieved from cache.

        Refusals, insufficient data notices, retrieval failures, zero retrieved documents/chunks,
        and empty answers must NOT be cached to avoid poisoning future queries and blocking newly ingested data.
        """
        if not response:
            return False, "Empty response payload"

        if isinstance(response, dict):
            if response.get("error"):
                return False, f"Response contains error: {response.get('error')}"

            # Document retrieval verification: if chunks key is present, verify at least one chunk exists
            chunks = response.get("chunks")
            if chunks is not None and len(chunks) == 0:
                return False, "No document chunks were retrieved from knowledge base"

            if response.get("num_chunks") == 0:
                return False, "Zero document chunks retrieved from knowledge base"

            if "citations" in response and len(response.get("citations") or []) == 0:
                return False, "Zero document citations retrieved from knowledge base"

            ans = str(response.get("answer") or response.get("content") or "").strip()
        elif isinstance(response, str):
            ans = response.strip()
        else:
            ans = str(response).strip()

        if len(ans) < 15:
            return False, f"Answer is too short ({len(ans)} chars) to be cacheable"

        ans_lower = ans.lower()
        for phrase in cls.INSUFFICIENT_DATA_PHRASES:
            if phrase in ans_lower:
                return False, f"Response indicates insufficient data / unretrieved documents: '{phrase}'"

        return True, "Cacheable"

    def lookup(
        self,
        query: str,
        threshold: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Synchronously lookup semantically similar cached query from Redis."""
        self.init_index()
        th = threshold or self.similarity_threshold

        # Fast path 0: Exact normalized text hash match (< 1ms in Redis, 0 CPU compute)
        norm_q = query.strip().lower()
        exact_key = f"{self.key_prefix}exact:{hashlib.sha256(norm_q.encode('utf-8')).hexdigest()}"
        try:
            raw_exact = self.sync_client.get(exact_key)
            if raw_exact:
                exact_data = json.loads(raw_exact.decode("utf-8") if isinstance(raw_exact, bytes) else raw_exact)
                exact_resp = exact_data.get("response", {})
                cacheable, cache_reason = self.is_cacheable_response(exact_resp)
                if not cacheable:
                    logger.info("Exact match found but rejected as uncacheable: %s", cache_reason)
                    try:
                        self.sync_client.delete(exact_key)
                    except Exception:
                        pass
                else:
                    return {
                        "cached": True,
                        "similarity": 1.0,
                        "matched_prompt": exact_data.get("prompt", query),
                        "response": exact_resp,
                        "fast_exact": True,
                    }
        except Exception:
            pass

        try:
            query_vec = self.embedder.embed_query(query)
            vec_bytes = np.array(query_vec, dtype=np.float32).tobytes()

            search_query = "*=>[KNN 1 @vector $vec EF_RUNTIME 64 AS score]"
            res = self.sync_client.execute_command(
                "FT.SEARCH", self.index_name,
                search_query,
                "PARAMS", "2", "vec", vec_bytes,
                "SORTBY", "score", "ASC",
                "RETURN", "3", "prompt", "response", "score",
                "DIALECT", "2",
            )

            # RediSearch response format: dict with 'results' or list [total, doc_id, [fields]]
            match = self._extract_match_from_ft_search(res)
            if match is None:
                return None

            cosine_dist, matched_prompt, response_str = match
            similarity = round(1.0 - cosine_dist, 4)

            if similarity >= th:
                has_conflict, reason = self.check_entity_conflict(query, matched_prompt)
                if has_conflict:
                    logger.info(
                        "Semantic match candidate (sim=%.4f) rejected by entity/intent guard: %s",
                        similarity, reason
                    )
                    return None

                response_data = json.loads(response_str)
                cacheable, cache_reason = self.is_cacheable_response(response_data)
                if not cacheable:
                    logger.info(
                        "Found cached candidate for '%s' (sim=%.4f), but rejected as uncacheable: %s",
                        matched_prompt, similarity, cache_reason
                    )
                    return None

                return {
                    "cached": True,
                    "similarity": similarity,
                    "matched_prompt": matched_prompt,
                    "response": response_data,
                    "query_vec": query_vec,
                }
            return None
        except Exception as err:
            logger.warning("Semantic cache lookup error: %s", err)
            return None

    async def alookup(
        self,
        query: str,
        threshold: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Asynchronously lookup semantically similar cached query from Redis."""
        await self.ainit_index()
        th = threshold or self.similarity_threshold

        # Fast path 0: Exact normalized text hash match (< 1ms in Redis, 0 CPU compute)
        norm_q = query.strip().lower()
        exact_key = f"{self.key_prefix}exact:{hashlib.sha256(norm_q.encode('utf-8')).hexdigest()}"
        try:
            raw_exact = await self.async_client.get(exact_key)
            if raw_exact:
                exact_data = json.loads(raw_exact.decode("utf-8") if isinstance(raw_exact, bytes) else raw_exact)
                exact_resp = exact_data.get("response", {})
                cacheable, cache_reason = self.is_cacheable_response(exact_resp)
                if not cacheable:
                    logger.info("Async exact match found but rejected as uncacheable: %s", cache_reason)
                    try:
                        await self.async_client.delete(exact_key)
                    except Exception:
                        pass
                else:
                    return {
                        "cached": True,
                        "similarity": 1.0,
                        "matched_prompt": exact_data.get("prompt", query),
                        "response": exact_resp,
                        "fast_exact": True,
                    }
        except Exception:
            pass

        try:
            # Vector embedding in thread to preserve async event loop
            import asyncio
            loop = asyncio.get_running_loop()
            query_vec = await loop.run_in_executor(None, self.embedder.embed_query, query)
            vec_bytes = np.array(query_vec, dtype=np.float32).tobytes()

            search_query = "*=>[KNN 1 @vector $vec EF_RUNTIME 64 AS score]"
            res = await self.async_client.execute_command(
                "FT.SEARCH", self.index_name,
                search_query,
                "PARAMS", "2", "vec", vec_bytes,
                "SORTBY", "score", "ASC",
                "RETURN", "3", "prompt", "response", "score",
                "DIALECT", "2",
            )

            match = self._extract_match_from_ft_search(res)
            if match is None:
                return None

            cosine_dist, matched_prompt, response_str = match
            similarity = round(1.0 - cosine_dist, 4)

            if similarity >= th:
                has_conflict, reason = self.check_entity_conflict(query, matched_prompt)
                if has_conflict:
                    logger.info(
                        "Semantic match candidate (sim=%.4f) rejected by entity/intent guard: %s",
                        similarity, reason
                    )
                    return None

                response_data = json.loads(response_str)
                cacheable, cache_reason = self.is_cacheable_response(response_data)
                if not cacheable:
                    logger.info(
                        "Async semantic candidate found (sim=%.4f), but rejected as uncacheable: %s",
                        similarity, cache_reason
                    )
                    return None

                return {
                    "cached": True,
                    "similarity": similarity,
                    "matched_prompt": matched_prompt,
                    "response": response_data,
                    "query_vec": query_vec,
                }
            return None
        except Exception as err:
            logger.warning("Async semantic cache lookup error: %s", err)
            return None

    def store(
        self,
        query: str,
        response: Dict[str, Any],
        query_vec: Optional[List[float]] = None,
        ttl_seconds: Optional[int] = None,
        owner: Optional[str] = None,
    ) -> bool:
        """Synchronously store query and generated answer in Redis semantic cache."""
        cacheable, cache_reason = self.is_cacheable_response(response)
        if not cacheable:
            logger.info("Semantic cache STORE bypassed for query '%s': %s", query, cache_reason)
            return False

        if isinstance(response, dict):
            response.setdefault("cached_at", time.time())
            response.setdefault("model_version", response.get("target_tier", "institution-rag-v1"))

        self.init_index()
        ttl = ttl_seconds or self.ttl_seconds

        try:
            if query_vec is None:
                query_vec = self.embedder.embed_query(query)

            vec_bytes = np.array(query_vec, dtype=np.float32).tobytes()
            key_id = uuid.uuid4().hex[:16]
            key = f"{self.key_prefix}{key_id}"

            payload = {
                "prompt": query,
                "response": json.dumps(response),
                "vector": vec_bytes,
                "created_at": time.time(),
            }
            owner_tag = self.owner_tag(owner)
            if owner_tag:
                payload["owner"] = owner_tag  # audit / targeted purge only; lookups stay shared

            self.sync_client.hset(key, mapping=payload)
            self.sync_client.expire(key, ttl)

            # Store exact hash key for sub-millisecond fast path
            try:
                norm_q = query.strip().lower()
                exact_key = f"{self.key_prefix}exact:{hashlib.sha256(norm_q.encode('utf-8')).hexdigest()}"
                self.sync_client.set(exact_key, json.dumps({"prompt": query, "response": response, "owner": owner_tag}), ex=ttl)
            except Exception:
                pass
            return True
        except Exception as err:
            logger.warning("Failed to store semantic cache entry: %s", err)
            return False

    async def astore(
        self,
        query: str,
        response: Dict[str, Any],
        query_vec: Optional[List[float]] = None,
        ttl_seconds: Optional[int] = None,
        owner: Optional[str] = None,
    ) -> bool:
        """Asynchronously store query and generated answer in Redis semantic cache."""
        cacheable, cache_reason = self.is_cacheable_response(response)
        if not cacheable:
            logger.info("Async semantic cache ASTORE bypassed for query '%s': %s", query, cache_reason)
            return False

        if isinstance(response, dict):
            response.setdefault("cached_at", time.time())
            response.setdefault("model_version", response.get("target_tier", "institution-rag-v1"))

        await self.ainit_index()
        ttl = ttl_seconds or self.ttl_seconds

        try:
            if query_vec is None:
                import asyncio
                loop = asyncio.get_running_loop()
                query_vec = await loop.run_in_executor(None, self.embedder.embed_query, query)

            vec_bytes = np.array(query_vec, dtype=np.float32).tobytes()
            key_id = uuid.uuid4().hex[:16]
            key = f"{self.key_prefix}{key_id}"

            payload = {
                "prompt": query,
                "response": json.dumps(response),
                "vector": vec_bytes,
                "created_at": time.time(),
            }
            owner_tag = self.owner_tag(owner)
            if owner_tag:
                payload["owner"] = owner_tag  # audit / targeted purge only; lookups stay shared

            await self.async_client.hset(key, mapping=payload)
            await self.async_client.expire(key, ttl)

            # Store exact hash key for sub-millisecond fast path
            try:
                norm_q = query.strip().lower()
                exact_key = f"{self.key_prefix}exact:{hashlib.sha256(norm_q.encode('utf-8')).hexdigest()}"
                await self.async_client.set(exact_key, json.dumps({"prompt": query, "response": response, "owner": owner_tag}), ex=ttl)
            except Exception:
                pass
            return True
        except Exception as err:
            logger.warning("Failed to async store semantic cache entry: %s", err)
            return False

    def count(self) -> int:
        """Return the count of keys currently in the semantic cache."""
        try:
            keys = self.sync_client.keys(f"{self.key_prefix}*")
            return len(keys)
        except Exception:
            return 0

    async def acount(self) -> int:
        """Return count of keys asynchronously."""
        try:
            keys = await self.async_client.keys(f"{self.key_prefix}*")
            return len(keys)
        except Exception:
            return 0

    def clear(self) -> bool:
        """Delete all cached entries and re-initialize index."""
        try:
            keys = self.sync_client.keys(f"{self.key_prefix}*")
            if keys:
                self.sync_client.delete(*keys)
            try:
                self.sync_client.execute_command("FT.DROPINDEX", self.index_name, "DD")
            except Exception:
                pass
            self._index_initialized = False
            self.init_index()
            return True
        except Exception as e:
            logger.warning("Failed to clear semantic cache: %s", e)
            return False

    async def aclear(self) -> bool:
        """Async delete all cached entries and re-initialize index."""
        try:
            keys = await self.async_client.keys(f"{self.key_prefix}*")
            if keys:
                await self.async_client.delete(*keys)
            try:
                await self.async_client.execute_command("FT.DROPINDEX", self.index_name, "DD")
            except Exception:
                pass
            self._index_initialized = False
            await self.ainit_index()
            return True
        except Exception as e:
            logger.warning("Failed to async clear semantic cache: %s", e)
            return False

    def purge_uncacheable_entries(self) -> int:
        """Scan all cached entries in Redis and delete any containing insufficient data, unretrieved docs, or errors."""
        purged = 0
        try:
            keys = self.sync_client.keys(f"{self.key_prefix}*")
            for k in keys:
                try:
                    k_type = self.sync_client.type(k)
                    k_type_str = k_type.decode("utf-8") if isinstance(k_type, bytes) else str(k_type)
                    if k_type_str == "hash":
                        raw_resp = self.sync_client.hget(k, "response")
                        if not raw_resp:
                            continue
                        resp_str = raw_resp.decode("utf-8", errors="replace") if isinstance(raw_resp, bytes) else str(raw_resp)
                        resp_data = json.loads(resp_str)
                        cacheable, _ = self.is_cacheable_response(resp_data)
                        if not cacheable:
                            self.sync_client.delete(k)
                            purged += 1
                    elif k_type_str == "string":
                        raw_val = self.sync_client.get(k)
                        if not raw_val:
                            continue
                        val_str = raw_val.decode("utf-8", errors="replace") if isinstance(raw_val, bytes) else str(raw_val)
                        val_data = json.loads(val_str)
                        resp_data = val_data.get("response", {}) if isinstance(val_data, dict) else val_data
                        cacheable, _ = self.is_cacheable_response(resp_data)
                        if not cacheable:
                            self.sync_client.delete(k)
                            purged += 1
                except Exception:
                    continue
            logger.info("Purged %d uncacheable / insufficient data entries from Redis.", purged)
        except Exception as e:
            logger.warning("Error purging uncacheable entries: %s", e)
        return purged

    async def apurge_uncacheable_entries(self) -> int:
        """Asynchronously scan all cached entries in Redis and delete uncacheable entries."""
        purged = 0
        try:
            keys = await self.async_client.keys(f"{self.key_prefix}*")
            for k in keys:
                try:
                    k_type = await self.async_client.type(k)
                    k_type_str = k_type.decode("utf-8") if isinstance(k_type, bytes) else str(k_type)
                    if k_type_str == "hash":
                        raw_resp = await self.async_client.hget(k, "response")
                        if not raw_resp:
                            continue
                        resp_str = raw_resp.decode("utf-8", errors="replace") if isinstance(raw_resp, bytes) else str(raw_resp)
                        resp_data = json.loads(resp_str)
                        cacheable, _ = self.is_cacheable_response(resp_data)
                        if not cacheable:
                            await self.async_client.delete(k)
                            purged += 1
                    elif k_type_str == "string":
                        raw_val = await self.async_client.get(k)
                        if not raw_val:
                            continue
                        val_str = raw_val.decode("utf-8", errors="replace") if isinstance(raw_val, bytes) else str(raw_val)
                        val_data = json.loads(val_str)
                        resp_data = val_data.get("response", {}) if isinstance(val_data, dict) else val_data
                        cacheable, _ = self.is_cacheable_response(resp_data)
                        if not cacheable:
                            await self.async_client.delete(k)
                            purged += 1
                except Exception:
                    continue
            logger.info("Async purged %d uncacheable / insufficient data entries from Redis.", purged)
        except Exception as e:
            logger.warning("Error async purging uncacheable entries: %s", e)
        return purged

    def purge(
        self,
        query: str,
        threshold: float = 0.85,
    ) -> int:
        """Purge all semantic cache entries matching query with similarity >= threshold."""
        self.init_index()
        purged = 0
        try:
            norm_q = query.strip().lower()
            q_hash = hashlib.sha256(norm_q.encode("utf-8")).hexdigest()
            exact_key = f"{self.key_prefix}exact:{q_hash}"
            if self.sync_client.delete(exact_key):
                purged += 1

            query_vec = self.embedder.embed_query(query)
            vec_bytes = np.array(query_vec, dtype=np.float32).tobytes()

            search_query = "*=>[KNN 50 @vector $vec EF_RUNTIME 64 AS score]"
            res = self.sync_client.execute_command(
                "FT.SEARCH", self.index_name,
                search_query,
                "PARAMS", "2", "vec", vec_bytes,
                "SORTBY", "score", "ASC",
                "RETURN", "3", "prompt", "response", "score",
                "DIALECT", "2",
            )

            keys_to_delete = set()
            if isinstance(res, dict):
                results = res.get(b"results") or res.get("results") or []
                for item in results:
                    key_id = item.get(b"id") or item.get("id")
                    extra = item.get(b"extra_attributes") or item.get("extra_attributes") or {}
                    score_raw = extra.get(b"score") or extra.get("score")
                    if key_id and score_raw is not None:
                        similarity = 1.0 - float(score_raw)
                        if similarity >= threshold:
                            k_str = key_id.decode() if isinstance(key_id, bytes) else str(key_id)
                            keys_to_delete.add(k_str)
                            p_raw = extra.get(b"prompt") or extra.get("prompt")
                            if p_raw:
                                p_str = p_raw.decode("utf-8", errors="replace") if isinstance(p_raw, bytes) else str(p_raw)
                                p_hash = hashlib.sha256(p_str.strip().lower().encode("utf-8")).hexdigest()
                                ex_k = f"{self.key_prefix}exact:{p_hash}"
                                keys_to_delete.add(ex_k)
            elif isinstance(res, (list, tuple)) and len(res) >= 3:
                for i in range(1, len(res), 2):
                    key_id = res[i]
                    fields_list = res[i + 1]
                    fields = {}
                    if isinstance(fields_list, (list, tuple)):
                        for j in range(0, len(fields_list), 2):
                            k = fields_list[j].decode() if isinstance(fields_list[j], bytes) else str(fields_list[j])
                            v = fields_list[j + 1]
                            fields[k] = v.decode() if isinstance(v, bytes) else str(v)
                        score = float(fields.get("score", 1.0))
                        similarity = 1.0 - score
                        if similarity >= threshold:
                            k_str = key_id.decode() if isinstance(key_id, bytes) else str(key_id)
                            keys_to_delete.add(k_str)
                            if "prompt" in fields:
                                p_hash = hashlib.sha256(fields["prompt"].strip().lower().encode("utf-8")).hexdigest()
                                ex_k = f"{self.key_prefix}exact:{p_hash}"
                                keys_to_delete.add(ex_k)

            if keys_to_delete:
                del_count = self.sync_client.delete(*keys_to_delete)
                purged += del_count

            logger.info("Purged %d semantic cache entries for query '%s' at threshold >= %.2f", purged, query, threshold)
            return purged
        except Exception as e:
            logger.warning("Error during semantic cache purge: %s", e)
            return purged

    async def apurge(
        self,
        query: str,
        threshold: float = 0.85,
    ) -> int:
        """Asynchronously purge all semantic cache entries matching query with similarity >= threshold."""
        await self.ainit_index()
        purged = 0
        try:
            norm_q = query.strip().lower()
            q_hash = hashlib.sha256(norm_q.encode("utf-8")).hexdigest()
            exact_key = f"{self.key_prefix}exact:{q_hash}"
            del_exact = await self.async_client.delete(exact_key)
            if del_exact:
                purged += 1

            import asyncio
            loop = asyncio.get_running_loop()
            query_vec = await loop.run_in_executor(None, self.embedder.embed_query, query)
            vec_bytes = np.array(query_vec, dtype=np.float32).tobytes()

            search_query = "*=>[KNN 50 @vector $vec EF_RUNTIME 64 AS score]"
            res = await self.async_client.execute_command(
                "FT.SEARCH", self.index_name,
                search_query,
                "PARAMS", "2", "vec", vec_bytes,
                "SORTBY", "score", "ASC",
                "RETURN", "3", "prompt", "response", "score",
                "DIALECT", "2",
            )

            keys_to_delete = set()
            if isinstance(res, dict):
                results = res.get(b"results") or res.get("results") or []
                for item in results:
                    key_id = item.get(b"id") or item.get("id")
                    extra = item.get(b"extra_attributes") or item.get("extra_attributes") or {}
                    score_raw = extra.get(b"score") or extra.get("score")
                    if key_id and score_raw is not None:
                        similarity = 1.0 - float(score_raw)
                        if similarity >= threshold:
                            k_str = key_id.decode() if isinstance(key_id, bytes) else str(key_id)
                            keys_to_delete.add(k_str)
                            p_raw = extra.get(b"prompt") or extra.get("prompt")
                            if p_raw:
                                p_str = p_raw.decode("utf-8", errors="replace") if isinstance(p_raw, bytes) else str(p_raw)
                                p_hash = hashlib.sha256(p_str.strip().lower().encode("utf-8")).hexdigest()
                                ex_k = f"{self.key_prefix}exact:{p_hash}"
                                keys_to_delete.add(ex_k)
            elif isinstance(res, (list, tuple)) and len(res) >= 3:
                for i in range(1, len(res), 2):
                    key_id = res[i]
                    fields_list = res[i + 1]
                    fields = {}
                    if isinstance(fields_list, (list, tuple)):
                        for j in range(0, len(fields_list), 2):
                            k = fields_list[j].decode() if isinstance(fields_list[j], bytes) else str(fields_list[j])
                            v = fields_list[j + 1]
                            fields[k] = v.decode() if isinstance(v, bytes) else str(v)
                        score = float(fields.get("score", 1.0))
                        similarity = 1.0 - score
                        if similarity >= threshold:
                            k_str = key_id.decode() if isinstance(key_id, bytes) else str(key_id)
                            keys_to_delete.add(k_str)
                            if "prompt" in fields:
                                p_hash = hashlib.sha256(fields["prompt"].strip().lower().encode("utf-8")).hexdigest()
                                ex_k = f"{self.key_prefix}exact:{p_hash}"
                                keys_to_delete.add(ex_k)

            if keys_to_delete:
                del_count = await self.async_client.delete(*keys_to_delete)
                purged += del_count

            logger.info("Async purged %d semantic cache entries for query '%s' at threshold >= %.2f", purged, query, threshold)
            return purged
        except Exception as e:
            logger.warning("Error during async semantic cache purge: %s", e)
            return purged

    @staticmethod
    def _extract_match_from_ft_search(res: Any) -> Optional[Tuple[float, str, str]]:
        """Parse raw RediSearch response into (score, prompt, response)."""
        if not res:
            return None

        # RediSearch dict format: {'results': [{'extra_attributes': {'score': ..., 'prompt': ..., 'response': ...}}]}
        if isinstance(res, dict):
            results = res.get(b"results") or res.get("results") or []
            if not results:
                return None
            first = results[0]
            extra = first.get(b"extra_attributes") or first.get("extra_attributes") or {}
            score_raw = extra.get(b"score") or extra.get("score")
            prompt_raw = extra.get(b"prompt") or extra.get("prompt")
            resp_raw = extra.get(b"response") or extra.get("response")

            if score_raw is None or prompt_raw is None or resp_raw is None:
                return None

            score = float(score_raw)
            prompt = prompt_raw.decode("utf-8", errors="replace") if isinstance(prompt_raw, bytes) else str(prompt_raw)
            response = resp_raw.decode("utf-8", errors="replace") if isinstance(resp_raw, bytes) else str(resp_raw)
            return score, prompt, response

        # Legacy RediSearch list format: [total, doc_id, [k1, v1, k2, v2, ...]]
        if isinstance(res, (list, tuple)) and len(res) >= 3:
            total = res[0]
            if total == 0:
                return None
            fields_list = res[2]
            if isinstance(fields_list, (list, tuple)):
                fields = {}
                for i in range(0, len(fields_list), 2):
                    k = fields_list[i].decode() if isinstance(fields_list[i], bytes) else str(fields_list[i])
                    v = fields_list[i + 1]
                    fields[k] = v.decode() if isinstance(v, bytes) else str(v)
                score = float(fields.get("score", 1.0))
                prompt = fields.get("prompt", "")
                response = fields.get("response", "{}")
                return score, prompt, response

        return None

    @staticmethod
    def owner_tag(owner: Optional[str]) -> str:
        """Keyed, non-reversible tag of the user who created a cache entry (audit / targeted purge).

        The cache stays shared across users; the tag is recorded, never used to filter lookups.
        """
        if not owner:
            return ""
        secret = os.getenv("SESSION_SECRET", "semantic-cache-owner")
        return hmac.new(secret.encode("utf-8"), owner.strip().lower().encode("utf-8"), hashlib.sha256).hexdigest()[:16]

    @staticmethod
    def check_entity_conflict(query_a: str, query_b: str) -> Tuple[bool, str]:
        """Return (True, reason) when two embedding-similar queries must NOT share a cached answer.

        Dense embeddings (all-MiniLM-L6-v2) measure topical overlap, not intent: 'in 2023' vs
        'in 2024', 'Why does X always...' vs 'Does X always...', 'advantages' vs 'disadvantages'
        and 'HCl added to NaOH' vs 'NaOH added to HCl' all score 0.91-0.999, so no similarity
        threshold separates them from genuine paraphrases. A false conflict only costs a fresh
        generation; a false hit serves a wrong (possibly poisoned) answer, so checks are strict.
        """
        import re

        # 1. Numerical & Temporal Entity Check (e.g., '2023' vs '2024', '5' vs '50', '200' vs '400')
        nums_a = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", query_a.lower()))
        nums_b = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", query_b.lower()))
        if nums_a != nums_b:
            return True, f"Numerical / Year mismatch: {nums_a} vs {nums_b}"

        # 2. Negation / Polarity Check (prevents answering the inverse question)
        words_a = set(re.findall(r"\b\w+(?:'\w+)?\b", _normalize_quotes(query_a.lower())))
        words_b = set(re.findall(r"\b\w+(?:'\w+)?\b", _normalize_quotes(query_b.lower())))
        has_neg_a = _has_negation(words_a)
        has_neg_b = _has_negation(words_b)
        if has_neg_a != has_neg_b:
            return True, f"Polarity / Negation mismatch: {has_neg_a} vs {has_neg_b}"

        # 3. Intent equivalence (red-team finding: semantic cache intent collisions)
        return check_intent_conflict(query_a, query_b)


# ---------------------------------------------------------------------------
# Intent-equivalence guard for semantic cache hits
# ---------------------------------------------------------------------------
_NEGATIONS = {"not", "no", "never", "without", "none", "neither", "nor", "cannot", "unable"}

_WH_TYPES = {"why": "why", "who": "who", "whom": "who", "whose": "who", "when": "when", "where": "where"}
_YESNO_AUX = {"is", "are", "was", "were", "do", "does", "did", "can", "could", "will", "would", "should",
              "shall", "has", "have", "had", "may", "might", "must"}
_INTRO_VERBS = {"explain", "describe", "tell", "discuss", "show", "know", "understand"}
_POLITE_PREFIX = re.compile(
    r"^(?:(?:please|kindly)\s+|(?:can|could|would|will)\s+you\s+(?:please\s+)?|"
    r"(?:i\s+(?:want|would\s+like|wish)\s+to\s+know)\s+|do\s+you\s+know\s+)+"
)
_QUANTIFIERS = {"always", "never", "sometimes", "often", "usually", "rarely", "all", "every", "each", "any",
                "some", "none", "only", "most", "few", "many", "both", "either", "neither"}
# Directional words whose left/right operands must not swap (titration order, 'effect of X on Y', 'X than Y').
_DIRECTIONAL = {"to", "into", "onto", "on", "from", "than", "toward", "towards", "over", "against"}
_INTENT_STOPWORDS = {
    "the", "a", "an", "of", "in", "for", "and", "or", "with", "as", "at", "by", "about", "its", "it", "this", "that",
    "these", "those", "there", "their", "be", "been", "being", "s", "what", "which", "how", "why", "who", "whom",
    "whose", "when", "where", "me", "i", "you", "we", "please", "kindly",
} | _YESNO_AUX | _INTRO_VERBS | _DIRECTIONAL
_ANTONYM_PAIRS = [
    ("advantage", "disadvantage"), ("advantage", "limitation"), ("benefit", "drawback"), ("benefit", "risk"),
    ("pro", "con"), ("merit", "demerit"), ("strength", "weakness"), ("application", "limitation"),
    ("use", "limitation"), ("increase", "decrease"), ("increase", "reduce"), ("rise", "fall"), ("raise", "lower"),
    ("high", "low"), ("higher", "lower"), ("highest", "lowest"), ("more", "less"), ("most", "least"),
    ("maximum", "minimum"), ("maximize", "minimize"), ("max", "min"), ("gain", "loss"), ("positive", "negative"),
    ("success", "failure"), ("succeed", "fail"), ("find", "fail"), ("best", "worst"), ("better", "worse"),
    ("fast", "slow"), ("faster", "slower"), ("large", "small"), ("larger", "smaller"), ("largest", "smallest"),
    ("big", "small"), ("strong", "weak"), ("hot", "cold"), ("heat", "cool"), ("heating", "cooling"),
    ("add", "remove"), ("before", "after"), ("above", "below"), ("inside", "outside"), ("input", "output"),
    ("acidic", "basic"), ("oxidation", "reduction"), ("oxidize", "reduce"), ("charge", "discharge"),
    ("cause", "prevent"), ("enable", "prevent"), ("true", "false"), ("valid", "invalid"), ("safe", "unsafe"),
    ("stable", "unstable"), ("possible", "impossible"), ("accurate", "inaccurate"), ("efficient", "inefficient"),
    ("agree", "disagree"), ("support", "oppose"), ("include", "exclude"), ("first", "last"), ("start", "end"),
    ("early", "late"), ("past", "future"), ("upper", "lower"), ("forward", "backward"), ("import", "export"),
    ("supply", "demand"), ("similarity", "difference"), ("similar", "different"), ("same", "different"),
    ("internal", "external"), ("cause", "avoid"), ("correct", "incorrect"), ("legal", "illegal"),
]
_NEGATING_PREFIXES = ("un", "in", "im", "ir", "il", "dis", "non")
_SUFFIXES = ("ization", "isation", "ations", "ation", "ments", "ment", "ings", "ing", "ies", "es", "ed", "ly", "s")


def _normalize_quotes(text: str) -> str:
    return text.replace("\u2019", "'").replace("\u2018", "'").replace("`", "'")


def _has_negation(words) -> bool:
    return any(w in _NEGATIONS or w.endswith("n't") for w in words)


def _stem(word: str) -> str:
    """Tiny suffix stripper so 'advantages'/'advantage' and 'increasing'/'increase' compare equal."""
    for suf in _SUFFIXES:
        if suf == "s" and word.endswith("ss"):  # 'weakness', 'process', 'glass' are not plurals
            continue
        if len(word) > len(suf) + 2 and word.endswith(suf):
            word = word[: -len(suf)] + ("y" if suf == "ies" else "")
            break
    if len(word) > 4 and word.endswith("e"):
        word = word[:-1]
    return word


_ANTONYMS: Dict[str, set] = {}
for _a, _b in _ANTONYM_PAIRS:
    _ANTONYMS.setdefault(_stem(_a), set()).add(_stem(_b))
    _ANTONYMS.setdefault(_stem(_b), set()).add(_stem(_a))


def _intent_tokens(query: str) -> List[str]:
    text = _normalize_quotes(query.lower())
    text = re.sub(r"\bnon-(?=[a-z])", "non", text).replace("-", " ")
    text = _POLITE_PREFIX.sub("", text.strip())
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text)


def _question_type(tokens: List[str]) -> str:
    """Coarse intent class: why / who / when / where / yesno / describe (what, how, explain, keyword search)."""
    if not tokens:
        return "describe"
    first = tokens[0]
    if first in _INTRO_VERBS and len(tokens) > 1:  # 'Explain why X' asks the same thing as 'Why X'
        if tokens[1] == "me" and len(tokens) > 2:
            first = tokens[2]
        else:
            first = tokens[1]
    if first == "how" and len(tokens) > 1 and tokens[1] == "come":
        return "why"
    if first in _WH_TYPES:
        return _WH_TYPES[first]
    if first in _YESNO_AUX:
        return "yesno"
    return "describe"


def _content_stems(tokens: List[str]) -> set:
    return {_stem(w) for w in tokens if w not in _INTENT_STOPWORDS and w not in _QUANTIFIERS}


def _is_content(word: str) -> bool:
    return word not in _INTENT_STOPWORDS and word not in _QUANTIFIERS


def _sides(tokens: List[str], i: int) -> Tuple[set, set]:
    """Content stems before / after position i."""
    return ({_stem(w) for w in tokens[:i] if _is_content(w)}, {_stem(w) for w in tokens[i + 1:] if _is_content(w)})


def _crossing(side_a: Tuple[set, set], side_b: Tuple[set, set]) -> Optional[Tuple[str, str]]:
    """Return (x, y) if x moves from before->after the pivot AND y moves after->before (a true swap).

    Moving a whole phrase to the other side ('CO2 to methanol conversion') is a harmless reordering;
    only terms crossing each other ('HCl added to NaOH' vs 'NaOH added to HCl') change meaning.
    """
    (before_a, after_a), (before_b, after_b) = side_a, side_b
    to_right = sorted(w for w in before_a - after_a if w in after_b and w not in before_b)
    to_left = sorted(w for w in after_a - before_a if w in before_b and w not in after_b)
    return (to_right[0], to_left[0]) if to_right and to_left else None


def check_intent_conflict(query_a: str, query_b: str) -> Tuple[bool, str]:
    """Rule-based check that two queries ask the same thing (not just share a topic)."""
    ta, tb = _intent_tokens(query_a), _intent_tokens(query_b)

    # a) Question form: 'Why does X...' presupposes X; 'Does X...' asks whether X holds.
    qa, qb = _question_type(ta), _question_type(tb)
    if qa != qb:
        return True, f"Intent mismatch: question type '{qa}' vs '{qb}'"

    # b) Opposite / negated terms: advantages vs disadvantages, increase vs decrease, accurate vs inaccurate.
    sa, sb = _content_stems(ta), _content_stems(tb)
    for w in sa ^ sb:
        other = sb if w in sa else sa
        opposite = _ANTONYMS.get(w, set()) & other
        if opposite:
            return True, f"Intent mismatch: opposite terms '{w}' vs '{sorted(opposite)[0]}'"
        for p in _NEGATING_PREFIXES:
            base = w[len(p):]
            if (w.startswith(p) and len(base) >= 4 and base in other) or (p + w) in other:
                return True, f"Intent mismatch: negated form of '{w}'"

    # c) Quantifier scope: 'always' / 'every' / 'some' / 'only' change what is being claimed.
    quant_a, quant_b = set(ta) & _QUANTIFIERS, set(tb) & _QUANTIFIERS
    if quant_a != quant_b:
        return True, f"Intent mismatch: quantifiers {sorted(quant_a)} vs {sorted(quant_b)}"

    # d) Role reversal: two terms swap sides of a directional word ('HCl added to NaOH' vs
    #    'NaOH added to HCl', 'X than Y') or of a shared verb/noun ('insulin inhibits glucagon').
    for word in _DIRECTIONAL:
        pos_a = [i for i, w in enumerate(ta) if w == word]
        pos_b = [i for i, w in enumerate(tb) if w == word]
        if pos_a and len(pos_a) == len(pos_b):
            for i, j in zip(pos_a, pos_b):
                swap = _crossing(_sides(ta, i), _sides(tb, j))
                if swap:
                    return True, f"Intent mismatch: '{swap[0]}' and '{swap[1]}' swap around '{word}'"
    stems_a = [_stem(w) if _is_content(w) else None for w in ta]
    stems_b = [_stem(w) if _is_content(w) else None for w in tb]
    # Verb/noun pivots only for pure permutations (same content words, different order), so that
    # rephrasings like 'monitoring water quality using IoT' vs 'IoT for monitoring water quality' still hit.
    same_words = sorted(x for x in stems_a if x) == sorted(x for x in stems_b if x)
    for pivot in (sa & sb) if same_words else ():
        if stems_a.count(pivot) == 1 and stems_b.count(pivot) == 1:
            swap = _crossing(_sides(ta, stems_a.index(pivot)), _sides(tb, stems_b.index(pivot)))
            if swap:
                return True, f"Intent mismatch: '{swap[0]}' and '{swap[1]}' swap around '{pivot}'"

    return False, "No conflict"
