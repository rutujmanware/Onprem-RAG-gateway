import asyncio
import hashlib
import json
import logging
import os
from typing import Optional
import numpy as np
try:
    import redis.asyncio as redis
except ImportError:
    redis = None

from pipeline.embeddings.embedder import LocalEmbedder
from pipeline.env_utils import resolve_redis_url
from pipeline.text_processing.chunker import RecursiveCharacterChunker
from pipeline.text_processing.cleaner import TextCleaner

logger = logging.getLogger(__name__)

class RedisDocumentIndexer:
    def __init__(self, redis_url=None, index_name="rag_documents_v2_idx", vector_dim=384):
        self.redis_url = resolve_redis_url(redis_url)
        self.index_name = index_name
        self.vector_dim = vector_dim
        self.cleaner = TextCleaner()
        self.chunker = RecursiveCharacterChunker(1600, 150)
        self.embedder = LocalEmbedder(dimension=vector_dim)

    async def create_index(self, r):
        try:
            await r.execute_command("FT.INFO", self.index_name)
        except Exception:
            cmd = [
                "FT.CREATE", self.index_name, "ON", "HASH", "PREFIX", "1", "doc:chunk:",
                "SCHEMA",
                "doc_id", "TAG",
                "thesis_id", "TAG",
                "chapter", "TAG",
                "doc_version", "TAG",
                "access_groups", "TAG",
                "source_uri", "TEXT",
                "title", "TEXT",
                "author", "TEXT",
                "department", "TEXT",
                "advisor", "TEXT",
                "date", "TEXT",
                "degree", "TEXT",
                "handle_id", "TEXT",
                "content", "TEXT",
                "chunk_index", "NUMERIC",
                "metadata", "TEXT",
                "vector", "VECTOR", "HNSW", "6", "TYPE", "FLOAT32",
                "DIM", str(self.vector_dim), "DISTANCE_METRIC", "COSINE"
            ]
            try:
                await r.execute_command(*cmd)
                logger.info("Created RediSearch vector index with metadata fields: %s", self.index_name)
            except Exception as e:
                logger.warning("Index create notice: %s", e)

    async def index_document(self, doc_id: str, raw_text: str, metadata=None, is_paper=False) -> int:
        cleaned = self.cleaner.clean(raw_text, is_paper)
        if not cleaned: return 0
        chunks = self.chunker.chunk_document(doc_id, cleaned, metadata)
        if not chunks: return 0
        texts = [c.content for c in chunks]
        embs = self.embedder.embed_documents(texts)

        if not redis:
            msg = "[ERROR] 'redis' Python package is not installed! Chunks cannot be stored. Run: pip install redis"
            logger.error(msg)
            print(f"\n{msg}\n", flush=True)
            return 0

        meta = metadata or {}
        title_val = str(meta.get("title", ""))
        authors = meta.get("authors", [])
        author_val = ", ".join(authors) if isinstance(authors, list) else str(authors)
        advisors = meta.get("advisors", [])
        advisor_val = ", ".join(advisors) if isinstance(advisors, list) else str(advisors)
        dept_val = str(meta.get("department", ""))
        date_val = str(meta.get("date", ""))
        degree_val = str(meta.get("degree", ""))
        handle_val = str(meta.get("handle_id", doc_id))

        r = redis.from_url(self.redis_url, encoding="utf-8", decode_responses=False)
        try:
            await self.create_index(r)
            thesis_id = str(meta.get("thesis_id", doc_id)).strip().lower().replace("-", "_").replace(" ", "_")
            revision_digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]
            doc_version = f"v-{revision_digest}"
            current_key = f"doc:version:{thesis_id}:current"
            previous_raw = await r.get(current_key)
            previous = previous_raw.decode() if isinstance(previous_raw, bytes) else previous_raw
            access_policy = {
                "roles": list(meta.get("access_roles", ["student", "faculty", "examiner"])),
                "users": list(meta.get("access_users", [])),
            }
            await r.set(current_key, doc_version)
            await r.set(f"doc:access:{thesis_id}", json.dumps(access_policy))
            catalog_record = {
                "thesis_id": thesis_id,
                "title": title_val or thesis_id,
                "author": author_val,
                "department": dept_val,
                "year": date_val[:4] if date_val else "",
                "chapters": sorted({str(c.metadata.get("chapter", "all")) for c in chunks}),
            }
            await r.set(f"doc:catalog:{thesis_id}", json.dumps(catalog_record))
            history_key = f"doc:version:{thesis_id}:history"
            old_history = await r.lrange(history_key, 0, -1)
            await r.lrem(f"doc:version:{thesis_id}:history", 0, doc_version)
            await r.lpush(history_key, doc_version)
            await r.ltrim(history_key, 0, 2)
            pipe = r.pipeline(transaction=False)
            for chunk, emb in zip(chunks, embs):
                key = f"doc:chunk:{thesis_id}:{doc_version}:{chunk.chunk_index}"
                pipe.hset(key, mapping={
                    "doc_id": chunk.doc_id,
                    "thesis_id": thesis_id,
                    "chapter": str(chunk.metadata.get("chapter", "all")),
                    "doc_version": doc_version,
                    "access_groups": ",".join(access_policy["roles"]),
                    "source_uri": str(meta.get("source_uri", meta.get("source", doc_id))),
                    "title": title_val,
                    "author": author_val,
                    "department": dept_val,
                    "advisor": advisor_val,
                    "date": date_val,
                    "degree": degree_val,
                    "handle_id": handle_val,
                    "content": chunk.content,
                    "chunk_index": chunk.chunk_index,
                    "metadata": json.dumps(chunk.metadata),
                    "vector": np.array(emb, dtype=np.float32).tobytes(),
                })
            await pipe.execute()
            retained_raw = await r.lrange(history_key, 0, -1)
            retained = {
                value.decode("utf-8") if isinstance(value, bytes) else str(value)
                for value in retained_raw
            }
            old_versions = {
                value.decode("utf-8") if isinstance(value, bytes) else str(value)
                for value in old_history
            } - retained
            for obsolete_version in old_versions:
                stale_keys = await r.execute_command(
                    "FT.SEARCH", self.index_name,
                    f"(@thesis_id:{{{thesis_id}}} @doc_version:{{{obsolete_version}}})",
                    "NOCONTENT", "LIMIT", "0", "10000",
                )
                if stale_keys and int(stale_keys[0]) and len(stale_keys) > 1:
                    await r.delete(*stale_keys[1:])
            if previous and previous != doc_version:
                from gateway.cache.l0_exact import L0ExactCache
                from gateway.cache.l1_semantic import L1SemanticCache
                l0 = L0ExactCache(redis_client=r)
                l1 = L1SemanticCache(redis_client=r)
                await l1.init_index()
                await l0.invalidate_version(thesis_id, str(previous))
                await l1.invalidate_version(thesis_id, str(previous))
            print(f"[REDIS] Successfully committed {len(chunks)} chunks with metadata to Redis ({self.redis_url})", flush=True)
        except Exception as e:
            logger.error("Failed to write to Redis Stack: %s", e)
            print(f"[REDIS ERROR] Could not save to Redis Stack: {e}", flush=True)
            raise e
        finally:
            await r.aclose()
        return len(chunks)
