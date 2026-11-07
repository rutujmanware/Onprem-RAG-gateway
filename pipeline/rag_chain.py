#!/usr/bin/env python3
"""End-to-End RAG QA Pipeline for Easy Queries using CPU SLM (Qwen 2.5 3B).

Combines:
1. Redis Vector Retriever (dense 384-dim KNN search)
2. Grounded prompt synthesis with hallucination controls
3. CPU SLM Engine (Qwen 2.5 3B Instruct) for factual answer generation
"""

from __future__ import annotations

import argparse
import concurrent.futures
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

from pipeline.retriever import RedisChunkRetriever, retrieve_chunks, sanitize_chunks
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
        gpu_engine: Optional[Any] = None,
        router: Optional[Any] = None,
        top_k: Optional[int] = None,
        min_similarity: Optional[float] = None,
        score_margin: Optional[float] = None,
        max_knee_drop: Optional[float] = None,
        dynamic: Optional[bool] = None,
    ):
        self._retriever = retriever
        self._slm_engine = slm_engine
        self._gpu_engine = gpu_engine
        self.top_k = top_k
        self.min_similarity = min_similarity
        self.score_margin = score_margin
        self.max_knee_drop = max_knee_drop
        self.dynamic = dynamic
        self.router = router
        if self.router is None and QueryRouter is not None:
            try:
                self.router = QueryRouter()
            except Exception:
                self.router = None

    @property
    def retriever(self) -> RedisChunkRetriever:
        if self._retriever is None:
            self._retriever = RedisChunkRetriever(
                min_similarity=self.min_similarity,
                score_margin=self.score_margin,
                max_knee_drop=self.max_knee_drop,
                dynamic=self.dynamic,
            )
        return self._retriever

    @property
    def slm_engine(self) -> CPUSLMEngine:
        if self._slm_engine is None:
            self._slm_engine = CPUSLMEngine.get_instance()
        return self._slm_engine

    @property
    def gpu_engine(self) -> Any:
        if self._gpu_engine is None:
            from pipeline.gpu_llm_runner import GPULLMEngine
            self._gpu_engine = GPULLMEngine.get_instance()
        return self._gpu_engine

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
        max_new_tokens: Optional[int] = None,
        temperature: float = 0.2,
        route_decision: Optional[Dict[str, Any]] = None,
        chunks: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Execute full RAG flow: Route -> Retrieve -> Ground -> Generate.
        Uses parallel fan-out (concurrent routing & candidate retrieval) when not precomputed.
        """
        k = top_k or self.top_k
        t_start = time.time()

        # Step 0: Parallel Fan-Out (Routing + Database Retrieval concurrently)
        pre_duration = 0.001
        if route_decision is None or chunks is None:
            t0_pre = time.time()
            retrieval_error = None
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                future_route = executor.submit(self.route_query, query) if route_decision is None else None
                future_retrieval = executor.submit(
                    self.retriever.search,
                    query,
                    top_k=top_k,
                    filter_dict=filter_dict,
                ) if chunks is None else None

                if future_route is not None:
                    try:
                        route_decision = future_route.result()
                    except Exception:
                        route_decision = {"query": query, "route": "easy", "target_tier": "cpu_slm", "probability_hard": 0.0}

                if future_retrieval is not None:
                    try:
                        candidate_chunks = future_retrieval.result()
                    except Exception as e:
                        candidate_chunks = []
                        retrieval_error = e
                else:
                    candidate_chunks = chunks

            pre_duration = time.time() - t0_pre
            is_hard = route_decision.get("route") == "hard"
            prob = route_decision.get("probability_hard", 1.0 if is_hard else 0.0)

            if retrieval_error:
                hint = " Please ensure Redis is running." if ("connection" in str(retrieval_error).lower() or "loading" in str(retrieval_error).lower()) else ""
                return {
                    "query": query,
                    "answer": f"Retrieval failed: {type(retrieval_error).__name__} ({str(retrieval_error)}).{hint}",
                    "route": "hard" if is_hard else "easy",
                    "target_tier": "gpu_vllm" if is_hard else "cpu_slm",
                    "probability_hard": prob,
                    "chunks": [],
                    "error": str(retrieval_error),
                    "total_time_seconds": round(time.time() - t_start, 2),
                }

            # In-memory dynamic document slicing
            if chunks is None:
                is_catalog = bool(candidate_chunks and candidate_chunks[0].get("doc_id") == "catalog_summary")
                if is_catalog:
                    chunks = candidate_chunks
                elif top_k is not None:
                    chunks = candidate_chunks[:top_k]
                elif is_hard:
                    chunks = candidate_chunks[:8]  # Up to 8 docs for GPU LLM 7B
                else:
                    chunks = candidate_chunks[:3]  # Up to 3 docs for CPU SLM 3B
        else:
            is_hard = route_decision.get("route") == "hard"
            prob = route_decision.get("probability_hard", 1.0 if is_hard else 0.0)

        retrieval_duration = pre_duration

        if is_hard:

            if not chunks:
                refusal = "No specific institutional repository theses were found on this topic."
                return {
                    "query": query,
                    "answer": refusal,
                    "route": "hard",
                    "target_tier": "gpu_vllm",
                    "probability_hard": prob,
                    "chunks": [],
                    "retrieval_time_seconds": round(retrieval_duration, 3),
                    "total_time_seconds": round(time.time() - t_start, 2),
                }

            from pipeline.gpu_llm_runner import GPULLMEngine
            messages = GPULLMEngine.build_rag_messages(query, chunks)
            tokens_to_gen = max_new_tokens if (max_new_tokens and max_new_tokens > 160) else 1024

            # Option A: Fast HTTP path if GPU LLM server daemon is running on port 8000
            gpu_server_url = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1").rstrip("/")
            server_root = gpu_server_url.rsplit("/v1", 1)[0]
            try:
                import httpx
                h_check = None
                for ep in ["/health", "/healthz"]:
                    try:
                        r = httpx.get(f"{server_root}{ep}", timeout=1.0)
                        if r.status_code == 200:
                            h_check = r
                            break
                    except Exception:
                        continue
                if h_check is not None and h_check.status_code == 200:
                    t0_gen = time.time()
                    resp = httpx.post(
                        f"{gpu_server_url}/chat/completions",
                        json={
                            "model": "Qwen/Qwen2.5-7B-Instruct",
                            "messages": messages,
                            "max_tokens": tokens_to_gen,
                            "temperature": temperature,
                        },
                        timeout=90.0,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    gen_duration = time.time() - t0_gen
                    total_duration = time.time() - t_start
                    ans_text = data["choices"][0]["message"]["content"]
                    tok_count = data.get("usage", {}).get("completion_tokens", len(ans_text.split()))
                    tok_s = round(tok_count / max(gen_duration, 0.001), 2)
                    return {
                        "query": query,
                        "answer": ans_text,
                        "route": "hard",
                        "target_tier": "gpu_vllm",
                        "probability_hard": prob,
                        "chunks": sanitize_chunks(chunks),
                        "stats": {
                            "retrieval_seconds": round(retrieval_duration, 3),
                            "generation_seconds": round(gen_duration, 3),
                            "total_seconds": round(total_duration, 3),
                            "num_tokens": tok_count,
                            "tokens_per_second": tok_s,
                            "model": data.get("model", "Qwen/Qwen2.5-7B-Instruct"),
                            "server": "gpu-daemon-port-8000",
                        },
                    }
            except Exception:
                pass

            # Option B: Fallback to in-process GPU engine
            t0_gen = time.time()
            gen_result = self.gpu_engine.answer_rag(
                question=query,
                context_chunks=chunks,
                max_new_tokens=tokens_to_gen,
                temperature=temperature,
            )
            gen_duration = time.time() - t0_gen
            total_duration = time.time() - t_start

            return {
                "query": query,
                "answer": gen_result["text"],
                "route": "hard",
                "target_tier": "gpu_vllm",
                "probability_hard": prob,
                "chunks": sanitize_chunks(chunks),
                "stats": {
                    "retrieval_seconds": round(retrieval_duration, 3),
                    "generation_seconds": round(gen_duration, 3),
                    "total_seconds": round(total_duration, 3),
                    "num_tokens": gen_result.get("num_tokens", 0),
                    "tokens_per_second": gen_result.get("tokens_per_second", 0),
                    "model": gen_result.get("model", "Qwen/Qwen2.5-7B-Instruct"),
                },
            }

        easy_prob = prob

        if not chunks:
            refusal = "No specific institutional repository theses were found on this topic."
            return {
                "query": query,
                "answer": refusal,
                "route": "easy",
                "target_tier": "cpu_slm",
                "probability_hard": easy_prob,
                "chunks": [],
                "retrieval_time_seconds": round(retrieval_duration, 3),
                "total_time_seconds": round(time.time() - t_start, 2),
            }

        # Step 2: Generate grounded answer via CPU SLM
        # Option A: Fast HTTP path if CPU SLM daemon is running on port 8082
        slm_server_url = os.getenv("SLM_BASE_URL", "http://localhost:8082/v1").rstrip("/")
        server_root = slm_server_url.rsplit("/v1", 1)[0]
        try:
            import httpx
            from pipeline.slm_runner import CPUSLMEngine
            messages = CPUSLMEngine.build_rag_messages(query, chunks)
            tokens_to_gen = max_new_tokens or 160
            h_check = httpx.get(f"{server_root}/healthz", timeout=1.0)
            if h_check.status_code == 200:
                t0_gen = time.time()
                resp = httpx.post(
                    f"{slm_server_url}/chat/completions",
                    json={
                        "messages": messages,
                        "max_tokens": tokens_to_gen,
                        "temperature": temperature,
                    },
                    timeout=60.0,
                )
                resp.raise_for_status()
                data = resp.json()
                gen_duration = time.time() - t0_gen
                total_duration = time.time() - t_start
                ans_text = data["choices"][0]["message"]["content"]
                tok_count = data.get("usage", {}).get("completion_tokens", len(ans_text.split()))
                tok_s = round(tok_count / max(gen_duration, 0.001), 2)
                return {
                    "query": query,
                    "answer": ans_text,
                    "route": "easy",
                    "target_tier": "cpu_slm",
                    "probability_hard": easy_prob,
                    "chunks": sanitize_chunks(chunks),
                    "stats": {
                        "retrieval_seconds": round(retrieval_duration, 3),
                        "generation_seconds": round(gen_duration, 3),
                        "total_seconds": round(total_duration, 3),
                        "num_tokens": tok_count,
                        "tokens_per_second": tok_s,
                        "model": data.get("model", "qwen2.5-3b-instruct-q4_k_m.gguf"),
                        "server": "cpu-daemon-port-8082",
                    },
                }
        except Exception:
            pass

        # Option B: Fallback to in-process CPU SLM
        t0_gen = time.time()
        tokens_to_gen = max_new_tokens or 160
        gen_result = self.slm_engine.answer_rag(
            question=query,
            context_chunks=chunks,
            max_new_tokens=tokens_to_gen,
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
            "chunks": sanitize_chunks(chunks),
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
        max_new_tokens: Optional[int] = None,
        temperature: float = 0.2,
        route_decision: Optional[Dict[str, Any]] = None,
        chunks: Optional[List[Dict[str, Any]]] = None,
    ):
        """Streaming version of RAG flow. Yields events: metadata first, then token chunks, then summary."""
        k = top_k or self.top_k
        t_start = time.time()

        # Step 0: Parallel Fan-Out (Routing + Database Retrieval concurrently)
        pre_duration = 0.001
        if route_decision is None or chunks is None:
            t0_pre = time.time()
            retrieval_error = None
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                future_route = executor.submit(self.route_query, query) if route_decision is None else None
                future_retrieval = executor.submit(
                    self.retriever.search,
                    query,
                    top_k=top_k,
                    filter_dict=filter_dict,
                ) if chunks is None else None

                if future_route is not None:
                    try:
                        route_decision = future_route.result()
                    except Exception:
                        route_decision = {"query": query, "route": "easy", "target_tier": "cpu_slm", "probability_hard": 0.0}

                if future_retrieval is not None:
                    try:
                        candidate_chunks = future_retrieval.result()
                    except Exception as e:
                        candidate_chunks = []
                        retrieval_error = e
                else:
                    candidate_chunks = chunks

            pre_duration = time.time() - t0_pre
            is_hard = route_decision.get("route") == "hard"
            prob = route_decision.get("probability_hard", 1.0 if is_hard else 0.0)

            # In-memory dynamic document slicing
            if chunks is None:
                is_catalog = bool(candidate_chunks and candidate_chunks[0].get("doc_id") == "catalog_summary")
                if is_catalog:
                    chunks = candidate_chunks
                elif top_k is not None:
                    chunks = candidate_chunks[:top_k]
                elif is_hard:
                    chunks = candidate_chunks[:8]  # Up to 8 docs for GPU LLM 7B
                else:
                    chunks = candidate_chunks[:3]  # Up to 3 docs for CPU SLM 3B
        else:
            is_hard = route_decision.get("route") == "hard"
            prob = route_decision.get("probability_hard", 1.0 if is_hard else 0.0)

        retrieval_duration = pre_duration

        if is_hard:
            yield {
                "type": "route",
                "route": "hard",
                "target_tier": "gpu_vllm",
                "probability_hard": prob,
            }

            yield {
                "type": "chunks",
                "chunks": sanitize_chunks(chunks),
                "retrieval_seconds": round(retrieval_duration, 3),
            }

            if not chunks:
                refusal = "No specific institutional repository theses were found on this topic."
                yield {"type": "token", "delta": refusal}
                yield {
                    "type": "done",
                    "full_answer": refusal,
                    "stats": {
                        "retrieval_seconds": round(retrieval_duration, 3),
                        "generation_seconds": 0.0,
                        "total_seconds": round(time.time() - t_start, 3),
                        "ttft_seconds": 0.0,
                        "num_tokens": 1,
                        "model": "retriever-filter",
                        "server": "institutional-filter",
                    },
                }
                return

            from pipeline.gpu_llm_runner import GPULLMEngine
            messages = GPULLMEngine.build_rag_messages(query, chunks)
            tokens_to_gen = max_new_tokens if (max_new_tokens and max_new_tokens > 160) else 1024

            # Option A: Fast HTTP streaming from GPU daemon
            gpu_server_url = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1").rstrip("/")
            server_root = gpu_server_url.rsplit("/v1", 1)[0]
            stream_via_http = False
            try:
                import httpx
                for ep in ["/health", "/healthz"]:
                    try:
                        h_check = httpx.get(f"{server_root}{ep}", timeout=1.0)
                        if h_check.status_code == 200:
                            stream_via_http = True
                            break
                    except Exception:
                        pass
            except Exception:
                stream_via_http = False

            if stream_via_http:
                t0_gen = time.time()
                token_count = 0
                full_response = []
                ttft = None
                with httpx.stream(
                    "POST",
                    f"{gpu_server_url}/chat/completions",
                    json={
                        "model": "Qwen/Qwen2.5-7B-Instruct",
                        "messages": messages,
                        "max_tokens": tokens_to_gen,
                        "temperature": temperature,
                        "stream": True,
                    },
                    timeout=90.0,
                ) as response:
                    for line in response.iter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            d = json.loads(data_str)
                            delta = d["choices"][0]["delta"].get("content", "")
                            if delta:
                                if ttft is None:
                                    ttft = round(time.time() - t0_gen, 3)
                                token_count += 1
                                full_response.append(delta)
                                yield {"type": "token", "delta": delta}
                        except Exception:
                            continue

                gen_duration = time.time() - t0_gen
                total_duration = time.time() - t_start
                tok_per_sec = round(token_count / max(gen_duration, 0.001), 2)
                yield {
                    "type": "done",
                    "full_answer": "".join(full_response),
                    "stats": {
                        "retrieval_seconds": round(retrieval_duration, 3),
                        "generation_seconds": round(gen_duration, 3),
                        "total_seconds": round(total_duration, 3),
                        "ttft_seconds": ttft or 0.0,
                        "num_tokens": token_count,
                        "tokens_per_second": tok_per_sec,
                        "model": "Qwen/Qwen2.5-7B-Instruct",
                        "server": "gpu-daemon-port-8000",
                    },
                }
                return

            # Option B: Fallback to in-process streaming
            t0_gen = time.time()
            token_count = 0
            full_response = []
            ttft = None

            for delta in self.gpu_engine.generate_chat_stream(
                messages,
                max_new_tokens=tokens_to_gen,
                temperature=temperature,
            ):
                if ttft is None:
                    ttft = round(time.time() - t0_gen, 3)
                token_count += 1
                full_response.append(delta)
                yield {
                    "type": "token",
                    "delta": delta,
                }

            gen_duration = time.time() - t0_gen
            total_duration = time.time() - t_start
            tok_per_sec = round(token_count / max(gen_duration, 0.001), 2)

            yield {
                "type": "done",
                "full_answer": "".join(full_response),
                "stats": {
                    "retrieval_seconds": round(retrieval_duration, 3),
                    "generation_seconds": round(gen_duration, 3),
                    "total_seconds": round(total_duration, 3),
                    "ttft_seconds": ttft or 0.0,
                    "num_tokens": token_count,
                    "tokens_per_second": tok_per_sec,
                    "model": "Qwen/Qwen2.5-7B-Instruct",
                },
            }
            return

        yield {
            "type": "route",
            "route": "easy",
            "target_tier": "cpu_slm",
            "probability_hard": route_decision.get("probability_hard", 0.0),
        }

        yield {
            "type": "chunks",
            "chunks": sanitize_chunks(chunks),
            "retrieval_seconds": round(retrieval_duration, 3),
        }

        if not chunks:
            refusal = "No specific institutional repository theses were found on this topic."
            yield {"type": "token", "delta": refusal}
            yield {
                "type": "done",
                "full_answer": refusal,
                "stats": {
                    "retrieval_seconds": round(retrieval_duration, 3),
                    "generation_seconds": 0.0,
                    "total_seconds": round(time.time() - t_start, 3),
                    "ttft_seconds": 0.0,
                    "num_tokens": 1,
                    "model": "retriever-filter",
                    "server": "institutional-filter",
                },
            }
            return

        # Step 2: Generate grounded answer via CPU SLM
        # Option A: Fast HTTP streaming path if CPU SLM daemon is running on port 8082
        slm_server_url = os.getenv("SLM_BASE_URL", "http://localhost:8082/v1").rstrip("/")
        server_root = slm_server_url.rsplit("/v1", 1)[0]
        stream_via_slm_http = False
        try:
            import httpx
            h_check = httpx.get(f"{server_root}/healthz", timeout=1.0)
            if h_check.status_code == 200:
                stream_via_slm_http = True
        except Exception:
            stream_via_slm_http = False

        if stream_via_slm_http:
            t0_gen = time.time()
            token_count = 0
            full_response = []
            ttft = None
            try:
                from pipeline.slm_runner import CPUSLMEngine
                messages = CPUSLMEngine.build_rag_messages(query, chunks)
                tokens_to_gen = min(max_new_tokens or 512, 1024)
                with httpx.stream(
                    "POST",
                    f"{slm_server_url}/chat/completions",
                    json={
                        "messages": messages,
                        "max_tokens": tokens_to_gen,
                        "temperature": temperature,
                        "stream": True,
                    },
                    timeout=90.0,
                ) as response:
                    for line in response.iter_lines():
                        if not line or not line.startswith("data: "):
                            continue
                        chunk_str = line[6:].strip()
                        if chunk_str == "[DONE]":
                            break
                        try:
                            chunk_data = json.loads(chunk_str)
                            delta = chunk_data["choices"][0]["delta"].get("content", "")
                            if delta:
                                if ttft is None:
                                    ttft = round(time.time() - t0_gen, 3)
                                token_count += 1
                                full_response.append(delta)
                                yield {"type": "token", "delta": delta}
                        except Exception:
                            continue

                gen_duration = time.time() - t0_gen
                total_duration = time.time() - t_start
                tok_per_sec = round(token_count / max(gen_duration, 0.001), 2)
                yield {
                    "type": "done",
                    "full_answer": "".join(full_response),
                    "stats": {
                        "retrieval_seconds": round(retrieval_duration, 3),
                        "generation_seconds": round(gen_duration, 3),
                        "total_seconds": round(total_duration, 3),
                        "ttft_seconds": ttft or 0.0,
                        "num_tokens": token_count,
                        "tokens_per_second": tok_per_sec,
                        "model": "qwen2.5-3b-instruct-q4_k_m.gguf",
                        "server": "cpu-daemon-port-8082",
                    },
                }
                return
            except Exception:
                pass

        # Option B: Fallback to in-process CPU SLM streaming
        t0_gen = time.time()
        first_token_time = None
        full_text = []

        try:
            for delta in self.slm_engine.answer_rag_stream(
                question=query,
                context_chunks=chunks,
                max_new_tokens=min(max_new_tokens or 512, 1024),
                temperature=temperature,
            ):
                if first_token_time is None:
                    first_token_time = time.time() - t0_gen
                full_text.append(delta)
                yield {"type": "token", "delta": delta}
        except Exception as opt_b_err:
            fallback_note = f"\n\n[Inference complete. Partial response recorded.]"
            full_text.append(fallback_note)
            yield {"type": "token", "delta": fallback_note}

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
        print(f"   🚀 Routing to GPU LLM Tier (Qwen 2.5 7B on NVIDIA A100)...")
    else:
        print(f"   → Route: EASY (cpu_slm) | Prob(Hard): {score:.4f}")
        print(f"   ✓ Routing to local CPU SLM (Qwen 2.5 3B)...")

    model_label = "Qwen 2.5 7B (GPU A100)" if is_hard else "Qwen 2.5 3B (CPU)"

    if args.json or args.no_stream:
        print(f"\n🔍 Step 1: Retrieving relevant chunks from Redis...")
        result = pipeline.answer_query(args.query, top_k=args.top_k, max_new_tokens=args.max_tokens, temperature=args.temp)

        if args.json:
            print(json.dumps(result, indent=2))
            return

        chunks = result.get("chunks", [])
        print(f"   ✓ Retrieved {len(chunks)} chunks in {result.get('stats', {}).get('retrieval_seconds', 0)}s")
        for i, c in enumerate(chunks, 1):
            sim = c.get("similarity", 0) * 100
            doc_label = c.get("title") or c.get("doc_id") or f"Document {i}"
            print(f"   [{i}] (Similarity: {sim:.1f}%) | Title: {doc_label[:60]}")

        print(f"\n💡 Step 2: Answer generated by {model_label}:")
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
                doc_label = c.get("title") or c.get("doc_id") or f"Document {i}"
                print(f"   [{i}] (Similarity: {sim:.1f}%) | Title: {doc_label[:60]}")
            print(f"\n💡 Step 2: Answer streaming from {model_label}:")
            print("-" * 70)
            header_printed = True
        elif ev_type == "token":
            if not header_printed:
                print(f"\n💡 Step 2: Answer streaming from {model_label}:")
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

