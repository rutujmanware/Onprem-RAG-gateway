#!/usr/bin/env python3
"""Single-file Chunk Retriever for Redis Vector Database.

Flow:
1. Receives a user query.
2. Embeds the query using the 384-dim dense embedder (all-MiniLM-L6-v2) on CPU.
3. Executes a KNN vector similarity search against the RediSearch index.
4. Retrieves and returns matching text chunks with metadata & similarity scores.
"""

from __future__ import annotations

import os
import sys

# CRITICAL: Prevent OpenMP / C++ threading conflicts
# These must be set before numpy, torch, or sentence_transformers are imported.
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import argparse
import asyncio
import math
from pathlib import Path
from typing import Any, Dict, List, Optional
import numpy as np

# Ensure project root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
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


def load_env_file():
    """Auto-load variables from .env if present."""
    candidates = [
        Path(".env"),
        ROOT_DIR / ".env",
        Path(__file__).resolve().parent / ".env"
    ]
    for env_path in candidates:
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())
            break


load_env_file()


class RedisChunkRetriever:
    """Embeds queries and retrieves the top-k most relevant chunks from Redis."""

    def __init__(
        self,
        redis_url: Optional[str] = None,
        index_name: str = "rag_documents_idx",
        vector_dim: int = 384,
        device: str = "cpu"
    ):
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.index_name = index_name or os.getenv("REDIS_INDEX_NAME", "rag_documents_idx")
        self.vector_dim = vector_dim
        # Force CPU device for embedding to avoid any GPU/CUDA context driver crashes
        self.embedder = LocalEmbedder(dimension=self.vector_dim, device=device)

    def _build_filter_expression(self, filter_dict: Optional[Dict[str, Any]] = None) -> str:
        """Format optional metadata filter dictionary into RediSearch query clauses."""
        if not filter_dict:
            return "*"
        clauses = []
        for field, value in filter_dict.items():
            if value is None or value == "":
                continue
            if isinstance(value, (int, float)):
                clauses.append(f"@{field}:[{value} {value}]")
            else:
                val_str = str(value).replace('"', '\\"').strip()
                clauses.append(f'@{field}:"{val_str}"')
        return " ".join(clauses) if clauses else "*"

    def search(self, query: str, top_k: int = 3, filter_dict: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Synchronous retrieval: embed query and match against Redis vector index."""
        if not redis:
            raise RuntimeError("The 'redis' package is not installed. Run: pip install redis")

        # 1. Embed query into float32 vector bytes
        query_vec = self.embedder.embed_query(query)
        vec_bytes = np.array(query_vec, dtype=np.float32).tobytes()

        # 2. Search Redis Stack RediSearch vector index
        r = redis.from_url(self.redis_url, encoding="utf-8", decode_responses=False)
        try:
            base_filter = self._build_filter_expression(filter_dict)
            search_query = f"({base_filter})=>[KNN {top_k} @vector $vec AS score]"
            results = r.execute_command(
                "FT.SEARCH",
                self.index_name,
                search_query,
                "PARAMS", "2", "vec", vec_bytes,
                "SORTBY", "score", "ASC",
                "DIALECT", "2",
            )
            return self._parse_results(results)
        finally:
            r.close()

    async def asearch(self, query: str, top_k: int = 3, filter_dict: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Asynchronous retrieval for async applications."""
        if not aioredis:
            raise RuntimeError("The 'redis' package is not installed. Run: pip install redis")

        query_vec = self.embedder.embed_query(query)
        vec_bytes = np.array(query_vec, dtype=np.float32).tobytes()

        r = aioredis.from_url(self.redis_url, encoding="utf-8", decode_responses=False)
        try:
            base_filter = self._build_filter_expression(filter_dict)
            search_query = f"({base_filter})=>[KNN {top_k} @vector $vec AS score]"
            results = await r.execute_command(
                "FT.SEARCH",
                self.index_name,
                search_query,
                "PARAMS", "2", "vec", vec_bytes,
                "SORTBY", "score", "ASC",
                "DIALECT", "2",
            )
            return self._parse_results(results)
        finally:
            await r.aclose()

    def _parse_results(self, results: Any) -> List[Dict[str, Any]]:
        if not results:
            return []

        hits = []

        # Case 1: redis-py dictionary response with 'results' / b'results'
        if isinstance(results, dict):
            raw_items = results.get("results") or results.get(b"results") or []
            for item in raw_items:
                chunk_data = {}
                if isinstance(item, dict):
                    raw_id = item.get("id") or item.get(b"id") or ""
                    chunk_key = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
                    chunk_data["chunk_key"] = chunk_key

                    attrs = item.get("extra_attributes") or item.get(b"extra_attributes") or item
                    if isinstance(attrs, dict):
                        for k, v in attrs.items():
                            str_k = k.decode("utf-8") if isinstance(k, bytes) else str(k)
                            str_v = v.decode("utf-8", errors="ignore") if isinstance(v, bytes) else str(v)
                            chunk_data[str_k] = str_v

                try:
                    distance = float(chunk_data.get("score", 1.0))
                    if math.isnan(distance):
                        distance = 1.0
                except (ValueError, TypeError):
                    distance = 1.0

                similarity = max(0.0, 1.0 - distance)
                chunk_data["similarity"] = round(similarity, 4)
                hits.append(chunk_data)
            return hits

        # Case 2: redis-py Result object with .docs attribute
        if hasattr(results, "docs"):
            for doc in results.docs:
                chunk_data = {"chunk_key": getattr(doc, "id", "")}
                for attr, val in doc.__dict__.items():
                    if attr in ("id", "payload"):
                        continue
                    if isinstance(val, bytes):
                        val = val.decode("utf-8", errors="ignore")
                    chunk_data[attr] = val

                score = getattr(doc, "score", None) or chunk_data.get("score", 1.0)
                try:
                    distance = float(score)
                except (ValueError, TypeError):
                    distance = 1.0
                chunk_data["similarity"] = round(max(0.0, 1.0 - distance), 4)
                hits.append(chunk_data)
            return hits

        # Case 2: Raw list/tuple from redis execute_command
        if isinstance(results, (list, tuple)):
            if len(results) == 0 or results[0] == 0:
                return []

            for i in range(1, len(results), 2):
                if i >= len(results):
                    break
                raw_id = results[i]
                chunk_key = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
                chunk_data = {"chunk_key": chunk_key}

                if i + 1 < len(results):
                    fields = results[i + 1]
                    # If redis-py returned fields parsed as a dictionary:
                    if isinstance(fields, dict):
                        for k, v in fields.items():
                            str_k = k.decode("utf-8") if isinstance(k, bytes) else str(k)
                            str_v = v.decode("utf-8", errors="ignore") if isinstance(v, bytes) else str(v)
                            chunk_data[str_k] = str_v
                    # If fields is an alternating list [k1, v1, k2, v2, ...]
                    elif isinstance(fields, (list, tuple)):
                        for j in range(0, len(fields), 2):
                            if j + 1 < len(fields):
                                k = fields[j].decode("utf-8") if isinstance(fields[j], bytes) else str(fields[j])
                                v = fields[j + 1].decode("utf-8", errors="ignore") if isinstance(fields[j + 1], bytes) else str(fields[j + 1])
                                chunk_data[k] = v

                distance = float(chunk_data.get("score", 1.0))
                similarity = max(0.0, 1.0 - distance)
                chunk_data["similarity"] = round(similarity, 4)
                hits.append(chunk_data)

            return hits

        return []


def retrieve_chunks(
    query: str,
    top_k: int = 3,
    filter_dict: Optional[Dict[str, Any]] = None,
    redis_url: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Convenience helper function to retrieve chunks in one line."""
    retriever = RedisChunkRetriever(redis_url=redis_url)
    return retriever.search(query, top_k=top_k, filter_dict=filter_dict)


def main():
    parser = argparse.ArgumentParser(description="Test chunk retrieval against Redis Vector Database")
    parser.add_argument("query", nargs="?", default=None, help="The search query text")
    parser.add_argument("-k", "--top-k", type=int, default=3, help="Number of chunks to retrieve (default: 3)")
    parser.add_argument("--index", default=os.getenv("REDIS_INDEX_NAME", "rag_documents_idx"), help="RediSearch index name")
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://localhost:6379/0"), help="Redis URL")

    args = parser.parse_args()

    if not args.query:
        print("\n❌ Error: Please provide a query to test!")
        print("Usage:")
        print("  python retriever.py \"deep learning for semantic segmentation\"\n")
        sys.exit(1)

    print("=" * 70)
    print("🔍 CHUNK RETRIEVER")
    print("=" * 70)
    print(f"📡 Redis URL   : {args.redis_url}")
    print(f"📑 Index       : {args.index}")
    print(f"🎯 Top-K       : {args.top_k}")
    print(f"❓ Query       : \"{args.query}\"")
    print("-" * 70)

    try:
        retriever = RedisChunkRetriever(redis_url=args.redis_url, index_name=args.index)

        print("⚡ 1. Embedding query text (CPU)...")
        emb = retriever.embedder.embed_query(args.query)
        print(f"   ✓ Vector embedded (Dimensions: {len(emb)})")

        print("⚡ 2. Searching Redis Vector Database (KNN)...")
        chunks = retriever.search(args.query, top_k=args.top_k)

        if not chunks:
            print(f"\n⚠️  No matching chunks found in index '{args.index}'.")
            return

        print(f"\n✅ Retrieved {len(chunks)} chunks from Redis:\n")
        for i, chunk in enumerate(chunks, 1):
            sim_pct = chunk.get("similarity", 0.0) * 100
            print(f"[{i}] {chunk.get('chunk_key')}  |  Cosine Similarity: {sim_pct:.2f}%")
            if chunk.get("title"):
                print(f"    Title      : {chunk['title']}")
            if chunk.get("author"):
                print(f"    Author     : {chunk['author']}")
            if chunk.get("department"):
                print(f"    Department : {chunk['department']}")
            content_preview = chunk.get("content", "").replace("\n", " ")[:300]
            print(f"    Content    : \"{content_preview}...\"\n")

        print("=" * 70)

    except Exception as e:
        print(f"\n❌ Retrieval Error: {type(e).__name__}: {e}")
        if "connection refused" in str(e).lower():
            print("   Hint: Ensure Redis is running and reachable.")
        elif "no such index" in str(e).lower() or "unknown index" in str(e).lower():
            print(f"   Hint: Index '{args.index}' does not exist in Redis.")
        else:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
