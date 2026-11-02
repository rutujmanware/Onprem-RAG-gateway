#!/usr/bin/env python3
"""End-to-End RAG QA Pipeline for Easy Queries using CPU SLM (Qwen 2.5 3B).

Combines:
1. Redis Vector Retriever (dense 384-dim KNN search)
2. Grounded prompt synthesis with hallucination controls
3. CPU SLM Engine (Qwen 2.5 3B Instruct) for factual answer generation
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root is on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from pipeline.retriever import RedisChunkRetriever, retrieve_chunks
from pipeline.slm_runner import CPUSLMEngine

try:
    from gateway.router.mlp_router.inference import QueryRouter
except Exception:
    QueryRouter = None


class RAGQAPipeline:
    """End-to-end RAG QA coordinator with neural complexity routing for the CPU SLM tier."""

    def __init__(
        self,
        retriever: Optional[RedisChunkRetriever] = None,
        slm_engine: Optional[CPUSLMEngine] = None,
        router: Optional[Any] = None,
        top_k: int = 3,
    ):
        self._retriever = retriever
        self._slm_engine = slm_engine
        self.top_k = top_k
        self.router = router
        if self.router is None and QueryRouter is not None:
            try:
                self.router = QueryRouter()
            except Exception:
                self.router = None

    @property
    def retriever(self) -> RedisChunkRetriever:
        if self._retriever is None:
            self._retriever = RedisChunkRetriever()
        return self._retriever

    @property
    def slm_engine(self) -> CPUSLMEngine:
        if self._slm_engine is None:
            self._slm_engine = CPUSLMEngine.get_instance()
        return self._slm_engine

    def route_query(self, query: str) -> Dict[str, Any]:
        """Classify query using the Two-Tower MLP router."""
        if self.router is not None:
            try:
                return self.router.predict(query)
            except Exception:
                pass
        return {"query": query, "route": "easy", "target_tier": "cpu_slm", "probability_hard": 0.0}

    def answer_query(
        self,
        query: str,
        top_k: Optional[int] = None,
        filter_dict: Optional[Dict[str, str]] = None,
        max_new_tokens: int = 160,
        temperature: float = 0.2,
    ) -> Dict[str, Any]:
        """Execute full RAG flow: Route -> Retrieve -> Ground -> Generate."""
        k = top_k or self.top_k
        t_start = time.time()

        # Step 0: Neural Complexity Routing
        route_decision = self.route_query(query)
        if route_decision.get("route") == "hard":
            prob = route_decision.get("probability_hard", 1.0)
            return {
                "query": query,
                "answer": f"The 7B model is not yet ready. Your query has been classified as 'hard' (Complexity Score: {prob:.4f}) requiring the GPU LLM tier.",
                "route": "hard",
                "target_tier": "gpu_vllm",
                "probability_hard": prob,
                "chunks": [],
                "stats": {
                    "routing_seconds": round(time.time() - t_start, 4),
                    "total_seconds": round(time.time() - t_start, 4),
                },
            }

        easy_prob = route_decision.get("probability_hard", 0.0)

        # Step 1: Retrieve context chunks
        t0_retrieval = time.time()
        try:
            chunks = self.retriever.search(query, top_k=k, filter_dict=filter_dict)
        except Exception as e:
            hint = " Please ensure Redis is running." if ("connection" in str(e).lower() or "loading" in str(e).lower()) else ""
            return {
                "query": query,
                "answer": f"Retrieval failed: {type(e).__name__} ({str(e)}).{hint}",
                "route": "easy",
                "target_tier": "cpu_slm",
                "probability_hard": easy_prob,
                "chunks": [],
                "error": str(e),
                "total_time_seconds": round(time.time() - t_start, 2),
            }
        retrieval_duration = time.time() - t0_retrieval

        if not chunks:
            return {
                "query": query,
                "answer": "No relevant documents were found in the knowledge base matching your query.",
                "route": "easy",
                "target_tier": "cpu_slm",
                "probability_hard": easy_prob,
                "chunks": [],
                "retrieval_time_seconds": round(retrieval_duration, 3),
                "total_time_seconds": round(time.time() - t_start, 2),
            }

        # Step 2: Generate grounded answer via CPU SLM
        t0_gen = time.time()
        gen_result = self.slm_engine.answer_rag(
            question=query,
            context_chunks=chunks,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
        )
        gen_duration = time.time() - t0_gen

        total_duration = time.time() - t_start

        return {
            "query": query,
            "answer": gen_result["text"],
            "route": "easy",
            "target_tier": "cpu_slm",
            "probability_hard": easy_prob,
            "chunks": chunks,
            "stats": {
                "retrieval_seconds": round(retrieval_duration, 3),
                "generation_seconds": round(gen_duration, 3),
                "total_seconds": round(total_duration, 3),
                "num_tokens": gen_result.get("num_tokens", 0),
                "tokens_per_second": gen_result.get("tokens_per_second", 0),
                "model": gen_result.get("model", self.slm_engine.model_name),
            },
        }

    def answer_query_stream(
        self,
        query: str,
        top_k: Optional[int] = None,
        filter_dict: Optional[Dict[str, str]] = None,
        max_new_tokens: int = 160,
        temperature: float = 0.2,
    ):
        """Streaming version of RAG flow. Yields events: metadata first, then token chunks, then summary."""
        k = top_k or self.top_k
        t_start = time.time()

        # Step 0: Neural Complexity Routing
        route_decision = self.route_query(query)
        if route_decision.get("route") == "hard":
            prob = route_decision.get("probability_hard", 1.0)
            hard_msg = f"The 7B model is not yet ready. Your query has been classified as 'hard' (Complexity Score: {prob:.4f}) requiring the GPU LLM tier."
            yield {
                "type": "route",
                "route": "hard",
                "target_tier": "gpu_vllm",
                "probability_hard": prob,
            }
            yield {
                "type": "chunks",
                "chunks": [],
                "retrieval_seconds": 0.0,
            }
            yield {
                "type": "token",
                "delta": hard_msg,
            }
            yield {
                "type": "done",
                "full_answer": hard_msg,
                "stats": {
                    "total_seconds": round(time.time() - t_start, 3),
                    "route": "hard",
                    "target_tier": "gpu_vllm",
                    "complexity_score": prob,
                },
            }
            return

        yield {
            "type": "route",
            "route": "easy",
            "target_tier": "cpu_slm",
            "probability_hard": route_decision.get("probability_hard", 0.0),
        }

        t0_retrieval = time.time()
        chunks = self.retriever.search(query, top_k=k, filter_dict=filter_dict)
        retrieval_duration = time.time() - t0_retrieval

        yield {
            "type": "chunks",
            "chunks": chunks,
            "retrieval_seconds": round(retrieval_duration, 3),
        }

        if not chunks:
            yield {
                "type": "token",
                "delta": "No relevant documents were found in the knowledge base matching your query.",
            }
            yield {
                "type": "done",
                "total_seconds": round(time.time() - t_start, 2),
                "num_tokens": 0,
            }
            return

        t0_gen = time.time()
        first_token_time = None
        full_text = []

        for delta in self.slm_engine.answer_rag_stream(
            question=query,
            context_chunks=chunks,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
        ):
            if first_token_time is None:
                first_token_time = time.time() - t0_gen
            full_text.append(delta)
            yield {"type": "token", "delta": delta}

        gen_duration = time.time() - t0_gen
        total_duration = time.time() - t_start
        final_answer = "".join(full_text).strip()
        approx_tokens = len(final_answer.split()) * 4 // 3

        yield {
            "type": "done",
            "full_answer": final_answer,
            "stats": {
                "ttft_seconds": round(first_token_time or 0, 3),
                "retrieval_seconds": round(retrieval_duration, 3),
                "generation_seconds": round(gen_duration, 3),
                "total_seconds": round(total_duration, 3),
                "approx_tokens": approx_tokens,
                "tokens_per_second": round(approx_tokens / max(gen_duration, 0.001), 2),
            },
        }


def main():
    parser = argparse.ArgumentParser(description="End-to-end RAG QA with Qwen 2.5 3B on CPU")
    parser.add_argument("query", nargs="?", default=None, help="The question to ask")
    parser.add_argument("-k", "--top-k", type=int, default=3, help="Number of chunks to retrieve (default: 3)")
    parser.add_argument("--temp", type=float, default=0.2, help="Sampling temperature (default: 0.2 for factual answers)")
    parser.add_argument("--max-tokens", type=int, default=160, help="Max tokens to generate (default: 160)")
    parser.add_argument("--no-stream", action="store_true", help="Disable streaming output")
    parser.add_argument("--json", action="store_true", help="Output result as JSON")
    args = parser.parse_args()

    if not args.query:
        print("\n❌ Error: Please provide a question to answer!")
        print("Usage:")
        print("  python -m pipeline.rag_chain \"What is the main topic of the thesis on distributed systems?\"\n")
        sys.exit(1)

    print("=" * 70)
    print("🧠 ON-PREMISE RAG GATEWAY - HYBRID ROUTING & EXECUTION")
    print("=" * 70)
    print(f"❓ Question : \"{args.query}\"")
    print(f"🎯 Top-K    : {args.top_k}")
    print("-" * 70)

    pipeline = RAGQAPipeline(top_k=args.top_k)
    route_meta = pipeline.route_query(args.query)
    is_hard = route_meta.get("route") == "hard"
    score = route_meta.get("probability_hard", 0.0)

    print(f"🚦 Step 0: Dynamic Query Complexity Routing...")
    if is_hard:
        print(f"   → Route: HARD (gpu_vllm) | Prob(Hard): {score:.4f}")
        print(f"   ⚠️  The 7B model is not yet ready. Returning stubbed response.")
        result = pipeline.answer_query(args.query, top_k=args.top_k, max_new_tokens=args.max_tokens, temperature=args.temp)
        if args.json:
            print(json.dumps(result, indent=2))
            return
        print(f"\n💡 Step 1: Deferred Response (GPU LLM 7B Tier):")
        print("-" * 70)
        print(result.get("answer"))
        print("-" * 70)
        stats = result.get("stats", {})
        if stats:
            print(f"📊 Stats: Total: {stats.get('total_seconds')}s (MLP router deferred)")
        print("=" * 70)
        return

    print(f"   → Route: EASY (cpu_slm) | Prob(Hard): {score:.4f}")
    print(f"   ✓ Routing to local CPU SLM (Qwen 2.5 3B)...")

    if args.json or args.no_stream:
        print("\n🔍 Step 1: Retrieving relevant chunks from Redis...")
        result = pipeline.answer_query(args.query, top_k=args.top_k, max_new_tokens=args.max_tokens, temperature=args.temp)

        if args.json:
            print(json.dumps(result, indent=2))
            return

        chunks = result.get("chunks", [])
        print(f"   ✓ Retrieved {len(chunks)} chunks in {result.get('stats', {}).get('retrieval_seconds', 0)}s")
        for i, c in enumerate(chunks, 1):
            sim = c.get("similarity", 0) * 100
            print(f"   [{i}] {c.get('chunk_key')} (Similarity: {sim:.1f}%) | Title: {c.get('title', 'N/A')[:60]}")

        print(f"\n💡 Step 2: Answer generated by Qwen 2.5 3B (CPU):")
        print("-" * 70)
        print(result.get("answer"))
        print("-" * 70)

        stats = result.get("stats", {})
        if stats:
            print(f"📊 Stats: {stats.get('num_tokens')} tokens generated | Generation: {stats.get('generation_seconds')}s ({stats.get('tokens_per_second')} tok/s) | Total: {stats.get('total_seconds')}s")
        print("=" * 70)
        return

    # Default: Real-time Streaming
    print("\n🔍 Step 1: Retrieving relevant chunks from Redis...")
    events = pipeline.answer_query_stream(args.query, top_k=args.top_k, max_new_tokens=args.max_tokens, temperature=args.temp)

    header_printed = False
    done_event = None
    for ev in events:
        ev_type = ev.get("type")
        if ev_type == "route":
            continue
        elif ev_type == "chunks":
            chunks = ev.get("chunks", [])
            print(f"   ✓ Retrieved {len(chunks)} chunks in {ev.get('retrieval_seconds', 0)}s")
            for i, c in enumerate(chunks, 1):
                sim = c.get("similarity", 0) * 100
                print(f"   [{i}] {c.get('chunk_key')} (Similarity: {sim:.1f}%) | Title: {c.get('title', 'N/A')[:60]}")
            print("\n💡 Step 2: Answer streaming from Qwen 2.5 3B (CPU):")
            print("-" * 70)
            header_printed = True
        elif ev_type == "token":
            if not header_printed:
                print("\n💡 Step 2: Answer streaming from Qwen 2.5 3B (CPU):")
                print("-" * 70)
                header_printed = True
            sys.stdout.write(ev["delta"])
            sys.stdout.flush()
        elif ev_type == "done":
            done_event = ev

    print("\n" + "-" * 70)
    stats = (done_event or {}).get("stats", {})
    if stats:
        print(f"⚡ TTFT: {stats.get('ttft_seconds')}s | Generation: {stats.get('generation_seconds')}s ({stats.get('tokens_per_second')} tok/s) | Total: {stats.get('total_seconds')}s")
    print("=" * 70)


if __name__ == "__main__":
    main()

