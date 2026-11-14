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
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Ensure project root is on sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from pipeline.retriever import RedisChunkRetriever, retrieve_chunks, sanitize_chunks
from pipeline.sanitizer import (is_no_relevant_docs_answer, starts_with_refusal, strip_mixed_refusal,
                                strip_mixed_refusal_text)
from pipeline.slm_runner import CPUSLMEngine


logger = logging.getLogger("pipeline.rag_chain")


def _vllm_auth_headers() -> Dict[str, str]:
    """Bearer header for the vLLM server, which requires VLLM_API_KEY on /v1/*."""
    key = os.getenv("VLLM_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


# --- Upstream LLM daemons (DEFECT-17: CPU-tier ~90 s stalls) -------------------------------------
# The CPU SLM server answers 503 within SLM_QUEUE_TIMEOUT when all its slots are busy. Generation
# then overflows to the GPU daemon (if healthy) and only then to the in-process SLM. An answer that
# has already started streaming is never restarted: it is finished with PARTIAL_ANSWER_NOTE and
# flagged "incomplete" so the gateway does not cache it.
PARTIAL_ANSWER_NOTE = "\n\n[Generation was interrupted. This answer is incomplete.]"


class UpstreamBusy(RuntimeError):
    """The upstream model server has no free slot (HTTP 503)."""


# When a 3B refusal is re-asked to the 7B tier. Re-asking every refusal sent ~28% of ordinary questions to
# the GPU, and the 7B model refused 15 of 22 of those too. Re-ask only when relevant theses probably exist
# (strong best match) or for yes/no questions, the form the 3B model mishandles (on-topic theses that do
# not literally say yes or no). Measured on 35 escalations: 16/18 useful kept, 12/17 wasted dropped.
ESCALATE_MIN_SIMILARITY = float(os.getenv("ESCALATE_MIN_SIMILARITY", "0.65"))
_YES_NO_RE = re.compile(r"^\W*(?:is|are|was|were|does|do|did|can|could|will|would|should|has|have|had|may|might|must)\b",
                        re.IGNORECASE)


def should_escalate_refusal(query: str, chunks: List[Dict[str, Any]]) -> bool:
    """Re-ask a 3B refusal to the 7B tier only for yes/no questions or a strong retrieval match."""
    if _YES_NO_RE.match(query or ""):
        return True
    top = max((float(c.get("similarity") or 0.0) for c in chunks or []), default=0.0)
    return top >= ESCALATE_MIN_SIMILARITY


class SLMRefused(RuntimeError):
    """The 3B model gave the bare refusal although retrieval returned documents. The 3B model cannot
    reliably tell 'documents on the topic that do not answer the exact (yes/no) claim' from 'documents not
    about the topic' (red-team: on-topic plasmid theses, 3B refused), so such questions are re-asked to the
    7B model before anything is shown. Carries the refusal text for when the 7B tier is unavailable."""

    def __init__(self, text: str):
        super().__init__("CPU SLM refused with retrieved documents")
        self.text = text


def _slm_timeout(streaming: bool = True):
    import httpx
    # Streaming: read = longest gap between chunks (covers prompt processing before the first token).
    # Non-streaming: the whole answer arrives at once, so allow the full generation time.
    # Busy servers answer 503 within SLM_QUEUE_TIMEOUT, so neither limit is spent waiting in a queue.
    read = float(os.getenv("SLM_READ_TIMEOUT", "30")) if streaming else 60.0
    return httpx.Timeout(connect=2.0, read=read, write=10.0, pool=2.0)


def _gpu_timeout():
    import httpx
    return httpx.Timeout(connect=2.0, read=60.0, write=10.0, pool=2.0)


def _iter_sse_deltas(response):
    """Content deltas from an OpenAI-style chat completion SSE stream."""
    for line in response.iter_lines():
        if not line or not line.startswith("data: "):
            continue
        data_str = line[6:].strip()
        if data_str == "[DONE]":
            return
        try:
            choice = (json.loads(data_str).get("choices") or [{}])[0]
        except Exception:
            continue
        delta = (choice.get("delta") or {}).get("content", "")
        if delta:
            yield delta


def _stream_chat_http(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout=None):
    """Stream content deltas from an OpenAI-compatible server. Raises UpstreamBusy on 503."""
    import httpx
    with httpx.stream("POST", url, json={**payload, "stream": True}, headers=headers or {},
                      timeout=timeout if timeout is not None else _gpu_timeout()) as response:
        if response.status_code == 503:
            raise UpstreamBusy(f"{url} busy")
        response.raise_for_status()
        yield from _iter_sse_deltas(response)


def _complete_chat_http(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout=None):
    """Non-streaming chat completion. Returns (text, completion_tokens, model). Raises UpstreamBusy on 503."""
    import httpx
    resp = httpx.post(url, json=payload, headers=headers or {}, timeout=timeout if timeout is not None else _gpu_timeout())
    if resp.status_code == 503:
        raise UpstreamBusy(f"{url} busy")
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    return text, data.get("usage", {}).get("completion_tokens", len(text.split())), data.get("model")


from gateway.router.premise_check import premise_check_reason

try:
    from gateway.router.mlp_router.inference import QueryRouter
except Exception:
    QueryRouter = None


# Per-process shared retriever and router. The gateway creates a RAGQAPipeline per fresh request;
# building a router and a retriever each time cost ~110 ms (model reloads). Both are read-only after
# construction and safe to share across request threads.
_SHARED: Dict[Any, Any] = {}
_SHARED_LOCK = threading.Lock()


def _shared(key, factory):
    obj = _SHARED.get(key)
    if obj is None:
        with _SHARED_LOCK:
            obj = _SHARED.get(key)
            if obj is None:
                obj = factory()
                _SHARED[key] = obj
    return obj


def warm_up() -> None:
    """Load everything a first request would otherwise load (router, embedder, retriever, spell
    dictionaries) so no user pays it. Called at gateway startup."""
    from pipeline.text_processing.topic_speller import TopicSpeller
    TopicSpeller.get_instance()
    p = RAGQAPipeline()
    if p.router is not None:
        p.router.predict("warm up query")
    p.retriever.search("explain transformers")  # keyword route: spell check + lemmas + title KNN
    p.retriever.search("What is the role of mycorrhizal fungi in plant nutrition?")  # dense route


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
                # FR-2 alpha (ROUTING_ALPHA, default 0.65); QueryRouter's own default is 0.5
                alpha = float(os.getenv("ROUTING_ALPHA", "0.65"))
                self.router = _shared(("router", alpha), lambda: QueryRouter(threshold=alpha))
            except Exception:
                self.router = None

    @property
    def retriever(self) -> RedisChunkRetriever:
        if self._retriever is None:
            params = (self.min_similarity, self.score_margin, self.max_knee_drop, self.dynamic)
            self._retriever = _shared(("retriever",) + params, lambda: RedisChunkRetriever(
                min_similarity=self.min_similarity,
                score_margin=self.score_margin,
                max_knee_drop=self.max_knee_drop,
                dynamic=self.dynamic,
            ))
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
        """Classify query using the Two-Tower MLP router, escalating loaded 'why' claims to the GPU tier."""
        decision = {"query": query, "route": "easy", "target_tier": "cpu_slm", "probability_hard": 0.0}
        if self.router is not None:
            try:
                decision = self.router.predict(query)
            except Exception:
                pass
        if decision.get("route") != "hard":
            reason = premise_check_reason(query)
            if reason and self._gpu_daemon_healthy():
                decision = {**decision, "route": "hard", "target_tier": "gpu_vllm", "escalation": f"premise_check: {reason}"}
        return decision

    @staticmethod
    def _gpu_daemon_healthy() -> bool:
        """Escalate only when the vLLM daemon is up, never into the in-process GPU fallback."""
        try:
            import httpx
            root = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1").rstrip("/").rsplit("/v1", 1)[0]
            return httpx.get(f"{root}/health", timeout=1.0).status_code == 200
        except Exception:
            return False

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
        from pipeline.text_processing.typo_normalizer import TypoNormalizer
        query = TypoNormalizer.get_instance().normalize(query)
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
                        headers=_vllm_auth_headers(),
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
                    ans_text = strip_mixed_refusal_text(data["choices"][0]["message"]["content"])
                    tok_count = data.get("usage", {}).get("completion_tokens", len(ans_text.split()))
                    tok_s = round(tok_count / max(gen_duration, 0.001), 2)
                    served_chunks = [] if is_no_relevant_docs_answer(ans_text) else sanitize_chunks(chunks)
                    return {
                        "query": query,
                        "answer": ans_text,
                        "route": "hard",
                        "target_tier": "gpu_vllm",
                        "probability_hard": prob,
                        "chunks": served_chunks,
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
            ans_text = strip_mixed_refusal_text(gen_result["text"])
            served_chunks = [] if is_no_relevant_docs_answer(ans_text) else sanitize_chunks(chunks)

            return {
                "query": query,
                "answer": ans_text,
                "route": "hard",
                "target_tier": "gpu_vllm",
                "probability_hard": prob,
                "chunks": served_chunks,
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

        # Step 2: Generate grounded answer (DEFECT-17).
        # Order: CPU SLM daemon -> GPU daemon overflow (SLM busy/down) -> in-process CPU SLM.
        slm_server_url = os.getenv("SLM_BASE_URL", "http://localhost:8082/v1").rstrip("/")
        server_root = slm_server_url.rsplit("/v1", 1)[0]
        tokens_to_gen = max_new_tokens or 160
        backends = []
        try:
            import httpx
            if httpx.get(f"{server_root}/healthz", timeout=1.0).status_code == 200:
                backends.append((
                    "cpu-daemon-port-8082", "qwen2.5-3b-instruct-q4_k_m.gguf",
                    lambda: _complete_chat_http(
                        f"{slm_server_url}/chat/completions",
                        {"messages": CPUSLMEngine.build_rag_messages(query, chunks),
                         "max_tokens": tokens_to_gen, "temperature": temperature},
                        timeout=_slm_timeout(streaming=False),
                    ),
                ))
        except Exception:
            pass

        def gpu_overflow():
            from pipeline.gpu_llm_runner import GPULLMEngine
            gpu_server_url = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1").rstrip("/")
            return _complete_chat_http(
                f"{gpu_server_url}/chat/completions",
                {"model": "Qwen/Qwen2.5-7B-Instruct", "messages": GPULLMEngine.build_rag_messages(query, chunks),
                 "max_tokens": tokens_to_gen, "temperature": temperature},
                headers=_vllm_auth_headers(),
                timeout=_gpu_timeout(),
            )

        backends.append(("gpu-daemon-port-8000 (cpu-slm-overflow)", "Qwen/Qwen2.5-7B-Instruct", gpu_overflow))
        slm_refusal = None
        for server, default_model, call in backends:
            if server.startswith("gpu") and not self._gpu_daemon_healthy():
                continue
            try:
                t0_gen = time.time()
                ans_text, tok_count, model = call()
                ans_text = strip_mixed_refusal_text(ans_text)
            except Exception as exc:
                logger.warning("%s could not answer (%s)", server, exc)
                continue
            if (server.startswith("cpu") and is_no_relevant_docs_answer(ans_text)
                    and should_escalate_refusal(query, chunks) and self._gpu_daemon_healthy()):
                # 3B refused although documents were retrieved: re-ask the 7B tier (see SLMRefused)
                slm_refusal = (ans_text, tok_count, model, time.time() - t0_gen)
                continue
            if server.startswith("gpu") and slm_refusal is not None:
                server = "gpu-daemon-port-8000 (cpu-slm-refusal)"
            gen_duration = time.time() - t0_gen
            served_chunks = [] if is_no_relevant_docs_answer(ans_text) else sanitize_chunks(chunks)
            return {
                "query": query,
                "answer": ans_text,
                "route": "easy",
                "target_tier": "cpu_slm",
                "probability_hard": easy_prob,
                "chunks": served_chunks,
                "stats": {
                    "retrieval_seconds": round(retrieval_duration, 3),
                    "generation_seconds": round(gen_duration, 3),
                    "total_seconds": round(time.time() - t_start, 3),
                    "num_tokens": tok_count,
                    "tokens_per_second": round(tok_count / max(gen_duration, 0.001), 2),
                    "model": model or default_model,
                    "server": server,
                },
            }

        if slm_refusal is not None:
            ans_text, tok_count, model, gen_duration = slm_refusal
            return {
                "query": query, "answer": ans_text, "route": "easy", "target_tier": "cpu_slm",
                "probability_hard": easy_prob, "chunks": [],
                "stats": {"retrieval_seconds": round(retrieval_duration, 3), "generation_seconds": round(gen_duration, 3),
                          "total_seconds": round(time.time() - t_start, 3), "num_tokens": tok_count,
                          "model": model or "qwen2.5-3b-instruct-q4_k_m.gguf", "server": "cpu-daemon-port-8082"},
            }

        # Option B: last resort, in-process CPU SLM
        t0_gen = time.time()
        gen_result = self.slm_engine.answer_rag(
            question=query,
            context_chunks=chunks,
            max_new_tokens=tokens_to_gen,
            temperature=temperature,
        )
        gen_duration = time.time() - t0_gen

        total_duration = time.time() - t_start
        ans_text = strip_mixed_refusal_text(gen_result["text"])
        served_chunks = [] if is_no_relevant_docs_answer(ans_text) else sanitize_chunks(chunks)

        return {
            "query": query,
            "answer": ans_text,
            "route": "easy",
            "target_tier": "cpu_slm",
            "probability_hard": easy_prob,
            "chunks": served_chunks,
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
        from pipeline.text_processing.typo_normalizer import TypoNormalizer
        query = TypoNormalizer.get_instance().normalize(query)
        k = top_k or self.top_k
        t_start = time.time()

        # Step 0: Parallel Fan-Out (Routing + Database Retrieval concurrently)
        pre_duration = 0.001
        candidate_chunks = chunks  # full retrieval list (the CPU tier uses the first 3)
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

        def relay(deltas, model: str, server: str, escalate_refusal: bool = False):
            """Yield token events then the done event. Raises only if nothing was streamed yet
            (SLMRefused when escalate_refusal and the whole answer is the bare refusal)."""
            t0_gen = time.time()
            parts: List[str] = []
            token_count = 0
            ttft = None
            incomplete = False
            try:
                try:
                    for delta in strip_mixed_refusal(deltas):
                        if escalate_refusal and not token_count and starts_with_refusal(delta) \
                                and is_no_relevant_docs_answer(delta):
                            raise SLMRefused(delta)
                        if ttft is None:
                            ttft = round(time.time() - t0_gen, 3)
                        token_count += 1
                        parts.append(delta)
                        yield {"type": "token", "delta": delta}
                except Exception as exc:
                    if not token_count:
                        raise
                    logger.warning("%s failed mid-answer after %d tokens: %s", server, token_count, exc)
                    incomplete = True
                    parts.append(PARTIAL_ANSWER_NOTE)
                    yield {"type": "token", "delta": PARTIAL_ANSWER_NOTE}
            finally:
                close = getattr(deltas, "close", None)
                if close:
                    close()  # releases the upstream HTTP stream right away (client gone or done)
            gen_duration = time.time() - t0_gen
            full_ans = "".join(parts)
            refused = is_no_relevant_docs_answer(full_ans)
            if refused:
                yield {"type": "chunks", "chunks": []}
            done = {
                "type": "done",
                "full_answer": full_ans,
                "chunks": [] if refused else sanitize_chunks(chunks),
                "stats": {
                    "retrieval_seconds": round(retrieval_duration, 3),
                    "generation_seconds": round(gen_duration, 3),
                    "total_seconds": round(time.time() - t_start, 3),
                    "ttft_seconds": ttft or 0.0,
                    "num_tokens": token_count,
                    "tokens_per_second": round(token_count / max(gen_duration, 0.001), 2),
                    "model": model,
                    "server": server,
                },
            }
            if incomplete:
                done["incomplete"] = True
            yield done

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
                    "chunks": [],
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
                yield from relay(
                    _stream_chat_http(
                        f"{gpu_server_url}/chat/completions",
                        {"model": "Qwen/Qwen2.5-7B-Instruct", "messages": messages,
                         "max_tokens": tokens_to_gen, "temperature": temperature},
                        headers=_vllm_auth_headers(),
                        timeout=90.0,
                    ),
                    model="Qwen/Qwen2.5-7B-Instruct",
                    server="gpu-daemon-port-8000",
                )
                return

            # Option B: Fallback to in-process streaming
            t0_gen = time.time()
            token_count = 0
            full_response = []
            ttft = None

            for delta in strip_mixed_refusal(self.gpu_engine.generate_chat_stream(
                messages,
                max_new_tokens=tokens_to_gen,
                temperature=temperature,
            )):
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
            full_ans = "".join(full_response)
            if is_no_relevant_docs_answer(full_ans):
                yield {"type": "chunks", "chunks": []}

            yield {
                "type": "done",
                "full_answer": full_ans,
                "chunks": [] if is_no_relevant_docs_answer(full_ans) else sanitize_chunks(chunks),
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
                "chunks": [],
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

        # Step 2: Generate the grounded answer (DEFECT-17).
        # Order: CPU SLM daemon -> GPU daemon overflow (SLM busy/down) -> in-process CPU SLM.
        # An answer that has started streaming is never restarted on another backend.
        tokens_to_gen = min(max_new_tokens or 512, 1024)


        # Option A: CPU SLM daemon (port 8082). It answers 503 quickly when all slots are busy.
        slm_server_url = os.getenv("SLM_BASE_URL", "http://localhost:8082/v1").rstrip("/")
        server_root = slm_server_url.rsplit("/v1", 1)[0]
        slm_up = False
        try:
            import httpx
            slm_up = httpx.get(f"{server_root}/healthz", timeout=1.0).status_code == 200
        except Exception:
            slm_up = False

        slm_refusal: Optional[str] = None
        if slm_up:
            try:
                yield from relay(
                    _stream_chat_http(
                        f"{slm_server_url}/chat/completions",
                        {"messages": CPUSLMEngine.build_rag_messages(query, chunks),
                         "max_tokens": tokens_to_gen, "temperature": temperature},
                        timeout=_slm_timeout(),
                    ),
                    model="qwen2.5-3b-instruct-q4_k_m.gguf",
                    server="cpu-daemon-port-8082",
                    escalate_refusal=should_escalate_refusal(query, candidate_chunks or chunks),
                )
                return
            except SLMRefused as ref:
                slm_refusal = ref.text
                logger.info("CPU SLM refused with %d retrieved chunks; re-asking the GPU tier", len(chunks))
            except Exception as exc:
                logger.warning("CPU SLM daemon could not answer (%s); trying GPU overflow", exc)

        # Option A2: GPU daemon when the CPU tier refused with documents, is busy, or is down.
        if self._gpu_daemon_healthy():
            reason = "cpu_slm_refusal" if slm_refusal is not None else "cpu_slm_overflow"
            yield {"type": "route", "route": "hard", "target_tier": "gpu_vllm",
                   "probability_hard": route_decision.get("probability_hard", 0.0), "escalation": reason}
            # The 7B tier gets the same context it would on a direct route (up to 8 chunks); relay's done
            # event reads `chunks`, so the served sources follow.
            wider = (candidate_chunks or chunks)[:8]
            if len(wider) > len(chunks):
                chunks = wider
                yield {"type": "chunks", "chunks": sanitize_chunks(chunks), "retrieval_seconds": round(retrieval_duration, 3)}
            try:
                from pipeline.gpu_llm_runner import GPULLMEngine
                gpu_server_url = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1").rstrip("/")
                yield from relay(
                    _stream_chat_http(
                        f"{gpu_server_url}/chat/completions",
                        {"model": "Qwen/Qwen2.5-7B-Instruct",
                         "messages": GPULLMEngine.build_rag_messages(query, chunks),
                         "max_tokens": tokens_to_gen, "temperature": temperature},
                        headers=_vllm_auth_headers(),
                        timeout=_gpu_timeout(),
                    ),
                    model="Qwen/Qwen2.5-7B-Instruct",
                    server=f"gpu-daemon-port-8000 ({reason.replace('_', '-')})",
                )
                return
            except Exception as exc:
                logger.warning("GPU tier could not answer (%s)", exc)

        if slm_refusal is not None:  # the 3B answer stands (GPU tier unavailable)
            yield from relay(iter([slm_refusal]), model="qwen2.5-3b-instruct-q4_k_m.gguf", server="cpu-daemon-port-8082")
            return

        # Option B: last resort, in-process CPU SLM streaming
        t0_gen = time.time()
        first_token_time = None
        full_text = []
        incomplete = False

        try:
            for delta in strip_mixed_refusal(self.slm_engine.answer_rag_stream(
                question=query,
                context_chunks=chunks,
                max_new_tokens=tokens_to_gen,
                temperature=temperature,
            )):
                if first_token_time is None:
                    first_token_time = time.time() - t0_gen
                full_text.append(delta)
                yield {"type": "token", "delta": delta}
        except Exception as opt_b_err:
            logger.warning("In-process SLM failed: %s", opt_b_err)
            incomplete = True
            full_text.append(PARTIAL_ANSWER_NOTE)
            yield {"type": "token", "delta": PARTIAL_ANSWER_NOTE}

        gen_duration = time.time() - t0_gen
        total_duration = time.time() - t_start
        final_answer = "".join(full_text).strip()
        approx_tokens = len(final_answer.split()) * 4 // 3
        if is_no_relevant_docs_answer(final_answer):
            yield {"type": "chunks", "chunks": []}

        done = {
            "type": "done",
            "full_answer": final_answer,
            "chunks": [] if is_no_relevant_docs_answer(final_answer) else sanitize_chunks(chunks),
            "stats": {
                "ttft_seconds": round(first_token_time or 0, 3),
                "retrieval_seconds": round(retrieval_duration, 3),
                "generation_seconds": round(gen_duration, 3),
                "total_seconds": round(total_duration, 3),
                "approx_tokens": approx_tokens,
                "tokens_per_second": round(approx_tokens / max(gen_duration, 0.001), 2),
                "server": "in-process",
            },
        }
        if incomplete:
            done["incomplete"] = True
        yield done


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

