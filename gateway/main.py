import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

# Ensure project root is in sys.path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from gateway.auth.guard import UserIdentity, require_iisc_user
from gateway.cache.redis_semantic_cache import RedisSemanticCache
from gateway.dependencies import (
    container, get_semantic_cache, get_complexity_scorer,
    get_dispatcher, get_admission_controller, get_slm_client, get_vllm_client
)
from gateway.router.complexity_scorer import ComplexityScorer
from gateway.router.dispatcher import ModelTier, RequestDispatcher
from gateway.clients.cpu_slm_client import CPUSLMClient
from gateway.clients.vllm_client import VLLMClient
from pipeline.rag_chain import RAGQAPipeline

logger = logging.getLogger("gateway.main")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize container (creates Redis semantic cache index, etc.)
    try:
        await container.initialize()
        # Sanitize Redis cache on startup: purge any uncacheable entries (insufficient data, refusals)
        cache = container.semantic_cache()
        purged = await cache.apurge_uncacheable_entries()
        if purged > 0:
            logger.info(f"Startup sanitized Redis cache: purged {purged} uncacheable entries.")
    except Exception as e:
        logger.warning(f"Container initialization warning: {e}")
    yield
    try:
        await container.shutdown()
    except Exception:
        pass

app = FastAPI(
    title="On-Premise Institutional RAG Gateway",
    description="Intelligent Hybrid Routing Gateway with Redis Semantic Cache, CPU SLM (Qwen 2.5 3B) and GPU LLM (7B/70B)",
    version="2.0.0",
    lifespan=lifespan
)

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.2
    max_tokens: Optional[int] = 160
    stream: Optional[bool] = False

class DirectQueryRequest(BaseModel):
    query: str
    top_k: Optional[int] = 3
    temperature: Optional[float] = 0.2
    max_tokens: Optional[int] = 160
    stream: Optional[bool] = False

@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "service": "On-Premise RAG Gateway",
        "router": "Two-Tower Hybrid MLP (Active)",
        "cache": "Redis Semantic Vector Cache (Active)"
    }

@app.get("/cache/stats")
async def cache_stats(cache: RedisSemanticCache = Depends(get_semantic_cache)):
    count = await cache.acount()
    return {
        "status": "active",
        "cache_type": "RedisSemanticCache",
        "index_name": cache.index_name,
        "key_prefix": cache.key_prefix,
        "similarity_threshold": cache.similarity_threshold,
        "vector_dim": cache.vector_dim,
        "cached_queries_count": count,
    }

@app.post("/cache/clear")
async def cache_clear(cache: RedisSemanticCache = Depends(get_semantic_cache)):
    success = await cache.aclear()
    return {"status": "cleared" if success else "failed"}

@app.post("/cache/purge-invalid")
async def cache_purge_invalid(cache: RedisSemanticCache = Depends(get_semantic_cache)):
    purged = await cache.apurge_uncacheable_entries()
    return {"status": "ok", "purged_count": purged}

@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    user: UserIdentity = Depends(require_iisc_user),
    cache: RedisSemanticCache = Depends(get_semantic_cache),
    scorer: ComplexityScorer = Depends(get_complexity_scorer),
    dispatcher: RequestDispatcher = Depends(get_dispatcher),
    slm: CPUSLMClient = Depends(get_slm_client),
    vllm: VLLMClient = Depends(get_vllm_client),
):
    if not req.messages:
        raise HTTPException(status_code=400, detail="Messages list cannot be empty")

    query = req.messages[-1].content.strip()
    t0 = time.time()

    # Step 0: Semantic Vector Cache Lookup
    try:
        hit = await cache.alookup(query)
    except Exception:
        hit = None

    if hit is not None:
        cached_resp = hit.get("response", {})
        if isinstance(cached_resp, str):
            try:
                cached_resp = json.loads(cached_resp)
            except Exception:
                cached_resp = {"answer": cached_resp}
        ans = cached_resp.get("answer", "")
        sim = hit.get("similarity", 1.0)
        cmpl_id = f"chatcmpl-cached-{uuid.uuid4().hex[:8]}"

        if req.stream:
            def cached_chat_stream():
                words = ans.split(" ")
                for i, w in enumerate(words):
                    delta = w if i == len(words) - 1 else w + " "
                    chunk = {
                        "id": cmpl_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": "redis-semantic-cache",
                        "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                final_chunk = {
                    "id": cmpl_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "redis-semantic-cache",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(final_chunk)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                cached_chat_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        return {
            "id": cmpl_id,
            "object": "chat.completion",
            "model": "redis-semantic-cache",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": ans},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": len(ans.split()),
                "total_tokens": len(ans.split()),
            },
            "router_meta": {
                "score": 0.0,
                "tier": "semantic_cache",
                "route": "cached",
                "cached": True,
                "similarity": sim,
                "matched_prompt": hit.get("matched_prompt", query),
                "routing_time_seconds": round(time.time() - t0, 4),
            },
            "user": user.email,
        }

    # Step 1: Neural Complexity Routing (MLP Router)
    score = await scorer.score(query)
    decision = dispatcher.decide(score)

    if req.stream:
        rag = RAGQAPipeline()
        cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created_ts = int(time.time())

        def sse_chat_generator():
            full_ans = []
            for event in rag.answer_query_stream(
                query,
                max_new_tokens=req.max_tokens or 160,
                temperature=req.temperature or 0.2,
            ):
                if event.get("type") == "token":
                    delta_text = event.get("delta", "")
                    full_ans.append(delta_text)
                    chunk = {
                        "id": cmpl_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": "qwen2.5-3b-instruct-cpu" if decision.target_tier != ModelTier.GPU_LLM else "qwen2.5-7b-instruct-gpu",
                        "choices": [{
                            "index": 0,
                            "delta": {"content": delta_text},
                            "finish_reason": None,
                        }],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"
                elif event.get("type") == "done":
                    final_chunk = {
                        "id": cmpl_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": "qwen2.5-3b-instruct-cpu" if decision.target_tier != ModelTier.GPU_LLM else "qwen2.5-7b-instruct-gpu",
                        "choices": [{
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }],
                    }
                    yield f"data: {json.dumps(final_chunk)}\n\n"

            # Save generated completion to cache
            complete_text = "".join(full_ans)
            if complete_text:
                try:
                    cache.store(query, {"answer": complete_text})
                except Exception:
                    pass
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            sse_chat_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Step 2: Route Enforcement for Hard Queries -> GPU LLM Tier (Qwen 2.5 7B)
    if decision.target_tier == ModelTier.GPU_LLM:
        vllm_up = await vllm.check_health()
        if vllm_up:
            try:
                resp = await vllm.generate_chat(
                    [{"role": m.role, "content": m.content} for m in req.messages],
                    max_tokens=req.max_tokens or 512,
                    temperature=req.temperature or 0.2,
                )
                resp["router_meta"] = {
                    "score": round(score, 4),
                    "tier": decision.target_tier.value,
                    "route": "hard",
                    "reason": decision.reason,
                    "routing_time_seconds": round(time.time() - t0, 4),
                }
                resp["user"] = user.email
                ans_text = resp["choices"][0]["message"]["content"]
                await cache.astore(query, {"answer": ans_text})
                return resp
            except Exception:
                pass

        # In-process GPU LLM fallback
        try:
            from pipeline.gpu_llm_runner import GPULLMEngine
            engine = GPULLMEngine.get_instance()
            gpu_res = engine.generate_chat(
                [{"role": m.role, "content": m.content} for m in req.messages],
                max_new_tokens=req.max_tokens or 512,
                temperature=req.temperature or 0.2,
            )
            ans_text = gpu_res["text"]
            await cache.astore(query, {"answer": ans_text})
            return {
                "id": f"chatcmpl-hard-{uuid.uuid4().hex[:8]}",
                "object": "chat.completion",
                "model": "Qwen/Qwen2.5-7B-Instruct",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": ans_text},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": -1,
                    "completion_tokens": gpu_res["num_tokens"],
                    "total_tokens": gpu_res["num_tokens"],
                },
                "router_meta": {
                    "score": round(score, 4),
                    "tier": decision.target_tier.value,
                    "route": "hard",
                    "reason": decision.reason,
                    "routing_time_seconds": round(time.time() - t0, 4),
                },
                "user": user.email,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"GPU LLM generation failed: {str(e)}")

    # Step 3: Easy Queries -> CPU SLM Tier (Qwen 2.5 3B)
    slm_up = await slm.check_health()
    if slm_up:
        try:
            resp = await slm.generate_chat(
                [{"role": m.role, "content": m.content} for m in req.messages],
                max_tokens=req.max_tokens or 160,
                temperature=req.temperature or 0.2,
            )
            resp["router_meta"] = {
                "score": round(score, 4),
                "tier": decision.target_tier.value,
                "route": "easy",
                "reason": decision.reason,
                "routing_time_seconds": round(time.time() - t0, 4),
            }
            ans_text = resp["choices"][0]["message"]["content"]
            await cache.astore(query, {"answer": ans_text})
            return resp
        except Exception:
            pass

    # Fallback attempt: In-process RAG QA Pipeline
    rag = RAGQAPipeline()
    rag_res = await run_in_threadpool(
        rag.answer_query,
        query,
        max_new_tokens=req.max_tokens or 160,
        temperature=req.temperature or 0.2,
    )
    ans_text = rag_res.get("answer", "")
    await cache.astore(query, {"answer": ans_text, "chunks": rag_res.get("chunks", [])})
    return {
        "id": f"chatcmpl-slm-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "model": "qwen2.5-3b-instruct-cpu",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": ans_text,
            },
            "finish_reason": "stop",
        }],
        "router_meta": {
            "score": round(score, 4),
            "tier": decision.target_tier.value,
            "route": "easy",
            "reason": decision.reason,
            "chunks_retrieved": len(rag_res.get("chunks", [])),
            "routing_time_seconds": round(time.time() - t0, 4),
        },
        "user": user.email,
    }


@app.post("/query")
async def query_endpoint(
    req: DirectQueryRequest,
    cache: RedisSemanticCache = Depends(get_semantic_cache),
    scorer: ComplexityScorer = Depends(get_complexity_scorer),
    dispatcher: RequestDispatcher = Depends(get_dispatcher),
    vllm: VLLMClient = Depends(get_vllm_client),
):
    query = req.query.strip()
    t0 = time.time()

    # Step 0: Check Semantic Vector Cache in Redis
    try:
        hit = await cache.alookup(query)
    except Exception:
        hit = None

    if hit is not None:
        cached_resp = hit.get("response", {})
        if isinstance(cached_resp, str):
            try:
                cached_resp = json.loads(cached_resp)
            except Exception:
                cached_resp = {"answer": cached_resp}

        sim = hit.get("similarity", 1.0)
        matched_prompt = hit.get("matched_prompt", query)

        if req.stream:
            def cached_stream():
                # Yield cache hit notification event
                yield f"data: {json.dumps({'type': 'cache', 'status': 'HIT', 'similarity': sim, 'matched_prompt': matched_prompt})}\n\n"
                # Yield route event
                yield f"data: {json.dumps({'type': 'route', 'route': cached_resp.get('route', 'cached'), 'target_tier': 'semantic_cache', 'probability_hard': 0.0})}\n\n"
                # Yield chunks event
                chunks = cached_resp.get("chunks", [])
                yield f"data: {json.dumps({'type': 'chunks', 'chunks': chunks, 'retrieval_seconds': 0.001})}\n\n"
                # Stream cached answer tokens
                ans = cached_resp.get("answer", "")
                words = ans.split(" ")
                for i, w in enumerate(words):
                    delta = w if i == len(words) - 1 else w + " "
                    yield f"data: {json.dumps({'type': 'token', 'delta': delta})}\n\n"
                # Yield completion stats event
                yield f"data: {json.dumps({'type': 'done', 'cached': True, 'similarity': sim, 'matched_prompt': matched_prompt, 'stats': {'tokens_per_second': 9999.0, 'total_seconds': round(time.time() - t0, 4), 'cached': True}})}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                cached_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # Non-streaming cache hit
        cached_resp["cached"] = True
        cached_resp["cache_hit"] = True
        cached_resp["similarity"] = sim
        cached_resp["matched_prompt"] = matched_prompt
        cached_resp["routing_time_seconds"] = round(time.time() - t0, 4)
        return cached_resp

    # Cache Miss: proceed with streaming or routing
    if req.stream:
        rag = RAGQAPipeline(top_k=req.top_k)

        def event_stream():
            collected_answer = []
            collected_chunks = []
            route_info = {}
            for event in rag.answer_query_stream(
                query,
                top_k=req.top_k,
                max_new_tokens=req.max_tokens or 160,
                temperature=req.temperature or 0.2,
            ):
                etype = event.get("type")
                if etype == "token":
                    collected_answer.append(event.get("delta", ""))
                elif etype == "chunks":
                    collected_chunks = event.get("chunks", [])
                elif etype == "route":
                    route_info = event
                yield f"data: {json.dumps(event)}\n\n"

            full_text = "".join(collected_answer)
            if full_text:
                try:
                    cache_payload = {
                        "query": query,
                        "answer": full_text,
                        "route": route_info.get("route", "easy"),
                        "target_tier": route_info.get("target_tier", "cpu_slm"),
                        "chunks": collected_chunks,
                    }
                    cache.store(query, cache_payload)
                except Exception:
                    pass
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    score = await scorer.score(query)
    decision = dispatcher.decide(score)

    if decision.target_tier == ModelTier.GPU_LLM:
        from pipeline.retriever import RedisChunkRetriever
        from pipeline.gpu_llm_runner import GPULLMEngine

        retriever = RedisChunkRetriever()
        t0_ret = time.time()
        try:
            chunks = retriever.search(query, top_k=req.top_k or 5)
        except Exception:
            chunks = []
        ret_time = round(time.time() - t0_ret, 3)

        vllm_up = await vllm.check_health()
        if vllm_up:
            try:
                messages = GPULLMEngine.build_rag_messages(query, chunks)
                resp = await vllm.generate_chat(
                    messages,
                    max_tokens=req.max_tokens or 512,
                    temperature=req.temperature or 0.2,
                )
                answer_text = resp["choices"][0]["message"]["content"]
                res = {
                    "query": query,
                    "answer": answer_text,
                    "route": "hard",
                    "target_tier": "gpu_vllm",
                    "model": resp.get("model", "Qwen/Qwen2.5-7B-Instruct"),
                    "score": round(score, 4),
                    "chunks": chunks,
                    "cached": False,
                    "stats": {
                        "retrieval_seconds": ret_time,
                        "routing_time_seconds": round(time.time() - t0, 4),
                    },
                }
                await cache.astore(query, res)
                return res
            except Exception:
                pass

        rag = RAGQAPipeline(top_k=req.top_k or 5)
        res = rag.answer_query(
            query,
            top_k=req.top_k or 5,
            max_new_tokens=req.max_tokens or 512,
            temperature=req.temperature or 0.2,
        )
        res["cached"] = False
        res["routing_time_seconds"] = round(time.time() - t0, 4)
        await cache.astore(query, res)
        return res

    # Route: Easy -> CPU SLM
    rag = RAGQAPipeline(top_k=req.top_k)
    res = await run_in_threadpool(
        rag.answer_query,
        query,
        top_k=req.top_k,
        max_new_tokens=req.max_tokens or 160,
        temperature=req.temperature or 0.2,
    )
    res["route"] = "easy"
    res["target_tier"] = "cpu_slm"
    res["score"] = round(score, 4)
    res["cached"] = False
    res["routing_time_seconds"] = round(time.time() - t0, 4)
    await cache.astore(query, res)
    return res
