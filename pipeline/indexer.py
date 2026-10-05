import asyncio
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
from pipeline.text_processing.chunker import RecursiveCharacterChunker
from pipeline.text_processing.cleaner import TextCleaner

logger = logging.getLogger(__name__)

class RedisDocumentIndexer:
    def __init__(self, redis_url=None, index_name="rag_documents_idx", vector_dim=384):
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.index_name = index_name
        self.vector_dim = vector_dim
        self.cleaner = TextCleaner()
        self.chunker = RecursiveCharacterChunker(512, 64)
        self.embedder = LocalEmbedder(dimension=vector_dim)

    async def create_index(self, r):
        try:
            await r.execute_command("FT.INFO", self.index_name)
        except Exception:
            cmd = [
                "FT.CREATE", self.index_name, "ON", "HASH", "PREFIX", "1", "doc:chunk:",
                "SCHEMA",
                "doc_id", "TEXT",
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
            pipe = r.pipeline(transaction=False)
            for chunk, emb in zip(chunks, embs):
                key = f"doc:chunk:{chunk.chunk_id}"
                pipe.hset(key, mapping={
                    "doc_id": chunk.doc_id,
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
            print(f"[REDIS] Successfully committed {len(chunks)} chunks with metadata to Redis ({self.redis_url})", flush=True)
        except Exception as e:
            logger.error("Failed to write to Redis Stack: %s", e)
            print(f"[REDIS ERROR] Could not save to Redis Stack: {e}", flush=True)
            raise e
        finally:
            await r.aclose()
        return len(chunks)
