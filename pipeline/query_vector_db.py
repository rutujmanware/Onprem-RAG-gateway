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

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("vector_query")


async def query_vector_db(
    query_text: str,
    top_k: int = 3,
    redis_url: Optional[str] = None,
    index_name: str = "rag_documents_idx",
):
    url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
    embedder = LocalEmbedder(dimension=384)

    print(f"\n[QUERY] Computing dense embedding for query: '{query_text}'...", flush=True)
    query_vec = embedder.embed_query(query_text)
    vec_bytes = np.array(query_vec, dtype=np.float32).tobytes()

    if not redis:
        print("[ERROR] redis-py is not installed. Run 'pip install redis' to query Redis Stack.", flush=True)
        return

    r = redis.from_url(url, encoding="utf-8", decode_responses=False)
    try:
        search_query = f"*=>[KNN {top_k} @vector $vec AS score]"
        try:
            results = await r.execute_command(
                "FT.SEARCH",
                index_name,
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
            if "no such index" in str(se).lower() or "unknown index" in str(se).lower():
                print(f"\n[INFO] The index '{index_name}' does not exist yet.", flush=True)
                print("Run the crawler to download and index your first thesis!\n", flush=True)
                return
            raise se

        total_matches = results[0]
        if total_matches == 0:
            print(f"\n[INFO] Index '{index_name}' exists but contains 0 matching documents.\n", flush=True)
            return
        print("\n" + "=" * 65, flush=True)
        print(f"Top {top_k} Results from '{index_name}' (Total hits: {total_matches}):", flush=True)
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
    parser.add_argument("--index", default="rag_documents_idx", help="RediSearch index name")
    parser.add_argument("--redis-url", default=os.getenv("REDIS_URL", "redis://localhost:6379/0"))

    args = parser.parse_args()
    asyncio.run(query_vector_db(args.query, args.top_k, args.redis_url, args.index))


if __name__ == "__main__":
    main()
