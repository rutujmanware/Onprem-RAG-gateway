"""Query Redis Stack Vector Search.

Allows testing semantic search against the populated vector database directly from the CLI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import List, Optional
import numpy as np
try:
    import redis.asyncio as redis
except ImportError:
    redis = None

from pipeline.embeddings.embedder import LocalEmbedder
from pipeline.env_utils import resolve_redis_url

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("vector_query")


async def query_vector_db(
    query_text: str,
    top_k: int = 3,
    redis_url: Optional[str] = None,
    index_name: str = "rag_documents_v2_idx",
):
    url = resolve_redis_url(redis_url)

    if not redis:
        print("[ERROR] redis-py is not installed. Run 'pip install redis' to query Redis Stack.", flush=True)
        return

    r = redis.from_url(url, encoding="utf-8", decode_responses=False)
    try:
        try:
            await r.ping()
        except Exception as e:
            print(f"\n[ERROR] Could not connect to Redis at {url}: {e}", flush=True)
            print("Please ensure Redis Stack is running (e.g. 'make redis-up') and authentication credentials are valid.\n", flush=True)
            return

        embedder = LocalEmbedder(dimension=384)
        print(f"\n[QUERY] Computing dense embedding for query: '{query_text}'...", flush=True)
        query_vec = embedder.embed_query(query_text)
        vec_bytes = np.array(query_vec, dtype=np.float32).tobytes()

        search_query = f"*=>[KNN {top_k} @vector $vec AS score]"
        target_index = index_name

        try:
            results = await r.execute_command(
                "FT.SEARCH",
                target_index,
                search_query,
                "PARAMS",
                "2",
                "vec",
                vec_bytes,
                "SORTBY",
                "score",
                "ASC",
                "DIALECT",
                "2",
            )
        except Exception as se:
            err_msg = str(se).lower()
            if "no such index" in err_msg or "unknown index" in err_msg:
                available_indices: List[str] = []
                try:
                    raw_indices = await r.execute_command("FT._LIST")
                    available_indices = [
                        idx.decode("utf-8") if isinstance(idx, bytes) else str(idx)
                        for idx in raw_indices
                    ]
                except Exception:
                    pass

                # If the requested index doesn't exist, check for alternative version
                fallback_index = None
                if target_index == "rag_documents_v2_idx" and "rag_documents_idx" in available_indices:
                    fallback_index = "rag_documents_idx"
                elif target_index == "rag_documents_idx" and "rag_documents_v2_idx" in available_indices:
                    fallback_index = "rag_documents_v2_idx"

                if fallback_index:
                    print(f"\n[INFO] Index '{target_index}' not found. Falling back to existing index '{fallback_index}'...", flush=True)
                    results = await r.execute_command(
                        "FT.SEARCH",
                        fallback_index,
                        search_query,
                        "PARAMS",
                        "2",
                        "vec",
                        vec_bytes,
                        "SORTBY",
                        "score",
                        "ASC",
                        "DIALECT",
                        "2",
                    )
                    target_index = fallback_index
                else:
                    print(f"\n[INFO] The index '{target_index}' does not exist yet.", flush=True)
                    if available_indices:
                        print(f"Available indices in Redis: {', '.join(available_indices)}", flush=True)
                        print(f"To query a specific index, use: make query Q=\"{query_text}\" (or --index <name>)\n", flush=True)
                    else:
                        print("Run 'make ingest-pdfs' or your crawler pipeline to index documents first!\n", flush=True)
                    return
            else:
                raise se

        total_matches = results[0]
        if total_matches == 0:
            print(f"\n[INFO] Index '{target_index}' exists but contains 0 matching documents.\n", flush=True)
            return
        print("\n" + "=" * 65, flush=True)
        print(f"Top {top_k} Results from '{target_index}' (Total hits: {total_matches}):", flush=True)
        print("=" * 65, flush=True)

        for i in range(1, len(results), 2):
            doc_id = results[i].decode("utf-8") if isinstance(results[i], bytes) else str(results[i])
            fields = results[i + 1]
            field_dict = {}
            for j in range(0, len(fields), 2):
                k = fields[j].decode("utf-8") if isinstance(fields[j], bytes) else str(fields[j])
                v = fields[j + 1].decode("utf-8", errors="ignore") if isinstance(fields[j + 1], bytes) else str(fields[j + 1])
                field_dict[k] = v

            distance = float(field_dict.get("score", 1.0))
            similarity = 1.0 - distance

            print(f"\n[Match {i//2 + 1}] ID: {doc_id} | Cosine Similarity: {similarity:.4f}", flush=True)
            if field_dict.get("title"):
                print(f"  Title      : {field_dict['title']}", flush=True)
            if field_dict.get("author"):
                print(f"  Author(s)  : {field_dict['author']}", flush=True)
            if field_dict.get("advisor"):
                print(f"  Advisor(s) : {field_dict['advisor']}", flush=True)
            if field_dict.get("department"):
                print(f"  Department : {field_dict['department']}", flush=True)
            if field_dict.get("date"):
                print(f"  Date       : {field_dict['date']}", flush=True)
            if field_dict.get("degree"):
                print(f"  Degree     : {field_dict['degree']}", flush=True)
            print(f"  Excerpt    :\n{field_dict.get('content', '')[:350]}...\n", flush=True)

        print("=" * 65 + "\n", flush=True)

    finally:
        await r.aclose()


def main():
    parser = argparse.ArgumentParser(description="Query the Redis Stack vector database")
    parser.add_argument("query", help="Text search query")
    parser.add_argument("--top-k", type=int, default=3, help="Number of nearest neighbors to retrieve")
    parser.add_argument("--index", default="rag_documents_v2_idx", help="RediSearch index name")
    parser.add_argument("--redis-url", default=None, help="Redis connection URL (defaults to REDIS_URL in .env)")

    args = parser.parse_args()
    asyncio.run(query_vector_db(args.query, args.top_k, args.redis_url, args.index))


if __name__ == "__main__":
    main()
